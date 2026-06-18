"""Agent-level Ripple Memory daemon.

This process owns the router, project caches, write queue, embedding model cache,
and the existing search rerank daemon for one host data root. Window-level MCP
processes should proxy to this daemon instead of carrying memory state.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
import asyncio
from pathlib import Path
from typing import Any, Dict, Optional

from .daemon_client import port_file_path
from .lifecycle import IdleLifecycleManager, ProcessRegistry, is_process_alive
from .mcp_sse_server import DaemonSSEServer
from .server import (
    DEFAULT_TOOL_STUCK_EXIT_SECONDS,
    EXPIRY_DAYS,
    PURGE_DAYS,
    MemoriaRouter,
    _json_text_for_mcp,
    _preload_embedding_model_if_requested,
    _read_bool_env,
    _read_float_env,
    _read_int_env,
    _runtime_change_exit_guard,
)


logger = logging.getLogger("RippleMemory.AgentDaemon")
MAX_REQUEST_BYTES = 4 * 1024 * 1024
DEFAULT_AGENT_EXIT_GRACE_SECONDS = 180.0
DAEMON_START_LOCK_NAME = "daemon.start.lock"
DAEMON_START_LOCK_STALE_SECONDS = 60.0
DAEMON_START_WAIT_SECONDS = 20.0
DAEMON_PING_TIMEOUT_SECONDS = 0.75


class AgentDaemonAlreadyRunning(Exception):
    def __init__(self, existing: Dict[str, Any]):
        super().__init__("agent daemon already running")
        self.existing = existing


def _write_port_file(path: Path, port: int, *, token: str, sse_port: int = 0) -> None:
    payload = {
        "schema": "ripple_memory_agent_daemon_v1",
        "host": "127.0.0.1",
        "port": int(port),
        "sse_port": int(sse_port),
        "pid": os.getpid(),
        "token": token,
        "started_at": time.time(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _remove_port_file(path: Path, *, token: str) -> None:
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if current.get("token") != token:
            return
    except (OSError, json.JSONDecodeError):
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _daemon_start_lock_path(base_dir: Path) -> Path:
    return port_file_path(base_dir).with_name(DAEMON_START_LOCK_NAME)


def _try_acquire_start_lock(base_dir: Path) -> Optional[int]:
    lock_path = _daemon_start_lock_path(base_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if lock_path.exists() and time.time() - lock_path.stat().st_mtime > DAEMON_START_LOCK_STALE_SECONDS:
            lock_path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii", errors="ignore"))
        return fd
    except FileExistsError:
        return None
    except OSError:
        return None


def _release_start_lock(base_dir: Path, fd: Optional[int]) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        _daemon_start_lock_path(base_dir).unlink(missing_ok=True)
    except OSError:
        pass


def _read_port_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.is_file():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        port = int(record.get("port") or 0)
        pid = int(record.get("pid") or 0)
        token = str(record.get("token") or "")
        if not (1 <= port <= 65535) or not token:
            return None
        if pid > 0 and not is_process_alive(pid):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        record["port"] = port
        record["pid"] = pid
        record["token"] = token
        return record
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _ping_port_record(record: Dict[str, Any], *, timeout: float = DAEMON_PING_TIMEOUT_SECONDS) -> Optional[Dict[str, Any]]:
    payload = {"token": record.get("token"), "op": "ping"}
    sock: Optional[socket.socket] = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(("127.0.0.1", int(record["port"])))
        sock.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
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
        return response if isinstance(response, dict) and response.get("ok") else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _find_existing_live_daemon(base_dir: Path, *, own_token: str = "") -> Optional[Dict[str, Any]]:
    record = _read_port_file(port_file_path(base_dir))
    if record is None:
        return None
    if int(record.get("pid") or 0) == os.getpid() or (own_token and record.get("token") == own_token):
        return None
    response = _ping_port_record(record)
    if not response:
        return None
    return {"record": record, "response": response}


def _wait_for_existing_live_daemon(base_dir: Path, *, own_token: str, timeout: float = DAEMON_START_WAIT_SECONDS) -> Optional[Dict[str, Any]]:
    deadline = time.time() + max(0.1, timeout)
    while time.time() < deadline:
        existing = _find_existing_live_daemon(base_dir, own_token=own_token)
        if existing is not None:
            return existing
        time.sleep(0.1)
    return None


class AgentDaemon:
    def __init__(self, base_dir: str):
        self.base_dir = str(Path(base_dir).expanduser())
        self.expiry_days = _read_int_env("MEMORIA_MCP_EXPIRY_DAYS", EXPIRY_DAYS)
        self.purge_days = _read_int_env("MEMORIA_MCP_PURGE_DAYS", PURGE_DAYS)
        self.token = f"{os.getpid()}-{uuid.uuid4().hex}"
        self.router: Optional[MemoriaRouter] = None
        self.lifecycle: Optional[IdleLifecycleManager] = None
        self.registry: Optional[ProcessRegistry] = None
        self.search_daemon: Any = None
        self.sse_server: Optional[DaemonSSEServer] = None
        self.server_socket: Optional[socket.socket] = None
        self.running = False
        self.tool_lock = threading.RLock()
        self.client_lock = threading.RLock()
        self.clients: Dict[int, Dict[str, Any]] = {}
        self.agent_owners: Dict[int, Dict[str, Any]] = {}
        self.agent_exit_since: Optional[float] = None
        self.owner_monitor_thread: Optional[threading.Thread] = None
        self.agent_exit_grace_seconds = _read_float_env(
            "MEMORIA_MCP_DAEMON_AGENT_EXIT_GRACE_SECONDS",
            DEFAULT_AGENT_EXIT_GRACE_SECONDS,
        )
        self.runtime_source = __file__
        self.runtime_source_mtime = os.path.getmtime(__file__) if os.path.exists(__file__) else 0.0

    def start(self) -> None:
        base_path = Path(self.base_dir)
        os.makedirs(self.base_dir, exist_ok=True)
        start_lock_fd = _try_acquire_start_lock(base_path)
        if start_lock_fd is None:
            existing = _wait_for_existing_live_daemon(base_path, own_token=self.token)
            if existing is not None:
                raise AgentDaemonAlreadyRunning(existing)
            start_lock_fd = _try_acquire_start_lock(base_path)
            if start_lock_fd is None:
                existing = _wait_for_existing_live_daemon(base_path, own_token=self.token, timeout=2.0)
                if existing is not None:
                    raise AgentDaemonAlreadyRunning(existing)
                raise RuntimeError("agent daemon start lock is held and no live daemon became available")
        try:
            existing = _find_existing_live_daemon(base_path, own_token=self.token)
            if existing is not None:
                raise AgentDaemonAlreadyRunning(existing)
            _preload_embedding_model_if_requested()
            self._start_locked()
        finally:
            _release_start_lock(base_path, start_lock_fd)

    def _start_locked(self) -> None:
        self.registry = ProcessRegistry(
            self.base_dir,
            host=os.environ.get("RIPPLE_MEMORY_HOST", "mcp"),
            window_id="agent-daemon",
            session_id=os.environ.get("RIPPLE_MEMORY_SESSION_ID", ""),
        )
        self.router = MemoriaRouter(self.base_dir, expiry_days=self.expiry_days, purge_days=self.purge_days)
        self.lifecycle = IdleLifecycleManager(
            close_cached_state=self.router.sleep_cached_state,
            registry=self.registry,
            exit_if=_runtime_change_exit_guard(self.runtime_source, self.runtime_source_mtime),
            exit_seconds=0,
            exit_on_parent_death=False,
        )
        self.router.set_lifecycle_manager(self.lifecycle)
        self.lifecycle.start()
        self.registry.heartbeat(
            status="active",
            role="agent_daemon",
            runtime_source=self.runtime_source,
            runtime_source_mtime=self.runtime_source_mtime,
            exit_on_parent_death=False,
            exit_on_runtime_change=_read_bool_env("MEMORIA_MCP_EXIT_ON_RUNTIME_CHANGE", False),
            tool_stuck_exit_seconds=_read_float_env(
                "MEMORIA_MCP_TOOL_STUCK_EXIT_SECONDS",
                DEFAULT_TOOL_STUCK_EXIT_SECONDS,
            ),
        )
        self._start_search_daemon()
        self._start_socket()
        self._start_sse_server()
        self._start_owner_monitor()
        logger.info("Ripple agent daemon listening on %s", port_file_path(Path(self.base_dir)))

    def _start_search_daemon(self) -> None:
        try:
            from .search_daemon import SearchDaemon
            self.search_daemon = SearchDaemon(self.router, self.base_dir)
            self.search_daemon.start()
        except Exception as exc:  # noqa: BLE001 - rerank daemon must fail open.
            self.search_daemon = None
            logger.warning("Search daemon not started inside agent daemon: %s", exc)

    def _start_socket(self) -> None:
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(("127.0.0.1", 0))
        self.server_socket.listen(32)
        self.server_socket.settimeout(1.0)
        self._socket_port = int(self.server_socket.getsockname()[1])
        self.running = True

    def _start_sse_server(self) -> None:
        """Start MCP-over-SSE HTTP server if MEMORIA_MCP_SSE_PORT is set.

        Set MEMORIA_MCP_SSE_PORT=0 for auto-assign, or a fixed port number.
        When disabled (default), only stdio proxy and custom TCP are available.
        """
        sse_env = os.environ.get("MEMORIA_MCP_SSE_PORT", "").strip()
        if not sse_env:
            # SSE not requested — write port file with socket port only
            _write_port_file(
                port_file_path(Path(self.base_dir)),
                self._socket_port,
                token=self.token,
            )
            return
        try:
            sse_port = int(sse_env)
        except ValueError:
            sse_port = 0

        expose_project = _read_bool_env("MEMORIA_MCP_EXPOSE_PROJECT_TOOLS", False)

        def _tool_handler(name: str, arguments: Dict[str, Any]) -> Any:
            with self.tool_lock:
                if self.lifecycle:
                    self.lifecycle.mark_activity(label=f"sse:{name}")
                if self.router is None:
                    return {"error": "daemon_not_ready"}
                loop = asyncio.new_event_loop()
                try:
                    return loop.run_until_complete(
                        self.router._dispatch_tool_for_mcp_async(name, arguments)
                    )
                finally:
                    loop.close()

        self.sse_server = DaemonSSEServer(
            _tool_handler,
            port=sse_port,
            expose_project_tools=expose_project,
        )
        try:
            self.sse_server.start()
            actual_sse_port = self.sse_server.port or sse_port
            logger.info("MCP SSE endpoint on 127.0.0.1:%s", actual_sse_port)
        except Exception as exc:
            logger.warning("MCP SSE server not started: %s", exc)
            self.sse_server = None
            actual_sse_port = 0

        # Write port file with both ports
        _write_port_file(
            port_file_path(Path(self.base_dir)),
            self._socket_port,
            token=self.token,
            sse_port=actual_sse_port,
        )

    def _start_owner_monitor(self) -> None:
        if self.owner_monitor_thread and self.owner_monitor_thread.is_alive():
            return
        self.owner_monitor_thread = threading.Thread(
            target=self._owner_monitor_loop,
            daemon=True,
            name="ripple-agent-daemon-owner-monitor",
        )
        self.owner_monitor_thread.start()

    def _owner_monitor_loop(self) -> None:
        """Exit only after the owning agent process disappears.

        A proxy can be idle for hours while the agent is still working or the
        user leaves it unattended. The daemon therefore monitors the registered
        agent owner PID, not whether a recent proxy/client process is active.
        """
        interval = max(1.0, min(15.0, self.agent_exit_grace_seconds / 6 if self.agent_exit_grace_seconds > 0 else 15.0))
        while self.running:
            time.sleep(interval)
            if not self.running or self.agent_exit_grace_seconds <= 0:
                continue
            if self._is_superseded_by_live_daemon():
                if self.registry is not None:
                    self.registry.heartbeat(status="superseded_daemon", role="agent_daemon")
                self.running = False
                if self.server_socket is not None:
                    try:
                        self.server_socket.close()
                    except OSError:
                        pass
                return
            live_owner_count = self._live_agent_owner_count()
            now = time.time()
            if live_owner_count > 0:
                self.agent_exit_since = None
                continue
            if self.agent_exit_since is None:
                self.agent_exit_since = now
            owner_missing_seconds = now - self.agent_exit_since
            if owner_missing_seconds >= self.agent_exit_grace_seconds:
                if self.registry is not None:
                    self.registry.heartbeat(
                        status="agent_exit",
                        role="agent_daemon",
                        owner_missing_seconds=round(owner_missing_seconds, 3),
                    )
                self.running = False
                if self.server_socket is not None:
                    try:
                        self.server_socket.close()
                    except OSError:
                        pass
                return

    def _record_client(self, raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        try:
            pid = int(raw.get("pid") or 0)
        except (TypeError, ValueError):
            return
        if pid <= 0 or pid == os.getpid():
            return
        try:
            parent_pid = int(raw.get("parent_pid") or 0)
        except (TypeError, ValueError):
            parent_pid = 0
        try:
            agent_pid = int(raw.get("agent_pid") or parent_pid or 0)
        except (TypeError, ValueError):
            agent_pid = parent_pid
        now = time.time()
        with self.client_lock:
            self.clients[pid] = {
                "pid": pid,
                "parent_pid": parent_pid,
                "agent_pid": agent_pid,
                "host": str(raw.get("host") or ""),
                "window_id": str(raw.get("window_id") or ""),
                "session_id": str(raw.get("session_id") or ""),
                "last_seen_at": now,
            }
            if agent_pid > 0 and agent_pid != os.getpid():
                self.agent_owners[agent_pid] = {
                    "pid": agent_pid,
                    "last_seen_at": now,
                    "host": str(raw.get("host") or ""),
                    "source": str(raw.get("agent_pid_source") or ""),
                }
                self.agent_exit_since = None

    def _live_client_count(self) -> int:
        live: Dict[int, Dict[str, Any]] = {}
        with self.client_lock:
            for pid, record in self.clients.items():
                parent_pid = int(record.get("parent_pid") or 0)
                if not is_process_alive(pid):
                    continue
                if parent_pid > 0 and not is_process_alive(parent_pid):
                    continue
                live[pid] = record
            self.clients = live
            return len(live)

    def _is_superseded_by_live_daemon(self) -> bool:
        return _find_existing_live_daemon(Path(self.base_dir), own_token=self.token) is not None

    def _live_agent_owner_count(self) -> int:
        live: Dict[int, Dict[str, Any]] = {}
        with self.client_lock:
            for pid, record in self.agent_owners.items():
                if is_process_alive(pid):
                    live[pid] = record
            self.agent_owners = live
            return len(live)

    def serve_forever(self) -> None:
        if self.server_socket is None:
            raise RuntimeError("daemon socket was not started")
        while self.running:
            try:
                conn, _addr = self.server_socket.accept()
                conn.settimeout(10.0)
                threading.Thread(target=self._handle_connection, args=(conn,), daemon=True).start()
            except socket.timeout:
                continue
            except OSError:
                if self.running:
                    time.sleep(0.1)

    def stop(self, *, status: str = "exited") -> None:
        self.running = False
        _remove_port_file(port_file_path(Path(self.base_dir)), token=self.token)
        if self.sse_server is not None:
            try:
                self.sse_server.stop()
            except Exception:  # noqa: BLE001
                pass
            self.sse_server = None
        if self.server_socket is not None:
            try:
                self.server_socket.close()
            except OSError:
                pass
            self.server_socket = None
        if self.search_daemon is not None:
            self.search_daemon.stop()
            self.search_daemon = None
        if self.lifecycle is not None:
            self.lifecycle.stop()
            self.lifecycle = None
        if self.registry is not None and status != "exited":
            self.registry.unregister(status=status)
        if self.router is not None:
            self.router.close()
            self.router = None

    def _read_request(self, conn: socket.socket) -> Optional[Dict[str, Any]]:
        data = b""
        while len(data) <= MAX_REQUEST_BYTES:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
        if not data:
            return None
        return json.loads(data.decode("utf-8").strip())

    def _write_response(self, conn: socket.socket, payload: Dict[str, Any]) -> None:
        conn.sendall(_json_text_for_mcp(payload).encode("utf-8") + b"\n")

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            request = self._read_request(conn)
            if not request:
                return
            if request.get("token") != self.token:
                self._write_response(conn, {"ok": False, "error": "invalid_daemon_token"})
                return
            self._record_client(request.get("client"))
            response = self._handle_request(request)
            self._write_response(conn, response)
        except Exception as exc:  # noqa: BLE001 - IPC must return structured errors.
            try:
                self._write_response(conn, {"ok": False, "error": str(exc), "error_type": exc.__class__.__name__})
            except OSError:
                pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        if self.router is None or self.lifecycle is None:
            return {"ok": False, "error": "daemon_not_ready"}
        op = str(request.get("op") or "").strip()
        if op == "ping":
            self.lifecycle.mark_activity(label="daemon:ping")
            return {
                "ok": True,
                "daemon": {
                    "pid": os.getpid(),
                    "base_dir": self.base_dir,
                    "role": "agent_daemon",
                    "search_daemon": bool(self.search_daemon),
                    "live_clients": self._live_client_count(),
                    "live_agent_owners": self._live_agent_owner_count(),
                    "agent_exit_grace_seconds": self.agent_exit_grace_seconds,
                },
            }
        if op == "shutdown":
            self.running = False
            return {"ok": True, "shutdown": True, "pid": os.getpid()}
        if op == "call_tool":
            tool = str(request.get("tool") or "")
            arguments = dict(request.get("arguments") or {})
            with self.tool_lock:
                self.lifecycle.mark_activity(label=f"daemon:{tool}")
                result = asyncio.run(self.router._dispatch_tool_for_mcp_async(tool, arguments))
            return {"ok": True, "result": result}
        return {"ok": False, "error": f"unknown_daemon_op:{op}"}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    base_dir = (
        os.environ.get("MEMORIA_MCP_DATA_DIR")
        or os.environ.get("RIPPLE_MEMORY_DATA_DIR")
        or os.path.expanduser("~/.ripple-memory")
    )
    daemon = AgentDaemon(base_dir)
    status = "exited"
    try:
        daemon.start()
        daemon.serve_forever()
        status = "daemon_shutdown"
        return 0
    except AgentDaemonAlreadyRunning as exc:
        logger.info("Ripple agent daemon already running: %s", exc.existing.get("record", {}))
        status = "already_running"
        return 0
    finally:
        daemon.stop(status=status)


def run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    run()
