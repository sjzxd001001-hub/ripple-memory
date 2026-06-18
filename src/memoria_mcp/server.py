"""RippleMemory — persistent memory system for coding agents.

Exposes only core memory tools to the agent:
- memoria_remember: Store a memory
- memoria_recall: Search memories
- memoria_read: Expand an exact recalled memory by ref_id
- memoria_forget: Delete a memory or confirmed project

All maintenance (tick/compress/dreamer/archive/index sync) runs automatically.
Supports multi-project routing: each project gets its own isolated memory database.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from mcp.server import Server
from mcp.types import TextContent, Tool

from .archive import ArchiveStorage
from .config import MemoriaConfig
from .dreamer import Dreamer
from .graph import MemoryGraph
from .lifecycle import IdleLifecycleManager, ProcessRegistry
from .models import CausalLink, MemoryLayer, MemoryNode, MemoryType, Summary
from .persistence import MemoriaPersistence
from .search_index import SearchIndex, compute_content_signature, extract_keywords
from .tool_specs import build_memory_tools
from .write_gate import WriteCandidate, WriteGate
from .write_queue import ProjectWriteQueue
from .bm25 import tokenize_retrieval_text

logger = logging.getLogger("RippleMemory.Server")

DEFAULT_TOOL_SOFT_TIMEOUT_SECONDS = 12.0
DEFAULT_READONLY_TOOL_SOFT_TIMEOUT_SECONDS = 30.0
DEFAULT_TOOL_RECOVERY_TIMEOUT_SECONDS = 3.0
DEFAULT_MCP_HANDLER_TIMEOUT_GRACE_SECONDS = 1.0
DEFAULT_WRITE_QUEUE_WAIT_SECONDS = 2.0
DEFAULT_WRITE_QUEUE_DRAIN_BUDGET_SECONDS = 8.0
DEFAULT_TOOL_STUCK_EXIT_SECONDS = 60.0
READONLY_MCP_TOOLS = {"memoria_recall", "memoria_read"}


def _utf8_safe_text(value: str) -> str:
    try:
        value.encode("utf-8")
        return value
    except UnicodeEncodeError:
        return value.encode("utf-8", "replace").decode("utf-8")


def _sanitize_json_text(value: Any, *, depth: int = 0) -> Any:
    if depth > 20:
        return _utf8_safe_text(repr(value))
    if isinstance(value, str):
        return _utf8_safe_text(value)
    if isinstance(value, dict):
        return {
            _sanitize_json_text(key, depth=depth + 1): _sanitize_json_text(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_json_text(item, depth=depth + 1) for item in value]
    return value


def _json_text_for_mcp(value: Any, *, indent: Optional[int] = None) -> str:
    return json.dumps(_sanitize_json_text(value), ensure_ascii=False, indent=indent)


def _trace_tool_events_enabled() -> bool:
    return _read_bool_env("MEMORIA_MCP_TRACE_TOOL_EVENTS", True)


def _runtime_event_path_from_base(base_dir: str | os.PathLike[str]) -> Path:
    return Path(base_dir).expanduser() / "_runtime" / "tool_events" / f"{os.getpid()}.json"


def _runtime_event_path_from_db(db_path: str | os.PathLike[str]) -> Path:
    return Path(db_path).expanduser().parent.parent / "_runtime" / "tool_events" / f"{os.getpid()}.json"


def _write_runtime_event_state(path: Path, event: Dict[str, Any]) -> None:
    if not _trace_tool_events_enabled():
        return
    try:
        payload = {
            "ts": time.time(),
            "ts_label": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
            "pid": os.getpid(),
            "cwd": os.getcwd(),
            "mode": "single_state_update",
            **event,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
        tmp_path.write_text(_json_text_for_mcp(payload, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        logger.debug("Failed to write Ripple runtime event state", exc_info=True)


def _read_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _debug_timing_enabled() -> bool:
    return _read_bool_env("MEMORIA_MCP_DEBUG_TIMING", False)


def _read_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"Ignoring invalid integer env {name}={raw!r}")
        return default


def _read_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"Ignoring invalid float env {name}={raw!r}")
        return default


def _tool_soft_timeout_seconds(tool_name: str = "") -> float:
    if tool_name in READONLY_MCP_TOOLS:
        return max(
            0.0,
            _read_float_env(
                "MEMORIA_MCP_READONLY_SOFT_TIMEOUT_SECONDS",
                DEFAULT_READONLY_TOOL_SOFT_TIMEOUT_SECONDS,
            ),
        )
    return max(0.0, _read_float_env("MEMORIA_MCP_TOOL_SOFT_TIMEOUT_SECONDS", DEFAULT_TOOL_SOFT_TIMEOUT_SECONDS))


def _tool_recovery_timeout_seconds() -> float:
    return max(
        0.1,
        _read_float_env("MEMORIA_MCP_TOOL_RECOVERY_TIMEOUT_SECONDS", DEFAULT_TOOL_RECOVERY_TIMEOUT_SECONDS),
    )


def _mcp_handler_timeout_seconds(tool_name: str = "") -> float:
    raw = os.environ.get("MEMORIA_MCP_HANDLER_TIMEOUT_SECONDS")
    if raw is not None and raw.strip() != "":
        return max(0.0, _read_float_env("MEMORIA_MCP_HANDLER_TIMEOUT_SECONDS", 0.0))
    soft_timeout = _tool_soft_timeout_seconds(tool_name)
    if soft_timeout <= 0:
        return 0.0
    return soft_timeout + _tool_recovery_timeout_seconds() + DEFAULT_MCP_HANDLER_TIMEOUT_GRACE_SECONDS


def _write_queue_enabled() -> bool:
    return _read_bool_env("MEMORIA_MCP_WRITE_QUEUE_ENABLED", True)


def _write_queue_wait_seconds() -> float:
    return max(
        0.0,
        _read_float_env("MEMORIA_MCP_WRITE_QUEUE_WAIT_SECONDS", DEFAULT_WRITE_QUEUE_WAIT_SECONDS),
    )


def _write_queue_drain_budget_seconds() -> float:
    return max(
        0.0,
        _read_float_env("MEMORIA_MCP_WRITE_QUEUE_DRAIN_BUDGET_SECONDS", DEFAULT_WRITE_QUEUE_DRAIN_BUDGET_SECONDS),
    )


def _write_queue_worker_enabled() -> bool:
    return _read_bool_env("MEMORIA_MCP_WRITE_QUEUE_WORKER_ENABLED", True)


def _recall_candidate_limit(top_k: int) -> int:
    multiplier = max(1, _read_int_env("MEMORIA_MCP_RECALL_CANDIDATE_MULTIPLIER", 8))
    max_limit = max(top_k, _read_int_env("MEMORIA_MCP_RECALL_CANDIDATE_LIMIT", 80))
    return min(max_limit, max(top_k, top_k * multiplier))


def _run_sync_with_timeout(
    *,
    label: str,
    timeout_seconds: float,
    runner: Callable[[], Any],
) -> tuple[bool, Any]:
    """Run sync work in a daemon thread so the MCP response path can fail open."""
    if timeout_seconds <= 0:
        return True, runner()

    result_queue: "queue.Queue[tuple[str, Any]]" = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            result_queue.put(("ok", runner()))
        except Exception as exc:  # noqa: BLE001 - return exception to caller thread.
            result_queue.put(("error", exc))

    thread = threading.Thread(target=worker, name=label[:64], daemon=True)
    thread.start()
    try:
        status, payload = result_queue.get(timeout=timeout_seconds)
    except queue.Empty:
        return False, None
    if status == "error":
        raise payload
    return True, payload


async def _run_sync_with_async_timeout(
    *,
    label: str,
    timeout_seconds: float,
    runner: Callable[[], Any],
) -> tuple[bool, Any]:
    """Async variant that does not wait for a stuck worker thread to finish."""
    if timeout_seconds <= 0:
        return True, await asyncio.to_thread(runner)

    result_queue: "queue.Queue[tuple[str, Any]]" = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            result_queue.put(("ok", runner()))
        except Exception as exc:  # noqa: BLE001 - return exception to caller task.
            result_queue.put(("error", exc))

    thread = threading.Thread(target=worker, name=label[:64], daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            status, payload = result_queue.get_nowait()
        except queue.Empty:
            await asyncio.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
            continue
        if status == "error":
            raise payload
        return True, payload
    return False, None


def _soft_timeout_payload(name: str, timeout_seconds: float, recovery_note: str = "") -> Dict[str, Any]:
    return {
        "error": "tool_soft_timeout",
        "tool": name,
        "soft_timeout_seconds": timeout_seconds,
        "recovered_after_soft_timeout": False,
        "retry_performed": False,
        "message": (
            "Tool exceeded the Ripple Memory soft timeout. No safe committed result "
            "could be recovered before returning to the agent."
        ),
        "recovery_note": recovery_note,
    }


def _apply_runtime_env_overrides(config: MemoriaConfig) -> MemoriaConfig:
    """Apply lightweight runtime knobs for MCP deployments."""
    config.enable_semantic = _read_bool_env("MEMORIA_MCP_ENABLE_SEMANTIC", config.enable_semantic)
    embedding_model = os.environ.get("MEMORIA_MCP_EMBEDDING_MODEL")
    if embedding_model and embedding_model.strip():
        config.embedding_model = embedding_model.strip()
    config.enable_semantic_resonance = _read_bool_env(
        "MEMORIA_MCP_ENABLE_SEMANTIC_RESONANCE",
        config.enable_semantic_resonance,
    )
    if not config.enable_semantic:
        config.enable_semantic_resonance = False

    query_mode = os.environ.get("MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE")
    if query_mode:
        query_mode = query_mode.strip().lower()
        if query_mode in {"off", "shadow", "live"}:
            config.search_index_query_mode = query_mode
        else:
            logger.warning(f"Ignoring invalid MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE={query_mode!r}")
    return config


def _slice_text(text: str, offset: int, max_chars: int) -> Dict[str, Any]:
    """Return a bounded slice with continuation metadata."""
    safe_offset = max(0, int(offset or 0))
    safe_max = max(200, min(int(max_chars or 4000), 20000))
    total = len(text)
    end = min(total, safe_offset + safe_max)
    return {
        "text": text[safe_offset:end],
        "offset": safe_offset,
        "max_chars": safe_max,
        "next_offset": end if end < total else None,
        "has_more": end < total,
        "total_chars": total,
    }


def _memory_node_text(node: MemoryNode) -> str:
    return str(getattr(node.summary, "description", "") or "")


def _memory_entry_text(entry: Dict[str, Any]) -> str:
    summary = entry.get("summary")
    if isinstance(summary, dict) and summary.get("description"):
        return str(summary.get("description") or "")
    if entry.get("description"):
        return str(entry.get("description") or "")
    return json.dumps(entry, ensure_ascii=False, indent=2)


def _memory_ref_to_node_id(ref_id: str = "", node_id: str = "") -> tuple[str, str]:
    """Parse Ripple memory refs while accepting a raw node_id fallback."""
    node = str(node_id or "").strip()
    if node:
        return node, "node_id"
    ref = str(ref_id or "").strip()
    for prefix, kind in (("memory_node:", "memory_node"), ("memory_index:", "memory_index")):
        if ref.startswith(prefix):
            return ref[len(prefix):].strip(), kind
    if ref and ":" not in ref:
        return ref, "raw_node_id"
    return "", "unknown"


def _normalize_fact_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^\w.:-]+", "_", text, flags=re.UNICODE)
    return text.strip("_.:-")[:160]


def _coerce_ref_list(value: Any) -> List[str]:
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                decoded = json.loads(text)
                return _coerce_ref_list(decoded)
            except Exception:
                pass
        return [item.strip() for item in re.split(r"[\s,]+", text) if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _node_to_display(
    node: MemoryNode,
    *,
    project: Optional[str] = None,
    evolution: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Convert node to user-friendly display dict."""
    limit = max(80, min(_read_int_env("MEMORIA_MCP_RECALL_DESCRIPTION_CHARS", 800), 4000))
    description = node.summary.description
    truncated = len(description) > limit
    ref_id = f"memory_node:{node.id}"
    read_args: Dict[str, Any] = {"ref_id": ref_id, "max_chars": 4000}
    if project:
        read_args["project"] = project
    result = {
        "id": node.id,
        "ref_id": ref_id,
        "description": description[:limit],
        "description_truncated": truncated,
        "description_chars": len(description),
        "has_archive": bool(node.archive_pointer),
        "read_hint": {
            "tool": "memoria_read",
            "args": read_args,
            "purpose": "expand exact memory evidence when the recall summary is not enough",
        },
        "type": node.type.value,
        "layer": node.layer.value,
        "strength": round(node.strength, 3),
        "importance": round(node.importance, 3),
        "access_count": node.access_count,
        "muscle": node.muscle,
        "tags": node.summary.people + node.summary.locations + node.archive_tags,
    }
    if evolution:
        result.update(evolution)
    return result


