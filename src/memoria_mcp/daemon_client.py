"""Client and launcher for the agent-level Ripple Memory daemon."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from .lifecycle import is_process_alive


DAEMON_DIR_NAME = "agent_daemon"
PORT_FILE_NAME = "port.json"
LAUNCH_LOCK_NAME = "launch.lock"
CONNECT_TIMEOUT_SECONDS = 0.5
RECV_TIMEOUT_SECONDS = 30.0
STARTUP_TIMEOUT_SECONDS = 12.0
LOCK_STALE_SECONDS = max(60.0, STARTUP_TIMEOUT_SECONDS * 2)


def default_data_dir() -> Path:
    return Path(
        os.environ.get("MEMORIA_MCP_DATA_DIR")
        or os.environ.get("RIPPLE_MEMORY_DATA_DIR")
        or os.path.expanduser("~/.ripple-memory")
    ).expanduser()


def daemon_runtime_dir(data_dir: Path) -> Path:
    return data_dir / "_runtime" / DAEMON_DIR_NAME


def port_file_path(data_dir: Path) -> Path:
    return daemon_runtime_dir(data_dir) / PORT_FILE_NAME


def _lock_file_path(data_dir: Path) -> Path:
    return daemon_runtime_dir(data_dir) / LAUNCH_LOCK_NAME


def _read_port_record(data_dir: Path) -> Optional[Dict[str, Any]]:
    path = port_file_path(data_dir)
    try:
        if not path.is_file():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        port = int(record.get("port") or 0)
        pid = int(record.get("pid") or 0)
        if not (1 <= port <= 65535):
            return None
        if pid > 0 and not is_process_alive(pid):
            remove_port_record(data_dir, record)
            return None
        record["port"] = port
        record["pid"] = pid
        return record
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def remove_port_record(data_dir: Path, record: Optional[Dict[str, Any]] = None) -> None:
    path = port_file_path(data_dir)
    try:
        if record:
            current = _read_port_record_no_cleanup(path)
            if not current:
                return
            if int(current.get("port") or 0) != int(record.get("port") or 0):
                return
            token = str(record.get("token") or "")
            if token and str(current.get("token") or "") != token:
                return
        path.unlink(missing_ok=True)
    except OSError:
        pass


def current_agent_owner_pid() -> tuple[int, str]:
    """Return the host/agent process PID that should keep the daemon alive.

    Window-level MCP proxy processes are short-lived implementation details.
    The daemon must instead track the owning agent process. Hosts may provide
    an explicit PID; otherwise the proxy parent is the best portable signal
    because MCP servers are launched by their host agent.
    """
    for name in ("RIPPLE_MEMORY_AGENT_PID", "MEMORIA_MCP_AGENT_PID"):
        raw = os.environ.get(name)
        if raw:
            try:
                pid = int(raw)
            except ValueError:
                continue
            if pid > 0:
                return pid, name
    return os.getppid(), "parent_pid"


def _read_port_record_no_cleanup(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _request_once(data_dir: Path, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    record = _read_port_record(data_dir)
    if record is None:
        return None
    request = dict(payload)
    request["token"] = record.get("token")
    agent_pid, agent_pid_source = current_agent_owner_pid()
    request.setdefault("client", {
        "pid": os.getpid(),
        "parent_pid": os.getppid(),
        "agent_pid": agent_pid,
        "agent_pid_source": agent_pid_source,
        "host": os.environ.get("RIPPLE_MEMORY_HOST", ""),
        "window_id": os.environ.get("RIPPLE_MEMORY_WINDOW_ID", ""),
        "session_id": os.environ.get("RIPPLE_MEMORY_SESSION_ID", ""),
    })

    sock: Optional[socket.socket] = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT_SECONDS)
        try:
            sock.connect(("127.0.0.1", int(record["port"])))
        except (ConnectionRefusedError, socket.timeout, OSError):
            remove_port_record(data_dir, record)
            return None
        sock.settimeout(RECV_TIMEOUT_SECONDS)
        sock.sendall(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")

        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        if not data:
            return None
        response = json.loads(data.decode("utf-8").strip())
        if isinstance(response, dict):
            return response
        return {"ok": False, "error": "daemon_response_not_object"}
    except socket.timeout:
        return {
            "ok": False,
            "error": "agent_daemon_response_timeout",
            "data_dir": str(data_dir),
            "pid": int(record.get("pid") or 0),
            "port": int(record.get("port") or 0),
            "timeout_seconds": RECV_TIMEOUT_SECONDS,
        }
    except (OSError, json.JSONDecodeError):
        remove_port_record(data_dir, record)
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _try_acquire_launch_lock(data_dir: Path) -> Optional[int]:
    lock_path = _lock_file_path(data_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if lock_path.exists() and time.time() - lock_path.stat().st_mtime > LOCK_STALE_SECONDS:
            lock_path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None


def _release_launch_lock(data_dir: Path, fd: Optional[int]) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        _lock_file_path(data_dir).unlink(missing_ok=True)
    except OSError:
        pass


def _spawn_daemon(data_dir: Path) -> None:
    env = os.environ.copy()
    env["MEMORIA_MCP_DATA_DIR"] = str(data_dir)
    env.setdefault("RIPPLE_MEMORY_HOST", env.get("RIPPLE_MEMORY_HOST", "mcp"))
    env["RIPPLE_MEMORY_DAEMON_TOKEN_SEED"] = uuid.uuid4().hex
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [sys.executable, "-m", "memoria_mcp.agent_daemon"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        creationflags=creationflags,
        close_fds=True,
    )


def ensure_agent_daemon(data_dir: Path | None = None, *, timeout_seconds: float = STARTUP_TIMEOUT_SECONDS) -> Dict[str, Any]:
    data_dir = (data_dir or default_data_dir()).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    existing = _request_once(data_dir, {"op": "ping"})
    if existing and existing.get("ok"):
        return {"ok": True, "started": False, "daemon": existing.get("daemon", {})}

    lock_fd = _try_acquire_launch_lock(data_dir)
    if lock_fd is not None:
        try:
            # Double-check after acquiring lock — another caller may have
            # finished spawning while we waited for the lock.
            existing = _request_once(data_dir, {"op": "ping"})
            if existing and existing.get("ok"):
                return {"ok": True, "started": False, "daemon": existing.get("daemon", {})}
            _spawn_daemon(data_dir)

            # *** FIX: hold the lock until the daemon is fully ready ***
            # Previously the lock was released immediately after Popen(),
            # creating a race window where a second caller would see no
            # port.json and spawn a duplicate daemon.
            deadline = time.time() + max(0.1, timeout_seconds)
            while time.time() < deadline:
                response = _request_once(data_dir, {"op": "ping"})
                if response and response.get("ok"):
                    # Daemon is confirmed ready — safe to release lock.
                    return {"ok": True, "started": True, "daemon": response.get("daemon", {})}
                time.sleep(0.1)
            # Timed out — release lock and fall through to return error.
            return {"ok": False, "error": "agent_daemon_start_timeout", "data_dir": str(data_dir)}
        finally:
            _release_launch_lock(data_dir, lock_fd)

    # Another caller holds the lock — wait for them to finish.
    deadline = time.time() + max(0.1, timeout_seconds)
    while time.time() < deadline:
        response = _request_once(data_dir, {"op": "ping"})
        if response and response.get("ok"):
            return {"ok": True, "started": False, "daemon": response.get("daemon", {})}
        time.sleep(0.1)
    return {"ok": False, "error": "agent_daemon_start_timeout", "data_dir": str(data_dir)}


def request_daemon(data_dir: Path | None, payload: Dict[str, Any]) -> Dict[str, Any]:
    data_dir = (data_dir or default_data_dir()).expanduser()
    ready = ensure_agent_daemon(data_dir)
    if not ready.get("ok"):
        return ready
    response = _request_once(data_dir, payload)
    if response is None:
        ready = ensure_agent_daemon(data_dir, timeout_seconds=3.0)
        if not ready.get("ok"):
            return ready
        response = _request_once(data_dir, payload)
    if response is None:
        return {"ok": False, "error": "agent_daemon_request_failed", "data_dir": str(data_dir)}
    return response


def call_daemon_tool(data_dir: Path | None, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return request_daemon(data_dir, {"op": "call_tool", "tool": name, "arguments": arguments or {}})


def shutdown_daemon(data_dir: Path | None = None) -> Dict[str, Any]:
    data_dir = (data_dir or default_data_dir()).expanduser()
    response = _request_once(data_dir, {"op": "shutdown"})
    if response is None:
        return {"ok": True, "already_stopped": True}
    return response
