"""Regression check for the Ripple dual-rail storage contract.

SQLite owns runtime truth/search/evolution. JSONL/archive owns frozen/full
memory content. The old SQL memory_stream/archive_blocks rail must stay gone.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from memoria_mcp.server import MemoriaRouter


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _db_tables(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    finally:
        conn.close()


def _jsonl_contains(root: Path, marker: str) -> bool:
    for path in root.rglob("*.jsonl"):
        try:
            if marker in path.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False


def run_check() -> dict[str, Any]:
    old_env = {
        "MEMORIA_MCP_ENABLE_SEMANTIC": os.environ.get("MEMORIA_MCP_ENABLE_SEMANTIC"),
        "MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE": os.environ.get("MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE"),
        "MEMORIA_MCP_WRITE_QUEUE_ENABLED": os.environ.get("MEMORIA_MCP_WRITE_QUEUE_ENABLED"),
    }
    os.environ["MEMORIA_MCP_ENABLE_SEMANTIC"] = "false"
    os.environ["MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE"] = "live"
    os.environ["MEMORIA_MCP_WRITE_QUEUE_ENABLED"] = "false"

    marker = f"storage_architecture_{int(time.time() * 1000)}"
    project = "storage_architecture_project"
    fact_key = f"test.storage.{marker}"
    old_text = f"{marker}: OLD rail claim should become history."
    new_text = f"{marker}: CURRENT rail claim should be active."

    try:
        with tempfile.TemporaryDirectory(prefix="ripple-memory-storage-") as tmp:
            data_dir = Path(tmp)
            project_dir = data_dir / project
            router = MemoriaRouter(str(data_dir))
            try:
                old = router._dispatch_tool(
                    "memoria_remember",
                    {
                        "project": project,
                        "content": old_text,
                        "type": "arch_decision",
                        "importance": 0.84,
                        "confidence": 0.96,
                        "fact_key": fact_key,
                    },
                )
                _assert(old.get("stored") is True, f"old remember failed: {old}")
                old_ref = f"memory_node:{old['node_id']}"

                new = router._dispatch_tool(
                    "memoria_remember",
                    {
                        "project": project,
                        "content": new_text,
                        "type": "arch_decision",
                        "importance": 0.9,
                        "confidence": 0.97,
                        "fact_key": fact_key,
                        "supersedes_ref_ids": [old_ref],
                    },
                )
                _assert(new.get("stored") is True, f"new remember failed: {new}")
                new_ref = f"memory_node:{new['node_id']}"

                db_path = project_dir / "memoria.db"
                tables = _db_tables(db_path)
                _assert({"graph_state", "search_index", "memory_evolution_state", "memory_evolution_edges"}.issubset(tables), f"missing runtime tables: {tables}")
                _assert("memory_stream" not in tables and "archive_blocks" not in tables, f"obsolete SQL rail tables present: {tables}")
                _assert(_jsonl_contains(project_dir / "archives" / "streams", marker), "JSONL archive stream does not contain memory content")

                conn = sqlite3.connect(str(db_path))
                try:
                    old_index = conn.execute(
                        "SELECT deleted, deleted_reason, json_file, json_offset FROM search_index WHERE node_id = ?",
                        (old["node_id"],),
                    ).fetchone()
                    edges = conn.execute(
                        "SELECT from_ref_id, to_ref_id, relation FROM memory_evolution_edges WHERE fact_key = ?",
                        (fact_key,),
                    ).fetchall()
                finally:
                    conn.close()
                _assert(old_index and int(old_index[0]) == 1, f"old search row not logically deleted: {old_index}")
                _assert(old_index[1] == "memory_evolution_superseded", f"wrong delete reason: {old_index}")
                _assert(old_index[2] and old_index[3] is not None, f"deleted row lacks JSONL pointer: {old_index}")
                _assert(any(row[0] == old_ref and row[1] == new_ref and row[2] == "superseded_by" for row in edges), f"missing evolution edge: {edges}")

                srv = router._get_server(project)
                srv.config.dreamer_batch_threshold = 1
                srv.config.dreamer_interval_days = 0.0
                srv.config.dreamer_idle_hours = 0.0
                srv.config.dreamer_min_entry_age_hours = 0.0
                srv._auto_maintenance()

                conn = sqlite3.connect(str(db_path))
                try:
                    purged_old_index = conn.execute(
                        "SELECT node_id FROM search_index WHERE node_id = ?",
                        (old["node_id"],),
                    ).fetchone()
                finally:
                    conn.close()
                _assert(purged_old_index is None, f"Dreamer did not purge processed deleted row: {purged_old_index}")

                archive_text = ""
                for path in (project_dir / "archives" / "content").rglob("*.json"):
                    archive_text += path.read_text(encoding="utf-8", errors="replace")
                _assert(str(old["node_id"]) in archive_text, "Dreamer archive block did not include old node sample")

                router.close()
                restart_router = MemoriaRouter(str(data_dir))
                try:
                    recall_default = restart_router._dispatch_tool(
                        "memoria_recall",
                        {"project": project, "query": marker, "top_k": 8},
                    )
                    recall_text = json.dumps(recall_default, ensure_ascii=False)
                    _assert(new_text in recall_text, f"restart default recall missed active claim: {recall_default}")
                    _assert(old_text not in recall_text, f"restart default recall resurrected old claim: {recall_default}")

                    recall_history = restart_router._dispatch_tool(
                        "memoria_recall",
                        {"project": project, "query": marker, "top_k": 8, "include_evolution": True},
                    )
                    history_text = json.dumps(recall_history, ensure_ascii=False)
                    _assert(old_text in history_text, f"restart history recall lost old claim audit trail: {recall_history}")

                    conn = sqlite3.connect(str(db_path))
                    try:
                        restarted_old_index = conn.execute(
                            "SELECT deleted, deleted_reason FROM search_index WHERE node_id = ?",
                            (old["node_id"],),
                        ).fetchone()
                    finally:
                        conn.close()
                    _assert(
                        restarted_old_index and int(restarted_old_index[0]) == 1,
                        f"restart did not restore superseded search delete mark: {restarted_old_index}",
                    )
                finally:
                    restart_router.close()
            finally:
                router.close()

            return {
                "ok": True,
                "data_dir": str(data_dir),
                "project": project,
                "active_ref": new_ref,
                "superseded_ref": old_ref,
                "obsolete_sql_tables_absent": True,
                "jsonl_is_frozen_content_rail": True,
                "dreamer_purged_deleted_search_row": True,
                "restart_does_not_resurrect_old_claim": True,
            }
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> int:
    result = run_check()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
