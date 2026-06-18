"""Regression check for the durable per-project remember write queue."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from memoria_mcp.server import MemoriaRouter
from memoria_mcp.write_queue import ProjectWriteQueue


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _graph_descriptions(data_dir: Path, project: str) -> list[str]:
    db_path = data_dir / project / "memoria.db"
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute("SELECT state_json FROM graph_state WHERE id = 1").fetchone()
    _assert(row is not None, f"missing graph_state for {project}")
    state = json.loads(row[0])
    return [
        str(node.get("summary", {}).get("description") or "")
        for node in state.get("nodes", {}).values()
    ]


def _flush_until_empty(router: MemoriaRouter, data_dir: Path, project: str) -> dict[str, int]:
    queue = ProjectWriteQueue(data_dir, project)
    counts = queue.counts()
    for _ in range(20):
        router._flush_write_queue(project, budget_seconds=5.0)
        counts = queue.counts()
        if counts["ready"] == 0 and counts["processing"] == 0:
            return counts
        time.sleep(0.05)
    return counts


def _worker_code() -> str:
    return r'''
import json
import os
import sys
from memoria_mcp.server import MemoriaRouter

data_dir, project, marker, worker = sys.argv[1:5]
os.environ["MEMORIA_MCP_ENABLE_SEMANTIC"] = "false"
os.environ["MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE"] = "live"
os.environ["MEMORIA_MCP_WRITE_QUEUE_ENABLED"] = "true"
os.environ["MEMORIA_MCP_WRITE_QUEUE_WAIT_SECONDS"] = "1"
router = MemoriaRouter(data_dir)
results = []
try:
    for index in range(3):
        content = f"{marker} worker={worker} item={index}"
        result = router._dispatch_tool("memoria_remember", {
            "project": project,
            "content": content,
            "type": "debug_insight",
            "importance": 0.5,
            "confidence": 0.95,
            "fact_key": f"test.write_queue.{marker}.{worker}.{index}",
        })
        results.append({
            "content": content,
            "stored": result.get("stored"),
            "commit_state": result.get("commit_state"),
            "pending_id": result.get("pending_id"),
        })
finally:
    router.close()
print(json.dumps({"ok": True, "results": results}, ensure_ascii=False))
'''


def run_check() -> dict[str, Any]:
    old_env = {
        "MEMORIA_MCP_ENABLE_SEMANTIC": os.environ.get("MEMORIA_MCP_ENABLE_SEMANTIC"),
        "MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE": os.environ.get("MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE"),
        "MEMORIA_MCP_WRITE_QUEUE_ENABLED": os.environ.get("MEMORIA_MCP_WRITE_QUEUE_ENABLED"),
        "MEMORIA_MCP_WRITE_QUEUE_WORKER_ENABLED": os.environ.get("MEMORIA_MCP_WRITE_QUEUE_WORKER_ENABLED"),
        "MEMORIA_MCP_WRITE_QUEUE_WAIT_SECONDS": os.environ.get("MEMORIA_MCP_WRITE_QUEUE_WAIT_SECONDS"),
        "MEMORIA_MCP_WRITE_QUEUE_DONE_MAX_FILES": os.environ.get("MEMORIA_MCP_WRITE_QUEUE_DONE_MAX_FILES"),
    }
    os.environ["MEMORIA_MCP_ENABLE_SEMANTIC"] = "false"
    os.environ["MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE"] = "live"
    os.environ["MEMORIA_MCP_WRITE_QUEUE_ENABLED"] = "true"
    os.environ["MEMORIA_MCP_WRITE_QUEUE_WORKER_ENABLED"] = "false"
    os.environ["MEMORIA_MCP_WRITE_QUEUE_DONE_MAX_FILES"] = "20"

    marker = f"write_queue_marker_{int(time.time() * 1000)}"
    project = "write_queue_project"

    try:
        with tempfile.TemporaryDirectory(prefix="ripple-memory-write-queue-", ignore_cleanup_errors=True) as tmp:
            data_dir = Path(tmp)
            router = MemoriaRouter(str(data_dir))
            try:
                normal_content = f"{marker} normal committed remember"
                normal = router._dispatch_tool("memoria_remember", {
                    "project": project,
                    "content": normal_content,
                    "type": "fact",
                    "importance": 0.5,
                    "confidence": 0.95,
                })
                _assert(normal.get("stored") is True, f"normal queued remember failed: {normal}")
                _assert(normal.get("commit_state") == "queued", f"normal remember did not queue first: {normal}")

                worker_env = os.environ.copy()
                worker_env["PYTHONPATH"] = str(Path.cwd() / "src")
                worker = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "memoria_mcp.queue_worker",
                        "--data-dir",
                        str(data_dir),
                        "--project",
                        project,
                        "--budget-seconds",
                        "5",
                    ],
                    cwd=str(Path.cwd()),
                    env=worker_env,
                    text=True,
                    capture_output=True,
                    timeout=20,
                    check=False,
                )
                _assert(worker.returncode == 0, f"queue worker failed: stdout={worker.stdout} stderr={worker.stderr}")
                worker_payload = json.loads(worker.stdout)
                _assert(worker_payload.get("processed", 0) >= 1, f"queue worker did not process normal write: {worker_payload}")
                _assert(normal_content in _graph_descriptions(data_dir, project), "worker did not commit normal write")

                queue = ProjectWriteQueue(data_dir, project)
                token = queue.try_acquire_lock()
                _assert(token, "failed to acquire artificial writer lock")
                try:
                    os.environ["MEMORIA_MCP_WRITE_QUEUE_WAIT_SECONDS"] = "0.05"
                    queued_content = f"{marker} queued while another writer is active"
                    start = time.perf_counter()
                    queued = router._dispatch_tool("memoria_remember", {
                        "project": project,
                        "content": queued_content,
                        "type": "debug_insight",
                        "importance": 0.5,
                        "confidence": 0.95,
                    })
                    queued_elapsed = time.perf_counter() - start
                    _assert(queued_elapsed < 0.5, f"queued remember waited too long: {queued_elapsed:.3f}s")
                    _assert(queued.get("stored") is True, f"queued remember was not durably accepted: {queued}")
                    _assert(queued.get("commit_state") == "queued", f"expected queued state: {queued}")

                    start = time.perf_counter()
                    recall = router._dispatch_tool("memoria_recall", {
                        "project": project,
                        "query": normal_content,
                        "top_k": 3,
                    })
                    recall_elapsed = time.perf_counter() - start
                    _assert(recall_elapsed < 0.5, f"recall waited behind write queue: {recall_elapsed:.3f}s")
                    _assert(recall.get("count", 0) >= 1, f"recall failed while writer lock held: {recall}")
                finally:
                    queue.release_lock(token)

                counts = _flush_until_empty(router, data_dir, project)
                _assert(counts["ready"] == 0 and counts["processing"] == 0, f"queue did not drain: {counts}")
                descriptions = _graph_descriptions(data_dir, project)
                _assert(queued_content in descriptions, "queued remember was not committed after drain")

                def hanging_commit(project_name: str, args: dict) -> dict:
                    time.sleep(1.0)
                    return {"stored": True}

                router._commit_queued_remember = hanging_commit  # type: ignore[method-assign]
                hang_content = f"{marker} request path must not start in-process commit"
                start = time.perf_counter()
                hang_result = router._dispatch_tool_for_mcp("memoria_remember", {
                    "project": project,
                    "content": hang_content,
                    "type": "debug_insight",
                    "importance": 0.5,
                    "confidence": 0.95,
                })
                hang_elapsed = time.perf_counter() - start
                hang_counts = queue.counts()
                _assert(hang_elapsed < 0.2, f"queue-first remember was too slow: {hang_elapsed:.3f}s")
                _assert(hang_result.get("commit_state") == "queued", f"hang probe did not queue: {hang_result}")
                _assert(hang_counts["processing"] == 0 and hang_counts["lock_present"] == 0, f"request path started writer work: {hang_counts}")
                router._commit_queued_remember = MemoriaRouter._commit_queued_remember.__get__(router, MemoriaRouter)  # type: ignore[method-assign]
                counts = _flush_until_empty(router, data_dir, project)
                _assert(counts["ready"] == 0 and counts["processing"] == 0, f"hang probe did not drain after restore: {counts}")

                budget_project = "write_queue_budget_project"
                budget_queue = ProjectWriteQueue(data_dir, budget_project)
                for index in range(2):
                    budget_queue.enqueue({
                        "project": budget_project,
                        "content": f"{marker} budget follow-up item {index}",
                    })

                def slow_budget_commit(args: dict) -> dict:
                    time.sleep(0.05)
                    return {"stored": True, "content": args.get("content")}

                budget_first = budget_queue.process_ready(slow_budget_commit, budget_seconds=0.01, max_items=10)
                budget_ready_remaining = int(budget_first.get("ready_remaining") or 0)
                _assert(
                    0 < budget_ready_remaining <= 2,
                    f"budget worker did not report remaining ready work: {budget_first}",
                )
                _assert(budget_first.get("budget_exhausted") is True, f"budget exhaustion not reported: {budget_first}")
                budget_second = budget_queue.process_ready(lambda args: {"stored": True}, budget_seconds=5.0, max_items=10)
                _assert(
                    budget_second.get("processed") == budget_ready_remaining,
                    f"budget follow-up drain failed: {budget_second}",
                )
                budget_counts = budget_queue.counts()
                _assert(budget_counts["ready"] == 0 and budget_counts["processing"] == 0, f"budget queue did not drain: {budget_counts}")

                stale_router = MemoriaRouter(str(data_dir))
                try:
                    stale_router._get_server(project)
                    fresh_router = MemoriaRouter(str(data_dir))
                    try:
                        fresh_content = f"{marker} fresh write must survive stale close"
                        fresh = fresh_router._dispatch_tool("memoria_remember", {
                            "project": project,
                            "content": fresh_content,
                            "type": "arch_decision",
                            "importance": 0.6,
                            "confidence": 0.95,
                        })
                        _assert(fresh.get("commit_state") == "queued", f"fresh write did not queue: {fresh}")
                        fresh_router._flush_write_queue(project, budget_seconds=5.0)
                    finally:
                        fresh_router.close()

                    stale_recall = stale_router._dispatch_tool("memoria_recall", {
                        "project": project,
                        "query": fresh_content,
                        "top_k": 5,
                    })
                    _assert(stale_recall.get("count", 0) >= 1, f"stale router did not reload newer graph: {stale_recall}")
                finally:
                    stale_router.close()

                after_stale_close = _graph_descriptions(data_dir, project)
                _assert(fresh_content in after_stale_close, "stale close overwrote a newer graph_state")
            finally:
                router.close()

            worker_env = os.environ.copy()
            worker_env["PYTHONPATH"] = str(Path.cwd() / "src")
            worker_project = "write_queue_concurrent_project"
            processes = [
                subprocess.run(
                    [sys.executable, "-c", _worker_code(), str(data_dir), worker_project, marker, str(worker)],
                    cwd=str(Path.cwd()),
                    env=worker_env,
                    text=True,
                    capture_output=True,
                    timeout=20,
                    check=False,
                )
                for worker in range(4)
            ]
            for proc in processes:
                _assert(proc.returncode == 0, f"worker failed: stdout={proc.stdout} stderr={proc.stderr}")
                payload = json.loads(proc.stdout)
                for item in payload["results"]:
                    _assert(item["stored"] is True, f"worker remember not stored: {item}")
                    _assert(item["commit_state"] in {"committed", "queued"}, f"bad worker state: {item}")

            verify_router = MemoriaRouter(str(data_dir))
            try:
                concurrent_counts = _flush_until_empty(verify_router, data_dir, worker_project)
                _assert(
                    concurrent_counts["ready"] == 0 and concurrent_counts["processing"] == 0,
                    f"concurrent queue did not drain: {concurrent_counts}",
                )
            finally:
                verify_router.close()

            concurrent_descriptions = _graph_descriptions(data_dir, worker_project)
            expected = [f"{marker} worker={worker} item={index}" for worker in range(4) for index in range(3)]
            missing = [content for content in expected if content not in concurrent_descriptions]
            _assert(not missing, f"concurrent queued writes were lost: {missing}")

            queue_counts = ProjectWriteQueue(data_dir, worker_project).counts()
            return {
                "ok": True,
                "data_dir": str(data_dir),
                "normal_commit_state": normal.get("commit_state"),
                "queued_return_elapsed_seconds": round(queued_elapsed, 4),
                "recall_while_locked_elapsed_seconds": round(recall_elapsed, 4),
                "no_inprocess_writer_elapsed_seconds": round(hang_elapsed, 4),
                "budget_ready_remaining_reported": budget_first.get("ready_remaining"),
                "queued_committed_after_drain": queued_content in descriptions,
                "stale_close_preserved_newer_graph": fresh_content in after_stale_close,
                "concurrent_expected_writes": len(expected),
                "concurrent_committed_writes": len([d for d in concurrent_descriptions if marker in d]),
                "queue_counts": queue_counts,
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
