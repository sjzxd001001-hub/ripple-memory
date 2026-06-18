"""Regression check for lightweight memory evolution.

This verifies the no-extra-tool path:

- memoria_remember can mark a new memory as replacing old refs via optional
  fact_key/supersedes_ref_ids.
- memoria_recall keeps the default four-tool surface and filters superseded
  old口径 unless include_evolution=true.
- memoria_read labels historical memories clearly.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from mcp.types import ListToolsRequest

from memoria_mcp.server import MemoriaRouter


EXPECTED_CORE_TOOLS = [
    "memoria_remember",
    "memoria_recall",
    "memoria_read",
    "memoria_forget",
]


async def _list_tool_names(router: MemoriaRouter) -> list[str]:
    handler = router.server.request_handlers[ListToolsRequest]
    result = await handler(ListToolsRequest())
    return [tool.name for tool in result.root.tools]


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def run_check() -> dict[str, Any]:
    os.environ["MEMORIA_MCP_ENABLE_SEMANTIC"] = "false"
    os.environ["MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE"] = "live"
    os.environ["MEMORIA_MCP_WRITE_QUEUE_ENABLED"] = "false"
    os.environ.pop("MEMORIA_MCP_EXPOSE_PROJECT_TOOLS", None)

    marker = f"evolution_marker_{int(time.time() * 1000)}"
    project = "evolution_project"
    fact_key = f"test.policy.{marker}"
    old_text = f"{marker}: OLD_POLICY says use Alpha."
    new_text = f"{marker}: CURRENT_POLICY says use Beta."

    with tempfile.TemporaryDirectory(prefix="ripple-memory-evolution-", ignore_cleanup_errors=True) as tmp:
        data_dir = Path(tmp)
        router = MemoriaRouter(str(data_dir))
        try:
            tool_names = await _list_tool_names(router)
            _assert(tool_names == EXPECTED_CORE_TOOLS, f"unexpected tool list: {tool_names}")

            old = router._dispatch_tool(
                "memoria_remember",
                {
                    "project": project,
                    "content": old_text,
                    "type": "arch_decision",
                    "importance": 0.86,
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

            recall = router._dispatch_tool(
                "memoria_recall",
                {"project": project, "query": marker, "top_k": 5},
            )
            rendered = json.dumps(recall.get("results", []), ensure_ascii=False)
            rendered_full = json.dumps(recall, ensure_ascii=False)
            _assert("CURRENT_POLICY" in rendered, f"default recall missed current policy: {recall}")
            _assert("OLD_POLICY" not in rendered, f"default recall leaked old policy in results: {recall}")
            _assert("OLD_POLICY" not in rendered_full, f"default recall leaked old policy outside results: {recall}")
            _assert(old_ref in recall.get("filtered_superseded_refs", []), f"old ref not filtered: {recall}")
            _assert(
                any(claim.get("ref_id") == new_ref for claim in recall.get("truth_projection", {}).get("current_claims", [])),
                f"truth projection missing active ref: {recall}",
            )

            recall_history = router._dispatch_tool(
                "memoria_recall",
                {"project": project, "query": marker, "top_k": 5, "include_evolution": True},
            )
            history = json.dumps(recall_history, ensure_ascii=False)
            _assert("OLD_POLICY" in history, f"history recall missed old policy: {recall_history}")
            _assert('"evolution_status": "superseded"' in history, f"old policy not labeled: {recall_history}")
            _assert(
                any(edge.get("from_ref_id") == old_ref and edge.get("to_ref_id") == new_ref
                    for edge in recall_history.get("evolution_chains", [])),
                f"history recall missing evolution edge: {recall_history}",
            )

            read_old = router._dispatch_tool(
                "memoria_read",
                {"project": project, "ref_id": old_ref, "max_chars": 2000},
            )
            _assert(read_old.get("ok") is True, f"read old failed: {read_old}")
            _assert(read_old.get("evolution_status") == "superseded", f"read old not superseded: {read_old}")
        finally:
            router.close()

        db_path = data_dir / project / "memoria.db"
        conn = sqlite3.connect(str(db_path))
        try:
            statuses = {
                row[0]
                for row in conn.execute(
                    "SELECT status FROM memory_evolution_state WHERE fact_key = ?",
                    (fact_key,),
                ).fetchall()
            }
            edge_rows = conn.execute(
                "SELECT from_ref_id, to_ref_id, relation FROM memory_evolution_edges WHERE fact_key = ?",
                (fact_key,),
            ).fetchall()
            old_index = conn.execute(
                "SELECT deleted, deleted_reason FROM search_index WHERE node_id = ?",
                (old["node_id"],),
            ).fetchone()
        finally:
            conn.close()
        _assert(statuses == {"active", "superseded"}, f"unexpected DB statuses: {statuses}")
        _assert(
            any(row[0] == old_ref and row[1] == new_ref and row[2] == "superseded_by" for row in edge_rows),
            f"missing DB evolution edge: {edge_rows}",
        )
        _assert(
            old_index and int(old_index[0]) == 1 and old_index[1] == "memory_evolution_superseded",
            f"old search index not marked for Dreamer cleanup: {old_index}",
        )

        return {
            "ok": True,
            "data_dir": str(data_dir),
            "tools": tool_names,
            "active_ref": new_ref,
            "superseded_ref": old_ref,
            "db_statuses": sorted(statuses),
            "db_edge_rows": len(edge_rows),
            "old_deleted_reason": old_index[1] if old_index else None,
            "default_recall_count": recall.get("count", 0),
        }


def main() -> int:
    result = asyncio.run(run_check())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