class RippleMemoryServer:
    def __init__(self, config: Optional[MemoriaConfig] = None):
        self.config = _apply_runtime_env_overrides(config or MemoriaConfig())
        self.persistence = MemoriaPersistence(self.config)
        self.write_gate = WriteGate(
            min_event_confidence=self.config.min_event_confidence,
            min_stable_confidence=self.config.min_stable_confidence,
            min_stable_evidence=self.config.min_stable_evidence,
        )
        # Dual-track: archive (JSON) + active graph (memory)
        data_dir = os.path.dirname(self.config.db_path)
        self.archive = ArchiveStorage(data_dir)
        self.dreamer = Dreamer(self.config, self.archive)
        self.search_index = SearchIndex(self.config.db_path)
        self._graph_state_updated_at: float = 0.0
        self._local_unsaved_node_ids: set[str] = set()
        self._local_deleted_node_ids: set[str] = set()
        self._graph_dirty: bool = False
        self.graph: MemoryGraph = self._load_or_create_graph()
        self._last_activity_real: float = time.time()
        self._last_snapshot_real: float = 0.0

    def _record_runtime_event(self, event: str, **details: Any) -> None:
        _write_runtime_event_state(
            _runtime_event_path_from_db(self.config.db_path),
            {
                "event": event,
                "project_db": self.config.db_path,
                **details,
            },
        )

    def _load_or_create_graph(self) -> MemoryGraph:
        record = self.persistence.load_graph_record()
        if record:
            try:
                self._graph_state_updated_at = float(record.get("updated_at") or 0.0)
                graph = MemoryGraph.from_dict(record["state"], self.config)
                logger.info(f"Loaded graph: {len(graph.nodes)} nodes, {len(graph.muscle_memory_ids)} muscle")
                # Rebuild retrieval metadata on boot while preserving side-rail pointers.
                self.search_index.rebuild_from_nodes(graph.nodes)
                self._restore_evolution_delete_marks(graph)
                return graph
            except Exception as e:
                logger.warning(f"Failed to load saved graph: {e}")
        return MemoryGraph(self.config)

    def _restore_evolution_delete_marks(self, graph: MemoryGraph) -> None:
        """Keep superseded memories hidden from default retrieval after restarts."""
        try:
            states = self.persistence.list_memory_evolution_states(
                statuses=["superseded"],
                limit=0,
            )
        except Exception as exc:
            logger.warning(f"Failed to restore memory evolution delete marks: {exc}")
            return
        node_ids = sorted({
            str(state.get("node_id") or "").strip()
            for state in states
            if str(state.get("node_id") or "").strip() in graph.nodes
        })
        if node_ids:
                self.search_index.mark_deleted(node_ids, reason="memory_evolution_superseded")

    def reload_graph_if_newer(self) -> bool:
        """Adopt a newer graph_state written by another MCP process."""
        return self._adopt_newer_graph_state(save_after_merge=False)["adopted"]

    def _merge_local_pending_changes(self, remote_graph: MemoryGraph) -> None:
        local_nodes = self.graph.nodes
        pending_ids = {node_id for node_id in self._local_unsaved_node_ids if node_id in local_nodes}
        for node_id in pending_ids:
            if node_id not in remote_graph.nodes:
                remote_graph.nodes[node_id] = local_nodes[node_id]

        existing_links = {
            (src, link.target)
            for src, links in remote_graph.links.items()
            for link in links
        }
        for src, links in self.graph.links.items():
            if src not in remote_graph.nodes:
                continue
            for link in links:
                if link.target not in remote_graph.nodes:
                    continue
                if src not in pending_ids and link.target not in pending_ids:
                    continue
                key = (src, link.target)
                if key in existing_links:
                    continue
                remote_graph.links[src].append(link)
                remote_graph.nodes[src].links.append(link)
                existing_links.add(key)

        for node_id in self.graph.muscle_memory_ids:
            if node_id in remote_graph.nodes:
                remote_graph.muscle_memory_ids.add(node_id)
        remote_graph._current_tick = max(remote_graph._current_tick, self.graph._current_tick)

    def _apply_local_pending_deletions(self, graph: MemoryGraph) -> None:
        for node_id in list(self._local_deleted_node_ids):
            if node_id in graph.nodes:
                del graph.nodes[node_id]
            graph.hot_cache.discard(node_id)
            graph.warm_cache.discard(node_id)
            graph.muscle_memory_ids.discard(node_id)
            graph._strength_history.pop(node_id, None)
            graph.links.pop(node_id, None)
            graph.incoming_links.pop(node_id, None)
            for targets in graph.incoming_links.values():
                targets.discard(node_id)
            for links in graph.links.values():
                links[:] = [link for link in links if link.target != node_id]

    def _adopt_newer_graph_state(self, *, save_after_merge: bool) -> Dict[str, bool]:
        try:
            record = self.persistence.load_graph_record()
        except Exception as exc:
            logger.debug("Failed to check remote graph_state freshness: %s", exc)
            return {"adopted": False, "local_changes": False, "should_save": False}
        if not record:
            return {"adopted": False, "local_changes": False, "should_save": False}

        remote_updated_at = float(record.get("updated_at") or 0.0)
        if remote_updated_at <= self._graph_state_updated_at + 1e-9:
            return {"adopted": False, "local_changes": False, "should_save": False}

        remote_graph = MemoryGraph.from_dict(record["state"], self.config)
        has_local_changes = bool(self._local_unsaved_node_ids or self._local_deleted_node_ids)
        if has_local_changes:
            self._merge_local_pending_changes(remote_graph)
            self._apply_local_pending_deletions(remote_graph)
            remote_graph._rebuild_indices()

        self.graph = remote_graph
        self._graph_state_updated_at = remote_updated_at
        self.search_index.rebuild_from_nodes(self.graph.nodes)
        self._restore_evolution_delete_marks(self.graph)
        self._graph_dirty = has_local_changes
        self._record_runtime_event(
            "graph_state_reloaded",
            remote_updated_at=remote_updated_at,
            merged_local_changes=has_local_changes,
        )
        return {
            "adopted": True,
            "local_changes": has_local_changes,
            "should_save": has_local_changes and save_after_merge,
        }

    def _save_graph(self, *, force: bool = False):
        try:
            adoption = self._adopt_newer_graph_state(save_after_merge=True)
            if adoption["adopted"] and not adoption["should_save"]:
                return
            if not force and not self._graph_dirty and not self._local_unsaved_node_ids and not self._local_deleted_node_ids:
                return
            remote_updated_at = self.persistence.graph_updated_at()
            if adoption["should_save"] or remote_updated_at <= self._graph_state_updated_at + 1e-9:
                self._graph_state_updated_at = self.persistence.save_graph(self.graph.to_dict())
                self._local_unsaved_node_ids.clear()
                self._local_deleted_node_ids.clear()
                self._graph_dirty = False
        except Exception as e:
            logger.error(f"Failed to save graph: {e}")

    def close(self):
        """Flush graph state and close resources held by this project server."""
        try:
            self._save_graph()
        except Exception as e:
            logger.warning(f"Failed to save graph during close: {e}")
        try:
            self.search_index.close()
        except Exception as e:
            logger.warning(f"Failed to close search index: {e}")

    def _memory_evolution_states_by_node(self, node_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        clean_ids = [str(node_id).strip() for node_id in node_ids if str(node_id).strip()]
        if not clean_ids:
            return {}
        try:
            states = self.persistence.list_memory_evolution_states(node_ids=clean_ids, limit=max(50, len(clean_ids) * 8))
        except Exception as exc:
            logger.warning(f"Failed to load memory evolution states: {exc}")
            return {}
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for state in states:
            node_id = str(state.get("node_id") or "").strip()
            if node_id:
                grouped.setdefault(node_id, []).append(dict(state))
        return grouped

    def _memory_evolution_for_node(
        self,
        node_id: str,
        states_by_node: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> Dict[str, Any]:
        states_by_node = states_by_node or self._memory_evolution_states_by_node([node_id])
        states = [dict(item) for item in states_by_node.get(node_id, [])]
        if not states:
            return {"evolution_status": "untracked"}

        if any(state.get("status") == "active" for state in states):
            public_status = "active"
        elif any(state.get("status") == "pending_conflict" for state in states):
            public_status = "pending_conflict"
        elif any(state.get("status") == "superseded" for state in states):
            public_status = "superseded"
        else:
            public_status = str(states[0].get("status") or "tracked")

        claims = [
            {
                "fact_key": state.get("fact_key"),
                "ref_id": state.get("ref_id"),
                "claim_text": state.get("claim_text"),
                "status": state.get("status"),
                "superseded_by_ref_id": state.get("superseded_by_ref_id") or "",
                "confidence": state.get("confidence"),
            }
            for state in states[:8]
        ]
        if public_status == "superseded":
            guidance = "Historical memory; do not treat it as current guidance."
        elif public_status == "pending_conflict":
            guidance = "Unresolved memory conflict; verify against the current user message and repository."
        elif public_status == "active":
            guidance = "Current memory claim for its fact_key unless the latest user message says otherwise."
        else:
            guidance = "No explicit current-vs-historical status is recorded."
        return {
            "evolution_status": public_status,
            "fact_keys": sorted({str(state.get("fact_key") or "") for state in states if state.get("fact_key")}),
            "evolution_claims": claims,
            "truth_guidance": guidance,
        }

    @staticmethod
    def _truth_projection_from_items(
        items: List[Dict[str, Any]],
        filtered_claims: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        current_claims: List[Dict[str, Any]] = []
        historical_claims: List[Dict[str, Any]] = list(filtered_claims or [])
        pending_claims: List[Dict[str, Any]] = []
        seen = set()

        def append_unique(target: List[Dict[str, Any]], claim: Dict[str, Any]) -> None:
            key = (
                str(claim.get("status") or ""),
                str(claim.get("fact_key") or ""),
                str(claim.get("ref_id") or ""),
                str(claim.get("claim_text") or ""),
            )
            if key in seen:
                return
            seen.add(key)
            target.append(dict(claim))

        for item in items:
            for claim in list(item.get("evolution_claims") or []):
                status = str(claim.get("status") or "")
                if status == "active":
                    append_unique(current_claims, claim)
                elif status == "superseded":
                    append_unique(historical_claims, claim)
                elif status == "pending_conflict":
                    append_unique(pending_claims, claim)

        return {
            "current_claims": current_claims[:12],
            "historical_claims": historical_claims[:12],
            "pending_claims": pending_claims[:12],
            "guidance": "Use current_claims for present answers; historical_claims are old口径/history, not current truth.",
        }

    def _memory_evolution_edges_for_claims(self, claims: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        fact_keys = sorted({
            str(claim.get("fact_key") or "").strip().lower()
            for claim in claims
            if str(claim.get("fact_key") or "").strip()
        })
        edges: List[Dict[str, Any]] = []
        seen = set()
        for fact_key in fact_keys:
            try:
                for edge in self.persistence.list_memory_evolution_edges(fact_key=fact_key, limit=100):
                    edge_id = str(edge.get("edge_id") or "")
                    if edge_id in seen:
                        continue
                    seen.add(edge_id)
                    edges.append(edge)
            except Exception as exc:
                logger.warning(f"Failed to load memory evolution edges for {fact_key}: {exc}")
        return edges

    @staticmethod
    def _recall_relevance_score(query: str, node: MemoryNode, evolution: Dict[str, Any], *, vec_sim: float = 0.0) -> Dict[str, Any]:
        query_text = str(query or "").strip().lower()
        node_text = _memory_node_text(node)
        node_text_lower = node_text.lower()
        query_tokens = set(tokenize_retrieval_text(query_text, limit=96))
        node_tokens = set(tokenize_retrieval_text(node_text_lower, limit=None))
        overlap = query_tokens & node_tokens
        query_size = max(1, len(query_tokens))
        coverage = len(overlap) / query_size
        ascii_tokens = [token for token in query_tokens if re.search(r"[a-z0-9_]", token)]
        ascii_overlap = sum(1 for token in ascii_tokens if token in node_tokens)
        special_overlap = sum(
            1
            for token in ascii_tokens
            if any(mark in token for mark in ("_", ".", "/", "-")) and token in node_text_lower
        )

        status = str(evolution.get("evolution_status") or "untracked")
        status_weight = {
            "active": 0.75,
            "pending_conflict": 0.25,
            "untracked": -0.35,
            "superseded": -1.0,
        }.get(status, -0.1)
        exact_phrase = 1.0 if query_text and query_text in node_text_lower else 0.0
        fact_key_bonus = 0.2 if evolution.get("fact_keys") else 0.0

        # Time decay: ~2% per day, starts after 3-day grace period
        now = time.time()
        node_ts = float(node.created_at_real or node.timestamp or 0.0)
        age_days = max(0.0, (now - node_ts) / 86400.0)
        grace_days = 3.0
        decay_days = max(0.0, age_days - grace_days)
        time_decay = max(0.3, 1.0 - decay_days * 0.02)

        # Vector similarity bonus: 0-10 scale (cosine sim 0-1 * 10)
        vec_bonus = float(vec_sim) * 10.0

        score = (
            len(overlap) * 1.0
            + coverage * 4.0
            + ascii_overlap * 0.35
            + special_overlap * 3.0
            + exact_phrase * 5.0
            + status_weight
            + fact_key_bonus
            + float(node.importance) * 0.25
            + float(node.strength) * 0.15
            + vec_bonus
        ) * time_decay
        return {
            "score": round(float(score), 6),
            "overlap": len(overlap),
            "coverage": round(float(coverage), 6),
            "ascii_query_terms": len(ascii_tokens),
            "ascii_overlap": ascii_overlap,
            "special_overlap": special_overlap,
            "exact_phrase": exact_phrase > 0.0,
            "status": status,
            "age_days": round(age_days, 1),
            "time_decay": round(time_decay, 4),
            "vec_sim": round(float(vec_sim), 4),
            "vec_bonus": round(float(vec_bonus), 4),
        }

    @staticmethod
    def _recall_candidate_is_too_weak(relevance: Dict[str, Any]) -> bool:
        """Avoid padding code/module queries with one-token weak matches."""
        # Vector similarity overrides weak-match filter
        if float(relevance.get("vec_sim") or 0) >= 0.35:
            return False
        if not _read_bool_env("MEMORIA_MCP_RECALL_FILTER_WEAK_ASCII_MATCHES", True):
            return False
        ascii_terms = int(relevance.get("ascii_query_terms") or 0)
        if ascii_terms < 2:
            return False
        if bool(relevance.get("exact_phrase")):
            return False
        # CJK queries: when most query terms are non-ASCII, ASCII overlap
        # requirement doesn't apply. Bypass if non-ASCII overlap is significant.
        non_ascii_terms = max(0, ascii_terms - int(relevance.get("ascii_overlap") or 0))
        cjk_overlap = max(0, int(relevance.get("overlap") or 0) - int(relevance.get("ascii_overlap") or 0))
        if cjk_overlap >= 2 and float(relevance.get("coverage") or 0) >= 0.15:
            return False
        if int(relevance.get("special_overlap") or 0) >= 1:
            return False
        if int(relevance.get("index_special_overlap") or 0) >= 1:
            return False
        if (
            str(relevance.get("status") or "") == "untracked"
            and ascii_terms >= 3
            and int(relevance.get("special_overlap") or 0) <= 0
            and int(relevance.get("ascii_overlap") or 0) < 3
        ):
            return True
        return int(relevance.get("ascii_overlap") or 0) < 2

    def _rerank_recall_candidates(
        self,
        query: str,
        nodes: List[MemoryNode],
        states_by_node: Dict[str, List[Dict[str, Any]]],
        vector_sims: Optional[Dict[str, float]] = None,
    ) -> List[tuple[MemoryNode, Dict[str, Any], Dict[str, Any]]]:
        ranked: List[tuple[float, int, float, str, MemoryNode, Dict[str, Any], Dict[str, Any]]] = []
        for index, node in enumerate(nodes):
            evolution = self._memory_evolution_for_node(node.id, states_by_node)
            vec_sim = (vector_sims or {}).get(node.id, 0.0)
            relevance = self._recall_relevance_score(query, node, evolution, vec_sim=vec_sim)
            ranked.append((
                float(relevance["score"]),
                -index,
                float(node.created_at_real or node.timestamp or 0.0),
                node.id,
                node,
                evolution,
                relevance,
            ))
        ranked.sort(key=lambda item: (item[0], item[1], item[2], item[3]), reverse=True)
        return [(node, evolution, relevance) for _score, _index, _created, _id, node, evolution, relevance in ranked]

    def _claim_text_for_node(self, node_id: str) -> str:
        node = self.graph.nodes.get(node_id)
        if node is not None:
            return _memory_node_text(node)
        entry, _row = self._read_stream_entry_by_node_id(node_id)
        if entry:
            return _memory_entry_text(entry)
        return node_id

    def _superseded_node_ids_from_args(self, args: Dict[str, Any]) -> List[str]:
        refs = []
        refs.extend(_coerce_ref_list(args.get("supersedes_ref_ids")))
        refs.extend(_coerce_ref_list(args.get("supersedes_refs")))
        refs.extend(_coerce_ref_list(args.get("supersedes_node_ids")))
        node_ids: List[str] = []
        for ref in refs:
            node_id, _kind = _memory_ref_to_node_id(ref)
            if node_id and node_id not in node_ids:
                node_ids.append(node_id)
        return node_ids

    def _record_memory_evolution_for_new_node(
        self,
        node_id: str,
        content: str,
        args: Dict[str, Any],
        *,
        confidence: float,
    ) -> Dict[str, Any]:
        fact_key = _normalize_fact_key(args.get("fact_key") or args.get("evolution_key"))
        superseded_node_ids = [item for item in self._superseded_node_ids_from_args(args) if item != node_id]
        requested_status = str(args.get("evolution_status") or "active").strip().lower()
        if requested_status not in {"active", "pending_conflict"}:
            requested_status = "active"
        if not fact_key and not superseded_node_ids and "evolution_status" not in args:
            return {}
        if not fact_key:
            fact_key = f"memory_update:{node_id}"

        reason = str(args.get("evolution_reason") or args.get("reason") or "")[:1000]
        active_state = self.persistence.upsert_memory_evolution_state(
            {
                "fact_key": fact_key,
                "node_id": node_id,
                "ref_id": f"memory_node:{node_id}",
                "claim_text": content,
                "status": requested_status,
                "reason": reason,
                "confidence": confidence,
            }
        )
        old_status = "pending_conflict" if requested_status == "pending_conflict" else "superseded"
        superseded_states = []
        evolution_edges = []
        for old_node_id in superseded_node_ids:
            old_claim = self._claim_text_for_node(old_node_id)
            superseded_states.append(
                self.persistence.upsert_memory_evolution_state(
                    {
                        "fact_key": fact_key,
                        "node_id": old_node_id,
                        "ref_id": f"memory_node:{old_node_id}",
                        "claim_text": old_claim,
                        "status": old_status,
                        "superseded_by_node_id": "" if old_status == "pending_conflict" else node_id,
                        "superseded_by_ref_id": "" if old_status == "pending_conflict" else f"memory_node:{node_id}",
                        "reason": reason or f"Superseded by memory_node:{node_id}",
                        "confidence": confidence,
                    }
                )
            )
            if old_status == "superseded":
                evolution_edges.append(
                    self.persistence.insert_memory_evolution_edge(
                        {
                            "fact_key": fact_key,
                            "from_ref_id": f"memory_node:{old_node_id}",
                            "from_claim_hash": self.persistence.memory_evolution_claim_hash(old_claim),
                            "to_ref_id": f"memory_node:{node_id}",
                            "to_claim_hash": self.persistence.memory_evolution_claim_hash(content),
                            "relation": "superseded_by",
                            "confidence": confidence,
                            "reason": reason or f"Superseded by memory_node:{node_id}",
                            "metadata": {"source": "memoria_remember"},
                        }
                    )
                )

        cleanup_mark = {"search_index_marked": 0, "node_ids": []}
        if old_status == "superseded" and superseded_node_ids:
            self.search_index.mark_deleted(superseded_node_ids, reason="memory_evolution_superseded")
            cleanup_mark = {
                "search_index_marked": len(superseded_node_ids),
                "node_ids": list(superseded_node_ids),
            }
        return {
            "fact_key": fact_key,
            "status": requested_status,
            "active_ref_id": f"memory_node:{node_id}",
            "superseded_ref_ids": [state["ref_id"] for state in superseded_states if state.get("status") == "superseded"],
            "pending_conflict_ref_ids": [state["ref_id"] for state in superseded_states if state.get("status") == "pending_conflict"],
            "state_count": 1 + len(superseded_states),
            "edge_count": len(evolution_edges),
            "cleanup_mark": cleanup_mark,
        }

    def _read_recovery_evolution_states(self, fact_key: str, content: str) -> List[Dict[str, Any]]:
        """Short, read-only lookup used only after a memoria_remember soft timeout."""
        if not fact_key:
            return []
        try:
            conn = sqlite3.connect(f"file:{self.config.db_path}?mode=ro", uri=True, timeout=0.25)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    SELECT fact_key, node_id, ref_id, claim_text, status, confidence, updated_at
                    FROM memory_evolution_state
                    WHERE fact_key = ? AND status IN ('active', 'pending_conflict')
                    ORDER BY updated_at DESC
                    LIMIT 10
                    """,
                    (fact_key,),
                ).fetchall()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning(f"Soft-timeout recovery evolution lookup failed: {exc}")
            return []

        matches = []
        for row in rows:
            state = dict(row)
            if content and str(state.get("claim_text") or "") != content:
                continue
            matches.append(state)
        return matches

    def _find_recovered_remember_node(self, args: Dict[str, Any]) -> tuple[str, str, Optional[Dict[str, Any]]]:
        content = str(args.get("content") or "")
        fact_key = _normalize_fact_key(args.get("fact_key") or args.get("evolution_key"))
        for state in self._read_recovery_evolution_states(fact_key, content):
            node_id = str(state.get("node_id") or "").strip()
            if node_id:
                return node_id, "memory_evolution_state", state

        # No fact_key, or evolution did not commit yet: check this server's
        # in-memory graph for an exact content match without creating another write.
        if content:
            for node in sorted(self.graph.nodes.values(), key=lambda item: item.timestamp, reverse=True):
                if _memory_node_text(node) == content:
                    return node.id, "memory_graph_exact_content", None
        return "", "", None

    def _recovered_remember_result(
        self,
        node_id: str,
        args: Dict[str, Any],
        *,
        source: str,
        state: Optional[Dict[str, Any]],
        timeout_seconds: float,
    ) -> Dict[str, Any]:
        node = self.graph.nodes.get(node_id)
        evolution = self._memory_evolution_for_node(node_id) if node_id else {"evolution_status": "untracked"}
        if state and evolution.get("evolution_status") in {None, "untracked"}:
            fact_key = str(state.get("fact_key") or "")
            status = str(state.get("status") or "active")
            evolution = {
                "fact_key": fact_key,
                "status": status,
                "active_ref_id": f"memory_node:{node_id}" if status == "active" else "",
                "pending_conflict_ref_ids": [f"memory_node:{node_id}"] if status == "pending_conflict" else [],
                "superseded_ref_ids": [],
                "state_count": 1,
            }

        layer = node.layer.value if node else ""
        strength = round(node.strength, 3) if node else None
        return {
            "stored": True,
            "node_id": node_id,
            "decision": "stable_update",
            "layer": layer,
            "strength": strength,
            "memory_evolution": evolution or None,
            "recovered_after_soft_timeout": True,
            "recovery": {
                "kind": "committed_remember_result",
                "source": source,
                "soft_timeout_seconds": timeout_seconds,
                "retry_performed": False,
                "note": (
                    "Original memoria_remember exceeded the soft timeout. "
                    "Ripple Memory did not write again; it reconstructed the "
                    "response from committed memory state."
                ),
            },
        }

    def recover_remember_after_soft_timeout(self, args: Dict[str, Any], timeout_seconds: float) -> Optional[Dict[str, Any]]:
        node_id, source, state = self._find_recovered_remember_node(args)
        if not node_id:
            return None
        return self._recovered_remember_result(
            node_id,
            args,
            source=source,
            state=state,
            timeout_seconds=timeout_seconds,
        )

    def _auto_maintenance(self):
        """Run automatic maintenance after every tool call.

        - Tick: decay + attractor + consolidation + phase transition (throttled)
        - Compress: if over active_memory_limit
        - Dreamer: if conditions met
        - Search index sync
        - Periodic snapshot
        """
        now = time.time()

        # 1. Tick (throttled: at most once per tick_interval seconds)
        tick_interval = self.config.consolidation_interval_seconds
        if now - getattr(self, '_last_tick_real', 0.0) >= tick_interval:
            self.graph.tick()
            self._last_tick_real = now

        # 2. Compress (if over limit)
        non_muscle = len(self.graph.nodes) - len(self.graph.muscle_memory_ids)
        if non_muscle > self.config.active_memory_limit:
            # Write compressed nodes to JSONL before they disappear from graph
            if self.config.enable_memory_jsonl_stream:
                self._sync_compressed_to_jsonl()

            result = self.graph.compress(
                self.graph._current_tick,
                compression_batch=self.config.compression_batch,
                current_real_time=now,
            )
            if result:
                compressed_ids = result.get("compressed_node_ids", [])
                if compressed_ids:
                    self.search_index.mark_deleted(compressed_ids, reason="compression")
                summary_id = result.get("summary_id")
                if summary_id and summary_id in self.graph.nodes:
                    node = self.graph.nodes[summary_id]
                    # Write summary node to JSONL
                    if self.config.enable_memory_jsonl_stream:
                        stream_result = self.archive.append_memory_stream_entry(
                            node.to_dict(),
                            flush=self.config.jsonl_flush_immediately,
                        )
                        sig = compute_content_signature(node.to_dict())
                        self.search_index.mark_content_synced(
                            summary_id, stream_result["json_file"],
                            stream_result["json_offset"], sig,
                        )
                    self.search_index.upsert_node(
                        summary_id, node.summary.description,
                        node.importance, node.strength,
                        node.timestamp, node.layer.value,
                        content_dirty=False, index_dirty=True,
                    )
                logger.info(f"Auto-compressed: {len(compressed_ids)} nodes")

        # 3. Dreamer (if conditions met; uses search_index as data source)
        deleted_count = self.search_index.get_deleted_count()
        if self.dreamer.should_run(deleted_count, last_activity_real=self._last_activity_real):
            deleted_entries = self._load_deleted_entries_for_dreamer()
            if deleted_entries:
                survivors = {
                    nid: node for nid, node in self.graph.nodes.items()
                    if node.origin_kind == "compressed_summary"
                }
                report = self.dreamer.run(deleted_entries, survivors)
                archived = report.get("archived", 0)
                if archived:
                    processed_ids = [
                        str(item).strip()
                        for item in list(report.get("processed_node_ids") or [])
                        if str(item).strip()
                    ]
                    purged = 0
                    if processed_ids:
                        self.search_index.purge_deleted(processed_ids)
                        purged = len(processed_ids)
                        report["purged_index_rows"] = purged
                    logger.info(f"Auto-dreamer: archived={archived}, groups={report.get('groups', 0)}")

        # 4. Periodic snapshot (every snapshot_interval_hours)
        snapshot_interval = self.config.dreamer_interval_days * 86400.0 / 4  # 4x per dreamer cycle
        if now - self._last_snapshot_real >= snapshot_interval:
            try:
                state = self.graph.to_dict()
                self.archive.archive_core_snapshot(state, label="auto")
                self._last_snapshot_real = now
                logger.info("Auto-snapshot saved")
            except Exception as e:
                logger.warning(f"Auto-snapshot failed: {e}")

        # 5. Save. This is write-side maintenance; read tools intentionally do
        # not call it so recall/read cannot block behind snapshots or DB writes.
        self._graph_dirty = True
        self._save_graph(force=True)
        self._last_activity_real = now

    def _sync_compressed_to_jsonl(self):
        """Write nodes about to be compressed into JSONL before they vanish."""
        now = time.time()
        candidates = self.graph._select_compression_candidates(
            self.graph._current_tick,
            current_real_time=now,
            recent_write_grace_seconds=300.0,
            recent_access_grace_seconds=60.0,
        )
        for node in candidates[:self.config.compression_batch]:
            # Skip if already synced (has json_file in search_index)
            already_synced = False
            conn = self.search_index._get_conn()
            row = conn.execute(
                "SELECT json_file FROM search_index WHERE node_id = ? AND deleted = 0",
                (node.id,)
            ).fetchone()
            if row and row["json_file"]:
                already_synced = True
            if not already_synced:
                stream_result = self.archive.append_memory_stream_entry(
                    node.to_dict(),
                    flush=self.config.jsonl_flush_immediately,
                )
                sig = compute_content_signature(node.to_dict())
                self.search_index.mark_content_synced(
                    node.id, stream_result["json_file"],
                    stream_result["json_offset"], sig,
                )

    def _load_deleted_entries_for_dreamer(self) -> List[Dict[str, Any]]:
        """Rebuild deleted entries from search_index + JSONL (no memory list needed)."""
        rows = self.search_index.get_deleted_entries(
            limit=self.config.dreamer_max_rows_per_run,
        )
        # Filter out entries that are too recent (still in grace period)
        min_age_seconds = self.config.dreamer_min_entry_age_hours * 3600.0
        cutoff = time.time() - min_age_seconds
        entries = []
        for row in rows:
            node_id = row["node_id"]
            json_file = row.get("json_file")
            json_offset = row.get("json_offset")
            deleted_at = row.get("deleted_at") or 0
            allowed_reasons = {
                str(item).strip().lower()
                for item in list(self.config.dreamer_allowed_delete_reasons or [])
                if str(item).strip()
            }
            deleted_reason = str(row.get("deleted_reason") or "").strip().lower()
            if allowed_reasons and deleted_reason not in allowed_reasons:
                continue
            # Skip entries that were deleted too recently
            if deleted_at and deleted_at > cutoff:
                continue
            if json_file is not None and json_offset is not None:
                try:
                    full = self.archive.read_memory_stream_entry(json_file, json_offset)
                    entries.append({
                        "id": node_id,
                        "description": full.get("summary", {}).get("description", ""),
                        "source_node_ids": full.get("source_node_ids", []),
                        "timestamp": full.get("timestamp", 0),
                    })
                except Exception as e:
                    logger.warning(f"Dreamer: failed to read JSONL for {node_id}: {e}")
                    entries.append({
                        "id": node_id,
                        "description": row.get("keywords", ""),
                        "source_node_ids": [],
                        "timestamp": 0,
                    })
        return entries

    def _tool_remember(self, args: dict) -> dict:
        debug_timing = _debug_timing_enabled()
        start = last = time.time()
        remember_id = f"remember-{int(start * 1000)}-{id(args) % 10000:04d}"
        self._record_runtime_event(
            "remember_start",
            remember_id=remember_id,
            type=args.get("type", "fact"),
            has_fact_key=bool(args.get("fact_key") or args.get("evolution_key")),
            content_chars=len(str(args.get("content") or "")),
        )

        def mark(label: str):
            nonlocal last
            now = time.time()
            if debug_timing:
                logger.info(f"remember timing {label}: step={now - last:.3f}s total={now - start:.3f}s")
            self._record_runtime_event(
                "remember_phase",
                remember_id=remember_id,
                phase=label,
                step_seconds=round(now - last, 4),
                total_seconds=round(now - start, 4),
            )
            last = now

        content = args["content"]
        mem_type = MemoryType(args.get("type", "fact"))
        importance = float(args.get("importance", 0.5))
        confidence = float(args.get("confidence", 0.7))
        mark("parsed")

        # WriteGate review
        candidate = WriteCandidate(
            content=content,
            candidate_type="stable_update",
            confidence=confidence,
            importance=importance,
            source="user",
        )
        decision = self.write_gate.review(candidate, evidence_quality=confidence)

        if not decision.approved:
            self._record_runtime_event(
                "remember_rejected",
                remember_id=remember_id,
                decision=decision.decision_type,
                total_seconds=round(time.time() - start, 4),
            )
            return {
                "stored": False,
                "decision": decision.decision_type,
                "reasons": decision.reasons,
            }
        mark("write_gate")

        now = time.time()
        node_id = f"mem_{int(now * 1000)}_{id(content) % 10000:04d}"

        node = MemoryNode(
            id=node_id,
            timestamp=int(now),
            type=mem_type,
            importance=importance,
            strength=importance,
            summary=Summary(description=content),
            created_at_real=now,
            last_accessed_at_real=now,
        )

        self.graph.add_node(node)
        self._local_unsaved_node_ids.add(node_id)
        mark("graph_add_node")

        # Dual dirty tracking
        self.search_index.upsert_node(
            node_id, content, importance, importance, int(now), node.layer.value,
            content_dirty=True, index_dirty=True,
        )
        mark("search_index_upsert")

        # Content-sync to JSONL
        if self.config.enable_memory_jsonl_stream:
            stream_result = self.archive.append_memory_stream_entry(
                node.to_dict(),
                flush=self.config.jsonl_flush_immediately,
            )
            sig = compute_content_signature(node.to_dict())
            self.search_index.mark_content_synced(
                node_id, stream_result["json_file"], stream_result["json_offset"], sig,
            )
        self.search_index.mark_index_synced(node_id)
        mark("jsonl_sync")

        memory_evolution = self._record_memory_evolution_for_new_node(
            node_id,
            content,
            args,
            confidence=confidence,
        )
        mark("memory_evolution")

        # Propagate initial pulse
        self.graph.propagate(node_id, importance, self.graph._current_tick)
        mark("propagate")

        # Auto-maintenance (tick/compress/dreamer)
        self._auto_maintenance()
        mark("auto_maintenance")

        self._record_runtime_event(
            "remember_complete",
            remember_id=remember_id,
            node_id=node_id,
            decision=decision.decision_type,
            total_seconds=round(time.time() - start, 4),
        )
        return {
            "stored": True,
            "node_id": node_id,
            "decision": decision.decision_type,
            "layer": node.layer.value,
            "strength": round(node.strength, 3),
            "memory_evolution": memory_evolution or None,
        }

    def _tool_recall(self, args: dict) -> dict:
        started = time.perf_counter()
        query = args["query"]
        top_k = int(args.get("top_k", 5))
        include_evolution = bool(args.get("include_evolution", False))
        mode = self.config.search_index_query_mode
        candidate_limit = _recall_candidate_limit(top_k)
        self._record_runtime_event(
            "recall_start",
            top_k=top_k,
            include_evolution=include_evolution,
            candidate_limit=candidate_limit,
            query_chars=len(str(query or "")),
        )

        # Main retrieval
        results, vector_sims = self.graph.hybrid_retrieve(query, top_k=candidate_limit)
        by_id: Dict[str, MemoryNode] = {n.id: n for n in results}

        # Search index (shadow/live mode)
        shadow_info = {"mode": mode, "index_hits": 0, "overlap": 0}
        index_signals: Dict[str, Dict[str, Any]] = {}
        if self.config.enable_search_index and mode != "off":
            keywords = extract_keywords(query)
            if keywords:
                index_hits = self.search_index.query_keywords(keywords, top_k=candidate_limit * 2)
                shadow_ids = {r["node_id"] for r in index_hits}
                shadow_info["index_hits"] = len(index_hits)
                shadow_info["overlap"] = len(shadow_ids & set(by_id))
                for row in index_hits:
                    keyword_text = str(row.get("keywords") or "").lower()
                    matched = [keyword for keyword in keywords if keyword and keyword.lower() in keyword_text]
                    if not matched:
                        continue
                    index_signals[str(row["node_id"])] = {
                        "index_overlap": len(matched),
                        "index_special_overlap": sum(
                            1 for keyword in matched if any(mark in keyword for mark in ("_", ".", "/", "-"))
                        ),
                    }

                if mode == "live":
                    for nid in shadow_ids:
                        if nid in self.graph.nodes and len(by_id) < candidate_limit:
                            by_id.setdefault(nid, self.graph.nodes[nid])

        results = list(by_id.values())
        states_by_node = self._memory_evolution_states_by_node([node.id for node in results])
        ranked_candidates = self._rerank_recall_candidates(query, results, states_by_node, vector_sims)
        visible_nodes: List[MemoryNode] = []
        visible_items: List[Dict[str, Any]] = []
        filtered_claims: List[Dict[str, Any]] = []
        filtered_refs: List[str] = []
        relevance_by_node: Dict[str, Dict[str, Any]] = {}
        weak_filtered_refs: List[str] = []
        seen_visible = set()
        for node, evolution, relevance in ranked_candidates:
            if node.id in seen_visible:
                continue
            seen_visible.add(node.id)
            if node.id in index_signals:
                relevance = {**relevance, **index_signals[node.id]}
            if evolution.get("evolution_status") == "superseded" and not include_evolution:
                filtered_refs.append(f"memory_node:{node.id}")
                filtered_claims.extend(list(evolution.get("evolution_claims") or []))
                continue
            if self._recall_candidate_is_too_weak(relevance):
                weak_filtered_refs.append(f"memory_node:{node.id}")
                continue
            relevance_by_node[node.id] = relevance
            visible_nodes.append(node)
            display = _node_to_display(node, project=args.get("project"), evolution=evolution)
            if "age_days" in relevance:
                display["age_days"] = relevance["age_days"]
            if "time_decay" in relevance:
                display["time_decay"] = relevance["time_decay"]
            if "vec_sim" in relevance:
                display["vec_sim"] = relevance["vec_sim"]
            visible_items.append(display)
            if len(visible_nodes) >= top_k:
                break

        # Access recalled nodes
        now = time.time()
        for node in visible_nodes:
            node.access(self.graph._current_tick, now)

        projection_filtered_claims = filtered_claims if include_evolution else []
        all_claims: List[Dict[str, Any]] = []
        for item in visible_items:
            all_claims.extend(list(item.get("evolution_claims") or []))
        if include_evolution:
            all_claims.extend(filtered_claims)

        elapsed = time.perf_counter() - started
        self._record_runtime_event(
            "recall_complete",
            elapsed_seconds=round(elapsed, 4),
            result_count=len(visible_items),
            candidate_count=len(results),
            filtered_superseded_count=len(filtered_refs),
        )
        return {
            "query": query,
            "count": len(visible_items),
            "results": visible_items,
            "include_evolution": include_evolution,
            "filtered_superseded_refs": filtered_refs,
            "filtered_weak_refs": weak_filtered_refs,
            "recall_diagnostics": {
                "candidate_count": len(results),
                "elapsed_seconds": round(elapsed, 4),
                "read_only": True,
                "maintenance_ran": False,
                "filtered_weak_count": len(weak_filtered_refs),
                "relevance": relevance_by_node,
                "search_index": shadow_info,
            },
            "truth_projection": self._truth_projection_from_items(visible_items, projection_filtered_claims),
            "evolution_chains": self._memory_evolution_edges_for_claims(all_claims) if include_evolution else [],
        }

    def _lookup_search_index_row(self, node_id: str) -> Optional[Dict[str, Any]]:
        conn = self.search_index._get_conn()
        row = conn.execute(
            """
            SELECT node_id, json_file, json_offset, deleted, deleted_reason,
                   importance, strength, timestamp, layer
            FROM search_index
            WHERE node_id = ?
            """,
            (node_id,),
        ).fetchone()
        return dict(row) if row else None

    def _read_stream_entry_by_node_id(self, node_id: str) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        row = self._lookup_search_index_row(node_id)
        if row and row.get("json_file"):
            try:
                entry = self.archive.read_memory_stream_entry(str(row["json_file"]), int(row.get("json_offset") or 0))
                if str(entry.get("id") or "") == node_id:
                    return entry, row
            except Exception as exc:
                logger.warning(f"memory.read: failed indexed JSONL read for {node_id}: {exc}")

        for json_file in self.archive.list_memory_stream_files():
            try:
                for entry in self.archive.read_memory_stream_entries(json_file):
                    if str(entry.get("id") or "") == node_id:
                        fallback_row = row or {
                            "node_id": node_id,
                            "json_file": json_file,
                            "json_offset": entry.get("_json_offset", 0),
                        }
                        return entry, fallback_row
            except Exception as exc:
                logger.warning(f"memory.read: failed scanning {json_file}: {exc}")
        return None, row

    def _tool_read(self, args: dict) -> dict:
        ref_id = str(args.get("ref_id") or "").strip()
        node_id, ref_kind = _memory_ref_to_node_id(ref_id, str(args.get("node_id") or ""))
        if not node_id:
            return {
                "ok": False,
                "error": "memoria_read requires ref_id or node_id",
                "accepted_refs": ["memory_node:<id>", "memory_index:<id>", "<raw_node_id>"],
            }

        offset = int(args.get("offset") or 0)
        max_chars = int(args.get("max_chars") or 4000)
        source = ""
        hydration_rule = ""
        metadata: Dict[str, Any] = {}

        if ref_kind != "memory_index" and node_id in self.graph.nodes:
            node = self.graph.nodes[node_id]
            text = _memory_node_text(node)
            node.access(self.graph._current_tick, time.time())
            source = "memory_graph"
            hydration_rule = "memory_node"
            metadata = {
                "type": node.type.value,
                "layer": node.layer.value,
                "importance": round(node.importance, 3),
                "strength": round(node.strength, 3),
                "has_archive": bool(node.archive_pointer),
                "tags": node.summary.people + node.summary.locations + node.archive_tags,
            }
        else:
            entry, row = self._read_stream_entry_by_node_id(node_id)
            if not entry:
                return {
                    "ok": False,
                    "error": f"Memory {node_id} not found in active graph or JSONL stream",
                    "ref_id": ref_id or f"memory_node:{node_id}",
                    "node_id": node_id,
                    "ref_kind": ref_kind,
                    "index_row": row,
                }
            text = _memory_entry_text(entry)
            source = "jsonl_stream"
            hydration_rule = "jsonl_stream"
            metadata = {
                "type": entry.get("type"),
                "layer": entry.get("layer") or (row or {}).get("layer"),
                "importance": entry.get("importance") or (row or {}).get("importance"),
                "strength": entry.get("strength") or (row or {}).get("strength"),
                "json_file": (row or {}).get("json_file") or entry.get("_json_file"),
                "json_offset": (row or {}).get("json_offset") or entry.get("_json_offset"),
                "deleted": bool((row or {}).get("deleted", 0)),
                "deleted_reason": (row or {}).get("deleted_reason"),
            }

        evolution = self._memory_evolution_for_node(node_id)
        metadata["memory_evolution"] = evolution
        page = _slice_text(text, offset, max_chars)
        read_args: Dict[str, Any] = {
            "ref_id": ref_id or f"memory_node:{node_id}",
            "offset": page["next_offset"],
            "max_chars": page["max_chars"],
        }
        if args.get("project"):
            read_args["project"] = args.get("project")
        return {
            "ok": True,
            "ref_id": ref_id or f"memory_node:{node_id}",
            "node_id": node_id,
            "ref_kind": ref_kind,
            "source": source,
            "hydration_rule": hydration_rule,
            "evolution_status": evolution.get("evolution_status"),
            "truth_guidance": evolution.get("truth_guidance"),
            **page,
            "metadata": metadata,
            "read_hint": {
                "tool": "memoria_read",
                "args": read_args,
                "purpose": "continue this exact memory slice",
            } if page["has_more"] else None,
        }

    def _remove_node_from_graph(self, node_id: str) -> bool:
        if node_id not in self.graph.nodes:
            return False
        del self.graph.nodes[node_id]
        self.graph.hot_cache.discard(node_id)
        self.graph.warm_cache.discard(node_id)
        self.graph.muscle_memory_ids.discard(node_id)
        self.graph._strength_history.pop(node_id, None)
        self.graph.links.pop(node_id, None)
        self.graph.incoming_links.pop(node_id, None)
        for targets in self.graph.incoming_links.values():
            targets.discard(node_id)
        for links in self.graph.links.values():
            links[:] = [link for link in links if link.target != node_id]
        self.graph._rebuild_indices()
        return True

    def _tool_forget(self, args: dict) -> dict:
        node_id = str(args.get("node_id") or "").strip()
        if not node_id:
            return {"deleted": False, "error": "memoria_forget requires node_id for memory deletion"}

        desc = ""
        if node_id in self.graph.nodes:
            desc = self.graph.nodes[node_id].summary.description[:100]

        graph_removed = self._remove_node_from_graph(node_id)
        index_removed = self.search_index.purge_nodes([node_id])
        stream_result = self.archive.purge_memory_stream_entries([node_id])
        evolution_removed = self.persistence.delete_memory_evolution_states([node_id])

        self._local_deleted_node_ids.add(node_id)
        self._local_unsaved_node_ids.discard(node_id)
        self._save_graph()
        deleted = bool(graph_removed or index_removed or stream_result.get("purged") or evolution_removed)
        return {
            "deleted": deleted,
            "forgotten": deleted,
            "purged": deleted,
            "node_id": node_id,
            "description": desc,
            "removed_from_graph": graph_removed,
            "removed_index_rows": index_removed,
            "removed_evolution_states": evolution_removed,
            "redacted_stream_records": stream_result.get("purged", 0),
            "stream_files_touched": stream_result.get("files_touched", 0),
            "readable_after_delete": False,
        }


def _sanitize_project_name(name: str) -> str:
    """Convert project name to a safe directory name."""
    name = name.strip().lower()
    name = re.sub(r'[^\w\-]', '_', name)
    name = re.sub(r'_+', '_', name).strip('_')
    return name or "default"


# 60 days inactive -> archive; physical purge remains opt-in.
EXPIRY_DAYS = 60
PURGE_DAYS = 0
ARCHIVE_DIR_NAME = "_archived"
PROJECT_DELETE_CONFIRM_PREFIX = "DELETE:"


class MemoriaRouter:
    """Multi-project router: one MCP endpoint, per-project isolated memory databases.

    Each project gets its own subdirectory under base_data_dir, with its own
    SQLite database, archive, and search index. Servers are lazily created on
    first access.

    Auto-expiry: projects unused for EXPIRY_DAYS are moved to _archived/.
    Accessing an archived project restores it automatically. Physical project
    deletion is explicit via memoria_forget(scope="project", confirm="DELETE:<project>").
    """

    def __init__(self, base_data_dir: str, expiry_days: int = EXPIRY_DAYS, purge_days: int = PURGE_DAYS):
        self.base_data_dir = base_data_dir
        self.archive_dir = os.path.join(base_data_dir, ARCHIVE_DIR_NAME)
        self.expiry_days = expiry_days
        self.purge_days = purge_days
        self._servers: Dict[str, RippleMemoryServer] = {}
        self._last_access: Dict[str, float] = {}
        self._meta_path = os.path.join(base_data_dir, "_project_meta.json")
        self.expose_project_tools = _read_bool_env("MEMORIA_MCP_EXPOSE_PROJECT_TOOLS", False)
        self.lifecycle_manager: Optional[IdleLifecycleManager] = None
        self._active_mcp_tools: Dict[str, Dict[str, Any]] = {}
        self._active_mcp_tools_lock = threading.Lock()
        os.makedirs(self.base_data_dir, exist_ok=True)
        self._load_meta()
        self.server = Server("ripple-memory")
        self._register_tools()
        # 启动时检查一次过期
        self._check_expired_projects()

    def _record_runtime_event(self, event: str, **details: Any) -> None:
        _write_runtime_event_state(
            _runtime_event_path_from_base(self.base_data_dir),
            {
                "event": event,
                **details,
            },
        )

    # ========== 元数据持久化 ==========

    def _load_meta(self):
        """Load project access timestamps from disk."""
        if os.path.exists(self._meta_path):
            try:
                with open(self._meta_path, "r", encoding="utf-8") as f:
                    self._last_access = json.load(f)
            except Exception:
                self._last_access = {}

    def _save_meta(self):
        """Persist project access timestamps."""
        try:
            with open(self._meta_path, "w", encoding="utf-8") as f:
                json.dump(self._last_access, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save project meta: {e}")

    def _touch_project(self, project: str):
        """Update last access time for a project."""
        self._last_access[project] = time.time()
        self._save_meta()

    def _close_project_server(self, project: str):
        """Close and forget a cached per-project server before moving/deleting files."""
        project = _sanitize_project_name(project)
        srv = self._servers.pop(project, None)
        if srv is not None:
            srv.close()

    def close(self):
        """Close all cached project servers."""
        for project in list(self._servers):
            self._close_project_server(project)
        self._save_meta()

    def set_lifecycle_manager(self, manager: Optional[IdleLifecycleManager]) -> None:
        self.lifecycle_manager = manager

    def _mark_lifecycle_activity(self, label: str) -> None:
        if self.lifecycle_manager is not None:
            self.lifecycle_manager.mark_activity(label=label)

    def _mark_active_mcp_tool(self, *, call_id: str, tool: str, project: str, timeout_seconds: float) -> None:
        if self.lifecycle_manager is None:
            return
        now = time.time()
        stuck_seconds = max(
            timeout_seconds + 5.0,
            _read_float_env("MEMORIA_MCP_TOOL_STUCK_EXIT_SECONDS", DEFAULT_TOOL_STUCK_EXIT_SECONDS),
        )
        item = {
            "call_id": call_id,
            "tool": str(tool or ""),
            "project": str(project or ""),
            "started_at": now,
            "started_label": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
            "timeout_seconds": round(float(timeout_seconds or 0.0), 3),
            "deadline_at": now + stuck_seconds if stuck_seconds > 0 else 0,
            "stuck_exit_seconds": round(stuck_seconds, 3),
        }
        with self._active_mcp_tools_lock:
            self._active_mcp_tools[call_id] = item
            active_tools = dict(self._active_mcp_tools)
        self.lifecycle_manager.registry.heartbeat(
            status="active",
            active_tools=active_tools,
            active_tool=item["tool"],
            active_tool_project=item["project"],
            active_tool_started_at=item["started_at"],
            active_tool_started_label=item["started_label"],
            active_tool_timeout_seconds=item["timeout_seconds"],
            active_tool_deadline_at=item["deadline_at"],
            active_tool_stuck_exit_seconds=item["stuck_exit_seconds"],
        )

    def _clear_active_mcp_tool(self, call_id: str) -> None:
        if self.lifecycle_manager is None:
            return
        with self._active_mcp_tools_lock:
            self._active_mcp_tools.pop(call_id, None)
            active_tools = dict(self._active_mcp_tools)
            latest = next(reversed(active_tools.values()), None) if active_tools else None
        self.lifecycle_manager.registry.heartbeat(
            status="active",
            active_tools=active_tools,
            active_tool=(latest or {}).get("tool", ""),
            active_tool_project=(latest or {}).get("project", ""),
            active_tool_started_at=(latest or {}).get("started_at", 0),
            active_tool_started_label=(latest or {}).get("started_label", ""),
            active_tool_timeout_seconds=(latest or {}).get("timeout_seconds", 0),
            active_tool_deadline_at=(latest or {}).get("deadline_at", 0),
            active_tool_stuck_exit_seconds=(latest or {}).get("stuck_exit_seconds", 0),
        )

    def sleep_cached_state(self) -> None:
        """Flush and unload cached per-project servers while keeping MCP alive."""
        for project in list(self._servers):
            self._close_project_server(project)
        self._save_meta()

    # ========== 过期归档 ==========

    def _check_expired_projects(self):
        """Move projects unused for expiry_days to _archived/."""
        now = time.time()
        cutoff = now - self.expiry_days * 86400
        os.makedirs(self.archive_dir, exist_ok=True)

        # 扫描活跃项目目录
        if not os.path.isdir(self.base_data_dir):
            return
        for name in os.listdir(self.base_data_dir):
            project_dir = os.path.join(self.base_data_dir, name)
            if not os.path.isdir(project_dir):
                continue
            if name.startswith("_"):  # 跳过 _archived, _project_meta.json 等
                continue

            last = self._last_access.get(name, 0)
            # 如果没有记录，用目录修改时间
            if last == 0:
                try:
                    last = os.path.getmtime(project_dir)
                except Exception:
                    continue

            if last < cutoff:
                # 归档：移动到 _archived/
                dest = os.path.join(self.archive_dir, name)
                if os.path.exists(dest):
                    logger.warning(f"Skip auto-archive for '{name}': archived project already exists")
                    continue
                self._close_project_server(name)
                shutil.move(project_dir, dest)
                logger.info(f"Archived expired project '{name}' (unused since {time.strftime('%Y-%m-%d', time.localtime(last))})")
                # 从活跃记录中移除（保留归档时间戳）
                self._last_access[f"_archived:{name}"] = now
                self._last_access.pop(name, None)

        # 清理过期归档：默认关闭；需要时通过 purge_days 显式开启。
        purge_cutoff = now - self.purge_days * 86400
        if self.purge_days > 0 and os.path.isdir(self.archive_dir):
            for name in os.listdir(self.archive_dir):
                archived_path = os.path.join(self.archive_dir, name)
                if not os.path.isdir(archived_path):
                    continue
                archived_at = self._last_access.get(f"_archived:{name}", 0)
                if archived_at > 0 and archived_at < purge_cutoff:
                    shutil.rmtree(archived_path)
                    self._last_access.pop(f"_archived:{name}", None)
                    logger.info(f"Purged archived project '{name}' (archived {int((now - archived_at) / 86400)} days ago)")

        self._save_meta()

    def _restore_if_archived(self, project: str) -> bool:
        """If project is in _archived/, restore it to active."""
        archived_path = os.path.join(self.archive_dir, project)
        if not os.path.isdir(archived_path):
            return False
        active_path = os.path.join(self.base_data_dir, project)
        if os.path.exists(active_path):
            # 活跃目录已存在，不需要恢复
            return False
        shutil.move(archived_path, active_path)
        # 清理归档标记
        self._last_access.pop(f"_archived:{project}", None)
        logger.info(f"Restored archived project '{project}'")
        return True

    # ========== 服务器实例管理 ==========

    def _get_server(self, project: str) -> RippleMemoryServer:
        """Get or create a per-project server instance."""
        project = _sanitize_project_name(project)
        self._touch_project(project)

        # 如果在归档中，先恢复
        self._restore_if_archived(project)

        if project not in self._servers:
            project_dir = os.path.join(self.base_data_dir, project)
            os.makedirs(project_dir, exist_ok=True)
            config = MemoriaConfig()
            config.db_path = os.path.join(project_dir, "memoria.db")
            server_instance = RippleMemoryServer(config)
            self._servers[project] = server_instance
            logger.info(f"Created memory server for project '{project}': {config.db_path}")
        else:
            self._servers[project].reload_graph_if_newer()
        return self._servers[project]

    def _commit_queued_remember(self, project: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        project = _sanitize_project_name(project)
        clean_args = dict(arguments or {})
        clean_args["project"] = project
        clean_args.pop("_ripple_mcp_call_id", None)
        clean_args.pop("_ripple_write_queue_pending_id", None)
        clean_args.pop("_ripple_write_queue_enqueued_at", None)

        # A queued commit must start from the latest graph_state on disk. This
        # prevents an old per-process graph cache from overwriting newer writes.
        self._close_project_server(project)
        srv = self._get_server(project)
        return srv._tool_remember(clean_args)

    def _flush_write_queue(self, project: str, *, budget_seconds: Optional[float] = None) -> Dict[str, Any]:
        project = _sanitize_project_name(project)
        queue_store = ProjectWriteQueue(self.base_data_dir, project)
        return queue_store.process_ready(
            lambda queued_args: self._commit_queued_remember(project, queued_args),
            budget_seconds=_write_queue_drain_budget_seconds() if budget_seconds is None else budget_seconds,
        )

    def _remember_queue_committed_payload(
        self,
        pending_id: str,
        result: Dict[str, Any],
        *,
        recovered_after_soft_timeout: bool = False,
        recovery: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = dict(result or {})
        payload["accepted"] = True
        payload["commit_state"] = "committed"
        payload["pending_id"] = pending_id
        payload["queued"] = False
        payload["write_queue"] = {
            "state": "committed",
            "pending_id": pending_id,
            "single_writer": True,
        }
        if recovered_after_soft_timeout:
            payload["recovered_after_soft_timeout"] = True
            payload["recovery"] = recovery or {
                "kind": "write_queue_committed_result",
                "retry_performed": False,
            }
        return payload

    def _remember_queue_failed_payload(self, pending_id: str, failed: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "stored": False,
            "accepted": False,
            "queued": False,
            "commit_state": "failed",
            "pending_id": pending_id,
            "error": failed.get("error") or "queued remember failed",
            "write_queue": {
                "state": "failed",
                "pending_id": pending_id,
                "single_writer": True,
            },
        }

    def _remember_queue_pending_payload(
        self,
        project: str,
        pending_id: str,
        *,
        recovered_after_soft_timeout: bool = False,
        recovery_note: str = "",
    ) -> Dict[str, Any]:
        payload = {
            "stored": True,
            "accepted": True,
            "queued": True,
            "commit_state": "queued",
            "pending_id": pending_id,
            "project": project,
            "searchable": False,
            "warning": (
                "memory write is durably queued but not committed yet; it becomes "
                "searchable after the project writer drains the queue"
            ),
            "write_queue": {
                "state": "queued",
                "pending_id": pending_id,
                "single_writer": True,
            },
        }
        if recovered_after_soft_timeout:
            payload["recovered_after_soft_timeout"] = True
            payload["recovery"] = {
                "kind": "write_queue_pending_result",
                "retry_performed": False,
                "note": recovery_note or "Original remember call timed out after durable queue enqueue.",
            }
        return payload

    def _result_for_queued_remember(
        self,
        project: str,
        pending_id: str,
        *,
        wait_seconds: float,
    ) -> Optional[Dict[str, Any]]:
        queue_store = ProjectWriteQueue(self.base_data_dir, project)
        result = queue_store.wait_for_result(pending_id, timeout_seconds=wait_seconds)
        if result is None:
            return None
        if result.get("state") == "committed":
            return self._remember_queue_committed_payload(pending_id, dict(result.get("result") or {}))
        return self._remember_queue_failed_payload(pending_id, result)

    def _start_write_queue_worker(self, project: str) -> Dict[str, Any]:
        project = _sanitize_project_name(project)
        if not _write_queue_worker_enabled():
            return {"started": False, "reason": "disabled"}

        cmd = [
            sys.executable,
            "-m",
            "memoria_mcp.queue_worker",
            "--data-dir",
            self.base_data_dir,
            "--project",
            project,
            "--budget-seconds",
            str(_write_queue_drain_budget_seconds()),
        ]
        env = os.environ.copy()
        env["MEMORIA_MCP_DATA_DIR"] = self.base_data_dir
        flags = 0
        startupinfo = None
        if os.name == "nt":
            flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=os.getcwd(),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=flags,
                startupinfo=startupinfo,
            )
        except Exception as exc:  # noqa: BLE001 - queue durability must not depend on worker launch.
            logger.warning("Failed to start Ripple write queue worker for %s: %s", project, exc)
            return {"started": False, "error": f"{exc.__class__.__name__}: {exc}"}
        return {"started": True, "pid": proc.pid}

    def _dispatch_remember_with_write_queue(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        project = _sanitize_project_name(str((arguments or {}).get("project", "default")))
        clean_args = dict(arguments or {})
        clean_args["project"] = project
        clean_args.pop("_ripple_mcp_call_id", None)
        clean_args.pop("_ripple_write_queue_pending_id", None)
        clean_args.pop("_ripple_write_queue_enqueued_at", None)

        queue_store = ProjectWriteQueue(self.base_data_dir, project)
        pending_id = queue_store.enqueue(clean_args)
        if isinstance(arguments, dict):
            arguments["_ripple_write_queue_pending_id"] = pending_id
            arguments["_ripple_write_queue_enqueued_at"] = time.time()
        self._record_runtime_event(
            "write_queue_enqueued",
            project=project,
            pending_id=pending_id,
        )

        worker = self._start_write_queue_worker(project)
        self._record_runtime_event(
            "write_queue_worker",
            project=project,
            pending_id=pending_id,
            **worker,
        )
        self._record_runtime_event(
            "write_queue_queued_return",
            project=project,
            pending_id=pending_id,
            queue_counts=queue_store.counts(),
        )
        payload = self._remember_queue_pending_payload(project, pending_id)
        payload["write_queue"]["worker"] = worker
        return payload

    # ========== 工具注册 ==========

    def _register_tools(self):
        server = self.server

        @server.list_tools()
        async def list_tools() -> list[Tool]:
            return build_memory_tools(expose_project_tools=self.expose_project_tools)

        @server.call_tool()
        async def call_tool(name: str, arguments: dict) -> list[TextContent]:
            try:
                result = await self._dispatch_tool_for_mcp_async(name, arguments)
                return [TextContent(type="text", text=_json_text_for_mcp(result, indent=2))]
            except Exception as e:
                logger.error(f"Tool {name} failed: {e}")
                return [TextContent(type="text", text=_json_text_for_mcp({"error": str(e)}))]

    async def _dispatch_tool_for_mcp_async(self, name: str, arguments: dict) -> Any:
        """Async MCP entry guard so a stuck handler cannot hit Codex's hard timeout."""
        safe_args = dict(arguments or {})
        call_id = f"{os.getpid()}-{threading.get_ident()}-{time.perf_counter_ns()}"
        safe_args["_ripple_mcp_call_id"] = call_id
        timeout_seconds = _mcp_handler_timeout_seconds(name)
        started = time.perf_counter()
        project = str(safe_args.get("project", "") or "")
        self._record_runtime_event(
            "mcp_handler_start",
            call_id=call_id,
            tool=name,
            project=project,
            timeout_seconds=timeout_seconds,
        )
        self._mark_active_mcp_tool(call_id=call_id, tool=name, project=project, timeout_seconds=timeout_seconds)
        try:
            if name == "memoria_remember" and _write_queue_enabled():
                result = self._dispatch_tool_for_mcp(name, safe_args)
                self._record_runtime_event(
                    "mcp_handler_complete",
                    call_id=call_id,
                    tool=name,
                    project=project,
                    elapsed_seconds=round(time.perf_counter() - started, 4),
                    timeout_enabled=False,
                    queue_first=True,
                )
                return result
            if timeout_seconds <= 0:
                result = await asyncio.to_thread(self._dispatch_tool_for_mcp, name, safe_args)
                self._record_runtime_event(
                    "mcp_handler_complete",
                    call_id=call_id,
                    tool=name,
                    project=project,
                    elapsed_seconds=round(time.perf_counter() - started, 4),
                    timeout_enabled=False,
                )
                return result

            completed, result = await _run_sync_with_async_timeout(
                label=f"ripple-memory-mcp-handler-{name}",
                timeout_seconds=timeout_seconds,
                runner=lambda: self._dispatch_tool_for_mcp(name, safe_args),
            )
            if completed:
                self._record_runtime_event(
                    "mcp_handler_complete",
                    call_id=call_id,
                    tool=name,
                    project=project,
                    elapsed_seconds=round(time.perf_counter() - started, 4),
                    timeout_enabled=True,
                )
                return result

            try:
                soft_timeout = _tool_soft_timeout_seconds(name) or timeout_seconds
                self._record_runtime_event(
                    "mcp_handler_timeout",
                    call_id=call_id,
                    tool=name,
                    project=project,
                    elapsed_seconds=round(time.perf_counter() - started, 4),
                    timeout_seconds=timeout_seconds,
                    soft_timeout_seconds=soft_timeout,
                )
                logger.warning(
                    "MCP handler for %s exceeded %.2fs; attempting outer fail-open recovery",
                    name,
                    timeout_seconds,
                )
                if name == "memoria_remember" and _write_queue_enabled():
                    recovered = await self._recover_remember_after_handler_timeout(
                        safe_args,
                        soft_timeout,
                    )
                else:
                    recovered = await self._recover_tool_after_handler_timeout(name, safe_args, soft_timeout)
                self._record_runtime_event(
                    "mcp_handler_recovery_complete",
                    call_id=call_id,
                    tool=name,
                    project=project,
                    elapsed_seconds=round(time.perf_counter() - started, 4),
                    recovered=bool(recovered),
                    recovered_after_soft_timeout=bool((recovered or {}).get("recovered_after_soft_timeout"))
                    if isinstance(recovered, dict) else False,
                )
                if recovered is not None:
                    return recovered
                return _soft_timeout_payload(
                    name,
                    soft_timeout,
                    f"mcp handler exceeded outer timeout {timeout_seconds:.2f}s",
                )
            except Exception as exc:  # noqa: BLE001 - never let recovery errors hit Codex's hard timeout.
                logger.warning(f"MCP handler outer timeout recovery for {name} failed: {exc}")
                return _soft_timeout_payload(name, timeout_seconds, f"outer timeout recovery failed: {exc}")
        finally:
            self._clear_active_mcp_tool(call_id)

    async def _recover_tool_after_handler_timeout(
        self,
        name: str,
        arguments: Dict[str, Any],
        timeout_seconds: float,
    ) -> Optional[Dict[str, Any]]:
        recovery_timeout = _tool_recovery_timeout_seconds()
        try:
            completed, result = await _run_sync_with_async_timeout(
                label=f"ripple-memory-mcp-recovery-{name}",
                timeout_seconds=recovery_timeout,
                runner=lambda: self._recover_tool_after_soft_timeout(name, arguments, timeout_seconds),
            )
            if completed:
                return result
            logger.warning(
                "Outer fail-open recovery for %s exceeded %.2fs",
                name,
                recovery_timeout,
            )
            return _soft_timeout_payload(
                name,
                timeout_seconds,
                f"outer recovery exceeded {recovery_timeout:.2f}s",
            )
        except Exception as exc:  # noqa: BLE001 - recovery should fail open to the agent.
            logger.warning(f"Outer fail-open recovery for {name} failed: {exc}")
            return _soft_timeout_payload(name, timeout_seconds, f"outer recovery failed: {exc}")

    async def _recover_remember_after_handler_timeout(
        self,
        arguments: Dict[str, Any],
        timeout_seconds: float,
    ) -> Dict[str, Any]:
        project = _sanitize_project_name(str(arguments.get("project", "default")))
        deadline = time.monotonic() + min(0.15, _tool_recovery_timeout_seconds())
        pending_id = str(arguments.get("_ripple_write_queue_pending_id") or "").strip()
        while not pending_id and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
            pending_id = str(arguments.get("_ripple_write_queue_pending_id") or "").strip()

        if pending_id:
            self._record_runtime_event(
                "mcp_handler_remember_fail_open",
                tool="memoria_remember",
                project=project,
                pending_id=pending_id,
                committed_result_checked=False,
            )
            return self._remember_queue_pending_payload(
                project,
                pending_id,
                recovered_after_soft_timeout=True,
                recovery_note=(
                    "memoria_remember exceeded the outer MCP handler timeout after durable "
                    "queue enqueue; Ripple returned the queued result without retrying or "
                    "running a second recovery pass."
                ),
            )

        self._record_runtime_event(
            "mcp_handler_remember_fail_open",
            tool="memoria_remember",
            project=project,
            pending_id="",
            committed_result_checked=False,
        )
        return _soft_timeout_payload(
            "memoria_remember",
            timeout_seconds,
            (
                "memoria_remember exceeded the outer MCP handler timeout before a durable "
                "queue enqueue was observable; no retry was attempted."
            ),
        )

    def _dispatch_tool_for_mcp(self, name: str, arguments: dict) -> Any:
        self._mark_lifecycle_activity(f"mcp:{name}")
        timeout_seconds = _tool_soft_timeout_seconds(name)
        started = time.perf_counter()
        call_id = str((arguments or {}).get("_ripple_mcp_call_id") or "")
        project = str((arguments or {}).get("project", "") or "")
        self._record_runtime_event(
            "tool_guard_start",
            call_id=call_id,
            tool=name,
            project=project,
            timeout_seconds=timeout_seconds,
        )
        if name == "memoria_remember" and _write_queue_enabled():
            result = self._dispatch_tool(name, arguments)
            self._record_runtime_event(
                "tool_guard_complete",
                call_id=call_id,
                tool=name,
                project=project,
                elapsed_seconds=round(time.perf_counter() - started, 4),
                timeout_enabled=False,
                queue_first=True,
            )
            return result
        if timeout_seconds <= 0:
            result = self._dispatch_tool(name, arguments)
            self._record_runtime_event(
                "tool_guard_complete",
                call_id=call_id,
                tool=name,
                project=project,
                elapsed_seconds=round(time.perf_counter() - started, 4),
                timeout_enabled=False,
            )
            return result

        completed, result = _run_sync_with_timeout(
            label=f"ripple-memory-tool-{name}",
            timeout_seconds=timeout_seconds,
            runner=lambda: self._dispatch_tool(name, arguments),
        )
        if completed:
            self._record_runtime_event(
                "tool_guard_complete",
                call_id=call_id,
                tool=name,
                project=project,
                elapsed_seconds=round(time.perf_counter() - started, 4),
                timeout_enabled=True,
            )
            return result

        self._record_runtime_event(
            "tool_guard_timeout",
            call_id=call_id,
            tool=name,
            project=project,
            elapsed_seconds=round(time.perf_counter() - started, 4),
            timeout_seconds=timeout_seconds,
        )
        logger.warning(
            "Tool %s exceeded soft timeout %.2fs; attempting safe recovery",
            name,
            timeout_seconds,
        )
        recovered = self._recover_tool_after_soft_timeout(name, arguments, timeout_seconds)
        self._record_runtime_event(
            "tool_guard_recovery_complete",
            call_id=call_id,
            tool=name,
            project=project,
            elapsed_seconds=round(time.perf_counter() - started, 4),
            recovered=bool(recovered),
            recovered_after_soft_timeout=bool((recovered or {}).get("recovered_after_soft_timeout"))
            if isinstance(recovered, dict) else False,
        )
        if recovered is not None:
            return recovered
        return _soft_timeout_payload(name, timeout_seconds, "no safe recovery path matched")

    def _dispatch_tool(self, name: str, arguments: dict) -> Any:
        self._mark_lifecycle_activity(f"dispatch:{name}")
        arguments = dict(arguments or {})
        arguments.pop("_ripple_mcp_call_id", None)
        project_tool_names = {"memoria_list_projects", "memoria_archive_project"}
        if name in project_tool_names and not self.expose_project_tools:
            return {
                "error": "Project management tools are hidden by default.",
                "enable_env": "MEMORIA_MCP_EXPOSE_PROJECT_TOOLS=true",
            }
        if name == "memoria_list_projects":
            return self._tool_list_projects()
        if name == "memoria_archive_project":
            return self._tool_archive_project(arguments)
        if name == "memoria_forget" and str(arguments.get("scope") or "memory").strip().lower() == "project":
            return self._tool_delete_project(arguments)

        project = arguments.get("project", "default")
        if name == "memoria_remember" and _write_queue_enabled():
            return self._dispatch_remember_with_write_queue(arguments)
        srv = self._get_server(project)
        if name == "memoria_remember":
            return srv._tool_remember(arguments)
        if name == "memoria_recall":
            return srv._tool_recall(arguments)
        if name == "memoria_read":
            return srv._tool_read(arguments)
        if name == "memoria_forget":
            return srv._tool_forget(arguments)
        return {"error": f"Unknown tool: {name}"}

    def _recover_tool_after_soft_timeout(
        self,
        name: str,
        arguments: Dict[str, Any],
        timeout_seconds: float,
    ) -> Optional[Dict[str, Any]]:
        if not _read_bool_env("MEMORIA_MCP_TOOL_SOFT_TIMEOUT_RECOVERY", True):
            return None

        if name == "memoria_remember":
            if _write_queue_enabled():
                queued_recovery = self._recover_queued_remember_after_soft_timeout(arguments, timeout_seconds)
                if queued_recovery is not None:
                    return queued_recovery
            project = _sanitize_project_name(str(arguments.get("project", "default")))
            srv = self._servers.get(project)
            if srv is None:
                return None
            return srv.recover_remember_after_soft_timeout(arguments, timeout_seconds)

        if name in {"memoria_recall", "memoria_read"}:
            return self._retry_readonly_after_soft_timeout(name, arguments, timeout_seconds)

        # Forget/archive/delete style tools are intentionally not retried because
        # they are destructive or move project state.
        return None

    def _recover_queued_remember_after_soft_timeout(
        self,
        arguments: Dict[str, Any],
        timeout_seconds: float,
    ) -> Optional[Dict[str, Any]]:
        pending_id = str(arguments.get("_ripple_write_queue_pending_id") or "").strip()
        if not pending_id:
            return None
        project = _sanitize_project_name(str(arguments.get("project", "default")))
        recovery_timeout = _tool_recovery_timeout_seconds()
        queue_store = ProjectWriteQueue(self.base_data_dir, project)

        result = queue_store.wait_for_result(pending_id, timeout_seconds=min(0.05, recovery_timeout))
        recovery = {
            "kind": "write_queue_recovery",
            "soft_timeout_seconds": timeout_seconds,
            "recovery_timeout_seconds": recovery_timeout,
            "retry_performed": False,
        }
        if result is None:
            return self._remember_queue_pending_payload(
                project,
                pending_id,
                recovered_after_soft_timeout=True,
                recovery_note=(
                    "Original memoria_remember exceeded the soft timeout after durable "
                    "queue enqueue; no duplicate write was attempted."
                ),
            )
        if result.get("state") == "committed":
            recovery["kind"] = "write_queue_committed_result"
            return self._remember_queue_committed_payload(
                pending_id,
                dict(result.get("result") or {}),
                recovered_after_soft_timeout=True,
                recovery=recovery,
            )
        failed = self._remember_queue_failed_payload(pending_id, result)
        failed["recovered_after_soft_timeout"] = True
        failed["recovery"] = recovery
        return failed

    def _retry_readonly_after_soft_timeout(
        self,
        name: str,
        arguments: Dict[str, Any],
        timeout_seconds: float,
    ) -> Optional[Dict[str, Any]]:
        if not _read_bool_env("MEMORIA_MCP_READONLY_SOFT_TIMEOUT_RETRY", True):
            return None

        recovery_timeout = _tool_recovery_timeout_seconds()

        def runner() -> Any:
            retry_router = MemoriaRouter(
                self.base_data_dir,
                expiry_days=self.expiry_days,
                purge_days=self.purge_days,
            )
            try:
                return retry_router._dispatch_tool(name, dict(arguments))
            finally:
                retry_router.close()

        try:
            completed, result = _run_sync_with_timeout(
                label=f"ripple-memory-retry-{name}",
                timeout_seconds=recovery_timeout,
                runner=runner,
            )
        except Exception as exc:
            logger.warning(f"Soft-timeout read-only retry for {name} failed: {exc}")
            return _soft_timeout_payload(name, timeout_seconds, f"read-only retry failed: {exc}")

        if not completed:
            logger.warning(
                "Soft-timeout read-only retry for %s exceeded %.2fs",
                name,
                recovery_timeout,
            )
            return _soft_timeout_payload(
                name,
                timeout_seconds,
                f"read-only retry exceeded {recovery_timeout:.2f}s",
            )

        if isinstance(result, dict):
            result = dict(result)
            result["recovered_after_soft_timeout"] = True
            result["recovery"] = {
                "kind": "readonly_retry_result",
                "soft_timeout_seconds": timeout_seconds,
                "recovery_timeout_seconds": recovery_timeout,
                "retry_performed": True,
                "note": (
                    f"Original {name} exceeded the soft timeout. Ripple Memory "
                    "retried this read-only tool once through a fresh router."
                ),
            }
        return result

    def _tool_archive_project(self, args: dict) -> dict:
        """Archive an active project on explicit user request."""
        project = _sanitize_project_name(str(args.get("project", "default")))
        active_path = os.path.join(self.base_data_dir, project)
        archived_path = os.path.join(self.archive_dir, project)

        if os.path.isdir(archived_path) and not os.path.isdir(active_path):
            return {
                "archived": True,
                "project": project,
                "status": "already_archived",
                "path": archived_path,
            }
        if os.path.isdir(archived_path):
            return {
                "archived": False,
                "project": project,
                "status": "archived_project_already_exists",
                "path": archived_path,
            }
        if not os.path.isdir(active_path):
            return {
                "archived": False,
                "project": project,
                "status": "not_found",
            }

        self._close_project_server(project)
        os.makedirs(self.archive_dir, exist_ok=True)
        shutil.move(active_path, archived_path)

        now = time.time()
        self._last_access[f"_archived:{project}"] = now
        self._last_access.pop(project, None)
        self._save_meta()
        return {
            "archived": True,
            "project": project,
            "status": "archived",
            "path": archived_path,
        }

    def _tool_delete_project(self, args: dict) -> dict:
        """Permanently delete an active and/or archived project after confirmation."""
        project = _sanitize_project_name(str(args.get("project", "default")))
        expected_confirm = f"{PROJECT_DELETE_CONFIRM_PREFIX}{project}"
        confirm = str(args.get("confirm", "")).strip()
        if confirm != expected_confirm:
            return {
                "deleted": False,
                "project": project,
                "status": "confirmation_required",
                "expected_confirm": expected_confirm,
            }

        self._close_project_server(project)
        deleted_locations = []
        active_path = os.path.join(self.base_data_dir, project)
        archived_path = os.path.join(self.archive_dir, project)

        if os.path.isdir(active_path):
            shutil.rmtree(active_path)
            deleted_locations.append("active")
        if os.path.isdir(archived_path):
            shutil.rmtree(archived_path)
            deleted_locations.append("archived")

        self._last_access.pop(project, None)
        self._last_access.pop(f"_archived:{project}", None)
        self._save_meta()
        return {
            "deleted": bool(deleted_locations),
            "project": project,
            "status": "deleted" if deleted_locations else "not_found",
            "deleted_locations": deleted_locations,
        }

    def _tool_list_projects(self) -> dict:
        """List all projects with metadata."""
        projects = []
        now = time.time()

        # 活跃项目
        if os.path.isdir(self.base_data_dir):
            for name in sorted(os.listdir(self.base_data_dir)):
                project_dir = os.path.join(self.base_data_dir, name)
                if not os.path.isdir(project_dir) or name.startswith("_"):
                    continue
                db_path = os.path.join(project_dir, "memoria.db")
                size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
                last = self._last_access.get(name, 0)
                days_ago = int((now - last) / 86400) if last > 0 else -1
                projects.append({
                    "project": name,
                    "status": "active",
                    "size_kb": round(size / 1024, 1),
                    "last_access_days_ago": days_ago,
                    "expires_in_days": max(0, self.expiry_days - days_ago) if days_ago >= 0 else "unknown",
                })

        # 归档项目
        if os.path.isdir(self.archive_dir):
            for name in sorted(os.listdir(self.archive_dir)):
                archived_dir = os.path.join(self.archive_dir, name)
                if not os.path.isdir(archived_dir):
                    continue
                db_path = os.path.join(archived_dir, "memoria.db")
                size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
                archived_at = self._last_access.get(f"_archived:{name}", 0)
                projects.append({
                    "project": name,
                    "status": "archived",
                    "size_kb": round(size / 1024, 1),
                    "archived_at": time.strftime("%Y-%m-%d", time.localtime(archived_at)) if archived_at else "unknown",
                })

        return {
            "total": len(projects),
            "expiry_days": self.expiry_days,
            "purge_days": self.purge_days,
            "auto_purge_enabled": self.purge_days > 0,
            "delete_confirm_prefix": PROJECT_DELETE_CONFIRM_PREFIX,
            "projects": projects,
        }


def _preload_embedding_model_if_requested():
    if not _read_bool_env("MEMORIA_MCP_PRELOAD_EMBEDDING", False):
        return
    config = _apply_runtime_env_overrides(MemoriaConfig())
    if not config.enable_semantic:
        return
    try:
        from .graph import _get_embedding_model
        start = time.time()
        model = _get_embedding_model(config.embedding_model)
        logger.info(
            "Embedding preload %s (%.2fs)",
            "ready" if model is not None else "unavailable",
            time.time() - start,
        )
    except Exception as e:
        logger.warning(f"Embedding preload failed: {e}")


def _runtime_change_exit_guard(source_path: str, startup_mtime: float) -> Callable[[], Optional[Dict[str, Any]]]:
    def check() -> Optional[Dict[str, Any]]:
        if not _read_bool_env("MEMORIA_MCP_EXIT_ON_RUNTIME_CHANGE", False):
            return None
        try:
            current_mtime = os.path.getmtime(source_path)
        except OSError:
            return None
        if current_mtime > startup_mtime + 1e-6:
            return {
                "status": "runtime_changed",
                "runtime_source": source_path,
                "startup_runtime_source_mtime": startup_mtime,
                "current_runtime_source_mtime": current_mtime,
            }
        return None

    return check


async def main():
    from .mcp_stdio_proxy import main as proxy_main

    await proxy_main()


def run():
    import asyncio
    asyncio.run(main())


if __name__ == "__main__":
    run()
