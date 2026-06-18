"""Process and window lifecycle helpers for Ripple Memory hosts.

This module stays host-neutral. Host adapters translate native lifecycle
events; the MCP daemon/proxy layer uses the process registry and idle manager
directly.
"""
from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


RUNTIME_DIR_NAME = "_runtime"
PROCESS_DIR_NAME = "processes"
PROCESS_COMMAND_DIR_NAME = "process_commands"
WINDOW_EVENTS_DIR_NAME = "window_events"
LEGACY_WORKSPACE_WINDOW_ARCHIVE_DIR_NAME = "archived-windows"
LEGACY_WORKSPACE_WINDOW_STATE_DIR_NAME = "windows"
WINDOW_STATE_STORE_DIR_NAME = "_window_state"
WINDOW_ARCHIVE_STORE_DIR_NAME = "_window_archives"

DEFAULT_IDLE_SLEEP_SECONDS = 60 * 60.0
DEFAULT_IDLE_EXIT_SECONDS = 10 * 60 * 60.0
DEFAULT_HEARTBEAT_SECONDS = 30.0
DEFAULT_WINDOW_ARCHIVE_RETENTION_DAYS = 30.0
DEFAULT_TOOL_STUCK_EXIT_SECONDS = 60.0


def _now() -> float:
    return time.time()


def _iso(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts or _now()))


def _read_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _safe_name(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in str(value or ""))
    return text.strip("._-") or "default"


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def is_process_alive(pid: int) -> bool:
    """Best-effort cross-platform process liveness check."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                exit_code = wintypes.DWORD()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                return bool(ok) and exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _path_text(value: Any) -> str:
    try:
        return str(Path(str(value)).expanduser().resolve()).lower()
    except Exception:
        return str(value or "").lower()


def _same_path(left: Any, right: Any) -> bool:
    return _path_text(left) == _path_text(right)


def _process_command_line(pid: int) -> str:
    """Best-effort command-line lookup used before killing orphan candidates."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    if pid <= 0:
        return ""
    if pid == os.getpid():
        return " ".join(str(part) for part in sys.argv)
    if os.name == "nt":
        command = (
            "$p = Get-CimInstance Win32_Process -Filter \"ProcessId = "
            f"{pid}\"; if ($p) {{ $p.CommandLine }}"
        )
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", command],
                text=True,
                capture_output=True,
                timeout=5,
            )
            return (proc.stdout or "").strip()
        except Exception:
            return ""
    proc_cmdline = Path("/proc") / str(pid) / "cmdline"
    try:
        raw = proc_cmdline.read_bytes()
        return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def _looks_like_ripple_server_text(text: str) -> bool:
    normalized = str(text or "").replace("\\", "/").lower()
    return any(
        token in normalized
        for token in (
            "memoria_mcp.server",
            "memoria_mcp/server.py",
            "memoria_mcp.agent_daemon",
            "memoria_mcp/agent_daemon.py",
            "memoria_mcp.mcp_stdio_proxy",
            "memoria_mcp/mcp_stdio_proxy.py",
            "ripple-memory",
        )
    )


def _record_looks_like_ripple_server(record: Dict[str, Any]) -> bool:
    argv = record.get("argv") or []
    if isinstance(argv, list):
        argv_text = " ".join(str(part) for part in argv)
    else:
        argv_text = str(argv)
    return _looks_like_ripple_server_text(f"{record.get('executable') or ''} {argv_text}")


