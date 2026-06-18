"""Regression check for MCP soft-timeout recovery.

The MCP response guard must not retry normal calls. It should only activate
after a soft timeout, and memoria_remember must recover from committed state
instead of writing the same memory again.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from memoria_mcp.bm25 import tokenize_retrieval_text
from memoria_mcp.server import MemoriaRouter, _json_text_for_mcp
from memoria_mcp.write_queue import ProjectWriteQueue


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_check() -> dict[str, Any]:
    old_env = {
        "MEMORIA_MCP_ENABLE_SEMANTIC": os.environ.get("MEMORIA_MCP_ENABLE_SEMANTIC"),
        "MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE": os.environ.get("MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE"),
        "MEMORIA_MCP_TOOL_SOFT_TIMEOUT_SECONDS": os.environ.get("MEMORIA_MCP_TOOL_SOFT_TIMEOUT_SECONDS"),
        "MEMORIA_MCP_READONLY_SOFT_TIMEOUT_SECONDS": os.environ.get("MEMORIA_MCP_READONLY_SOFT_TIMEOUT_SECONDS"),
        "MEMORIA_MCP_TOOL_SOFT_TIMEOUT_RECOVERY": os.environ.get("MEMORIA_MCP_TOOL_SOFT_TIMEOUT_RECOVERY"),
        "MEMORIA_MCP_TOOL_RECOVERY_TIMEOUT_SECONDS": os.environ.get("MEMORIA_MCP_TOOL_RECOVERY_TIMEOUT_SECONDS"),
        "MEMORIA_MCP_HANDLER_TIMEOUT_SECONDS": os.environ.get("MEMORIA_MCP_HANDLER_TIMEOUT_SECONDS"),
        "MEMORIA_MCP_WRITE_QUEUE_ENABLED": os.environ.get("MEMORIA_MCP_WRITE_QUEUE_ENABLED"),
        "MEMORIA_MCP_WRITE_QUEUE_WORKER_ENABLED": os.environ.get("MEMORIA_MCP_WRITE_QUEUE_WORKER_ENABLED"),
    }
    os.environ["MEMORIA_MCP_ENABLE_SEMANTIC"] = "false"
    os.environ["MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE"] = "live"
    os.environ["MEMORIA_MCP_TOOL_SOFT_TIMEOUT_RECOVERY"] = "true"
    os.environ["MEMORIA_MCP_WRITE_QUEUE_ENABLED"] = "false"

    marker = f"soft_timeout_marker_{int(time.time() * 1000)}"
    project = "soft_timeout_recovery_project"

    try:
        with tempfile.TemporaryDirectory(prefix="ripple-memory-soft-timeout-") as tmp:
            data_dir = Path(tmp)
            router = MemoriaRouter(str(data_dir))
            try:
                srv = router._get_server(project)
                original_remember = srv._tool_remember
                call_count = {"fast": 0, "slow": 0}

                os.environ["MEMORIA_MCP_TOOL_SOFT_TIMEOUT_SECONDS"] = "2"

                def counted_remember(args: dict) -> dict:
                    call_count["fast"] += 1
                    return original_remember(args)

                srv._tool_remember = counted_remember  # type: ignore[method-assign]
                fast = router._dispatch_tool_for_mcp(
                    "memoria_remember",
                    {
                        "project": project,
                        "content": f"{marker} fast normal call",
                        "type": "debug_insight",
                        "importance": 0.4,
                        "confidence": 0.95,
                    },
                )
                _assert(fast.get("stored") is True, f"fast remember failed: {fast}")
                _assert(call_count["fast"] == 1, f"fast path retried unexpectedly: {call_count}")
                _assert(not fast.get("recovered_after_soft_timeout"), f"fast path recovered unexpectedly: {fast}")

                os.environ["MEMORIA_MCP_TOOL_SOFT_TIMEOUT_SECONDS"] = "0.05"
                slow_content = f"{marker} committed remember response should be recovered"
                slow_fact_key = f"test.soft_timeout.{marker}"

                def slow_after_commit_remember(args: dict) -> dict:
                    call_count["slow"] += 1
                    result = original_remember(args)
                    time.sleep(0.75)
                    return result

                srv._tool_remember = slow_after_commit_remember  # type: ignore[method-assign]
                start = time.perf_counter()
                recovered = router._dispatch_tool_for_mcp(
                    "memoria_remember",
                    {
                        "project": project,
                        "content": slow_content,
                        "type": "arch_decision",
                        "importance": 0.82,
                        "confidence": 0.96,
                        "fact_key": slow_fact_key,
                    },
                )
                elapsed = time.perf_counter() - start
                _assert(elapsed < 0.7, f"soft timeout recovery returned too slowly: {elapsed:.3f}s")
                _assert(recovered.get("stored") is True, f"recovered remember failed: {recovered}")
                _assert(recovered.get("recovered_after_soft_timeout") is True, f"missing recovery flag: {recovered}")
                _assert(recovered.get("recovery", {}).get("retry_performed") is False, f"write was retried: {recovered}")
                _assert(call_count["slow"] == 1, f"slow remember retried unexpectedly: {call_count}")

                time.sleep(0.45)
                recall = router._dispatch_tool(
                    "memoria_recall",
                    {"project": project, "query": slow_content, "top_k": 5},
                )
                matching = [
                    item
                    for item in recall.get("results", [])
                    if item.get("description") == slow_content
                ]
                _assert(len(matching) == 1, f"expected exactly one committed slow memory, got {matching}")

                original_dispatch_for_mcp = router._dispatch_tool_for_mcp

                os.environ.pop("MEMORIA_MCP_HANDLER_TIMEOUT_SECONDS", None)
                os.environ["MEMORIA_MCP_TOOL_SOFT_TIMEOUT_SECONDS"] = "0.05"
                os.environ["MEMORIA_MCP_READONLY_SOFT_TIMEOUT_SECONDS"] = "1.5"
                os.environ["MEMORIA_MCP_TOOL_RECOVERY_TIMEOUT_SECONDS"] = "0.1"

                def slow_readonly_handler(name: str, arguments: dict) -> dict:
                    time.sleep(0.25)
                    return original_dispatch_for_mcp(name, arguments)

                router._dispatch_tool_for_mcp = slow_readonly_handler  # type: ignore[method-assign]
                try:
                    start = time.perf_counter()
                    readonly_budget = asyncio.run(router._dispatch_tool_for_mcp_async(
                        "memoria_recall",
                        {"project": project, "query": slow_content, "top_k": 5},
                    ))
                    readonly_budget_elapsed = time.perf_counter() - start
                finally:
                    router._dispatch_tool_for_mcp = original_dispatch_for_mcp  # type: ignore[method-assign]
                _assert(
                    readonly_budget.get("error") != "tool_soft_timeout",
                    f"read-only recall used the short write timeout budget: {readonly_budget}",
                )
                _assert(
                    readonly_budget.get("count", 0) >= 1,
                    f"read-only recall did not return results under its own budget: {readonly_budget}",
                )
                _assert(
                    0.2 <= readonly_budget_elapsed < 1.2,
                    f"read-only independent budget behaved unexpectedly: {readonly_budget_elapsed:.3f}s",
                )

                def wedged_handler(name: str, arguments: dict) -> dict:
                    time.sleep(0.35)
                    return {"count": 0, "results": []}

                os.environ["MEMORIA_MCP_TOOL_SOFT_TIMEOUT_SECONDS"] = "2"
                os.environ["MEMORIA_MCP_TOOL_RECOVERY_TIMEOUT_SECONDS"] = "0.2"
                os.environ["MEMORIA_MCP_HANDLER_TIMEOUT_SECONDS"] = "0.05"
                router._dispatch_tool_for_mcp = wedged_handler  # type: ignore[method-assign]
                try:
                    start = time.perf_counter()
                    outer_recovered = asyncio.run(router._dispatch_tool_for_mcp_async(
                        "memoria_recall",
                        {"project": project, "query": slow_content, "top_k": 5},
                    ))
                    outer_elapsed = time.perf_counter() - start
                finally:
                    router._dispatch_tool_for_mcp = original_dispatch_for_mcp  # type: ignore[method-assign]

                _assert(outer_elapsed < 0.3, f"outer MCP handler guard returned too slowly: {outer_elapsed:.3f}s")
                _assert(
                    outer_recovered.get("recovered_after_soft_timeout") is True,
                    f"outer MCP handler guard did not recover read-only call: {outer_recovered}",
                )

                os.environ["MEMORIA_MCP_WRITE_QUEUE_ENABLED"] = "true"
                os.environ["MEMORIA_MCP_WRITE_QUEUE_WORKER_ENABLED"] = "false"
                try:
                    start = time.perf_counter()
                    outer_remember = asyncio.run(router._dispatch_tool_for_mcp_async(
                        "memoria_remember",
                        {
                            "project": project,
                            "content": f"{marker} queued remember should fail open immediately",
                            "type": "debug_insight",
                        },
                    ))
                    outer_remember_elapsed = time.perf_counter() - start
                finally:
                    os.environ["MEMORIA_MCP_WRITE_QUEUE_ENABLED"] = "false"
                    os.environ["MEMORIA_MCP_WRITE_QUEUE_WORKER_ENABLED"] = "false"

                _assert(
                    outer_remember_elapsed < 0.16,
                    f"queue-first remember MCP path was too slow: {outer_remember_elapsed:.3f}s",
                )
                _assert(
                    outer_remember.get("commit_state") == "queued",
                    f"queue-first remember did not return queued payload: {outer_remember}",
                )
                _assert(
                    outer_remember.get("recovered_after_soft_timeout") is not True,
                    f"queue-first remember should not use timeout recovery: {outer_remember}",
                )
                queue_counts = ProjectWriteQueue(data_dir, project).counts()
                _assert(
                    queue_counts["processing"] == 0 and queue_counts["lock_present"] == 0,
                    f"queue-first remember started in-process writer work: {queue_counts}",
                )
                router._flush_write_queue(project, budget_seconds=5.0)

                poison_query = "资料库\ud800 活资料 D:\\sample-agent-project"
                poison_recall = asyncio.run(router._dispatch_tool_for_mcp_async(
                    "memoria_recall",
                    {"project": project, "query": poison_query, "top_k": 3},
                ))
                rendered = _json_text_for_mcp(poison_recall, indent=2)
                rendered.encode("utf-8")
                _assert(
                    not any(0xD800 <= ord(char) <= 0xDFFF for char in rendered),
                    "MCP response still contains a lone surrogate",
                )

                long_tokens = tokenize_retrieval_text("资料库中文路径" * 1000, limit=64)
                _assert(long_tokens, "long Chinese input produced no retrieval tokens")
                _assert(
                    max(len(token) for token in long_tokens) <= 128,
                    f"retrieval token length was not capped: {long_tokens}",
                )

                jsonl_trace = data_dir / "_runtime" / "tool_events.jsonl"
                _assert(not jsonl_trace.exists(), "tool event trace must not append to tool_events.jsonl")
                state_files = list((data_dir / "_runtime" / "tool_events").glob("*.json"))
                _assert(state_files, "tool event state file was not written")
                state = json.loads(state_files[0].read_text(encoding="utf-8"))
                _assert(state.get("mode") == "single_state_update", f"tool trace is not update-style: {state}")

                return {
                    "ok": True,
                    "data_dir": str(data_dir),
                    "fast_call_count": call_count["fast"],
                    "slow_call_count": call_count["slow"],
                    "recovered_node_id": recovered.get("node_id"),
                    "soft_timeout_elapsed_seconds": round(elapsed, 4),
                    "readonly_independent_budget_elapsed_seconds": round(readonly_budget_elapsed, 4),
                    "outer_handler_timeout_elapsed_seconds": round(outer_elapsed, 4),
                    "queue_first_remember_elapsed_seconds": round(outer_remember_elapsed, 4),
                    "mcp_response_utf8_safe": True,
                    "max_retrieval_token_chars": max(len(token) for token in long_tokens),
                    "tool_event_state_files": len(state_files),
                    "matching_recall_count": len(matching),
                }
            finally:
                router.close()
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
