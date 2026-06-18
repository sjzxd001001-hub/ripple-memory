"""Regression check for recall latency boundaries and relevance quality."""
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


def run_check() -> dict[str, Any]:
    old_env = {
        "MEMORIA_MCP_ENABLE_SEMANTIC": os.environ.get("MEMORIA_MCP_ENABLE_SEMANTIC"),
        "MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE": os.environ.get("MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE"),
        "MEMORIA_MCP_WRITE_QUEUE_ENABLED": os.environ.get("MEMORIA_MCP_WRITE_QUEUE_ENABLED"),
        "MEMORIA_MCP_RECALL_FILTER_WEAK_ASCII_MATCHES": os.environ.get("MEMORIA_MCP_RECALL_FILTER_WEAK_ASCII_MATCHES"),
    }
    os.environ["MEMORIA_MCP_ENABLE_SEMANTIC"] = "false"
    os.environ["MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE"] = "live"
    os.environ["MEMORIA_MCP_WRITE_QUEUE_ENABLED"] = "false"
    os.environ["MEMORIA_MCP_RECALL_FILTER_WEAK_ASCII_MATCHES"] = "true"

    marker = f"recall_quality_marker_{int(time.time() * 1000)}"
    project = "recall_quality_project"

    try:
        with tempfile.TemporaryDirectory(prefix="ripple-memory-recall-quality-", ignore_cleanup_errors=True) as tmp:
            data_dir = Path(tmp)
            router = MemoriaRouter(str(data_dir))
            try:
                old_loop = router._dispatch_tool("memoria_remember", {
                    "project": project,
                    "content": (
                        f"{marker} 2026-05-24 old broad five-closure explanation: "
                        "Agent Loop productization debt map and broad architecture commentary."
                    ),
                    "type": "debug_insight",
                    "importance": 0.9,
                    "confidence": 0.95,
                })
                session_store = router._dispatch_tool("memoria_remember", {
                    "project": project,
                    "content": (
                        f"{marker} current src/session_store.py SessionContextAssembler "
                        "owns main Agent Loop transaction material assembly."
                    ),
                    "type": "arch_decision",
                    "importance": 0.7,
                    "confidence": 0.95,
                    "fact_key": f"test.recall_quality.session_store.{marker}",
                })
                old_access = router._dispatch_tool("memoria_remember", {
                    "project": project,
                    "content": (
                        f"{marker} 2026-05-24 old baseline inventory around Access Control "
                        "and refactor stage planning."
                    ),
                    "type": "debug_insight",
                    "importance": 0.95,
                    "confidence": 0.95,
                })
                access_v2 = router._dispatch_tool("memoria_remember", {
                    "project": project,
                    "content": (
                        f"{marker} current Access Control v2 landed: "
                        "AccessDecision fact projection covers cli.exec and mcp.call outcomes."
                    ),
                    "type": "arch_decision",
                    "importance": 0.7,
                    "confidence": 0.95,
                    "fact_key": f"test.recall_quality.access_v2.{marker}",
                })
                exact_identifier = f"hook_marker_{marker}"
                exact_identifier_memory = router._dispatch_tool("memoria_remember", {
                    "project": project,
                    "content": (
                        f"{exact_identifier}: exact installer hook marker should survive weak ASCII filtering."
                    ),
                    "type": "debug_insight",
                    "importance": 0.9,
                    "confidence": 0.95,
                })
                _assert(
                    session_store.get("node_id")
                    and access_v2.get("node_id")
                    and exact_identifier_memory.get("node_id"),
                    "setup remember failed",
                )

                srv = router._get_server(project)
                original_auto_maintenance = srv._auto_maintenance

                def fail_if_read_runs_maintenance() -> None:
                    raise AssertionError("read-only recall/read must not run auto maintenance")

                srv._auto_maintenance = fail_if_read_runs_maintenance  # type: ignore[method-assign]
                try:
                    start = time.perf_counter()
                    chat_recall = router._dispatch_tool("memoria_recall", {
                        "project": project,
                        "query": "session_store main Agent Loop transaction",
                        "top_k": 5,
                    })
                    chat_elapsed = time.perf_counter() - start
                    _assert(chat_elapsed < 0.5, f"recall too slow: {chat_elapsed:.3f}s")
                    descriptions = [item.get("description", "") for item in chat_recall.get("results", [])]
                    _assert(descriptions, f"chat recall returned no results: {chat_recall}")
                    _assert("session_store.py" in descriptions[0], f"exact current session_store memory not first: {descriptions}")
                    _assert(
                        all(str(old_loop.get("node_id")) not in str(item.get("id")) for item in chat_recall.get("results", [])),
                        f"weak old Agent Loop memory leaked into code-name recall: {chat_recall}",
                    )
                    _assert(
                        chat_recall.get("recall_diagnostics", {}).get("maintenance_ran") is False,
                        f"recall diagnostics did not mark read-only mode: {chat_recall}",
                    )

                    auth_recall = router._dispatch_tool("memoria_recall", {
                        "project": project,
                        "query": "Access Control v2",
                        "top_k": 5,
                    })
                    auth_descriptions = [item.get("description", "") for item in auth_recall.get("results", [])]
                    _assert(auth_descriptions, f"auth recall returned no results: {auth_recall}")
                    _assert("v2" in auth_descriptions[0], f"current Access Control v2 not first: {auth_descriptions}")
                    _assert(
                        str(old_access.get("node_id")) not in str(auth_recall.get("results", [])[:1]),
                        f"old broad auth memory outranked current v2: {auth_recall}",
                    )

                    identifier_recall = router._dispatch_tool("memoria_recall", {
                        "project": project,
                        "query": f"Please continue with {exact_identifier} for window A.",
                        "top_k": 5,
                    })
                    identifier_descriptions = [item.get("description", "") for item in identifier_recall.get("results", [])]
                    _assert(
                        identifier_descriptions and exact_identifier in identifier_descriptions[0],
                        f"exact identifier marker was weak-filtered: {identifier_recall}",
                    )

                    read = router._dispatch_tool("memoria_read", {
                        "project": project,
                        "ref_id": f"memory_node:{session_store.get('node_id')}",
                        "max_chars": 1000,
                    })
                    _assert(read.get("ok") is True, f"read failed: {read}")
                finally:
                    srv._auto_maintenance = original_auto_maintenance  # type: ignore[method-assign]
            finally:
                router.close()

            db_path = data_dir / project / "memoria.db"
            deleted_node_id = str(session_store.get("node_id"))
            deleted_reason = "recall_quality_deleted_row_guard"
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(
                    """
                    UPDATE search_index
                    SET deleted = 1, deleted_reason = ?, deleted_at = ?
                    WHERE node_id = ?
                    """,
                    (deleted_reason, time.time(), deleted_node_id),
                )
                conn.commit()

            reopen_deleted = MemoriaRouter(str(data_dir))
            try:
                reopen_deleted._get_server(project)
            finally:
                reopen_deleted.close()
            with sqlite3.connect(str(db_path)) as conn:
                deleted_row = conn.execute(
                    "SELECT deleted, deleted_reason FROM search_index WHERE node_id = ?",
                    (deleted_node_id,),
                ).fetchone()
            _assert(
                deleted_row and int(deleted_row[0]) == 1 and deleted_row[1] == deleted_reason,
                f"rebuild_from_nodes resurrected deleted row: {deleted_row}",
            )

            with sqlite3.connect(str(db_path)) as conn:
                conn.execute("UPDATE search_index SET index_dirty = 1 WHERE deleted = 0")
                conn.commit()

            reopen = MemoriaRouter(str(data_dir))
            try:
                reopen._get_server(project)
            finally:
                reopen.close()
            with sqlite3.connect(str(db_path)) as conn:
                index_dirty = int(conn.execute(
                    "SELECT COUNT(*) FROM search_index WHERE index_dirty = 1 AND deleted = 0"
                ).fetchone()[0])
            _assert(index_dirty == 0, f"rebuild_from_nodes left index_dirty rows: {index_dirty}")

            return {
                "ok": True,
                "data_dir": str(data_dir),
                "chat_recall_count": chat_recall.get("count"),
                "chat_recall_elapsed_seconds": round(chat_elapsed, 4),
                "chat_filtered_weak_count": chat_recall.get("recall_diagnostics", {}).get("filtered_weak_count"),
                "auth_first": auth_descriptions[0],
                "exact_identifier_first": identifier_descriptions[0],
                "read_only_maintenance_ran": False,
                "deleted_row_preserved_after_rebuild": True,
                "index_dirty_after_rebuild": index_dirty,
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
