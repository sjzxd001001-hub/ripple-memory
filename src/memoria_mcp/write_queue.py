"""Durable per-project write queue for Ripple Memory remember calls."""
from __future__ import annotations

import itertools
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

from .lifecycle import is_process_alive

logger = logging.getLogger("RippleMemory.WriteQueue")

WRITE_QUEUE_SCHEMA = "ripple_memory_write_queue_v1"
WRITE_QUEUE_RESULT_SCHEMA = "ripple_memory_write_queue_result_v1"
WRITE_QUEUE_LOCK_SCHEMA = "ripple_memory_write_queue_lock_v1"

DEFAULT_WRITE_QUEUE_LOCK_STALE_SECONDS = 5 * 60.0
DEFAULT_WRITE_QUEUE_DONE_MAX_FILES = 50

_PENDING_COUNTER = itertools.count()


def _read_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _read_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _safe_text(value: str) -> str:
    try:
        value.encode("utf-8")
        return value
    except UnicodeEncodeError:
        return value.encode("utf-8", "replace").decode("utf-8")


def _safe_json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 20:
        return _safe_text(repr(value))
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, dict):
        return {
            _safe_json_value(key, depth=depth + 1): _safe_json_value(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_safe_json_value(item, depth=depth + 1) for item in value]
    return value


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(
        json.dumps(_safe_json_value(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_project_dir(project: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in str(project or ""))
    return text.strip("._-") or "default"


class ProjectWriteQueue:
    """File-backed single-writer queue scoped to one host data dir and project."""

    def __init__(self, base_data_dir: str | os.PathLike[str], project: str):
        self.base_data_dir = Path(base_data_dir).expanduser()
        self.project = _safe_project_dir(project)
        self.root = self.base_data_dir / "_runtime" / "write_queue" / self.project
        self.ready_dir = self.root / "ready"
        self.processing_dir = self.root / "processing"
        self.done_dir = self.root / "done"
        self.failed_dir = self.root / "failed"
        self.lock_path = self.root / "writer.lock"

    @property
    def lock_stale_seconds(self) -> float:
        return max(
            1.0,
            _read_float_env(
                "MEMORIA_MCP_WRITE_QUEUE_LOCK_STALE_SECONDS",
                DEFAULT_WRITE_QUEUE_LOCK_STALE_SECONDS,
            ),
        )

    @property
    def done_max_files(self) -> int:
        return max(
            1,
            _read_int_env("MEMORIA_MCP_WRITE_QUEUE_DONE_MAX_FILES", DEFAULT_WRITE_QUEUE_DONE_MAX_FILES),
        )

    def _ensure_dirs(self) -> None:
        for path in (self.ready_dir, self.processing_dir, self.done_dir, self.failed_dir):
            path.mkdir(parents=True, exist_ok=True)

    def enqueue(self, arguments: Dict[str, Any]) -> str:
        self._ensure_dirs()
        now = time.time()
        pending_id = (
            f"wq_{int(now * 1000)}_{os.getpid()}_"
            f"{threading.get_ident()}_{next(_PENDING_COUNTER)}"
        )
        payload = {
            "schema": WRITE_QUEUE_SCHEMA,
            "pending_id": pending_id,
            "project": self.project,
            "tool": "memoria_remember",
            "arguments": dict(arguments or {}),
            "created_at": now,
            "created_at_label": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
            "created_by_pid": os.getpid(),
            "created_by_thread": threading.get_ident(),
            "cwd": os.getcwd(),
        }
        _write_json_atomic(self.ready_dir / f"{pending_id}.json", payload)
        return pending_id

    def _lock_payload(self, token: str) -> Dict[str, Any]:
        now = time.time()
        return {
            "schema": WRITE_QUEUE_LOCK_SCHEMA,
            "project": self.project,
            "pid": os.getpid(),
            "token": token,
            "created_at": now,
            "heartbeat_at": now,
            "cwd": os.getcwd(),
        }

    def _lock_is_stale_or_dead(self) -> bool:
        try:
            payload = _read_json(self.lock_path)
        except Exception:
            try:
                age = time.time() - self.lock_path.stat().st_mtime
            except OSError:
                return True
            return age >= self.lock_stale_seconds

        pid = int(payload.get("pid") or 0)
        heartbeat = float(payload.get("heartbeat_at") or payload.get("created_at") or 0.0)
        if pid and not is_process_alive(pid):
            return True
        if heartbeat and time.time() - heartbeat >= self.lock_stale_seconds:
            return True
        return False

    def heartbeat_lock(self, token: str) -> bool:
        try:
            payload = _read_json(self.lock_path)
            if payload.get("pid") != os.getpid() or payload.get("token") != token:
                return False
            payload["heartbeat_at"] = time.time()
            payload["heartbeat_label"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
            _write_json_atomic(self.lock_path, payload)
            return True
        except Exception:
            logger.debug("Failed to heartbeat write queue lock", exc_info=True)
            return False

    def _start_lock_heartbeat(self, token: str) -> tuple[threading.Event, threading.Thread]:
        stop = threading.Event()
        interval = max(0.2, min(5.0, self.lock_stale_seconds / 3.0))

        def worker() -> None:
            while not stop.wait(interval):
                if not self.heartbeat_lock(token):
                    return

        thread = threading.Thread(target=worker, name="ripple-memory-write-queue-lock", daemon=True)
        thread.start()
        return stop, thread

    def try_acquire_lock(self) -> Optional[str]:
        self.root.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        payload = self._lock_payload(token)
        for attempt in range(2):
            try:
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                fd = os.open(str(self.lock_path), flags)
                try:
                    os.write(fd, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
                finally:
                    os.close(fd)
                return token
            except FileExistsError:
                if attempt == 0 and self._lock_is_stale_or_dead():
                    try:
                        self.lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        return None
                    continue
                return None
        return None

    def release_lock(self, token: str) -> None:
        try:
            payload = _read_json(self.lock_path)
            if payload.get("pid") != os.getpid() or payload.get("token") != token:
                return
            self.lock_path.unlink()
        except FileNotFoundError:
            return
        except Exception:
            logger.debug("Failed to release write queue lock", exc_info=True)

    def _ready_files(self) -> Iterable[Path]:
        if not self.ready_dir.is_dir():
            return []
        return sorted(self.ready_dir.glob("*.json"), key=lambda path: (path.stat().st_mtime, path.name))

    def _recover_processing_files(self) -> int:
        if not self.processing_dir.is_dir():
            return 0
        recovered = 0
        for path in sorted(self.processing_dir.glob("*.json")):
            dest = self.ready_dir / path.name
            try:
                self.ready_dir.mkdir(parents=True, exist_ok=True)
                os.replace(path, dest)
                recovered += 1
            except OSError:
                logger.debug("Failed to recover processing write queue item %s", path, exc_info=True)
        return recovered

    def _claim_next(self) -> Optional[Path]:
        for path in self._ready_files():
            dest = self.processing_dir / path.name
            try:
                self.processing_dir.mkdir(parents=True, exist_ok=True)
                os.replace(path, dest)
                return dest
            except FileNotFoundError:
                continue
            except OSError:
                logger.debug("Failed to claim write queue item %s", path, exc_info=True)
                continue
        return None

    def _write_result(
        self,
        *,
        item: Dict[str, Any],
        state: str,
        result: Optional[Dict[str, Any]] = None,
        error: str = "",
    ) -> None:
        pending_id = str(item.get("pending_id") or "")
        target_dir = self.done_dir if state == "committed" else self.failed_dir
        payload = {
            "schema": WRITE_QUEUE_RESULT_SCHEMA,
            "pending_id": pending_id,
            "project": self.project,
            "tool": item.get("tool") or "memoria_remember",
            "state": state,
            "created_at": item.get("created_at"),
            "finished_at": time.time(),
            "created_by_pid": item.get("created_by_pid"),
            "committed_by_pid": os.getpid(),
        }
        if result is not None:
            payload["result"] = result
        if error:
            payload["error"] = error
            payload["arguments"] = item.get("arguments") or {}
        _write_json_atomic(target_dir / f"{pending_id}.json", payload)

    def _cleanup_result_dir(self, path: Path) -> int:
        if not path.is_dir():
            return 0
        files = sorted(path.glob("*.json"), key=lambda item: (item.stat().st_mtime, item.name), reverse=True)
        removed = 0
        for stale in files[self.done_max_files:]:
            try:
                stale.unlink()
                removed += 1
            except OSError:
                logger.debug("Failed to clean write queue result %s", stale, exc_info=True)
        return removed

    def cleanup_results(self) -> int:
        return self._cleanup_result_dir(self.done_dir) + self._cleanup_result_dir(self.failed_dir)

    def process_ready(
        self,
        commit: Callable[[Dict[str, Any]], Dict[str, Any]],
        *,
        budget_seconds: float,
        max_items: int = 100,
    ) -> Dict[str, Any]:
        token = self.try_acquire_lock()
        if token is None:
            return {"acquired": False, "processed": 0, "failed": 0, "recovered_processing": 0}

        processed = 0
        failed = 0
        start = time.monotonic()
        stop_heartbeat, heartbeat_thread = self._start_lock_heartbeat(token)
        try:
            recovered = self._recover_processing_files()
            while processed + failed < max_items:
                if budget_seconds >= 0 and time.monotonic() - start >= budget_seconds:
                    break
                path = self._claim_next()
                if path is None:
                    break
                item: Dict[str, Any] = {"pending_id": path.stem, "arguments": {}}
                try:
                    item = _read_json(path)
                    if item.get("schema") != WRITE_QUEUE_SCHEMA:
                        raise ValueError(f"unexpected queue schema: {item.get('schema')}")
                    result = commit(dict(item.get("arguments") or {}))
                    self._write_result(item=item, state="committed", result=dict(result or {}))
                    processed += 1
                except Exception as exc:  # noqa: BLE001 - keep failed work inspectable.
                    failed += 1
                    try:
                        self._write_result(item=item, state="failed", error=f"{exc.__class__.__name__}: {exc}")
                    except Exception:
                        logger.debug("Failed to write failed queue result for %s", path, exc_info=True)
                finally:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        logger.debug("Failed to remove processing item %s", path, exc_info=True)
            removed = self.cleanup_results()
            remaining = self.counts()
            return {
                "acquired": True,
                "processed": processed,
                "failed": failed,
                "recovered_processing": recovered,
                "cleaned_results": removed,
                "ready_remaining": remaining["ready"],
                "processing_remaining": remaining["processing"],
                "budget_exhausted": budget_seconds >= 0 and time.monotonic() - start >= budget_seconds,
            }
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=0.2)
            self.release_lock(token)

    def read_done(self, pending_id: str) -> Optional[Dict[str, Any]]:
        path = self.done_dir / f"{pending_id}.json"
        if not path.is_file():
            return None
        return _read_json(path)

    def read_failed(self, pending_id: str) -> Optional[Dict[str, Any]]:
        path = self.failed_dir / f"{pending_id}.json"
        if not path.is_file():
            return None
        return _read_json(path)

    def wait_for_result(self, pending_id: str, *, timeout_seconds: float) -> Optional[Dict[str, Any]]:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            done = self.read_done(pending_id)
            if done is not None:
                return done
            failed = self.read_failed(pending_id)
            if failed is not None:
                return failed
            if time.monotonic() >= deadline:
                return None
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def counts(self) -> Dict[str, int]:
        return {
            "ready": len(list(self.ready_dir.glob("*.json"))) if self.ready_dir.is_dir() else 0,
            "processing": len(list(self.processing_dir.glob("*.json"))) if self.processing_dir.is_dir() else 0,
            "done": len(list(self.done_dir.glob("*.json"))) if self.done_dir.is_dir() else 0,
            "failed": len(list(self.failed_dir.glob("*.json"))) if self.failed_dir.is_dir() else 0,
            "lock_present": 1 if self.lock_path.exists() else 0,
        }