def terminate_process(pid: int, *, timeout_seconds: float = 2.0) -> bool:
    """Terminate a process and wait briefly for it to disappear."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid():
        return False
    if not is_process_alive(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        if not is_process_alive(pid):
            return True
        return False
    deadline = _now() + max(timeout_seconds, 0.0)
    while _now() < deadline:
        if not is_process_alive(pid):
            return True
        time.sleep(0.05)
    if os.name == "nt" and is_process_alive(pid):
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(1.0, timeout_seconds),
                check=False,
            )
        except Exception:
            pass
    return not is_process_alive(pid)


class ProcessRegistry:
    """Small file-based registry for MCP server processes."""

    def __init__(
        self,
        base_data_dir: str | Path,
        *,
        host: str = "",
        window_id: str = "",
        session_id: str = "",
    ):
        self.base_data_dir = Path(base_data_dir).expanduser()
        self.runtime_dir = self.base_data_dir / RUNTIME_DIR_NAME
        self.process_dir = self.runtime_dir / PROCESS_DIR_NAME
        self.command_dir = self.runtime_dir / PROCESS_COMMAND_DIR_NAME
        self.window_events_dir = self.runtime_dir / WINDOW_EVENTS_DIR_NAME
        self.host = host or os.environ.get("RIPPLE_MEMORY_HOST") or os.environ.get("CODEX_HOST") or ""
        self.window_id = window_id or os.environ.get("RIPPLE_MEMORY_WINDOW_ID") or os.environ.get("MEMORIA_WINDOW_ID") or ""
        self.session_id = session_id or os.environ.get("RIPPLE_MEMORY_SESSION_ID") or os.environ.get("CODEX_SESSION_ID") or ""
        self.pid = os.getpid()
        self.parent_pid = os.getppid()
        self.started_at = _now()
        self.record_path = self.process_dir / f"{self.pid}.json"
        self._lock = threading.Lock()

    def _base_payload(self, status: str) -> Dict[str, Any]:
        return {
            "schema": "ripple_memory_process_v1",
            "pid": self.pid,
            "parent_pid": self.parent_pid,
            "status": status,
            "host": self.host,
            "window_id": self.window_id,
            "session_id": self.session_id,
            "base_data_dir": str(self.base_data_dir),
            "cwd": os.getcwd(),
            "argv": list(sys.argv),
            "executable": sys.executable,
            "started_at": self.started_at,
            "started_at_label": _iso(self.started_at),
            "last_seen_at": _now(),
            "last_seen_label": _iso(),
        }

    def register(self, *, status: str = "active") -> Dict[str, Any]:
        with self._lock:
            payload = self._base_payload(status)
            _atomic_write_json(self.record_path, payload)
            return payload

    def heartbeat(self, *, status: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
        with self._lock:
            if self.record_path.exists():
                try:
                    payload = json.loads(self.record_path.read_text(encoding="utf-8"))
                except Exception:
                    payload = self._base_payload(status or "active")
            else:
                payload = self._base_payload(status or "active")
            if status:
                payload["status"] = status
            payload["last_seen_at"] = _now()
            payload["last_seen_label"] = _iso()
            payload.update({k: v for k, v in extra.items() if v is not None})
            _atomic_write_json(self.record_path, payload)
            return payload

    def unregister(self, *, status: str = "exited") -> None:
        with self._lock:
            try:
                if self.record_path.exists():
                    payload = json.loads(self.record_path.read_text(encoding="utf-8"))
                    payload["status"] = status
                    payload["ended_at"] = _now()
                    payload["ended_at_label"] = _iso()
                    _atomic_write_json(self.record_path.with_suffix(".final.json"), payload)
                self.record_path.unlink(missing_ok=True)
            except OSError:
                pass

    def record_window_event(
        self,
        *,
        agent: str,
        project: str,
        window_id: str,
        action: str,
        cwd: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> Path:
        event_id = f"{int(_now() * 1000)}-{_safe_name(agent)}-{_safe_name(window_id)}-{_safe_name(action)}.json"
        path = self.window_events_dir / event_id
        _atomic_write_json(path, {
            "schema": "ripple_memory_window_event_v1",
            "created_at": _now(),
            "created_at_label": _iso(),
            "agent": agent,
            "project": project,
            "window_id": window_id,
            "action": action,
            "cwd": cwd,
            "details": details or {},
        })
        return path

    def list_processes(self) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        if not self.process_dir.is_dir():
            return records
        for path in sorted(self.process_dir.glob("*.json")):
            if path.name.endswith(".final.json"):
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            payload["_path"] = str(path)
            payload["alive"] = is_process_alive(int(payload.get("pid") or 0))
            records.append(payload)
        return records

    def request_exit_for_window(
        self,
        *,
        window_id: str = "",
        session_id: str = "",
        reason: str = "",
        include_current: bool = False,
    ) -> Dict[str, Any]:
        """Ask registered MCP processes for a window/session to exit gracefully."""
        clean_window = str(window_id or "").strip()
        clean_session = str(session_id or "").strip()
        if not clean_window and not clean_session:
            return {"requested_count": 0, "requested_pids": [], "reason": "missing_window_or_session"}

        requested: List[int] = []
        for record in self.list_processes():
            pid = int(record.get("pid") or 0)
            if pid <= 0 or not record.get("alive"):
                continue
            if not include_current and pid == os.getpid():
                continue
            record_window = str(record.get("window_id") or "")
            record_session = str(record.get("session_id") or "")
            if clean_window and record_window == clean_window:
                matched = True
            elif clean_session and record_session == clean_session:
                matched = True
            else:
                matched = False
            if not matched:
                continue
            command_path = self.command_dir / f"{pid}.exit.json"
            _atomic_write_json(command_path, {
                "schema": "ripple_memory_process_command_v1",
                "action": "exit",
                "reason": reason or "window_lifecycle",
                "window_id": clean_window,
                "session_id": clean_session,
                "created_at": _now(),
                "created_at_label": _iso(),
            })
            requested.append(pid)
        return {
            "requested_count": len(requested),
            "requested_pids": requested,
            "reason": reason or "window_lifecycle",
        }

    def pop_exit_request(self) -> Optional[Dict[str, Any]]:
        command_path = self.command_dir / f"{self.pid}.exit.json"
        if not command_path.exists():
            return None
        try:
            payload = json.loads(command_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {"action": "exit", "reason": "unreadable_command"}
        try:
            command_path.unlink(missing_ok=True)
        except OSError:
            pass
        if payload.get("action") == "exit":
            return payload
        return None

    def cleanup_stale_records(self, *, stale_after_seconds: float = 24 * 60 * 60.0) -> Dict[str, Any]:
        now = _now()
        removed: List[str] = []
        kept = 0
        for record in self.list_processes():
            path = Path(str(record.get("_path") or ""))
            last_seen = float(record.get("last_seen_at") or 0.0)
            alive = bool(record.get("alive"))
            if alive:
                kept += 1
                continue
            if (now - last_seen) <= stale_after_seconds:
                kept += 1
                continue
            try:
                path.unlink(missing_ok=True)
                removed.append(str(path))
            except OSError:
                kept += 1
        return {"removed": removed, "removed_count": len(removed), "kept_count": kept}

    def cleanup_orphaned_processes(self) -> Dict[str, Any]:
        """Kill safe same-data-dir orphan MCP processes and remove dead active records."""
        killed: List[int] = []
        removed: List[str] = []
        skipped: List[Dict[str, Any]] = []
        for record in self.list_processes():
            path = Path(str(record.get("_path") or ""))
            pid = int(record.get("pid") or 0)
            parent_pid = int(record.get("parent_pid") or 0)
            if pid <= 0:
                skipped.append({"pid": pid, "reason": "missing_pid"})
                continue
            if pid == self.pid:
                skipped.append({"pid": pid, "reason": "current_process"})
                continue
            if not _same_path(record.get("base_data_dir") or "", self.base_data_dir):
                skipped.append({"pid": pid, "reason": "different_data_dir"})
                continue
            if not _record_looks_like_ripple_server(record):
                skipped.append({"pid": pid, "reason": "record_not_ripple_server"})
                continue
            if not bool(record.get("alive")):
                try:
                    path.unlink(missing_ok=True)
                    removed.append(str(path))
                except OSError:
                    skipped.append({"pid": pid, "reason": "dead_record_remove_failed"})
                continue
            if parent_pid > 0 and is_process_alive(parent_pid):
                skipped.append({"pid": pid, "reason": "parent_alive"})
                continue
            command_line = _process_command_line(pid)
            if not _looks_like_ripple_server_text(command_line):
                skipped.append({"pid": pid, "reason": "live_process_not_confirmed_as_ripple_server"})
                continue
            if terminate_process(pid):
                killed.append(pid)
                try:
                    payload = dict(record)
                    payload.pop("_path", None)
                    payload.pop("alive", None)
                    payload["status"] = "orphan_killed"
                    payload["ended_at"] = _now()
                    payload["ended_at_label"] = _iso()
                    _atomic_write_json(path.with_suffix(".final.json"), payload)
                    path.unlink(missing_ok=True)
                    removed.append(str(path))
                except OSError:
                    skipped.append({"pid": pid, "reason": "orphan_record_finalize_failed"})
            else:
                skipped.append({"pid": pid, "reason": "terminate_failed"})
        return {
            "killed_pids": killed,
            "killed_count": len(killed),
            "removed": removed,
            "removed_count": len(removed),
            "skipped": skipped,
        }


class IdleLifecycleManager:
    """Close cached project state on idle, then exit old Ripple processes."""

    def __init__(
        self,
        *,
        close_cached_state: Callable[[], None],
        registry: ProcessRegistry,
        sleep_seconds: Optional[float] = None,
        exit_seconds: Optional[float] = None,
        heartbeat_seconds: Optional[float] = None,
        exit_process: bool = True,
        exit_on_parent_death: bool = True,
        exit_if: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
    ):
        self.close_cached_state = close_cached_state
        self.registry = registry
        self.exit_if = exit_if
        self.sleep_seconds = (
            _read_float_env("MEMORIA_MCP_IDLE_SLEEP_SECONDS", DEFAULT_IDLE_SLEEP_SECONDS)
            if sleep_seconds is None else sleep_seconds
        )
        self.exit_seconds = (
            _read_float_env("MEMORIA_MCP_IDLE_EXIT_SECONDS", DEFAULT_IDLE_EXIT_SECONDS)
            if exit_seconds is None else exit_seconds
        )
        self.heartbeat_seconds = max(
            0.2,
            _read_float_env("MEMORIA_MCP_HEARTBEAT_SECONDS", DEFAULT_HEARTBEAT_SECONDS)
            if heartbeat_seconds is None else heartbeat_seconds,
        )
        self.exit_process = exit_process
        self.exit_on_parent_death = exit_on_parent_death
        self._last_activity = _now()
        self._sleeping = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.registry.register(status="active")
        self.registry.cleanup_stale_records()
        self.registry.cleanup_orphaned_processes()
        self._thread = threading.Thread(target=self._run, name="ripple-memory-lifecycle", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.registry.unregister(status="exited")

    def mark_activity(self, *, label: str = "tool_call") -> None:
        with self._lock:
            self._last_activity = _now()
            was_sleeping = self._sleeping
            self._sleeping = False
        self.registry.heartbeat(status="active", last_activity_label=label, woke_from_sleep=was_sleeping)

    def sleep_now(self, *, reason: str = "manual") -> Dict[str, Any]:
        with self._lock:
            if self._sleeping:
                self.registry.heartbeat(status="sleeping", sleep_reason=reason)
                return {"slept": False, "already_sleeping": True}
            self._sleeping = True
        self.close_cached_state()
        self.registry.heartbeat(status="sleeping", sleep_reason=reason)
        return {"slept": True, "already_sleeping": False}

    def _idle_for(self) -> float:
        with self._lock:
            return _now() - self._last_activity

    def _run(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                if self._run_once():
                    return
            except Exception as exc:  # noqa: BLE001 - lifecycle must not silently die.
                try:
                    self.registry.heartbeat(
                        status="active",
                        lifecycle_loop_error=f"{exc.__class__.__name__}: {exc}",
                    )
                except Exception:
                    pass

    def _run_once(self) -> bool:
        parent_pid = int(getattr(self.registry, "parent_pid", 0) or 0)
        if self.exit_on_parent_death and parent_pid > 0 and not is_process_alive(parent_pid):
            self._exit_after_cleanup(status="parent_exit", extra={"parent_pid": parent_pid})
            return True
        exit_request = self.registry.pop_exit_request()
        if exit_request is not None:
            self._exit_after_cleanup(status="window_exit", extra=exit_request)
            return True
        stuck_tool = self._stuck_tool_payload()
        if stuck_tool is not None:
            self._exit_after_cleanup(status="tool_stuck_exit", extra=stuck_tool)
            return True
        if self.exit_if is not None:
            try:
                exit_reason = self.exit_if()
            except Exception as exc:  # noqa: BLE001 - lifecycle guard must stay fail-open.
                exit_reason = None
                self.registry.heartbeat(status="active", lifecycle_exit_check_error=str(exc))
            if exit_reason:
                status = str(exit_reason.pop("status", "runtime_changed") or "runtime_changed")
                self._exit_after_cleanup(status=status, extra=exit_reason)
                return True
        idle_for = self._idle_for()
        if self.exit_seconds > 0 and idle_for >= self.exit_seconds:
            self._exit_after_cleanup(status="idle_exit", extra={"idle_for_seconds": round(idle_for, 3)})
            return True
        if self.sleep_seconds > 0 and idle_for >= self.sleep_seconds:
            self.sleep_now(reason="idle")
        else:
            status = "sleeping" if self._sleeping else "active"
            self.registry.heartbeat(status=status, idle_for_seconds=round(idle_for, 3))
        return False

    def _stuck_tool_payload(self) -> Optional[Dict[str, Any]]:
        """Detect an MCP tool call that outlived the normal async timeout layer."""
        try:
            if not self.registry.record_path.exists():
                return None
            payload = json.loads(self.registry.record_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        max_seconds = _read_float_env(
            "MEMORIA_MCP_TOOL_STUCK_EXIT_SECONDS",
            DEFAULT_TOOL_STUCK_EXIT_SECONDS,
        )
        if max_seconds <= 0:
            return None
        now = _now()

        def as_float(value: Any) -> float:
            try:
                return float(value or 0.0)
            except (TypeError, ValueError):
                return 0.0

        def candidate_from_item(call_id: str, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            tool = str(item.get("tool") or item.get("active_tool") or "").strip()
            if not tool:
                return None
            started_at = as_float(item.get("started_at", item.get("active_tool_started_at")))
            deadline_at = as_float(item.get("deadline_at", item.get("active_tool_deadline_at")))
            stuck_seconds = as_float(item.get("stuck_exit_seconds", item.get("active_tool_stuck_exit_seconds")))
            if stuck_seconds <= 0:
                stuck_seconds = max_seconds
            if deadline_at <= 0 and started_at > 0:
                deadline_at = started_at + stuck_seconds
            if deadline_at <= 0 or now < deadline_at:
                return None
            return {
                "active_tool": tool,
                "active_tool_call_id": str(item.get("call_id") or call_id or ""),
                "active_tool_project": item.get("project", item.get("active_tool_project") or ""),
                "active_tool_started_at": started_at,
                "active_tool_elapsed_seconds": round(max(0.0, now - started_at), 3) if started_at > 0 else None,
                "active_tool_deadline_at": deadline_at,
                "active_tool_timeout_seconds": item.get("timeout_seconds", item.get("active_tool_timeout_seconds") or 0),
                "active_tool_stuck_exit_seconds": stuck_seconds,
            }

        active_tools = payload.get("active_tools")
        if isinstance(active_tools, dict):
            expired = [
                candidate
                for call_id, item in active_tools.items()
                if isinstance(item, dict)
                for candidate in [candidate_from_item(str(call_id), item)]
                if candidate is not None
            ]
            if expired:
                expired.sort(key=lambda item: (float(item.get("active_tool_deadline_at") or 0.0), item.get("active_tool_call_id") or ""))
                selected = expired[0]
                selected["active_tools_count"] = len(active_tools)
                return selected

        legacy = candidate_from_item(
            "",
            {
                "active_tool": payload.get("active_tool"),
                "active_tool_project": payload.get("active_tool_project"),
                "active_tool_started_at": payload.get("active_tool_started_at"),
                "active_tool_deadline_at": payload.get("active_tool_deadline_at"),
                "active_tool_timeout_seconds": payload.get("active_tool_timeout_seconds"),
                "active_tool_stuck_exit_seconds": payload.get("active_tool_stuck_exit_seconds"),
            },
        )
        return legacy

    def _exit_after_cleanup(self, *, status: str, extra: Optional[Dict[str, Any]] = None) -> None:
        try:
            self.close_cached_state()
            heartbeat_extra = extra or {}
            if heartbeat_extra:
                self.registry.heartbeat(status=status, **heartbeat_extra)
            self.registry.unregister(status=status)
        except Exception as exc:  # noqa: BLE001 - lifecycle guard must not hang shutdown
            try:
                self.registry.heartbeat(status=f"{status}_error", lifecycle_error=str(exc))
            except Exception:
                pass
        if self.exit_process:
            os._exit(0)


def _workspace_ripple_root(cwd: str | Path) -> Path:
    return Path(cwd or os.getcwd()).expanduser().resolve() / ".ripple-memory"


def _window_active_dir(
    cwd: str | Path,
    window_id: str,
    *,
    project: str = "",
    data_dir: str | Path | None = None,
) -> Path:
    if data_dir:
        return (
            Path(data_dir).expanduser().resolve()
            / WINDOW_STATE_STORE_DIR_NAME
            / _safe_name(project or "default")
            / _safe_name(window_id)
        )
    return _workspace_ripple_root(cwd) / LEGACY_WORKSPACE_WINDOW_STATE_DIR_NAME / _safe_name(window_id)


def window_latch_file(
    *,
    cwd: str | Path,
    project: str,
    window_id: str,
    data_dir: str | Path | None = None,
) -> Path:
    return _window_active_dir(cwd, window_id, project=project, data_dir=data_dir) / "original-word-latch.md"


def _window_archive_root(
    cwd: str | Path,
    window_id: str,
    *,
    project: str = "",
    data_dir: str | Path | None = None,
) -> Path:
    if data_dir:
        return (
            Path(data_dir).expanduser().resolve()
            / WINDOW_ARCHIVE_STORE_DIR_NAME
            / _safe_name(project or "default")
            / _safe_name(window_id)
        )
    return _workspace_ripple_root(cwd) / LEGACY_WORKSPACE_WINDOW_ARCHIVE_DIR_NAME / _safe_name(window_id)


def prune_window_archives(
    cwd: str | Path,
    *,
    data_dir: str | Path | None = None,
    retention_days: Optional[float] = None,
) -> Dict[str, Any]:
    retention = (
        _read_float_env("RIPPLE_MEMORY_WINDOW_ARCHIVE_RETENTION_DAYS", DEFAULT_WINDOW_ARCHIVE_RETENTION_DAYS)
        if retention_days is None else retention_days
    )
    if retention <= 0:
        return {"removed_count": 0, "removed": []}
    root = (
        Path(data_dir).expanduser().resolve() / WINDOW_ARCHIVE_STORE_DIR_NAME
        if data_dir else _workspace_ripple_root(cwd) / LEGACY_WORKSPACE_WINDOW_ARCHIVE_DIR_NAME
    )
    cutoff = _now() - retention * 86400.0
    removed: List[str] = []
    if not root.is_dir():
        return {"removed_count": 0, "removed": []}
    patterns = ["*/*/*"] if data_dir else ["*/*"]
    for pattern in patterns:
        candidates = root.glob(pattern)
        for path in candidates:
            try:
                if path.is_dir() and path.stat().st_mtime < cutoff:
                    shutil.rmtree(path)
                    removed.append(str(path))
            except OSError:
                continue
    return {"removed_count": len(removed), "removed": removed}


def archive_window_state(
    *,
    cwd: str,
    project: str,
    window_id: str,
    action: str,
    data_dir: str | Path | None = None,
    agent: str = "",
    reason: str = "",
) -> Dict[str, Any]:
    """Move the active window-local latch directory into the archive rail."""
    action = "delete" if action == "window_delete" else "archive"
    active = _window_active_dir(cwd, window_id, project=project, data_dir=data_dir)
    legacy_active = _window_active_dir(cwd, window_id)
    if data_dir and not active.exists() and legacy_active.exists():
        active = legacy_active
    archive_root = _window_archive_root(cwd, window_id, project=project, data_dir=data_dir)
    archive_root.mkdir(parents=True, exist_ok=True)
    timestamp = int(_now() * 1000)
    dest = archive_root / f"{timestamp}-{action}"
    manifest = {
        "schema": "ripple_memory_window_archive_v1",
        "project": project,
        "window_id": window_id,
        "action": action,
        "agent": agent,
        "reason": reason,
        "archived_at": _now(),
        "archived_at_label": _iso(),
        "source": str(active),
        "destination": str(dest),
    }
    if active.exists():
        shutil.move(str(active), str(dest))
        manifest["moved"] = True
    else:
        dest.mkdir(parents=True, exist_ok=True)
        manifest["moved"] = False
        manifest["missing_active_window"] = True
    _atomic_write_json(dest / "window-archive.json", manifest)
    prune_window_archives(cwd, data_dir=data_dir)
    return {
        "ok": True,
        "action": action,
        "window_id": window_id,
        "active_path": str(active),
        "archive_path": str(dest),
        "active_exists_after": active.exists(),
        "moved": bool(manifest.get("moved")),
    }


def restore_window_state(
    *,
    cwd: str,
    project: str,
    window_id: str,
    data_dir: str | Path | None = None,
    agent: str = "",
) -> Dict[str, Any]:
    """Restore the latest archived window-local latch directory."""
    active = _window_active_dir(cwd, window_id, project=project, data_dir=data_dir)
    archive_root = _window_archive_root(cwd, window_id, project=project, data_dir=data_dir)
    if active.exists():
        return {
            "ok": True,
            "action": "restore",
            "window_id": window_id,
            "status": "already_active",
            "active_path": str(active),
        }
    candidates = [path for path in archive_root.glob("*") if path.is_dir()] if archive_root.is_dir() else []
    if not candidates:
        return {
            "ok": False,
            "action": "restore",
            "window_id": window_id,
            "status": "no_archive",
            "active_path": str(active),
        }
    latest = sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)[0]
    active.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(latest), str(active))
    _atomic_write_json(active / "window-restore.json", {
        "schema": "ripple_memory_window_restore_v1",
        "project": project,
        "window_id": window_id,
        "agent": agent,
        "restored_at": _now(),
        "restored_at_label": _iso(),
        "source": str(latest),
        "destination": str(active),
    })
    return {
        "ok": True,
        "action": "restore",
        "window_id": window_id,
        "status": "restored",
        "active_path": str(active),
        "archive_path": str(latest),
    }
