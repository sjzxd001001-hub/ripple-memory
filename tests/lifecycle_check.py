"""Regression check for process and window lifecycle management."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from memoria_mcp.hook_core import RippleHookEvent, handle_hook_event
from memoria_mcp.lifecycle import IdleLifecycleManager, ProcessRegistry, is_process_alive, window_latch_file
from memoria_mcp.server import MemoriaRouter


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _dead_pid() -> int:
    pid = 99999991
    while is_process_alive(pid):
        pid += 1
    return pid


def _spawn_sleep_process() -> subprocess.Popen[Any]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "memoria_mcp.server"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def run_check() -> dict[str, Any]:
    marker = f"lifecycle_marker_{int(time.time() * 1000)}"
    old_env = os.environ.copy()
    try:
        with tempfile.TemporaryDirectory(prefix="ripple-memory-lifecycle-") as tmp:
            root = Path(tmp)
            data_dir = root / "memory-data"
            workspace = root / "workspace"
            workspace.mkdir()
            os.environ["MEMORIA_MCP_DATA_DIR"] = str(data_dir)
            os.environ["MEMORIA_MCP_ENABLE_SEMANTIC"] = "false"
            os.environ["MEMORIA_MCP_WRITE_QUEUE_ENABLED"] = "false"
            os.environ["RIPPLE_MEMORY_HOOK_ENABLE_SEMANTIC"] = "false"
            os.environ["RIPPLE_MEMORY_HOOK_SEARCH_MODE"] = "live"

            registry = ProcessRegistry(data_dir, host="test", window_id="test-window", session_id="test-session")
            registry.register()
            registry.heartbeat(status="active", marker=marker)
            records = registry.list_processes()
            _assert(any(int(item.get("pid") or 0) == os.getpid() for item in records), "process not registered")

            stale = registry.process_dir / "99999998.json"
            stale.parent.mkdir(parents=True, exist_ok=True)
            stale.write_text(json.dumps({"pid": 99999998, "last_seen_at": 1}), encoding="utf-8")
            cleanup = registry.cleanup_stale_records(stale_after_seconds=0)
            _assert(cleanup.get("removed_count", 0) >= 1 and not stale.exists(), "stale cleanup failed")

            dead_parent = _dead_pid()
            same_data_proc = _spawn_sleep_process()
            other_data_proc = _spawn_sleep_process()
            try:
                same_data_record = registry.process_dir / f"{same_data_proc.pid}.json"
                other_data_record = registry.process_dir / f"{other_data_proc.pid}.json"
                base_record = {
                    "schema": "ripple_memory_process_v1",
                    "status": "active",
                    "parent_pid": dead_parent,
                    "last_seen_at": time.time(),
                    "argv": [sys.executable, "-m", "memoria_mcp.server"],
                    "executable": sys.executable,
                }
                same_data_record.write_text(
                    json.dumps({**base_record, "pid": same_data_proc.pid, "base_data_dir": str(data_dir)}),
                    encoding="utf-8",
                )
                other_data_record.write_text(
                    json.dumps({**base_record, "pid": other_data_proc.pid, "base_data_dir": str(root / "other-data")}),
                    encoding="utf-8",
                )
                orphan_cleanup = registry.cleanup_orphaned_processes()
                same_data_proc.wait(timeout=5)
                _assert(not is_process_alive(same_data_proc.pid), f"orphan process was not killed: {orphan_cleanup}")
                _assert(not same_data_record.exists(), "orphan active record was not removed")
                _assert(is_process_alive(other_data_proc.pid), "different data-dir process should not be killed")
                _assert(other_data_record.exists(), "different data-dir process record should be skipped")
            finally:
                for proc in (same_data_proc, other_data_proc):
                    if proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                other_data_record.unlink(missing_ok=True)

            parent_exit_registry = ProcessRegistry(root / "parent-exit-data", host="test", window_id="parent-exit", session_id="parent-exit")
            parent_exit_registry.parent_pid = _dead_pid()
            parent_manager = IdleLifecycleManager(
                close_cached_state=lambda: None,
                registry=parent_exit_registry,
                sleep_seconds=999,
                exit_seconds=0,
                heartbeat_seconds=0.2,
                exit_process=False,
            )
            parent_manager.start()
            if parent_manager._thread is not None:
                parent_manager._thread.join(timeout=2)
            parent_final = parent_exit_registry.record_path.with_suffix(".final.json")
            _assert(parent_final.is_file(), "parent-exit final lifecycle record missing")
            parent_payload = json.loads(parent_final.read_text(encoding="utf-8"))
            _assert(parent_payload.get("status") == "parent_exit", f"parent death did not exit cleanly: {parent_payload}")

            exit_request = registry.request_exit_for_window(
                window_id="test-window",
                session_id="test-session",
                reason="test",
                include_current=True,
            )
            _assert(os.getpid() in [int(pid) for pid in exit_request.get("requested_pids") or []], "exit request missed current process")
            exit_payload = registry.pop_exit_request()
            _assert(bool(exit_payload) and exit_payload.get("action") == "exit", "exit request was not readable")

            router = MemoriaRouter(str(data_dir))
            manager = IdleLifecycleManager(
                close_cached_state=router.sleep_cached_state,
                registry=registry,
                sleep_seconds=999,
                exit_seconds=0,
                heartbeat_seconds=999,
                exit_process=False,
            )
            router.set_lifecycle_manager(manager)
            try:
                project = "lifecycle_project"
                router._dispatch_tool(
                    "memoria_remember",
                    {
                        "project": project,
                        "content": f"{marker}: process lifecycle check",
                        "type": "debug_insight",
                        "importance": 0.7,
                        "confidence": 0.8,
                    },
                )
                _assert(project in router._servers, "project server was not cached")
                sleep = manager.sleep_now(reason="test")
                _assert(sleep.get("slept") and project not in router._servers, "sleep did not unload cache")
                manager.mark_activity(label="test_wake")
                router._dispatch_tool("memoria_recall", {"project": project, "query": marker, "top_k": 1})
                _assert(project in router._servers, "project server did not wake on use")
            finally:
                router.close()
                registry.unregister(status="test_done")

            window_id = "lifecycle-window"
            project = "lifecycle_project"
            submit = handle_hook_event(
                RippleHookEvent(
                    agent="test",
                    event="UserPromptSubmit",
                    cwd=str(workspace),
                    project=project,
                    window_id=window_id,
                    user_text=f"{marker}: window lifecycle prompt",
                )
            )
            _assert(submit.get("latch", {}).get("updated"), f"latch setup failed: {submit}")
            latch = window_latch_file(cwd=workspace, project=project, window_id=window_id, data_dir=data_dir)
            _assert(latch.is_file(), "latch not created")

            archived = handle_hook_event(
                RippleHookEvent(agent="test", event="WindowArchive", cwd=str(workspace), project=project, window_id=window_id)
            )
            _assert(not latch.exists(), f"archive left active latch behind: {archived}")
            _assert(archived.get("window_lifecycle", {}).get("moved"), f"archive did not move latch: {archived}")
            archive_path = Path(str(archived.get("window_lifecycle", {}).get("archive_path") or ""))
            _assert(str(data_dir) in str(archive_path), f"window archive did not use data dir: {archive_path}")
            _assert(str(data_dir) in str(latch), f"active latch did not use data dir: {latch}")
            _assert(not (workspace / ".ripple-memory" / "archived-windows").exists(), "workspace archive store was created")

            restored = handle_hook_event(
                RippleHookEvent(agent="test", event="WindowRestore", cwd=str(workspace), project=project, window_id=window_id)
            )
            _assert(latch.is_file() and marker in latch.read_text(encoding="utf-8"), f"restore failed: {restored}")

            deleted = handle_hook_event(
                RippleHookEvent(agent="test", event="WindowDelete", cwd=str(workspace), project=project, window_id=window_id)
            )
            _assert(not latch.exists(), f"delete left active latch behind: {deleted}")

            return {
                "ok": True,
                "data_dir": str(data_dir),
                "workspace": str(workspace),
                "process_registered": True,
                "stale_cleanup_removed": cleanup.get("removed_count"),
                "orphan_cleanup_killed": orphan_cleanup.get("killed_count"),
                "parent_death_exits_process": True,
                "window_process_exit_request": True,
                "sleep_unloaded_cache": True,
                "wake_reopened_cache": True,
                "window_archive_restore_delete": True,
            }
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def main() -> int:
    print(json.dumps(run_check(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
