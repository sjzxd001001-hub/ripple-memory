from __future__ import annotations

import json
import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import memoria_mcp.install_check as install_check
import memoria_mcp.daemon_client as daemon_client
from memoria_mcp.daemon_client import call_daemon_tool, ensure_agent_daemon, port_file_path, shutdown_daemon
from memoria_mcp.lifecycle import is_process_alive
from memoria_mcp.write_queue import ProjectWriteQueue


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _wait_for_stop(data_dir: Path, pid: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not port_file_path(data_dir).exists() and (pid <= 0 or not is_process_alive(pid)):
            return
        time.sleep(0.1)
    raise AssertionError("agent daemon remained alive after shutdown")


def _wait_for_port(data_dir: Path, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_file_path(data_dir).exists():
            return
        time.sleep(0.1)
    raise AssertionError("agent daemon port file was not created")


def _check_no_owner_auto_exit(base_env: dict[str, str]) -> dict:
    with tempfile.TemporaryDirectory(prefix="ripple-agent-daemon-no-owner-") as tmp:
        data_dir = Path(tmp)
        env = dict(base_env)
        env.update({
            "MEMORIA_MCP_DATA_DIR": str(data_dir),
            "MEMORIA_MCP_ENABLE_SEMANTIC": "false",
            "MEMORIA_MCP_PRELOAD_EMBEDDING": "false",
            "MEMORIA_MCP_WRITE_QUEUE_ENABLED": "false",
            "MEMORIA_MCP_DAEMON_AGENT_EXIT_GRACE_SECONDS": "1",
        })
        proc = subprocess.Popen(
            [sys.executable, "-m", "memoria_mcp.agent_daemon"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        start = time.perf_counter()
        try:
            _wait_for_port(data_dir)
            deadline = time.time() + 8.0
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.1)
            elapsed = time.perf_counter() - start
            _assert(proc.poll() is not None, "daemon did not auto-exit after no-owner grace")
            _assert(not port_file_path(data_dir).exists(), "port file remained after no-owner exit")
            return {"no_owner_exit_elapsed_seconds": round(elapsed, 3)}
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()


def _check_owner_exit_after_registration(base_env: dict[str, str]) -> dict:
    with tempfile.TemporaryDirectory(prefix="ripple-agent-daemon-owner-exit-") as tmp:
        data_dir = Path(tmp)
        env = dict(base_env)
        env.update({
            "MEMORIA_MCP_DATA_DIR": str(data_dir),
            "MEMORIA_MCP_ENABLE_SEMANTIC": "false",
            "MEMORIA_MCP_PRELOAD_EMBEDDING": "false",
            "MEMORIA_MCP_WRITE_QUEUE_ENABLED": "false",
            "MEMORIA_MCP_DAEMON_AGENT_EXIT_GRACE_SECONDS": "1",
        })
        helper_code = r"""
import json
import os
from pathlib import Path

from memoria_mcp.daemon_client import ensure_agent_daemon

os.environ["RIPPLE_MEMORY_AGENT_PID"] = str(os.getpid())
ready = ensure_agent_daemon(Path(os.environ["MEMORIA_MCP_DATA_DIR"]))
print(json.dumps(ready), flush=True)
"""
        helper = subprocess.run(
            [sys.executable, "-c", helper_code],
            text=True,
            capture_output=True,
            timeout=12,
            env=env,
            check=False,
        )
        _assert(helper.returncode == 0, f"owner helper failed: {helper.stderr or helper.stdout}")
        try:
            ready = json.loads(helper.stdout.strip().splitlines()[-1])
        except Exception as exc:
            raise AssertionError(f"owner helper did not print daemon state: {helper.stdout!r}") from exc
        _assert(ready.get("ok"), f"owner helper daemon start failed: {ready}")
        pid = int((ready.get("daemon") or {}).get("pid") or 0)
        _assert(pid > 0, f"owner-exit daemon pid missing: {ready}")
        start = time.perf_counter()
        _wait_for_stop(data_dir, pid, timeout=8.0)
        elapsed = time.perf_counter() - start
        return {"owner_exit_elapsed_seconds": round(elapsed, 3)}


def _check_owner_alive_prevents_exit(base_env: dict[str, str]) -> dict:
    with tempfile.TemporaryDirectory(prefix="ripple-agent-daemon-owner-alive-") as tmp:
        data_dir = Path(tmp)
        env = dict(base_env)
        env.update({
            "MEMORIA_MCP_DATA_DIR": str(data_dir),
            "MEMORIA_MCP_ENABLE_SEMANTIC": "false",
            "MEMORIA_MCP_PRELOAD_EMBEDDING": "false",
            "MEMORIA_MCP_WRITE_QUEUE_ENABLED": "false",
            "MEMORIA_MCP_DAEMON_AGENT_EXIT_GRACE_SECONDS": "1",
        })
        old_env = os.environ.copy()
        os.environ.clear()
        os.environ.update(env)
        try:
            ready = ensure_agent_daemon(data_dir)
            _assert(ready.get("ok"), f"owner-alive daemon did not start: {ready}")
            pid = int((ready.get("daemon") or {}).get("pid") or 0)
            _assert(pid > 0, "owner-alive daemon pid missing")
            time.sleep(2.5)
            ping = ensure_agent_daemon(data_dir)
            ping_pid = int((ping.get("daemon") or {}).get("pid") or 0)
            _assert(ping_pid == pid, f"daemon exited while agent owner was alive: {pid} vs {ping_pid}")
            shutdown = shutdown_daemon(data_dir)
            _assert(shutdown.get("ok"), f"owner-alive shutdown failed: {shutdown}")
            _wait_for_stop(data_dir, pid)
            return {"owner_alive_retained_daemon": True}
        finally:
            os.environ.clear()
            os.environ.update(old_env)
            try:
                shutdown_daemon(data_dir)
            except Exception:
                pass


def _check_direct_daemon_singleton(base_env: dict[str, str]) -> dict:
    """Direct daemon starts must self-collapse to one owner for a data root.

    This covers hosts or stale runtime copies that bypass the stdio proxy's
    client-side launch lock. Only one daemon may publish/own the port file.
    """
    with tempfile.TemporaryDirectory(prefix="ripple-agent-daemon-direct-singleton-") as tmp:
        data_dir = Path(tmp)
        env = dict(base_env)
        env.update({
            "MEMORIA_MCP_DATA_DIR": str(data_dir),
            "MEMORIA_MCP_ENABLE_SEMANTIC": "false",
            "MEMORIA_MCP_PRELOAD_EMBEDDING": "false",
            "MEMORIA_MCP_WRITE_QUEUE_ENABLED": "false",
            "MEMORIA_MCP_DAEMON_AGENT_EXIT_GRACE_SECONDS": "60",
        })
        procs = [
            subprocess.Popen(
                [sys.executable, "-m", "memoria_mcp.agent_daemon"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            for _ in range(3)
        ]
        try:
            _wait_for_port(data_dir, timeout=10.0)
            deadline = time.time() + 8.0
            alive: list[int] = []
            while time.time() < deadline:
                alive = [proc.pid for proc in procs if proc.poll() is None and is_process_alive(proc.pid)]
                if len(alive) <= 1:
                    break
                time.sleep(0.1)
            _assert(len(alive) == 1, f"direct daemon starts left multiple live daemons: {alive}")
            shutdown = shutdown_daemon(data_dir)
            _assert(shutdown.get("ok"), f"direct singleton shutdown failed: {shutdown}")
            _wait_for_stop(data_dir, alive[0])
            return {
                "direct_daemon_singleton_pid": alive[0],
                "direct_daemon_suppressed": len(procs) - 1,
            }
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.terminate()
            time.sleep(0.2)
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()


def _check_slow_response_preserves_port(base_env: dict[str, str]) -> dict:
    """A slow but live daemon response must not make clients delete port.json."""
    with tempfile.TemporaryDirectory(prefix="ripple-agent-daemon-slow-response-") as tmp:
        data_dir = Path(tmp)
        env = dict(base_env)
        env["MEMORIA_MCP_DATA_DIR"] = str(data_dir)
        token = "slow-response-token"
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = int(server.getsockname()[1])
        port_file = port_file_path(data_dir)
        port_file.parent.mkdir(parents=True, exist_ok=True)
        port_file.write_text(
            json.dumps({
                "schema": "ripple_memory_agent_daemon_v1",
                "host": "127.0.0.1",
                "port": port,
                "pid": os.getpid(),
                "token": token,
                "started_at": time.time(),
            }),
            encoding="utf-8",
        )

        def _slow_server() -> None:
            try:
                conn, _addr = server.accept()
                with conn:
                    try:
                        conn.recv(65536)
                        time.sleep(0.25)
                        conn.sendall(b'{"ok": true, "daemon": {"pid": 1}}\n')
                    except OSError:
                        pass
            except OSError:
                pass

        old_env = os.environ.copy()
        old_timeout = daemon_client.RECV_TIMEOUT_SECONDS
        thread = threading.Thread(target=_slow_server, daemon=True)
        thread.start()
        os.environ.clear()
        os.environ.update(env)
        daemon_client.RECV_TIMEOUT_SECONDS = 0.05
        try:
            result = daemon_client._request_once(data_dir, {"op": "ping"})
            _assert(result and result.get("error") == "agent_daemon_response_timeout", f"unexpected slow response result: {result}")
            _assert(port_file.exists(), "slow daemon response incorrectly removed port.json")
            return {"slow_response_preserved_port": True}
        finally:
            daemon_client.RECV_TIMEOUT_SECONDS = old_timeout
            os.environ.clear()
            os.environ.update(old_env)
            try:
                server.close()
            except OSError:
                pass
            thread.join(timeout=1.0)


def _write_fake_live_smoke_hook(hook_cmd: Path, helper_py: Path) -> None:
    helper_py.write_text(
        r'''
import json
import os
import sys
from pathlib import Path

payload = json.loads(sys.stdin.read() or "{}")
data_dir = Path(os.environ["MEMORIA_MCP_DATA_DIR"])
project = payload.get("project") or os.environ.get("RIPPLE_MEMORY_PROJECT") or "default"
window_id = payload.get("window_id") or os.environ.get("RIPPLE_MEMORY_WINDOW_ID") or "default"
prompt = payload.get("prompt") or ""
latch = data_dir / "_window_state" / project / window_id / "original-word-latch.md"
latch.parent.mkdir(parents=True, exist_ok=True)
latch.write_text(
    "# Original Words Latch\n\n"
    "## Task State\n"
    "- State: active\n"
    f"- Goal: Task: {prompt}\n\n"
    "## Recent User Turns\n"
    f"- Task: {prompt}\n",
    encoding="utf-8",
)
print(json.dumps({
    "continue": True,
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": f"<ripple_memory_context>\n{prompt}\n</ripple_memory_context>",
    },
}, ensure_ascii=False))
'''.strip(),
        encoding="utf-8",
    )
    hook_cmd.write_text(
        f'@echo off\r\n"{sys.executable}" "{helper_py}"\r\n',
        encoding="utf-8",
    )


def _check_install_live_smoke_handles_queued_remember() -> dict:
    """The live install smoke must follow the production queued-write contract."""
    with tempfile.TemporaryDirectory(prefix="ripple-live-smoke-check-") as tmp:
        root = Path(tmp)
        data_dir = root / "data"
        hook_cmd = root / "fake-hook.cmd"
        helper_py = root / "fake-hook.py"
        _write_fake_live_smoke_hook(hook_cmd, helper_py)

        port = install_check.port_file_path(data_dir)
        port.parent.mkdir(parents=True, exist_ok=True)
        port.write_text(
            json.dumps({
                "schema": "ripple_memory_agent_daemon_v1",
                "host": "127.0.0.1",
                "port": 1,
                "pid": os.getpid(),
                "token": "fake-live-smoke",
                "started_at": time.time(),
            }),
            encoding="utf-8",
        )

        state: dict[str, str] = {}
        original_call_daemon_tool = install_check.call_daemon_tool

        def fake_call_daemon_tool(call_data_dir: Path, name: str, arguments: dict) -> dict:
            project = str(arguments.get("project") or "default")
            if name == "memoria_remember":
                queue = ProjectWriteQueue(call_data_dir, project)
                pending_id = queue.enqueue(arguments)
                node_id = "mem_live_smoke_queued"
                content = str(arguments.get("content") or "")
                state.update({"node_id": node_id, "content": content})
                worker = queue.process_ready(
                    lambda _queued_args: {
                        "stored": True,
                        "node_id": node_id,
                        "id": node_id,
                        "ref_id": f"memory_node:{node_id}",
                    },
                    budget_seconds=2.0,
                )
                _assert(worker.get("processed") == 1, f"fake queue worker did not process item: {worker}")
                return {
                    "ok": True,
                    "result": {
                        "stored": True,
                        "accepted": True,
                        "queued": True,
                        "commit_state": "queued",
                        "pending_id": pending_id,
                    },
                }
            if name == "memoria_recall":
                node_id = state.get("node_id", "")
                return {
                    "ok": True,
                    "result": {
                        "count": 1,
                        "results": [{"ref_id": f"memory_node:{node_id}", "id": node_id}],
                    },
                }
            if name == "memoria_read":
                return {"ok": True, "result": {"text": state.get("content", "")}}
            if name == "memoria_forget":
                return {"ok": True, "result": {"deleted": arguments.get("node_id") == state.get("node_id")}}
            raise AssertionError(f"unexpected fake daemon tool: {name}")

        install_check.call_daemon_tool = fake_call_daemon_tool
        try:
            result = install_check.check_live_smoke(data_dir=data_dir, hook_cmd=hook_cmd, host="codex")
        finally:
            install_check.call_daemon_tool = original_call_daemon_tool

        _assert(result.ok, f"live smoke rejected queued remember/hook output: {result}")
        return {
            "install_live_smoke_queued_commit": True,
            "install_live_smoke_hook_context": result.details.get("hook_context_has_marker"),
        }


def run_check() -> dict:
    old_env = os.environ.copy()
    _assert(importlib.util.find_spec("memoria_mcp.tool_worker") is None, "read-only tool_worker module must stay removed")
    with tempfile.TemporaryDirectory(prefix="ripple-agent-daemon-check-") as tmp:
        data_dir = Path(tmp)
        os.environ.update({
            "MEMORIA_MCP_DATA_DIR": str(data_dir),
            "MEMORIA_MCP_ENABLE_SEMANTIC": "false",
            "MEMORIA_MCP_PRELOAD_EMBEDDING": "false",
            "MEMORIA_MCP_WRITE_QUEUE_ENABLED": "false",
            "MEMORIA_MCP_IDLE_SLEEP_SECONDS": "3600",
            "MEMORIA_MCP_IDLE_EXIT_SECONDS": "36000",
        })
        try:
            first = ensure_agent_daemon(data_dir)
            _assert(first.get("ok"), f"first daemon start failed: {first}")
            first_pid = int((first.get("daemon") or {}).get("pid") or 0)
            _assert(first_pid > 0, f"missing daemon pid: {first}")

            second = ensure_agent_daemon(data_dir)
            _assert(second.get("ok"), f"second daemon ensure failed: {second}")
            second_pid = int((second.get("daemon") or {}).get("pid") or 0)
            _assert(second_pid == first_pid, f"daemon was not reused: {first_pid} vs {second_pid}")

            marker = f"agent_daemon_marker_{int(time.time() * 1000)}"
            project = "agent_daemon_check"
            remember = call_daemon_tool(
                data_dir,
                "memoria_remember",
                {
                    "project": project,
                    "content": f"{marker}: daemon-shared MCP execution smoke.",
                    "type": "debug_insight",
                    "importance": 0.6,
                    "confidence": 0.9,
                },
            )
            remember_result = remember.get("result") or {}
            _assert(remember.get("ok") and remember_result.get("stored"), f"remember failed: {remember}")

            recall_start = time.perf_counter()
            recall = call_daemon_tool(
                data_dir,
                "memoria_recall",
                {"project": project, "query": marker, "top_k": 3},
            )
            recall_elapsed = time.perf_counter() - recall_start
            recall_result = recall.get("result") or {}
            _assert(recall.get("ok") and recall_result.get("count", 0) >= 1, f"recall failed: {recall}")
            _assert(
                recall_elapsed < 0.8,
                f"daemon recall was unexpectedly slow on the direct daemon path: {recall_elapsed:.3f}s",
            )
            ref_id = recall_result["results"][0]["ref_id"]

            read = call_daemon_tool(
                data_dir,
                "memoria_read",
                {"project": project, "ref_id": ref_id, "max_chars": 1000},
            )
            read_result = read.get("result") or {}
            _assert(read.get("ok") and marker in str(read_result.get("text") or ""), f"read failed: {read}")

            forget = call_daemon_tool(
                data_dir,
                "memoria_forget",
                {"project": project, "node_id": remember_result.get("node_id")},
            )
            forget_result = forget.get("result") or {}
            _assert(forget.get("ok") and forget_result.get("deleted"), f"forget failed: {forget}")

            shutdown = shutdown_daemon(data_dir)
            _assert(shutdown.get("ok"), f"shutdown failed: {shutdown}")
            _wait_for_stop(data_dir, first_pid)

            restart_start = time.perf_counter()
            restarted = call_daemon_tool(
                data_dir,
                "memoria_remember",
                {
                    "project": project,
                    "content": f"{marker}: daemon restart after shutdown smoke.",
                    "type": "debug_insight",
                    "importance": 0.5,
                    "confidence": 0.9,
                },
            )
            restart_elapsed = time.perf_counter() - restart_start
            _assert((restarted.get("result") or {}).get("stored"), f"restart call failed: {restarted}")
            restarted_state = ensure_agent_daemon(data_dir)
            restarted_pid = int((restarted_state.get("daemon") or {}).get("pid") or 0)
            _assert(restarted_pid > 0 and restarted_pid != first_pid, "daemon did not restart with a new pid")
            shutdown = shutdown_daemon(data_dir)
            _assert(shutdown.get("ok"), f"second shutdown failed: {shutdown}")
            _wait_for_stop(data_dir, restarted_pid)

            no_owner = _check_no_owner_auto_exit(old_env)
            owner_exit = _check_owner_exit_after_registration(old_env)
            owner_alive = _check_owner_alive_prevents_exit(old_env)
            direct_singleton = _check_direct_daemon_singleton(old_env)
            slow_response = _check_slow_response_preserves_port(old_env)
            live_smoke = _check_install_live_smoke_handles_queued_remember()
            return {
                "passed": True,
                "daemon_pid": first_pid,
                "restarted_pid": restarted_pid,
                "reused": True,
                "restart_elapsed_seconds": round(restart_elapsed, 3),
                "daemon_recall_elapsed_seconds": round(recall_elapsed, 3),
                **no_owner,
                **owner_exit,
                **owner_alive,
                **direct_singleton,
                **slow_response,
                **live_smoke,
                "tools": ["memoria_remember", "memoria_recall", "memoria_read", "memoria_forget"],
            }
        finally:
            os.environ.clear()
            os.environ.update(old_env)
            try:
                shutdown_daemon(data_dir)
            except Exception:
                pass


if __name__ == "__main__":
    result = run_check()
    print(result)
