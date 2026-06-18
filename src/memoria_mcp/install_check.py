"""Post-install functional check for Ripple Memory.

The check is intentionally practical: it verifies the installed runtime can use
MCP tools, project isolation, SQLite/JSONL dual-rail storage, window-local
latches, hook entry points, semantic configuration, and skill guidance.
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import importlib.util
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ListToolsRequest

from .config import DEFAULT_EMBEDDING_MODEL_DIR, MemoriaConfig
from .context_cli import _default_data_dir
from .graph import _check_embedding_available
from .hook_core import RippleHookEvent, handle_hook_event
from .lifecycle import (
    DEFAULT_IDLE_EXIT_SECONDS,
    IdleLifecycleManager,
    ProcessRegistry,
    is_process_alive,
    window_latch_file,
)
from .bm25 import tokenize_retrieval_text
from . import daemon_client
from .daemon_client import call_daemon_tool, ensure_agent_daemon, port_file_path, shutdown_daemon
from .server import MemoriaRouter, _apply_runtime_env_overrides, _json_text_for_mcp, _sanitize_project_name
from .tool_specs import EXPECTED_CORE_TOOLS as CANONICAL_CORE_TOOLS
from .write_queue import ProjectWriteQueue


EXPECTED_CORE_TOOLS = list(CANONICAL_CORE_TOOLS)

SKILL_CUES = [
    "继续",
    "接着",
    "上次",
    "之前",
    "刚才",
    "你记得吗",
    "还记得",
    "按原方案",
    "按刚才的方案",
    "别又忘了",
    "不要再",
    "上下文没了",
]
SKILL_TRIGGER_CATEGORIES = [
    "Continuation / resume",
    "Prior work / earlier turns",
    "Memory questions",
    "Existing plan / decision reuse",
    "Corrections / preferences / avoid repeat",
    "Compression / lost context",
]
SKILL_EVOLUTION_TERMS = [
    "fact_key",
    "supersedes_ref_ids",
    "include_evolution",
    "pending_conflict",
]
SKILL_RECALL_DISCIPLINE_TERMS = [
    "Recall Discipline",
    "Chinese terms plus English technical aliases",
    "Original memory text",
    "High-score recall results must be read",
    "json_file`/`json_offset",
]
SKILL_TOOL_NAMES = EXPECTED_CORE_TOOLS


@dataclass
class CheckResult:
    name: str
    ok: bool
    status: str = "pass"
    details: Dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @classmethod
    def pass_(cls, name: str, **details: Any) -> "CheckResult":
        return cls(name=name, ok=True, status="pass", details=details)

    @classmethod
    def fail(cls, name: str, error: str, **details: Any) -> "CheckResult":
        return cls(name=name, ok=False, status="fail", error=error, details=details)

    @classmethod
    def skip(cls, name: str, reason: str, **details: Any) -> "CheckResult":
        return cls(name=name, ok=True, status="skip", error=reason, details=details)


@contextmanager
def _patched_env(updates: Dict[str, Optional[str]]) -> Iterator[None]:
    old = os.environ.copy()
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        os.environ.clear()
        os.environ.update(old)


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    return str(value)


async def _list_tool_names(router: MemoriaRouter) -> List[str]:
    handler = router.server.request_handlers[ListToolsRequest]
    result = await handler(ListToolsRequest())
    return [tool.name for tool in result.root.tools]


def _repo_root_from_module() -> Path:
    return Path(__file__).resolve().parents[2]


def _candidate_skill_paths(host: str) -> Iterable[Path]:
    home = Path.home()
    host = (host or "auto").lower()
    if host in {"mimo", "mimo-code", "mimocode"}:
        mimo_home = os.environ.get("MIMOCODE_HOME")
        if mimo_home:
            yield Path(mimo_home).expanduser() / "skills" / "ripple-memory" / "SKILL.md"
    if host in {"generic", "custom"}:
        generic_home = os.environ.get("RIPPLE_MEMORY_HOST_HOME")
        if generic_home:
            yield Path(generic_home).expanduser() / "skills" / "ripple-memory" / "SKILL.md"
    if host in {"auto", "codex"}:
        codex_home = Path(os.environ.get("CODEX_HOME") or (home / ".codex"))
        yield codex_home / "skills" / "ripple-memory" / "SKILL.md"
    if host in {"auto", "claude", "claude-code"}:
        claude_home = Path(os.environ.get("CLAUDE_HOME") or (home / ".claude"))
        yield claude_home / "skills" / "ripple-memory" / "SKILL.md"
    if host in {"auto", "qwen", "qwen-code"}:
        qwen_home = Path(os.environ.get("QWEN_HOME") or (home / ".qwen"))
        yield qwen_home / "skills" / "ripple-memory" / "SKILL.md"
    yield home / ".agents" / "skills" / "ripple-memory" / "SKILL.md"
    yield _repo_root_from_module() / "skills" / "ripple-memory" / "SKILL.md"


def _find_skill_path(host: str, explicit: Optional[str]) -> Optional[Path]:
    if explicit:
        return Path(explicit).expanduser()
    for path in _candidate_skill_paths(host):
        if path.is_file():
            return path
    return None


def _default_hook_cmd(host: str) -> Optional[Path]:
    home = Path.home()
    host = (host or "auto").lower()
    if host in {"codex", "auto"}:
        codex_home = Path(os.environ.get("CODEX_HOME") or (home / ".codex"))
        path = codex_home / "plugins" / "ripple-memory-hooks" / "scripts" / "ripple-memory-codex-hook.cmd"
        if path.is_file():
            return path
    if host in {"claude", "claude-code"}:
        path = Path(os.environ.get("CLAUDE_HOME") or (home / ".claude")) / "ripple-memory-hook.cmd"
        if path.is_file():
            return path
    if host in {"qwen", "qwen-code"}:
        path = Path(os.environ.get("QWEN_HOME") or (home / ".qwen")) / "ripple-memory-hook.cmd"
        if path.is_file():
            return path
    if host in {"mimo", "mimo-code", "mimocode"}:
        mimo_home = Path(os.environ.get("MIMOCODE_HOME") or (home / ".local" / "share" / "mimocode"))
        path = mimo_home / "mcp" / "ripple-memory-hooks" / "scripts" / "ripple-memory-mimocode-hook.cmd"
        if path.is_file():
            return path
    return None


def _make_marker(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{os.getpid()}"


def _delete_project_window_state(data_dir: Path, project: str) -> None:
    """Remove install-check window-local/runtime state for a temporary project."""
    project = _sanitize_project_name(project)
    for name in ("_window_state", "_window_archives"):
        path = data_dir / name / project
        try:
            resolved = path.resolve()
            root = data_dir.resolve()
            if str(resolved).lower().startswith(str(root).lower()) and resolved.exists():
                shutil.rmtree(resolved)
        except OSError:
            pass
    path = data_dir / "_runtime" / "write_queue" / project
    try:
        resolved = path.resolve()
        root = data_dir.resolve()
        if str(resolved).lower().startswith(str(root).lower()) and resolved.exists():
            shutil.rmtree(resolved)
    except OSError:
        pass


def _delete_project_with_retry(data_dir: Path, project: str, attempts: int = 12) -> Dict[str, Any]:
    """Delete a temporary test project, retrying transient Windows file locks."""
    project = _sanitize_project_name(project)
    last_error: Optional[BaseException] = None
    for attempt in range(attempts):
        gc.collect()
        if attempt:
            time.sleep(0.2 * attempt)
        router = MemoriaRouter(str(data_dir))
        try:
            result = router._dispatch_tool(
                "memoria_forget",
                {"project": project, "scope": "project", "confirm": f"DELETE:{project}"},
            )
            _delete_project_window_state(data_dir, project)
            return result
        except PermissionError as exc:
            last_error = exc
            gc.collect()
        finally:
            router.close()
    if last_error:
        raise last_error
    _delete_project_window_state(data_dir, project)
    return {"deleted": False, "project": project, "status": "not_found"}


def check_embedding_config(*, require_semantic: bool, require_local_model: bool) -> CheckResult:
    config = _apply_runtime_env_overrides(MemoriaConfig())
    dependency_available = _check_embedding_available()
    model_path = Path(config.embedding_model)
    model_exists = model_path.exists()
    preload = os.environ.get("MEMORIA_MCP_PRELOAD_EMBEDDING", "false")

    details = {
        "semantic_enabled": config.enable_semantic,
        "preload_env": preload,
        "embedding_model": str(model_path),
        "expected_model_dir": DEFAULT_EMBEDDING_MODEL_DIR,
        "model_name_matches_expected": model_path.name == DEFAULT_EMBEDDING_MODEL_DIR,
        "model_path_exists": model_exists,
        "sentence_transformers_importable": dependency_available,
    }
    if require_semantic and not config.enable_semantic:
        return CheckResult.fail("embedding_config", "MEMORIA_MCP_ENABLE_SEMANTIC is not enabled", **details)
    if require_semantic and not dependency_available:
        return CheckResult.fail("embedding_config", "sentence-transformers is not importable", **details)
    if require_local_model and not model_exists:
        return CheckResult.fail("embedding_config", "local embedding model path does not exist", **details)
    if require_local_model and model_path.name != DEFAULT_EMBEDDING_MODEL_DIR:
        return CheckResult.fail("embedding_config", "configured embedding model is not the current baseline", **details)
    return CheckResult.pass_("embedding_config", **details)


def check_skill(host: str, skill_path: Optional[str], *, require_skill: bool) -> CheckResult:
    path = _find_skill_path(host, skill_path)
    if not path or not path.is_file():
        if require_skill:
            return CheckResult.fail("skill_guidance", "ripple-memory skill was not found", searched_host=host)
        return CheckResult.skip("skill_guidance", "skill path was not found", searched_host=host)

    text = path.read_text(encoding="utf-8-sig", errors="replace")
    cue_hits = [cue for cue in SKILL_CUES if cue in text]
    tool_hits = [name for name in SKILL_TOOL_NAMES if name in text]
    category_hits = [name for name in SKILL_TRIGGER_CATEGORIES if name in text]
    evolution_hits = [name for name in SKILL_EVOLUTION_TERMS if name in text]
    recall_discipline_hits = [name for name in SKILL_RECALL_DISCIPLINE_TERMS if name in text]
    has_frontmatter = "name: ripple-memory" in text[:300]
    has_latch_guidance = "Original Words Latch" in text
    details = {
        "path": str(path),
        "frontmatter": has_frontmatter,
        "tool_hits": tool_hits,
        "chinese_cue_hits": cue_hits,
        "trigger_category_hits": category_hits,
        "memory_evolution_hits": evolution_hits,
        "recall_discipline_hits": recall_discipline_hits,
        "has_original_words_latch_guidance": has_latch_guidance,
    }
    if not has_frontmatter:
        return CheckResult.fail("skill_guidance", "skill frontmatter is missing name: ripple-memory", **details)
    if set(tool_hits) != set(SKILL_TOOL_NAMES):
        return CheckResult.fail("skill_guidance", "skill does not mention all four MCP tools", **details)
    if len(cue_hits) < len(SKILL_CUES):
        return CheckResult.fail("skill_guidance", "skill is missing Chinese continuation cues", **details)
    if len(category_hits) < len(SKILL_TRIGGER_CATEGORIES):
        return CheckResult.fail("skill_guidance", "skill is missing semantic trigger categories", **details)
    if len(evolution_hits) < len(SKILL_EVOLUTION_TERMS):
        return CheckResult.fail("skill_guidance", "skill is missing memory evolution guidance", **details)
    if len(recall_discipline_hits) < len(SKILL_RECALL_DISCIPLINE_TERMS):
        return CheckResult.fail("skill_guidance", "skill is missing recall discipline guidance", **details)
    if not has_latch_guidance:
        return CheckResult.fail("skill_guidance", "skill is missing Original Words Latch guidance", **details)
    return CheckResult.pass_("skill_guidance", **details)


def _inspect_project_db(project_dir: Path, node_id: str, marker: str) -> Dict[str, Any]:
    db_path = project_dir / "memoria.db"
    if not db_path.is_file():
        raise AssertionError(f"memoria.db not found at {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        graph_row = conn.execute("SELECT state_json FROM graph_state WHERE id = 1").fetchone()
        graph_has_marker = bool(graph_row and marker in str(graph_row[0]))
        index_row = conn.execute(
            "SELECT node_id, deleted, json_file, json_offset FROM search_index WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        edge_table_exists = "memory_evolution_edges" in tables
    finally:
        conn.close()

    required = {"graph_state", "search_index", "memory_evolution_state", "memory_evolution_edges"}
    obsolete = {"memory_stream", "archive_blocks"}
    jsonl_has_marker = False
    stream_root = project_dir / "archives" / "streams"
    for path in stream_root.rglob("*.jsonl") if stream_root.exists() else []:
        try:
            if marker in path.read_text(encoding="utf-8", errors="replace"):
                jsonl_has_marker = True
                break
        except OSError:
            continue
    return {
        "db_path": str(db_path),
        "tables_present": sorted(tables & required),
        "obsolete_tables_present": sorted(tables & obsolete),
        "required_tables_present": required.issubset(tables),
        "graph_state_contains_marker": graph_has_marker,
        "jsonl_stream_contains_marker": jsonl_has_marker,
        "memory_evolution_edges_table_exists": edge_table_exists,
        "search_index_row": {
            "node_id": index_row[0],
            "deleted": int(index_row[1]),
            "json_file": index_row[2],
            "json_offset": index_row[3],
        } if index_row else None,
    }


def _tool_result_json(result: Any) -> Dict[str, Any]:
    content = getattr(result, "content", None) or []
    if not content:
        return {}
    text = getattr(content[0], "text", "") or ""
    return json.loads(text)


async def _check_mcp_stdio_protocol_async(data_dir: Path) -> CheckResult:
    marker = _make_marker("stdio_marker")
    project = _sanitize_project_name(f"install_check_stdio_{marker}")
    env = os.environ.copy()
    env.update({
        "MEMORIA_MCP_DATA_DIR": str(data_dir),
        "MEMORIA_MCP_ENABLE_SEMANTIC": "false",
        "MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE": "live",
        "MEMORIA_MCP_WRITE_QUEUE_ENABLED": "false",
    })
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "memoria_mcp.server"],
        env=env,
    )
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                tool_names = [tool.name for tool in tools.tools]
                if tool_names != EXPECTED_CORE_TOOLS:
                    raise AssertionError(f"unexpected stdio tool list: {tool_names}")

                remember = _tool_result_json(await session.call_tool(
                    "memoria_remember",
                    {
                        "project": project,
                        "content": f"{marker}: MCP stdio protocol should round-trip this memory.",
                        "type": "debug_insight",
                        "importance": 0.86,
                        "confidence": 0.96,
                    },
                ))
                if not remember.get("stored"):
                    raise AssertionError(f"stdio remember failed: {remember}")
                node_id = str(remember.get("node_id") or "")

                recall = _tool_result_json(await session.call_tool(
                    "memoria_recall",
                    {"project": project, "query": marker, "top_k": 3},
                ))
                if recall.get("count", 0) < 1:
                    raise AssertionError(f"stdio recall failed: {recall}")
                ref_id = recall["results"][0].get("ref_id")

                read_result = _tool_result_json(await session.call_tool(
                    "memoria_read",
                    {"project": project, "ref_id": ref_id, "max_chars": 2000},
                ))
                if not read_result.get("ok") or marker not in str(read_result.get("text") or ""):
                    raise AssertionError(f"stdio read failed: {read_result}")

                forget = _tool_result_json(await session.call_tool(
                    "memoria_forget",
                    {"project": project, "node_id": node_id},
                ))
                if not forget.get("deleted"):
                    raise AssertionError(f"stdio forget failed: {forget}")

        return CheckResult.pass_(
            "mcp_stdio_protocol",
            tools=EXPECTED_CORE_TOOLS,
            remember=True,
            recall=True,
            read=True,
            forget=True,
            project=project,
        )
    finally:
        try:
            shutdown = shutdown_daemon(data_dir)
            daemon_pid = int(shutdown.get("pid") or 0) if isinstance(shutdown, dict) else 0
            deadline = time.time() + 5.0
            while (
                daemon_pid > 0
                and time.time() < deadline
                and is_process_alive(daemon_pid)
            ):
                time.sleep(0.1)
        except Exception:
            pass
        try:
            _delete_project_with_retry(data_dir, project)
        except Exception:
            pass


def check_mcp_stdio_protocol(data_dir: Path) -> CheckResult:
    return asyncio.run(_check_mcp_stdio_protocol_async(data_dir))


def check_agent_daemon_flow() -> CheckResult:
    if importlib.util.find_spec("memoria_mcp.tool_worker") is not None:
        return CheckResult.fail("agent_daemon_flow", "legacy read-only tool_worker module is still installed")
    with tempfile.TemporaryDirectory(prefix="ripple-agent-daemon-install-check-") as tmp:
        data_dir = Path(tmp)
        marker = _make_marker("agent_daemon")
        project = _sanitize_project_name(f"install_check_agent_daemon_{marker}")
        env = {
            "MEMORIA_MCP_DATA_DIR": str(data_dir),
            "MEMORIA_MCP_ENABLE_SEMANTIC": "false",
            "MEMORIA_MCP_PRELOAD_EMBEDDING": "false",
            "MEMORIA_MCP_WRITE_QUEUE_ENABLED": "false",
            "MEMORIA_MCP_IDLE_SLEEP_SECONDS": "3600",
            "MEMORIA_MCP_IDLE_EXIT_SECONDS": "36000",
        }
        with _patched_env(env):
            try:
                first = ensure_agent_daemon(data_dir)
                if not first.get("ok"):
                    raise AssertionError(f"first ensure failed: {first}")
                first_pid = int((first.get("daemon") or {}).get("pid") or 0)
                if first_pid <= 0:
                    raise AssertionError(f"daemon pid missing: {first}")
                second = ensure_agent_daemon(data_dir)
                second_pid = int((second.get("daemon") or {}).get("pid") or 0)
                if second_pid != first_pid:
                    raise AssertionError(f"daemon was not reused: {first_pid} vs {second_pid}")

                remember = call_daemon_tool(
                    data_dir,
                    "memoria_remember",
                    {
                        "project": project,
                        "content": f"{marker}: agent-level daemon flow memory.",
                        "type": "debug_insight",
                        "importance": 0.6,
                        "confidence": 0.9,
                    },
                )
                remember_result = remember.get("result") or {}
                if not remember.get("ok") or not remember_result.get("stored"):
                    raise AssertionError(f"daemon remember failed: {remember}")
                node_id = str(remember_result.get("node_id") or "")

                recall_started = time.perf_counter()
                recall = call_daemon_tool(data_dir, "memoria_recall", {"project": project, "query": marker, "top_k": 3})
                recall_elapsed = time.perf_counter() - recall_started
                recall_result = recall.get("result") or {}
                if not recall.get("ok") or recall_result.get("count", 0) < 1:
                    raise AssertionError(f"daemon recall failed: {recall}")
                if recall_elapsed >= 0.8:
                    raise AssertionError(
                        f"daemon recall was unexpectedly slow on the direct daemon path: {recall_elapsed:.3f}s"
                    )
                ref_id = recall_result["results"][0].get("ref_id")

                read_result = (call_daemon_tool(
                    data_dir,
                    "memoria_read",
                    {"project": project, "ref_id": ref_id, "max_chars": 2000},
                ).get("result") or {})
                if not read_result.get("ok") or marker not in str(read_result.get("text") or ""):
                    raise AssertionError(f"daemon read failed: {read_result}")

                forget = call_daemon_tool(data_dir, "memoria_forget", {"project": project, "node_id": node_id})
                if not (forget.get("result") or {}).get("deleted"):
                    raise AssertionError(f"daemon forget failed: {forget}")

                shutdown = shutdown_daemon(data_dir)
                if not shutdown.get("ok"):
                    raise AssertionError(f"daemon shutdown failed: {shutdown}")
                deadline = time.time() + 5.0
                while (
                    time.time() < deadline
                    and (port_file_path(data_dir).exists() or is_process_alive(first_pid))
                ):
                    time.sleep(0.1)
                if port_file_path(data_dir).exists() or is_process_alive(first_pid):
                    raise AssertionError("daemon remained alive after shutdown")

                restart_started = time.perf_counter()
                restarted = call_daemon_tool(
                    data_dir,
                    "memoria_remember",
                    {
                        "project": project,
                        "content": f"{marker}: agent-level daemon restart memory.",
                        "type": "debug_insight",
                        "importance": 0.5,
                        "confidence": 0.9,
                    },
                )
                restart_elapsed = time.perf_counter() - restart_started
                if not (restarted.get("result") or {}).get("stored"):
                    raise AssertionError(f"daemon restart call failed: {restarted}")
                restarted_state = ensure_agent_daemon(data_dir)
                restarted_pid = int((restarted_state.get("daemon") or {}).get("pid") or 0)
                if restarted_pid <= 0 or restarted_pid == first_pid:
                    raise AssertionError(f"daemon did not restart with a new pid: {restarted_state}")
                shutdown = shutdown_daemon(data_dir)
                if not shutdown.get("ok"):
                    raise AssertionError(f"second daemon shutdown failed: {shutdown}")
                deadline = time.time() + 5.0
                while (
                    time.time() < deadline
                    and (port_file_path(data_dir).exists() or is_process_alive(restarted_pid))
                ):
                    time.sleep(0.1)
                if port_file_path(data_dir).exists() or is_process_alive(restarted_pid):
                    raise AssertionError("restarted daemon remained alive after shutdown")

                no_owner_env = os.environ.copy()
                no_owner_env.update({
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
                    env=no_owner_env,
                )
                try:
                    deadline = time.time() + 8.0
                    while time.time() < deadline and not port_file_path(data_dir).exists():
                        time.sleep(0.1)
                    if not port_file_path(data_dir).exists():
                        raise AssertionError("no-owner daemon did not publish a port file")
                    no_owner_started = time.perf_counter()
                    deadline = time.time() + 8.0
                    while time.time() < deadline and proc.poll() is None:
                        time.sleep(0.1)
                    no_owner_elapsed = time.perf_counter() - no_owner_started
                    if proc.poll() is None:
                        raise AssertionError("no-owner daemon did not auto-exit")
                    if port_file_path(data_dir).exists():
                        raise AssertionError("no-owner daemon left port file after exit")
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            proc.kill()

                owner_exit_env = os.environ.copy()
                owner_exit_env.update({
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
                    env=owner_exit_env,
                    check=False,
                )
                if helper.returncode != 0:
                    raise AssertionError(f"owner helper failed: {helper.stderr or helper.stdout}")
                try:
                    owner_ready = json.loads(helper.stdout.strip().splitlines()[-1])
                except Exception as exc:
                    raise AssertionError(f"owner helper did not print daemon state: {helper.stdout!r}") from exc
                if not owner_ready.get("ok"):
                    raise AssertionError(f"owner helper daemon start failed: {owner_ready}")
                owner_exit_pid = int((owner_ready.get("daemon") or {}).get("pid") or 0)
                if owner_exit_pid <= 0:
                    raise AssertionError(f"owner-exit daemon pid missing: {owner_ready}")
                owner_exit_started = time.perf_counter()
                deadline = time.time() + 8.0
                while (
                    time.time() < deadline
                    and (port_file_path(data_dir).exists() or is_process_alive(owner_exit_pid))
                ):
                    time.sleep(0.1)
                owner_exit_elapsed = time.perf_counter() - owner_exit_started
                if port_file_path(data_dir).exists() or is_process_alive(owner_exit_pid):
                    raise AssertionError("owner-exit daemon remained alive after owner process exited")

                direct_env = os.environ.copy()
                direct_env.update({
                    "MEMORIA_MCP_DATA_DIR": str(data_dir),
                    "MEMORIA_MCP_ENABLE_SEMANTIC": "false",
                    "MEMORIA_MCP_PRELOAD_EMBEDDING": "false",
                    "MEMORIA_MCP_WRITE_QUEUE_ENABLED": "false",
                    "MEMORIA_MCP_DAEMON_AGENT_EXIT_GRACE_SECONDS": "60",
                })
                direct_procs = [
                    subprocess.Popen(
                        [sys.executable, "-m", "memoria_mcp.agent_daemon"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        env=direct_env,
                    )
                    for _ in range(3)
                ]
                direct_singleton_pid = 0
                try:
                    deadline = time.time() + 10.0
                    while time.time() < deadline and not port_file_path(data_dir).exists():
                        time.sleep(0.1)
                    if not port_file_path(data_dir).exists():
                        raise AssertionError("direct daemon singleton did not publish a port file")
                    deadline = time.time() + 8.0
                    direct_alive: List[int] = []
                    while time.time() < deadline:
                        direct_alive = [
                            proc.pid for proc in direct_procs
                            if proc.poll() is None and is_process_alive(proc.pid)
                        ]
                        if len(direct_alive) <= 1:
                            break
                        time.sleep(0.1)
                    if len(direct_alive) != 1:
                        raise AssertionError(f"direct daemon starts left multiple live daemons: {direct_alive}")
                    direct_singleton_pid = direct_alive[0]
                    shutdown = shutdown_daemon(data_dir)
                    if not shutdown.get("ok"):
                        raise AssertionError(f"direct singleton daemon shutdown failed: {shutdown}")
                    deadline = time.time() + 5.0
                    while (
                        time.time() < deadline
                        and (port_file_path(data_dir).exists() or is_process_alive(direct_singleton_pid))
                    ):
                        time.sleep(0.1)
                    if port_file_path(data_dir).exists() or is_process_alive(direct_singleton_pid):
                        raise AssertionError("direct singleton daemon remained alive after shutdown")
                finally:
                    for proc in direct_procs:
                        if proc.poll() is None:
                            proc.terminate()
                    time.sleep(0.2)
                    for proc in direct_procs:
                        if proc.poll() is None:
                            proc.kill()

                slow_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                slow_server.bind(("127.0.0.1", 0))
                slow_server.listen(1)
                slow_port = int(slow_server.getsockname()[1])
                slow_token = "install-check-slow-response-token"
                slow_port_file = port_file_path(data_dir)
                slow_port_file.parent.mkdir(parents=True, exist_ok=True)
                slow_port_file.write_text(
                    json.dumps({
                        "schema": "ripple_memory_agent_daemon_v1",
                        "host": "127.0.0.1",
                        "port": slow_port,
                        "pid": os.getpid(),
                        "token": slow_token,
                        "started_at": time.time(),
                    }),
                    encoding="utf-8",
                )

                def _slow_response_server() -> None:
                    try:
                        conn, _addr = slow_server.accept()
                        with conn:
                            try:
                                conn.recv(65536)
                                time.sleep(0.25)
                                conn.sendall(b'{"ok": true, "daemon": {"pid": 1}}\n')
                            except OSError:
                                pass
                    except OSError:
                        pass

                old_timeout = daemon_client.RECV_TIMEOUT_SECONDS
                slow_thread = threading.Thread(target=_slow_response_server, daemon=True)
                slow_thread.start()
                daemon_client.RECV_TIMEOUT_SECONDS = 0.05
                try:
                    slow_result = daemon_client._request_once(data_dir, {"op": "ping"})
                    if not slow_result or slow_result.get("error") != "agent_daemon_response_timeout":
                        raise AssertionError(f"unexpected slow daemon response result: {slow_result}")
                    if not slow_port_file.exists():
                        raise AssertionError("slow daemon response incorrectly removed port.json")
                finally:
                    daemon_client.RECV_TIMEOUT_SECONDS = old_timeout
                    try:
                        slow_server.close()
                    except OSError:
                        pass
                    slow_thread.join(timeout=1.0)

                return CheckResult.pass_(
                    "agent_daemon_flow",
                    daemon_pid=first_pid,
                    restarted_pid=restarted_pid,
                    reused=True,
                    restart_elapsed_seconds=round(restart_elapsed, 3),
                    daemon_recall_elapsed_seconds=round(recall_elapsed, 3),
                    no_owner_exit_elapsed_seconds=round(no_owner_elapsed, 3),
                    owner_exit_elapsed_seconds=round(owner_exit_elapsed, 3),
                    direct_daemon_singleton_pid=direct_singleton_pid,
                    direct_daemon_suppressed=2,
                    slow_response_preserved_port=True,
                    tools=EXPECTED_CORE_TOOLS,
                    data_root=str(data_dir),
                )
            finally:
                try:
                    shutdown_daemon(data_dir)
                except Exception:
                    pass


def _host_tokens(host: str) -> List[str]:
    clean = (host or "auto").lower()
    if clean in {"claude", "claude-code"}:
        return ["claude"]
    if clean in {"qwen", "qwen-code"}:
        return ["qwen"]
    if clean in {"mimo", "mimo-code", "mimocode"}:
        return ["mimo", "mimocode"]
    if clean == "codex":
        return ["codex"]
    return []


def _windows_process_snapshot() -> Dict[int, Dict[str, Any]]:
    if os.name != "nt":
        return {}
    command = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            text=True,
            capture_output=True,
            timeout=8,
        )
    except Exception:
        return {}
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    rows = raw if isinstance(raw, list) else [raw]
    snapshot: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        try:
            pid = int(row.get("ProcessId") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if pid > 0:
            snapshot[pid] = row
    return snapshot


def _process_ancestor_text(pid: int, snapshot: Dict[int, Dict[str, Any]], *, max_depth: int = 8) -> str:
    parts: List[str] = []
    seen: set[int] = set()
    current = int(pid or 0)
    for _ in range(max_depth):
        if current <= 0 or current in seen:
            break
        seen.add(current)
        row = snapshot.get(current)
        if not row:
            break
        parts.append(str(row.get("Name") or ""))
        parts.append(str(row.get("CommandLine") or ""))
        try:
            current = int(row.get("ParentProcessId") or 0)
        except (TypeError, ValueError):
            break
    return " ".join(parts).lower()


def _hook_context_from_output(output: Dict[str, Any]) -> str:
    specific = output.get("hookSpecificOutput")
    if isinstance(specific, dict):
        context = specific.get("additionalContext")
        if context:
            return str(context)
    return str(output.get("context") or "")


def _last_json_object_from_stdout(stdout: str) -> Dict[str, Any]:
    for line in reversed((stdout or "").splitlines()):
        text = line.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def check_live_smoke(*, data_dir: Path, hook_cmd: Optional[Path], host: str) -> CheckResult:
    """End-to-end smoke test against the REAL installed daemon and data dir.

    Unlike other checks that use temp dirs and patched env, this connects to the
    actual running daemon (if any) and writes to the real data directory. It
    verifies the installed system works, not just that the code is correct.

    On a fresh install where the host has not been restarted yet, the daemon will
    be cold (no port.json). This is expected — the check returns skip with a
    reminder to restart the host and rerun.
    """
    details: Dict[str, Any] = {}
    errors: List[str] = []
    cold_start = False

    # 1. Check daemon port.json exists
    port_path = port_file_path(data_dir)
    port_ok = port_path.is_file()
    details["daemon_port_exists"] = port_ok
    if port_ok:
        try:
            port_data = json.loads(port_path.read_text(encoding="utf-8"))
            details["daemon_pid"] = port_data.get("pid")
            details["daemon_port"] = port_data.get("port")
            pid = int(port_data.get("pid") or 0)
            details["daemon_alive"] = pid > 0 and is_process_alive(pid)
            if not details["daemon_alive"]:
                errors.append("daemon port.json exists but daemon process is not alive")
        except Exception as exc:
            errors.append(f"daemon port.json unreadable: {exc.__class__.__name__}")
    else:
        cold_start = True
        details["daemon_alive"] = False

    # 2. Run remember/recall/read/forget via daemon (if alive)
    marker = _make_marker("live_smoke")
    project = _sanitize_project_name(f"live_smoke_{marker}")
    if details.get("daemon_alive"):
        try:
            remember = call_daemon_tool(
                data_dir,
                "memoria_remember",
                {
                    "project": project,
                    "content": f"{marker}: live smoke test memory.",
                    "type": "debug_insight",
                    "importance": 0.5,
                    "confidence": 0.9,
                },
            )
            remember_result = remember.get("result") or {}
            details["remember_ok"] = bool(remember.get("ok") and remember_result.get("stored"))
            details["remember_state"] = remember_result.get("commit_state")
            node_id = str(remember_result.get("node_id") or "")
            pending_id = str(remember_result.get("pending_id") or "")
            if details["remember_ok"] and remember_result.get("commit_state") == "queued" and pending_id:
                queue_result = ProjectWriteQueue(data_dir, project).wait_for_result(
                    pending_id,
                    timeout_seconds=15.0,
                )
                details["remember_pending_id"] = pending_id
                details["remember_queue_state"] = (queue_result or {}).get("state")
                if queue_result and queue_result.get("state") == "committed":
                    committed = dict(queue_result.get("result") or {})
                    node_id = str(committed.get("node_id") or node_id)
                    details["remember_state"] = "committed"
                    details["remember_queue_committed"] = True
                elif queue_result and queue_result.get("state") == "failed":
                    details["remember_queue_committed"] = False
                    errors.append(f"queued remember failed: {queue_result.get('error') or 'unknown'}")
                else:
                    details["remember_queue_committed"] = False
                    errors.append("queued remember did not commit before live smoke recall")

            recall = call_daemon_tool(
                data_dir, "memoria_recall", {"project": project, "query": marker, "top_k": 3}
            )
            recall_result = recall.get("result") or {}
            details["recall_ok"] = bool(recall.get("ok") and recall_result.get("count", 0) >= 1)
            details["recall_count"] = recall_result.get("count", 0)
            ref_id = (recall_result.get("results") or [{}])[0].get("ref_id") if recall_result.get("results") else ""

            if ref_id:
                read = call_daemon_tool(
                    data_dir, "memoria_read", {"project": project, "ref_id": ref_id, "max_chars": 2000}
                )
                read_result = read.get("result") or {}
                details["read_ok"] = bool(read.get("ok") and marker in str(read_result.get("text") or ""))
            else:
                details["read_ok"] = False
                errors.append("recall returned no ref_id")

            if node_id:
                forget = call_daemon_tool(
                    data_dir, "memoria_forget", {"project": project, "node_id": node_id}
                )
                details["forget_ok"] = bool((forget.get("result") or {}).get("deleted"))
            else:
                details["forget_ok"] = False
                errors.append("remember returned no committed node_id")

            for tool in ["remember_ok", "recall_ok", "read_ok", "forget_ok"]:
                if not details.get(tool):
                    errors.append(f"daemon {tool} failed")
        except Exception as exc:
            errors.append(f"daemon tool calls failed: {exc.__class__.__name__}: {exc}")
    else:
        for tool in ["remember_ok", "recall_ok", "read_ok", "forget_ok"]:
            details[tool] = False

    # 3. Check hook script works with real event data
    if hook_cmd and hook_cmd.is_file():
        try:
            hook_env = os.environ.copy()
            hook_env["MEMORIA_MCP_DATA_DIR"] = str(data_dir)
            hook_env["RIPPLE_MEMORY_DATA_DIR"] = str(data_dir)
            hook_env["RIPPLE_MEMORY_PROJECT"] = project
            hook_env["RIPPLE_MEMORY_WINDOW_ID"] = f"live-smoke-{marker}"
            proc = subprocess.run(
                ["cmd.exe", "/d", "/c", str(hook_cmd)],
                input=json.dumps({
                    "eventName": "UserPromptSubmit",
                    "hook_event_name": "UserPromptSubmit",
                    "cwd": str(data_dir.parent.parent),
                    "project": project,
                    "session_id": f"live-smoke-{marker}",
                    "window_id": f"live-smoke-{marker}",
                    "prompt": f"Recall live smoke marker {marker}",
                }),
                capture_output=True,
                text=True,
                timeout=10,
                encoding="utf-8",
                errors="replace",
                env=hook_env,
            )
            details["hook_exit_code"] = proc.returncode
            hook_output = _last_json_object_from_stdout(proc.stdout)
            hook_context = _hook_context_from_output(hook_output)
            details["hook_has_context"] = bool(hook_context)
            details["hook_context_length"] = len(hook_context)
            details["hook_context_has_marker"] = marker in hook_context
            if not details["hook_has_context"]:
                errors.append("hook script returned no additionalContext for UserPromptSubmit")
            elif not details["hook_context_has_marker"]:
                errors.append("hook context did not include the live smoke marker")
        except Exception as exc:
            errors.append(f"hook script failed: {exc.__class__.__name__}")
    else:
        details["hook_skipped"] = True

    # 4. Check latch file was created by hook
    latch_dir = data_dir / "_window_state" / project / f"live-smoke-{marker}"
    latch_file = latch_dir / "original-word-latch.md"
    details["latch_exists"] = latch_file.is_file()
    if details["latch_exists"]:
        try:
            latch_text = latch_file.read_text(encoding="utf-8", errors="replace")
            details["latch_has_task"] = "Task:" in latch_text or "Goal:" in latch_text
            details["latch_has_state"] = "Task State" in latch_text
            if not details["latch_has_task"]:
                errors.append("latch file exists but has no task content")
        except Exception as exc:
            errors.append(f"latch file unreadable: {exc.__class__.__name__}")

    # Cleanup: remove smoke test project
    try:
        _delete_project_with_retry(data_dir, project)
    except Exception:
        pass

    if cold_start and not details.get("daemon_alive"):
        return CheckResult.skip(
            "live_smoke",
            "COLD START: daemon not running. This is expected on fresh install before host restart. "
            "RESTART THE HOST AGENT, open a session, then rerun install_check with --live-smoke to verify the full chain. "
            "After restart, verify: (1) MCP tools visible in host, (2) daemon port.json exists, "
            "(3) hook injects <ripple_memory_context>, (4) Original Words Latch file created.",
            **details,
        )

    if errors:
        return CheckResult.fail("live_smoke", "; ".join(errors), **details)
    return CheckResult.pass_("live_smoke", **details)


def check_host_mcp_process(*, host: str, data_dir: Path, require_host_mcp_process: bool) -> CheckResult:
    if not require_host_mcp_process:
        return CheckResult.skip("host_mcp_process", "not requested")

    registry = ProcessRegistry(data_dir, host="install-check")
    records = registry.list_processes()
    live_records: List[Dict[str, Any]] = []
    data_dir_text = str(data_dir.resolve()).lower()
    for record in records:
        pid = int(record.get("pid") or 0)
        record_data_dir = str(Path(str(record.get("base_data_dir") or data_dir)).expanduser().resolve()).lower()
        argv_text = " ".join(str(part) for part in (record.get("argv") or []))
        command_hint = f"{record.get('executable') or ''} {argv_text}".lower().replace("\\", "/")
        if (
            pid > 0
            and bool(record.get("alive"))
            and record_data_dir == data_dir_text
            and (
                "memoria_mcp.agent_daemon" in command_hint
                or "memoria_mcp/agent_daemon.py" in command_hint
                or "memoria_mcp.server" in command_hint
                or "memoria_mcp/server.py" in command_hint
                or "ripple-memory" in command_hint
            )
            and is_process_alive(pid)
        ):
            live_records.append(record)

    if not live_records:
        return CheckResult.fail(
            "host_mcp_process",
            "no live registered Ripple agent daemon/proxy process was found for this host data directory",
            data_dir=str(data_dir),
            registry_dir=str(registry.process_dir),
            record_count=len(records),
            live_pids=[],
        )

    tokens = _host_tokens(host)
    snapshot = _windows_process_snapshot()
    host_matches: List[int] = []
    if tokens and snapshot:
        for record in live_records:
            pid = int(record.get("pid") or 0)
            ancestor_text = _process_ancestor_text(pid, snapshot)
            if any(token in ancestor_text for token in tokens):
                host_matches.append(pid)
        if not host_matches:
            return CheckResult.fail(
                "host_mcp_process",
                "Ripple MCP process exists, but its parent chain does not look owned by the requested host",
                host=host,
                live_pids=[int(record.get("pid") or 0) for record in live_records],
                host_tokens=tokens,
                process_snapshot_available=True,
            )

    return CheckResult.pass_(
        "host_mcp_process",
        host=host,
        live_pids=[int(record.get("pid") or 0) for record in live_records],
        host_verified_pids=host_matches,
        process_snapshot_available=bool(snapshot),
    )


def check_mcp_database_project_flow(data_dir: Path) -> CheckResult:
    marker = _make_marker("mcp_marker")
    project_a = _sanitize_project_name(f"install_check_project_a_{marker}")
    project_b = _sanitize_project_name(f"install_check_project_b_{marker}")
    content = (
        f"{marker}: Ripple install check memory. Project A should recall this, "
        "Project B must not."
    )
    env = {
        "MEMORIA_MCP_DATA_DIR": str(data_dir),
        "MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE": "live",
        "MEMORIA_MCP_PRELOAD_EMBEDDING": os.environ.get("MEMORIA_MCP_PRELOAD_EMBEDDING", "false"),
        "MEMORIA_MCP_WRITE_QUEUE_ENABLED": "false",
    }
    with _patched_env(env):
        router = MemoriaRouter(str(data_dir))
        try:
            tool_names = asyncio.run(_list_tool_names(router))
            if tool_names != EXPECTED_CORE_TOOLS:
                raise AssertionError(f"unexpected tool list: {tool_names}")

            remember = router._dispatch_tool(
                "memoria_remember",
                {
                    "project": project_a,
                    "content": content,
                    "type": "arch_decision",
                    "importance": 0.88,
                    "confidence": 0.96,
                },
            )
            if not remember.get("stored"):
                raise AssertionError(f"remember failed: {remember}")
            node_id = str(remember.get("node_id") or "")
            if not node_id:
                raise AssertionError("remember did not return node_id")

            recall_a = router._dispatch_tool(
                "memoria_recall",
                {"project": project_a, "query": marker, "top_k": 5},
            )
            if recall_a.get("count", 0) < 1:
                raise AssertionError(f"project A recall returned no hit: {recall_a}")
            ref_id = recall_a["results"][0].get("ref_id")
            if not ref_id:
                raise AssertionError(f"recall result missing ref_id: {recall_a}")

            read = router._dispatch_tool(
                "memoria_read",
                {"project": project_a, "ref_id": ref_id, "max_chars": 2000},
            )
            if not read.get("ok") or marker not in str(read.get("text") or ""):
                raise AssertionError(f"read did not hydrate exact memory: {read}")

            recall_b = router._dispatch_tool(
                "memoria_recall",
                {"project": project_b, "query": marker, "top_k": 5},
            )
            if recall_b.get("count", 0) != 0:
                raise AssertionError(f"project B leaked project A memory: {recall_b}")
        finally:
            router.close()

        db_details = _inspect_project_db(data_dir / project_a, node_id, marker)
        if not db_details["required_tables_present"]:
            raise AssertionError(f"database missing required tables: {db_details}")
        if db_details["obsolete_tables_present"]:
            raise AssertionError(f"obsolete SQL rail tables are still present: {db_details}")
        if not db_details["graph_state_contains_marker"]:
            raise AssertionError(f"graph_state does not contain marker: {db_details}")
        if not db_details["jsonl_stream_contains_marker"]:
            raise AssertionError(f"JSONL archive stream does not contain marker: {db_details}")
        if not db_details["search_index_row"] or db_details["search_index_row"]["deleted"] != 0:
            raise AssertionError(f"search_index row missing or deleted: {db_details}")

        router = MemoriaRouter(str(data_dir))
        try:
            forget = router._dispatch_tool(
                "memoria_forget",
                {"project": project_a, "node_id": node_id},
            )
            if not forget.get("deleted"):
                raise AssertionError(f"forget failed: {forget}")
            read_after = router._dispatch_tool(
                "memoria_read",
                {"project": project_a, "ref_id": ref_id, "max_chars": 2000},
            )
            if read_after.get("ok"):
                raise AssertionError(f"deleted memory is still readable: {read_after}")
        finally:
            router.close()
        _delete_project_with_retry(data_dir, project_a)
        _delete_project_with_retry(data_dir, project_b)

    return CheckResult.pass_(
        "mcp_tools_project_database",
        tools=EXPECTED_CORE_TOOLS,
        project_a=project_a,
        project_b=project_b,
        project_isolation=True,
        read_exact_memory=True,
        forget_unreadable=True,
        database=db_details,
    )


def check_memory_evolution_flow(data_dir: Path) -> CheckResult:
    marker = _make_marker("evolution_marker")
    project = _sanitize_project_name(f"install_check_evolution_{marker}")
    fact_key = f"install.check.{marker.lower()}"
    old_text = f"{marker}: OLD_POLICY says use Alpha for this install-check口径."
    new_text = f"{marker}: CURRENT_POLICY says use Beta for this install-check口径."
    env = {
        "MEMORIA_MCP_DATA_DIR": str(data_dir),
        "MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE": "live",
        "MEMORIA_MCP_ENABLE_SEMANTIC": "false",
        "MEMORIA_MCP_WRITE_QUEUE_ENABLED": "false",
    }
    with _patched_env(env):
        router = MemoriaRouter(str(data_dir))
        try:
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
            if not old.get("stored"):
                raise AssertionError(f"old口径 remember failed: {old}")
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
                    "evolution_reason": "install-check confirms new口径 supersedes old口径",
                },
            )
            if not new.get("stored"):
                raise AssertionError(f"new口径 remember failed: {new}")
            new_ref = f"memory_node:{new['node_id']}"
            evolution = new.get("memory_evolution") or {}
            if old_ref not in list(evolution.get("superseded_ref_ids") or []):
                raise AssertionError(f"new memory did not mark old ref superseded: {new}")

            recall = router._dispatch_tool(
                "memoria_recall",
                {"project": project, "query": marker, "top_k": 5},
            )
            rendered = json.dumps(recall.get("results", []), ensure_ascii=False)
            rendered_full = json.dumps(recall, ensure_ascii=False)
            if "CURRENT_POLICY" not in rendered:
                raise AssertionError(f"default recall missed current口径: {recall}")
            if "OLD_POLICY" in rendered:
                raise AssertionError(f"default recall still exposed old口径 in results: {recall}")
            if "OLD_POLICY" in rendered_full:
                raise AssertionError(f"default recall leaked old policy outside results: {recall}")
            if old_ref not in list(recall.get("filtered_superseded_refs") or []):
                raise AssertionError(f"default recall did not report filtered superseded ref: {recall}")
            current_claims = recall.get("truth_projection", {}).get("current_claims") or []
            if not any(claim.get("ref_id") == new_ref for claim in current_claims):
                raise AssertionError(f"truth projection missing current claim: {recall}")

            recall_with_history = router._dispatch_tool(
                "memoria_recall",
                {"project": project, "query": marker, "top_k": 5, "include_evolution": True},
            )
            history_rendered = json.dumps(recall_with_history, ensure_ascii=False)
            if "OLD_POLICY" not in history_rendered or '"evolution_status": "superseded"' not in history_rendered:
                raise AssertionError(f"include_evolution did not expose labeled old口径: {recall_with_history}")

            read_old = router._dispatch_tool(
                "memoria_read",
                {"project": project, "ref_id": old_ref, "max_chars": 2000},
            )
            if read_old.get("evolution_status") != "superseded":
                raise AssertionError(f"read old口径 was not labeled superseded: {read_old}")
            if "Historical memory" not in str(read_old.get("truth_guidance") or ""):
                raise AssertionError(f"read old口径 missing historical guidance: {read_old}")
        finally:
            router.close()

        db_path = data_dir / project / "memoria.db"
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT node_id, ref_id, status, fact_key, superseded_by_ref_id FROM memory_evolution_state WHERE fact_key = ?",
                (fact_key,),
            ).fetchall()
            edges = conn.execute(
                "SELECT from_ref_id, to_ref_id, relation FROM memory_evolution_edges WHERE fact_key = ?",
                (fact_key,),
            ).fetchall()
            old_index_row = conn.execute(
                "SELECT deleted, deleted_reason FROM search_index WHERE node_id = ?",
                (str(old["node_id"]),),
            ).fetchone()
        finally:
            conn.close()
        statuses = {row[2] for row in rows}
        if statuses != {"active", "superseded"}:
            raise AssertionError(f"unexpected memory evolution statuses: {rows}")
        if not any(row[0] == old_ref and row[1] == new_ref and row[2] == "superseded_by" for row in edges):
            raise AssertionError(f"memory evolution edge was not written: {edges}")
        if not old_index_row or int(old_index_row[0]) != 1 or old_index_row[1] != "memory_evolution_superseded":
            raise AssertionError(f"old口径 search_index was not marked for Dreamer cleanup: {old_index_row}")
        _delete_project_with_retry(data_dir, project)

    return CheckResult.pass_(
        "memory_evolution",
        project=project,
        fact_key=fact_key,
        active_ref=new_ref,
        superseded_ref=old_ref,
        default_recall_filters_superseded=True,
        include_evolution_exposes_history=True,
        read_labels_superseded=True,
        database_state_rows=len(rows),
        database_edge_rows=len(edges),
        old_search_index_deleted_reason="memory_evolution_superseded",
    )


def check_storage_architecture_flow(data_dir: Path) -> CheckResult:
    marker = _make_marker("storage_architecture")
    project = _sanitize_project_name(f"install_check_storage_{marker}")
    fact_key = f"install.check.storage.{marker.lower()}"
    old_text = f"{marker}: OLD storage claim should move to history."
    new_text = f"{marker}: CURRENT storage claim should stay active."
    env = {
        "MEMORIA_MCP_DATA_DIR": str(data_dir),
        "MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE": "live",
        "MEMORIA_MCP_ENABLE_SEMANTIC": "false",
        "MEMORIA_MCP_WRITE_QUEUE_ENABLED": "false",
    }
    with _patched_env(env):
        router = MemoriaRouter(str(data_dir))
        try:
            old = router._dispatch_tool(
                "memoria_remember",
                {
                    "project": project,
                    "content": old_text,
                    "type": "arch_decision",
                    "importance": 0.84,
                    "confidence": 0.96,
                    "fact_key": fact_key,
                },
            )
            if not old.get("stored"):
                raise AssertionError(f"old storage remember failed: {old}")
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
            if not new.get("stored"):
                raise AssertionError(f"new storage remember failed: {new}")
            new_ref = f"memory_node:{new['node_id']}"

            project_dir = data_dir / project
            db_details = _inspect_project_db(project_dir, str(new["node_id"]), marker)
            if db_details["obsolete_tables_present"]:
                raise AssertionError(f"obsolete SQL rail tables are still present: {db_details}")
            if not db_details["jsonl_stream_contains_marker"]:
                raise AssertionError(f"JSONL rail did not receive memory content: {db_details}")

            db_path = project_dir / "memoria.db"
            conn = sqlite3.connect(str(db_path))
            try:
                old_index = conn.execute(
                    "SELECT deleted, deleted_reason, json_file, json_offset FROM search_index WHERE node_id = ?",
                    (str(old["node_id"]),),
                ).fetchone()
                edge = conn.execute(
                    "SELECT from_ref_id, to_ref_id, relation FROM memory_evolution_edges WHERE fact_key = ?",
                    (fact_key,),
                ).fetchone()
            finally:
                conn.close()
            if not old_index or int(old_index[0]) != 1 or old_index[1] != "memory_evolution_superseded":
                raise AssertionError(f"superseded search row was not marked for Dreamer: {old_index}")
            if not old_index[2] or old_index[3] is None:
                raise AssertionError(f"superseded search row lacks JSONL pointer: {old_index}")
            if not edge or edge[0] != old_ref or edge[1] != new_ref or edge[2] != "superseded_by":
                raise AssertionError(f"memory evolution edge is missing or wrong: {edge}")

            srv = router._get_server(project)
            srv.config.dreamer_batch_threshold = 1
            srv.config.dreamer_interval_days = 0.0
            srv.config.dreamer_idle_hours = 0.0
            srv.config.dreamer_min_entry_age_hours = 0.0
            srv._auto_maintenance()

            conn = sqlite3.connect(str(db_path))
            try:
                purged_row = conn.execute(
                    "SELECT node_id FROM search_index WHERE node_id = ?",
                    (str(old["node_id"]),),
                ).fetchone()
            finally:
                conn.close()
            if purged_row is not None:
                raise AssertionError(f"Dreamer did not purge processed deleted search row: {purged_row}")

            router.close()
            restart_router = MemoriaRouter(str(data_dir))
            try:
                recall_default = restart_router._dispatch_tool(
                    "memoria_recall",
                    {"project": project, "query": marker, "top_k": 8},
                )
                recall_default_text = json.dumps(recall_default, ensure_ascii=False)
                if new_text not in recall_default_text:
                    raise AssertionError(f"restart default recall missed current storage claim: {recall_default}")
                if old_text in recall_default_text:
                    raise AssertionError(f"restart default recall resurrected old storage claim: {recall_default}")

                recall_history = restart_router._dispatch_tool(
                    "memoria_recall",
                    {"project": project, "query": marker, "top_k": 8, "include_evolution": True},
                )
                recall_history_text = json.dumps(recall_history, ensure_ascii=False)
                if old_text not in recall_history_text:
                    raise AssertionError(f"restart history recall lost old storage claim: {recall_history}")

                conn = sqlite3.connect(str(db_path))
                try:
                    restarted_old_index = conn.execute(
                        "SELECT deleted, deleted_reason FROM search_index WHERE node_id = ?",
                        (str(old["node_id"]),),
                    ).fetchone()
                finally:
                    conn.close()
                if not restarted_old_index or int(restarted_old_index[0]) != 1:
                    raise AssertionError(
                        f"restart did not restore superseded search delete mark: {restarted_old_index}"
                    )
            finally:
                restart_router.close()
        finally:
            router.close()
        _delete_project_with_retry(data_dir, project)

    return CheckResult.pass_(
        "storage_architecture",
        project=project,
        obsolete_sql_tables_absent=True,
        sqlite_runtime_truth=True,
        jsonl_frozen_content=True,
        evolution_edges=True,
        dreamer_cleanup=True,
        restart_does_not_resurrect_old_claim=True,
    )


def check_soft_timeout_recovery(data_dir: Path) -> CheckResult:
    marker = _make_marker("soft_timeout_marker")
    project = _sanitize_project_name(f"install_check_soft_timeout_{marker}")
    env = {
        "MEMORIA_MCP_DATA_DIR": str(data_dir),
        "MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE": "live",
        "MEMORIA_MCP_ENABLE_SEMANTIC": "false",
        "MEMORIA_MCP_TOOL_SOFT_TIMEOUT_RECOVERY": "true",
        "MEMORIA_MCP_WRITE_QUEUE_ENABLED": "false",
        "MEMORIA_MCP_WRITE_QUEUE_WORKER_ENABLED": "false",
    }
    with _patched_env(env):
        router = MemoriaRouter(str(data_dir))
        try:
            srv = router._get_server(project)
            original_remember = srv._tool_remember
            call_count = {"fast": 0, "slow": 0}

            os.environ["MEMORIA_MCP_TOOL_SOFT_TIMEOUT_SECONDS"] = "2"

            def counted_remember(args: Dict[str, Any]) -> Dict[str, Any]:
                call_count["fast"] += 1
                return original_remember(args)

            srv._tool_remember = counted_remember  # type: ignore[method-assign]
            fast = router._dispatch_tool_for_mcp(
                "memoria_remember",
                {
                    "project": project,
                    "content": f"{marker}: normal call should not retry",
                    "type": "debug_insight",
                    "importance": 0.4,
                    "confidence": 0.95,
                },
            )
            if not fast.get("stored") or call_count["fast"] != 1 or fast.get("recovered_after_soft_timeout"):
                raise AssertionError(f"normal soft-timeout guard path is wrong: fast={fast}, count={call_count}")

            os.environ["MEMORIA_MCP_TOOL_SOFT_TIMEOUT_SECONDS"] = "0.05"
            slow_content = f"{marker}: committed remember response should be recovered"
            slow_fact_key = f"install.check.soft_timeout.{marker.lower()}"

            def slow_after_commit_remember(args: Dict[str, Any]) -> Dict[str, Any]:
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
            if elapsed >= 0.7:
                raise AssertionError(f"soft timeout recovery returned too slowly: {elapsed:.3f}s")
            if not recovered.get("stored") or not recovered.get("recovered_after_soft_timeout"):
                raise AssertionError(f"soft timeout did not return recovered committed memory: {recovered}")
            if recovered.get("recovery", {}).get("retry_performed") is not False:
                raise AssertionError(f"remember recovery retried the write: {recovered}")
            if call_count["slow"] != 1:
                raise AssertionError(f"soft timeout remember retried unexpectedly: {call_count}")

            time.sleep(0.45)
            recall = router._dispatch_tool(
                "memoria_recall",
                {"project": project, "query": slow_content, "top_k": 5},
            )
            matching = [
                item for item in recall.get("results", [])
                if item.get("description") == slow_content
            ]
            if len(matching) != 1:
                raise AssertionError(f"expected one recovered memory, got {matching}")
            recovered_node_id = str(recovered.get("node_id") or "")

            original_dispatch_for_mcp = router._dispatch_tool_for_mcp

            os.environ.pop("MEMORIA_MCP_HANDLER_TIMEOUT_SECONDS", None)
            os.environ["MEMORIA_MCP_TOOL_SOFT_TIMEOUT_SECONDS"] = "0.05"
            os.environ["MEMORIA_MCP_READONLY_SOFT_TIMEOUT_SECONDS"] = "1.5"
            os.environ["MEMORIA_MCP_TOOL_RECOVERY_TIMEOUT_SECONDS"] = "0.1"

            def slow_readonly_handler(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
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
            if readonly_budget.get("error") == "tool_soft_timeout":
                raise AssertionError(f"read-only recall used the short write timeout budget: {readonly_budget}")
            if readonly_budget.get("count", 0) < 1:
                raise AssertionError(f"read-only recall did not return results under its own budget: {readonly_budget}")
            if not (0.2 <= readonly_budget_elapsed < 1.2):
                raise AssertionError(
                    f"read-only independent budget behaved unexpectedly: {readonly_budget_elapsed:.3f}s"
                )

            def wedged_handler(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
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

            if outer_elapsed >= 0.3:
                raise AssertionError(f"outer MCP handler guard returned too slowly: {outer_elapsed:.3f}s")
            if outer_recovered.get("recovered_after_soft_timeout") is not True:
                raise AssertionError(f"outer MCP handler guard did not recover read-only call: {outer_recovered}")

            os.environ["MEMORIA_MCP_WRITE_QUEUE_ENABLED"] = "true"
            os.environ["MEMORIA_MCP_WRITE_QUEUE_WORKER_ENABLED"] = "false"
            try:
                start = time.perf_counter()
                outer_remember = asyncio.run(router._dispatch_tool_for_mcp_async(
                    "memoria_remember",
                    {
                        "project": project,
                        "content": f"{marker}: queued remember should fail open immediately",
                        "type": "debug_insight",
                    },
                ))
                outer_remember_elapsed = time.perf_counter() - start
            finally:
                os.environ["MEMORIA_MCP_WRITE_QUEUE_ENABLED"] = "false"
                os.environ["MEMORIA_MCP_WRITE_QUEUE_WORKER_ENABLED"] = "false"

            if outer_remember_elapsed >= 0.16:
                raise AssertionError(
                    f"queue-first remember MCP path was too slow: {outer_remember_elapsed:.3f}s"
                )
            if outer_remember.get("commit_state") != "queued":
                raise AssertionError(f"queue-first remember did not return queued payload: {outer_remember}")
            if outer_remember.get("recovered_after_soft_timeout") is True:
                raise AssertionError(f"queue-first remember should not use timeout recovery: {outer_remember}")
            queue_counts = ProjectWriteQueue(data_dir, project).counts()
            if queue_counts["processing"] or queue_counts["lock_present"]:
                raise AssertionError(f"queue-first remember started in-process writer work: {queue_counts}")
            router._flush_write_queue(project, budget_seconds=5.0)

            jsonl_trace = data_dir / "_runtime" / "tool_events.jsonl"
            if jsonl_trace.exists():
                raise AssertionError("tool event trace must be update-style, not append-only tool_events.jsonl")
            state_files = list((data_dir / "_runtime" / "tool_events").glob("*.json"))
            if not state_files:
                raise AssertionError("tool event state file was not written")
            state = json.loads(state_files[0].read_text(encoding="utf-8"))
            if state.get("mode") != "single_state_update":
                raise AssertionError(f"tool event state is not update-style: {state}")
        finally:
            router.close()
        _delete_project_with_retry(data_dir, project)

    return CheckResult.pass_(
        "soft_timeout_recovery",
        project=project,
        recovered_node_id=recovered_node_id,
        fast_call_count=call_count["fast"],
        slow_call_count=call_count["slow"],
        recovered_without_write_retry=True,
        soft_timeout_elapsed_seconds=round(elapsed, 4),
        readonly_independent_budget_elapsed_seconds=round(readonly_budget_elapsed, 4),
        outer_handler_timeout_elapsed_seconds=round(outer_elapsed, 4),
        queue_first_remember_elapsed_seconds=round(outer_remember_elapsed, 4),
        tool_event_state_files=len(state_files),
        tool_event_trace_mode="single_state_update",
    )


def check_write_queue_flow(data_dir: Path) -> CheckResult:
    marker = _make_marker("write_queue_marker")
    project = _sanitize_project_name(f"install_check_write_queue_{marker}")
    stale_project = _sanitize_project_name(f"install_check_write_queue_stale_{marker}")
    env = {
        "MEMORIA_MCP_DATA_DIR": str(data_dir),
        "MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE": "live",
        "MEMORIA_MCP_ENABLE_SEMANTIC": "false",
        "MEMORIA_MCP_WRITE_QUEUE_ENABLED": "true",
        "MEMORIA_MCP_WRITE_QUEUE_WORKER_ENABLED": "false",
        "MEMORIA_MCP_WRITE_QUEUE_WAIT_SECONDS": "0.05",
        "MEMORIA_MCP_WRITE_QUEUE_DONE_MAX_FILES": "20",
    }
    with _patched_env(env):
        router = MemoriaRouter(str(data_dir))
        try:
            normal_content = f"{marker}: normal write queue commit"
            normal = router._dispatch_tool(
                "memoria_remember",
                {
                    "project": project,
                    "content": normal_content,
                    "type": "fact",
                    "importance": 0.5,
                    "confidence": 0.95,
                },
            )
            if normal.get("commit_state") != "queued" or normal.get("stored") is not True:
                raise AssertionError(f"normal remember did not queue first: {normal}")

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
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            if worker.returncode != 0:
                raise AssertionError(f"queue worker failed: stdout={worker.stdout} stderr={worker.stderr}")
            worker_payload = json.loads(worker.stdout)
            if int(worker_payload.get("processed") or 0) < 1:
                raise AssertionError(f"queue worker did not process normal write: {worker_payload}")

            queue_store = ProjectWriteQueue(data_dir, project)
            token = queue_store.try_acquire_lock()
            if not token:
                raise AssertionError("could not create artificial write queue lock")
            try:
                queued_content = f"{marker}: queued while writer lock is held"
                start = time.perf_counter()
                queued = router._dispatch_tool(
                    "memoria_remember",
                    {
                        "project": project,
                        "content": queued_content,
                        "type": "debug_insight",
                        "importance": 0.5,
                        "confidence": 0.95,
                    },
                )
                queued_elapsed = time.perf_counter() - start
                if queued_elapsed >= 0.5:
                    raise AssertionError(f"queued remember waited too long: {queued_elapsed:.3f}s")
                if queued.get("commit_state") != "queued" or queued.get("stored") is not True:
                    raise AssertionError(f"remember was not durably queued: {queued}")

                start = time.perf_counter()
                recall = router._dispatch_tool(
                    "memoria_recall",
                    {"project": project, "query": normal_content, "top_k": 3},
                )
                recall_elapsed = time.perf_counter() - start
                if recall_elapsed >= 0.5:
                    raise AssertionError(f"read path waited behind write queue: {recall_elapsed:.3f}s")
                if recall.get("count", 0) < 1:
                    raise AssertionError(f"read path failed while write queue locked: {recall}")
            finally:
                queue_store.release_lock(token)

            for _ in range(10):
                router._flush_write_queue(project, budget_seconds=5.0)
                counts = queue_store.counts()
                if counts["ready"] == 0 and counts["processing"] == 0:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError(f"queued remember did not drain: {queue_store.counts()}")
            committed_recall = router._dispatch_tool(
                "memoria_recall",
                {"project": project, "query": queued_content, "top_k": 5},
            )
            if committed_recall.get("count", 0) < 1:
                raise AssertionError(f"queued remember was not committed after drain: {committed_recall}")

            def hanging_commit(project_name: str, queued_args: Dict[str, Any]) -> Dict[str, Any]:
                time.sleep(1.0)
                return {"stored": True}

            router._commit_queued_remember = hanging_commit  # type: ignore[method-assign]
            hang_content = f"{marker}: request path must not start in-process commit"
            start = time.perf_counter()
            hang_result = router._dispatch_tool_for_mcp(
                "memoria_remember",
                {
                    "project": project,
                    "content": hang_content,
                    "type": "debug_insight",
                    "importance": 0.5,
                    "confidence": 0.95,
                },
            )
            hang_elapsed = time.perf_counter() - start
            hang_counts = queue_store.counts()
            if hang_elapsed >= 0.2:
                raise AssertionError(f"queue-first remember was too slow: {hang_elapsed:.3f}s")
            if hang_result.get("commit_state") != "queued":
                raise AssertionError(f"hang probe did not queue: {hang_result}")
            if hang_counts["processing"] or hang_counts["lock_present"]:
                raise AssertionError(f"request path started writer work: {hang_counts}")
            router._commit_queued_remember = MemoriaRouter._commit_queued_remember.__get__(router, MemoriaRouter)  # type: ignore[method-assign]
            router._flush_write_queue(project, budget_seconds=5.0)

            budget_project = _sanitize_project_name(f"{project}_budget")
            budget_queue = ProjectWriteQueue(data_dir, budget_project)
            for index in range(2):
                budget_queue.enqueue({
                    "project": budget_project,
                    "content": f"{marker}: budget follow-up item {index}",
                })

            def slow_budget_commit(queued_args: Dict[str, Any]) -> Dict[str, Any]:
                time.sleep(0.05)
                return {"stored": True, "content": queued_args.get("content")}

            budget_first = budget_queue.process_ready(slow_budget_commit, budget_seconds=0.01, max_items=10)
            budget_ready_remaining = int(budget_first.get("ready_remaining") or 0)
            if not (0 < budget_ready_remaining <= 2):
                raise AssertionError(f"budget drain did not expose remaining ready work: {budget_first}")
            if budget_first.get("budget_exhausted") is not True:
                raise AssertionError(f"budget drain did not report budget exhaustion: {budget_first}")
            budget_second = budget_queue.process_ready(lambda queued_args: {"stored": True}, budget_seconds=5.0, max_items=10)
            if budget_second.get("processed") != budget_ready_remaining:
                raise AssertionError(f"budget follow-up drain failed: {budget_second}")
            budget_counts = budget_queue.counts()
            if budget_counts["ready"] or budget_counts["processing"]:
                raise AssertionError(f"budget queue did not drain: {budget_counts}")
        finally:
            router.close()

        stale_router = MemoriaRouter(str(data_dir))
        try:
            stale_router._get_server(stale_project)
            fresh_router = MemoriaRouter(str(data_dir))
            try:
                fresh_content = f"{marker}: fresh graph survives stale close"
                fresh = fresh_router._dispatch_tool(
                    "memoria_remember",
                    {
                        "project": stale_project,
                        "content": fresh_content,
                        "type": "arch_decision",
                        "importance": 0.6,
                        "confidence": 0.95,
                    },
                )
                if fresh.get("commit_state") != "queued":
                    raise AssertionError(f"fresh write did not queue: {fresh}")
                fresh_router._flush_write_queue(stale_project, budget_seconds=5.0)
            finally:
                fresh_router.close()

            stale_recall = stale_router._dispatch_tool(
                "memoria_recall",
                {"project": stale_project, "query": fresh_content, "top_k": 5},
            )
            if stale_recall.get("count", 0) < 1:
                raise AssertionError(f"stale router did not reload newer graph: {stale_recall}")
        finally:
            stale_router.close()

        verify_router = MemoriaRouter(str(data_dir))
        try:
            verify = verify_router._dispatch_tool(
                "memoria_recall",
                {"project": stale_project, "query": fresh_content, "top_k": 5},
            )
            if verify.get("count", 0) < 1:
                raise AssertionError(f"stale close overwrote fresh graph: {verify}")
        finally:
            verify_router.close()
        queue_counts = queue_store.counts()
        _delete_project_with_retry(data_dir, project)
        _delete_project_with_retry(data_dir, stale_project)
        _delete_project_window_state(data_dir, budget_project)
        leftover_queue_dirs = [
            str(path)
            for path in (
                data_dir / "_runtime" / "write_queue" / _sanitize_project_name(project),
                data_dir / "_runtime" / "write_queue" / _sanitize_project_name(stale_project),
                data_dir / "_runtime" / "write_queue" / _sanitize_project_name(budget_project),
            )
            if path.exists()
        ]
        if leftover_queue_dirs:
            raise AssertionError(f"install-check write queue cleanup left project dirs: {leftover_queue_dirs}")

    return CheckResult.pass_(
        "write_queue_flow",
        project=project,
        normal_commit_state=normal.get("commit_state"),
        queued_commit_state=queued.get("commit_state"),
        queued_return_elapsed_seconds=round(queued_elapsed, 4),
        recall_while_locked_elapsed_seconds=round(recall_elapsed, 4),
        no_inprocess_writer_elapsed_seconds=round(hang_elapsed, 4),
        budget_ready_remaining_reported=budget_first.get("ready_remaining"),
        queue_counts=queue_counts,
        queue_worker_processed=worker_payload.get("processed"),
        write_queue_cleanup_verified=True,
        stale_graph_reload_preserved=True,
    )


def check_recall_quality_flow(data_dir: Path) -> CheckResult:
    marker = _make_marker("recall_quality")
    project = _sanitize_project_name(f"install_check_recall_quality_{marker}")
    env = {
        "MEMORIA_MCP_DATA_DIR": str(data_dir),
        "MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE": "live",
        "MEMORIA_MCP_ENABLE_SEMANTIC": "false",
        "MEMORIA_MCP_WRITE_QUEUE_ENABLED": "false",
        "MEMORIA_MCP_RECALL_FILTER_WEAK_ASCII_MATCHES": "true",
    }
    with _patched_env(env):
        router = MemoriaRouter(str(data_dir))
        try:
            old_loop = router._dispatch_tool(
                "memoria_remember",
                {
                    "project": project,
                    "content": (
                        f"{marker}: 2026-05-24 old broad Agent Loop closure map "
                        "and architecture commentary."
                    ),
                    "type": "debug_insight",
                    "importance": 0.9,
                    "confidence": 0.95,
                },
            )
            current = router._dispatch_tool(
                "memoria_remember",
                {
                    "project": project,
                    "content": (
                        f"{marker}: current src/session_store.py SessionContextAssembler "
                        "owns main Agent Loop transaction material assembly."
                    ),
                    "type": "arch_decision",
                    "importance": 0.7,
                    "confidence": 0.95,
                    "fact_key": f"install.check.recall_quality.{marker.lower()}",
                },
            )
            if not current.get("node_id"):
                raise AssertionError(f"current setup memory failed: {current}")

            srv = router._get_server(project)
            original_auto_maintenance = srv._auto_maintenance

            def fail_if_read_runs_maintenance() -> None:
                raise AssertionError("read-only recall/read must not run auto maintenance")

            srv._auto_maintenance = fail_if_read_runs_maintenance  # type: ignore[method-assign]
            try:
                start = time.perf_counter()
                recall = router._dispatch_tool(
                    "memoria_recall",
                    {
                        "project": project,
                        "query": "session_store main Agent Loop transaction",
                        "top_k": 5,
                    },
                )
                elapsed = time.perf_counter() - start
                if elapsed >= 0.5:
                    raise AssertionError(f"recall too slow: {elapsed:.3f}s")
                descriptions = [str(item.get("description") or "") for item in recall.get("results", [])]
                if not descriptions or "session_store.py" not in descriptions[0]:
                    raise AssertionError(f"current exact memory was not first: {recall}")
                if str(old_loop.get("node_id")) in json.dumps(recall.get("results", []), ensure_ascii=False):
                    raise AssertionError(f"weak old Agent Loop memory leaked into code-name recall: {recall}")
                if recall.get("recall_diagnostics", {}).get("maintenance_ran") is not False:
                    raise AssertionError(f"recall did not report read-only boundary: {recall}")
                read = router._dispatch_tool(
                    "memoria_read",
                    {
                        "project": project,
                        "ref_id": f"memory_node:{current.get('node_id')}",
                        "max_chars": 1000,
                    },
                )
                if read.get("ok") is not True:
                    raise AssertionError(f"read failed: {read}")
            finally:
                srv._auto_maintenance = original_auto_maintenance  # type: ignore[method-assign]
        finally:
            router.close()

        db_path = data_dir / project / "memoria.db"
        deleted_node_id = str(current.get("node_id") or "")
        deleted_reason = "install_check_deleted_row_guard"
        with closing(sqlite3.connect(str(db_path))) as conn:
            conn.execute(
                """
                UPDATE search_index
                SET deleted = 1, deleted_reason = ?, deleted_at = ?
                WHERE node_id = ?
                """,
                (deleted_reason, time.time(), deleted_node_id),
            )
            conn.commit()

        reopen_deleted = MemoriaRouter(str(data_dir))
        try:
            reopen_deleted._get_server(project)
        finally:
            reopen_deleted.close()
        with closing(sqlite3.connect(str(db_path))) as conn:
            deleted_row = conn.execute(
                "SELECT deleted, deleted_reason FROM search_index WHERE node_id = ?",
                (deleted_node_id,),
            ).fetchone()
        if not deleted_row or int(deleted_row[0]) != 1 or deleted_row[1] != deleted_reason:
            raise AssertionError(f"search-index rebuild resurrected deleted row: {deleted_row}")

        with closing(sqlite3.connect(str(db_path))) as conn:
            conn.execute("UPDATE search_index SET index_dirty = 1 WHERE deleted = 0")
            conn.commit()

        reopen = MemoriaRouter(str(data_dir))
        try:
            reopen._get_server(project)
        finally:
            reopen.close()
        with closing(sqlite3.connect(str(db_path))) as conn:
            index_dirty = int(conn.execute(
                "SELECT COUNT(*) FROM search_index WHERE index_dirty = 1 AND deleted = 0"
            ).fetchone()[0])
        if index_dirty:
            raise AssertionError(f"search-index rebuild left index_dirty rows: {index_dirty}")
        try:
            del original_auto_maintenance  # type: ignore[name-defined]
            del srv  # type: ignore[name-defined]
            del router  # type: ignore[name-defined]
            del reopen  # type: ignore[name-defined]
            del reopen_deleted  # type: ignore[name-defined]
        except Exception:
            pass
        gc.collect()
        cleanup_warning = ""
        try:
            _delete_project_with_retry(data_dir, project)
        except PermissionError as exc:
            # Windows can briefly hold sqlite files after deliberate reopen
            # checks. The feature result should not fail on cleanup friction.
            cleanup_warning = str(exc)

    details = {
        "project": project,
        "recall_elapsed_seconds": round(elapsed, 4),
        "result_count": recall.get("count"),
        "filtered_weak_count": recall.get("recall_diagnostics", {}).get("filtered_weak_count"),
        "read_only_maintenance_ran": False,
        "deleted_row_preserved_after_rebuild": True,
        "index_dirty_after_rebuild": index_dirty,
    }
    if cleanup_warning:
        details["cleanup_deferred"] = True
    return CheckResult.pass_("recall_quality", **details)


def check_input_encoding_safety(data_dir: Path) -> CheckResult:
    marker = _make_marker("input_safety")
    project = _sanitize_project_name(f"install_check_input_safety_{marker}")
    env = {
        "MEMORIA_MCP_DATA_DIR": str(data_dir),
        "MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE": "live",
        "MEMORIA_MCP_ENABLE_SEMANTIC": "false",
        "MEMORIA_MCP_WRITE_QUEUE_ENABLED": "false",
    }
    samples = {
        "slash_drive": "/D:\\sample-agent-project 资料库\\清债资料\\产品化技术债务表.md",
        "chinese_path": "D:\\sample-agent-project\\资料库\\活资料旧卷\\CURRENT_ACTION_QUEUE-旧卷.md",
        "control_nul": "资料库\x00 活资料 D:\\sample-agent-project",
        "lone_surrogate": "资料库\ud800 活资料 D:\\sample-agent-project",
        "long_chinese": "资料库中文路径" * 1000,
    }

    with _patched_env(env):
        router = MemoriaRouter(str(data_dir))
        try:
            for label, query in samples.items():
                recall = asyncio.run(router._dispatch_tool_for_mcp_async(
                    "memoria_recall",
                    {"project": project, "query": query, "top_k": 3},
                ))
                rendered = _json_text_for_mcp(recall, indent=2)
                rendered.encode("utf-8")
                if any(0xD800 <= ord(char) <= 0xDFFF for char in rendered):
                    raise AssertionError(f"{label} response still contains a lone surrogate")

            long_tokens = tokenize_retrieval_text(samples["long_chinese"], limit=64)
            if not long_tokens:
                raise AssertionError("long Chinese input produced no retrieval tokens")
            max_token_chars = max(len(token) for token in long_tokens)
            if max_token_chars > 128:
                raise AssertionError(f"retrieval token length was not capped: {max_token_chars}")
        finally:
            router.close()
            _delete_project_with_retry(data_dir, project)

    return CheckResult.pass_(
        "input_encoding_safety",
        project=project,
        samples=list(samples.keys()),
        utf8_safe_mcp_response=True,
        max_retrieval_token_chars=max_token_chars,
    )


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def check_search_daemon_safety() -> CheckResult:
    env = {
        "MEMORIA_MCP_ENABLE_SEMANTIC": "false",
        "MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE": "live",
        "MEMORIA_MCP_WRITE_QUEUE_ENABLED": "false",
    }
    with _patched_env(env):
        with tempfile.TemporaryDirectory(prefix="ripple-search-daemon-check-", ignore_cleanup_errors=True) as tmp:
            data_dir = Path(tmp)
            models_dir = data_dir / "models"
            models_dir.mkdir(parents=True, exist_ok=True)
            (models_dir / "config.json").write_text("{}", encoding="utf-8")

            marker = _make_marker("search_daemon")
            project = _sanitize_project_name(f"install_check_search_daemon_{marker}")
            router = MemoriaRouter(str(data_dir))
            daemon_one = None
            daemon_two = None
            try:
                remember = router._dispatch_tool(
                    "memoria_remember",
                    {
                        "project": project,
                        "content": f"{marker}: search daemon safety check memory.",
                        "type": "debug_insight",
                        "importance": 0.6,
                        "confidence": 0.9,
                    },
                )
                if not remember.get("stored"):
                    raise AssertionError(f"search daemon setup remember failed: {remember}")

                from .search_daemon import SearchDaemon

                daemon_one = SearchDaemon(router, str(data_dir))
                daemon_one.start()
                port_file = data_dir / ".search_port"
                first_record = json.loads(port_file.read_text(encoding="utf-8"))
                daemon_two = SearchDaemon(router, str(data_dir))
                daemon_two.start()
                second_record = json.loads(port_file.read_text(encoding="utf-8"))
                if first_record.get("token") == second_record.get("token"):
                    raise AssertionError("two search daemons wrote the same owner token")
                daemon_one.stop()
                after_first_stop = json.loads(port_file.read_text(encoding="utf-8"))
                if after_first_stop.get("token") != second_record.get("token"):
                    raise AssertionError("old search daemon removed or overwrote the newer port file")
                if (models_dir / "memoria.db").exists():
                    raise AssertionError("search daemon treated reserved models directory as a memory project")
                with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
                    sock.bind(("127.0.0.1", 0))
                    stale_port = sock.getsockname()[1]
                stale_record = {
                    "schema": "ripple_memory_search_daemon_v1",
                    "host": "127.0.0.1",
                    "port": stale_port,
                    "pid": 999999,
                    "token": "stale-install-check-token",
                }
                port_file.write_text(json.dumps(stale_record), encoding="utf-8")
                from .search_ipc import request_rerank

                ipc_result = request_rerank(
                    data_dir=data_dir,
                    project=project,
                    query=marker,
                    candidate_ids=[],
                    top_k=1,
                )
                if ipc_result is not None:
                    raise AssertionError("dead search daemon port unexpectedly returned a rerank result")
                if port_file.exists():
                    raise AssertionError("dead search daemon port file was not cleaned up")
            finally:
                if daemon_one is not None:
                    daemon_one.stop()
                if daemon_two is not None:
                    daemon_two.stop()
                router.close()

    return CheckResult.pass_(
        "search_daemon_safety",
        reserved_models_not_project=True,
        owner_token_protects_port_file=True,
        dead_port_file_cleanup=True,
    )


def check_hook_latch_flow(data_dir: Path, workspace: Path) -> CheckResult:
    marker = _make_marker("hook_marker")
    progress_marker = _make_marker("hook_progress")
    project = _sanitize_project_name(f"install_check_hook_{marker}")
    env = {
        "MEMORIA_MCP_DATA_DIR": str(data_dir),
        "MEMORIA_MCP_ENABLE_SEMANTIC": "false",
        "MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE": "live",
        "MEMORIA_MCP_WRITE_QUEUE_ENABLED": "false",
        "RIPPLE_MEMORY_HOOK_ENABLE_SEMANTIC": "false",
        "RIPPLE_MEMORY_HOOK_SEARCH_MODE": "live",
        "RIPPLE_MEMORY_PROJECT": project,
        "RIPPLE_MEMORY_HOOK_ENABLED": None,
    }

    with _patched_env(env):
        router = MemoriaRouter(str(data_dir))
        try:
            remember = router._dispatch_tool(
                "memoria_remember",
                {
                    "project": project,
                    "content": f"{marker}: Hook recall context should inject this memory.",
                    "type": "debug_insight",
                    "importance": 0.9,
                    "confidence": 0.96,
                },
            )
            if not remember.get("stored"):
                raise AssertionError(f"hook setup remember failed: {remember}")
            progress_remember = router._dispatch_tool(
                "memoria_remember",
                {
                    "project": project,
                    "content": (
                        f"{progress_marker}: current phase progress latest commit next step "
                        "record for hook natural-language progress recall."
                    ),
                    "type": "arch_decision",
                    "importance": 0.9,
                    "confidence": 0.96,
                },
            )
            if not progress_remember.get("stored"):
                raise AssertionError(f"hook progress setup remember failed: {progress_remember}")
        finally:
            router.close()

        session = handle_hook_event(
            RippleHookEvent(agent="install-check", event="SessionStart", cwd=str(workspace), project=project),
        )
        if not session.get("ok") or session.get("event") != "session_start":
            raise AssertionError(f"SessionStart hook failed: {session}")

        win_a = "install-check-window-a"
        win_b = "install-check-window-b"
        prompt_a = f"Please continue with {marker} for window A."
        result_a = handle_hook_event(
            RippleHookEvent(
                agent="install-check",
                event="UserPromptSubmit",
                cwd=str(workspace),
                project=project,
                window_id=win_a,
                user_text=prompt_a,
            ),
        )
        context_a = str(result_a.get("context") or "")
        if marker not in context_a:
            raise AssertionError(f"UserPromptSubmit did not inject recalled memory: {result_a}")

        prompt_b_marker = _make_marker("window_b_only")
        result_b = handle_hook_event(
            RippleHookEvent(
                agent="install-check",
                event="UserPromptSubmit",
                cwd=str(workspace),
                project=project,
                window_id=win_b,
                user_text=f"Window B owns {prompt_b_marker}.",
            ),
        )
        context_b = str(result_b.get("context") or "")
        if marker not in context_b:
            raise AssertionError("same-project memory was not visible from window B")
        if not result_b.get("latch", {}).get("updated"):
            raise AssertionError(f"window B latch was not updated: {result_b}")

        progress_win = "install-check-progress-window"
        progress_result = handle_hook_event(
            RippleHookEvent(
                agent="install-check",
                event="UserPromptSubmit",
                cwd=str(workspace),
                project=project,
                window_id=progress_win,
                user_text="任务进度到哪里了",
            ),
        )
        progress_context = str(progress_result.get("context") or "")
        if progress_marker not in progress_context:
            raise AssertionError(f"natural progress prompt did not inject recalled memory: {progress_result}")
        if "phase progress" not in progress_context:
            raise AssertionError("natural progress prompt was not expanded with progress aliases")
        progress_latch = window_latch_file(cwd=workspace, project=project, window_id=progress_win, data_dir=data_dir)
        progress_latch_text = _read_text(progress_latch)
        if "任务进度到哪里了" not in progress_latch_text:
            raise AssertionError("natural progress prompt was not stored as original words")
        if "phase progress" in progress_latch_text:
            raise AssertionError("expanded recall query polluted original words latch")

        latch_a = window_latch_file(cwd=workspace, project=project, window_id=win_a, data_dir=data_dir)
        latch_b = window_latch_file(cwd=workspace, project=project, window_id=win_b, data_dir=data_dir)
        if not latch_a.is_file() or not latch_b.is_file():
            raise AssertionError(f"window latch files missing: {latch_a}, {latch_b}")
        text_a = _read_text(latch_a)
        text_b = _read_text(latch_b)
        if prompt_a not in text_a or prompt_b_marker in text_a:
            raise AssertionError("window A latch was overwritten or polluted by window B")
        if prompt_b_marker not in text_b or prompt_a in text_b:
            raise AssertionError("window B latch was overwritten or polluted by window A")
        if "## Agent Task Understanding" not in text_a or "Hook seed only" in text_a:
            raise AssertionError("window A latch lacks a real agent task-understanding section")

        latch_win = "install-check-latch-window"
        for index in range(12):
            handle_hook_event(
                RippleHookEvent(
                    agent="install-check",
                    event="UserPromptSubmit",
                    cwd=str(workspace),
                    project=project,
                    window_id=latch_win,
                    user_text=f"latch-turn-{index:02d} marker {marker}",
                ),
            )
        latch_file = window_latch_file(cwd=workspace, project=project, window_id=latch_win, data_dir=data_dir)
        latch_text = _read_text(latch_file)
        turn_lines = [line for line in latch_text.splitlines() if line.startswith("- ") and "|" in line]
        if len(turn_lines) != 10:
            raise AssertionError(f"latch should keep exactly 10 user turns, got {len(turn_lines)}")
        if "latch-turn-00" in latch_text or "latch-turn-01" in latch_text or "latch-turn-11" not in latch_text:
            raise AssertionError("latch did not retain the latest 10 turns")

        burst_win = "install-check-burst-window"
        burst_one = f"burst-one {marker} " + ("A" * 700)
        burst_two_marker = f"burst-two {marker}"
        burst_two = burst_two_marker + " " + ("B" * 700)
        first_burst = handle_hook_event(
            RippleHookEvent(
                agent="install-check",
                event="UserPromptSubmit",
                cwd=str(workspace),
                project=project,
                window_id=burst_win,
                session_id="install-check-burst-session",
                turn_id="burst-1",
                user_text=burst_one,
            ),
        )
        if not first_burst.get("latch", {}).get("updated"):
            raise AssertionError(f"first burst prompt should be latched: {first_burst}")
        second_burst = handle_hook_event(
            RippleHookEvent(
                agent="install-check",
                event="UserPromptSubmit",
                cwd=str(workspace),
                project=project,
                window_id=burst_win,
                session_id="install-check-burst-session",
                turn_id="burst-2",
                user_text=burst_two,
            ),
        )
        if second_burst.get("latch", {}).get("reason") != "latch_burst_suppressed":
            raise AssertionError(f"second long burst prompt should be suppressed: {second_burst}")
        burst_latch = window_latch_file(cwd=workspace, project=project, window_id=burst_win, data_dir=data_dir)
        burst_latch_text = _read_text(burst_latch)
        if "burst-one" not in burst_latch_text or burst_two_marker in burst_latch_text:
            raise AssertionError("latch burst gate did not keep only the first long prompt")

        suppressed_stop_marker = _make_marker("suppressed_stop")
        burst_stop = handle_hook_event(
            RippleHookEvent(
                agent="install-check",
                event="Stop",
                cwd=str(workspace),
                project=project,
                window_id=burst_win,
                session_id="install-check-burst-session",
                turn_id="burst-2",
                assistant_text=f"{suppressed_stop_marker}: should not overwrite latch understanding.",
            ),
        )
        if burst_stop.get("latch", {}).get("reason") != "latch_burst_suppressed_stop":
            raise AssertionError(f"burst-suppressed Stop should not refresh latch: {burst_stop}")
        if suppressed_stop_marker in _read_text(burst_latch):
            raise AssertionError("burst-suppressed Stop polluted latch understanding")

        short_burst_marker = _make_marker("short_burst")
        short_burst = handle_hook_event(
            RippleHookEvent(
                agent="install-check",
                event="UserPromptSubmit",
                cwd=str(workspace),
                project=project,
                window_id=burst_win,
                session_id="install-check-burst-session",
                turn_id="burst-3",
                user_text=short_burst_marker,
            ),
        )
        if not short_burst.get("latch", {}).get("updated"):
            raise AssertionError(f"short prompt after burst should still latch: {short_burst}")
        short_stop_marker = _make_marker("short_stop")
        handle_hook_event(
            RippleHookEvent(
                agent="install-check",
                event="Stop",
                cwd=str(workspace),
                project=project,
                window_id=burst_win,
                session_id="install-check-burst-session",
                turn_id="burst-3",
                assistant_text=f"{short_stop_marker}: short prompt should refresh latch understanding.",
            ),
        )
        burst_latch_text = _read_text(burst_latch)
        if short_burst_marker not in burst_latch_text or short_stop_marker not in burst_latch_text:
            raise AssertionError("short prompt after burst did not latch and refresh understanding")

        agent_understanding_marker = _make_marker("agent_understanding")
        stop = handle_hook_event(
            RippleHookEvent(
                agent="install-check",
                event="Stop",
                cwd=str(workspace),
                project=project,
                window_id=win_a,
                assistant_text=(
                    f"Agent understanding {agent_understanding_marker}: "
                    "finish the current task, preserve boundaries, then run verification."
                ),
            ),
        )
        if str(stop.get("context") or ""):
            raise AssertionError(f"Stop hook emitted context unexpectedly: {stop}")
        text_a_after_stop = _read_text(latch_a)
        if agent_understanding_marker not in text_a_after_stop:
            raise AssertionError("Stop hook did not refresh agent task understanding in latch")
        if "- State: active" not in text_a_after_stop:
            raise AssertionError("ordinary Stop checkpoint should keep active task state")
        if "Next action: Resume from the concrete next step" in text_a_after_stop:
            raise AssertionError("Stop hook wrote the old generic Resume next-action")

        completed_win = "install-check-completed-window"
        completed_marker = _make_marker("completed_stop")
        completed_stop = handle_hook_event(
            RippleHookEvent(
                agent="install-check",
                event="Stop",
                cwd=str(workspace),
                project=project,
                window_id=completed_win,
                assistant_text=(
                    f"{completed_marker}: 已提交到当前分支，验证通过，"
                    "当前工作树干净。"
                ),
            ),
        )
        if completed_stop.get("latch", {}).get("task_state") != "completed":
            raise AssertionError(f"completed Stop was not classified: {completed_stop}")
        completed_latch = window_latch_file(cwd=workspace, project=project, window_id=completed_win, data_dir=data_dir)
        completed_latch_text = _read_text(completed_latch)
        if "- State: completed" not in completed_latch_text or "Completed checkpoint" not in completed_latch_text:
            raise AssertionError("completed latch did not record completed checkpoint state")
        completed_session = handle_hook_event(
            RippleHookEvent(
                agent="install-check",
                event="SessionStart",
                cwd=str(workspace),
                project=project,
                window_id=completed_win,
            ),
        )
        completed_context = str(completed_session.get("context") or "")
        if "- State: completed" not in completed_context or "Completed checkpoint" not in completed_context:
            raise AssertionError("SessionStart context did not preserve completed checkpoint state")
        if "Next action: Resume from the concrete next step" in completed_context:
            raise AssertionError("SessionStart context still contains old generic Resume next-action")

        disabled = workspace / ".ripple-memory" / "hooks.disabled"
        disabled.parent.mkdir(parents=True, exist_ok=True)
        disabled.write_text("disabled for install check\n", encoding="utf-8")
        disabled_result = handle_hook_event(
            RippleHookEvent(
                agent="install-check",
                event="UserPromptSubmit",
                cwd=str(workspace),
                project=project,
                window_id="disabled-window",
                user_text="this should not write a latch",
            ),
        )
        disabled.unlink(missing_ok=True)
        if disabled_result.get("enabled") is not False or disabled_result.get("context"):
            raise AssertionError(f"hook kill switch failed: {disabled_result}")
        disabled_latch = window_latch_file(cwd=workspace, project=project, window_id="disabled-window", data_dir=data_dir)
        if disabled_latch.exists():
            raise AssertionError("disabled hook still wrote a latch")

        _delete_project_with_retry(data_dir, project)

    return CheckResult.pass_(
        "hook_latch_window_flow",
        session_start_ok=True,
        user_prompt_context_contains_marker=True,
        same_project_memory_shared_across_windows=True,
        natural_progress_prompt_recall=True,
        stop_no_context=True,
        stop_refreshes_agent_understanding=True,
        completed_stop_do_not_redo=True,
        latch_burst_suppresses_long_prompts=True,
        latch_burst_allows_short_prompts=True,
        env_file_kill_switch=True,
        latch_latest_turns=10,
        window_a_latch=str(latch_a),
        window_b_latch=str(latch_b),
    )


def check_lifecycle_management(data_dir: Path, workspace: Path) -> CheckResult:
    if DEFAULT_IDLE_EXIT_SECONDS != 10 * 60 * 60.0:
        raise AssertionError(f"default idle exit should be 10 hours: {DEFAULT_IDLE_EXIT_SECONDS}")
    marker = _make_marker("lifecycle")
    project = _sanitize_project_name(f"install_check_lifecycle_{marker}")
    window_id = "install-check-lifecycle-window"
    env = {
        "MEMORIA_MCP_DATA_DIR": str(data_dir),
        "MEMORIA_MCP_WRITE_QUEUE_ENABLED": "false",
        "RIPPLE_MEMORY_PROJECT": project,
        "RIPPLE_MEMORY_HOOK_ENABLE_SEMANTIC": "false",
        "RIPPLE_MEMORY_HOOK_SEARCH_MODE": "live",
    }
    with _patched_env(env):
        registry = ProcessRegistry(data_dir, host="install-check", window_id=window_id, session_id="install-check-session")
        registry.register(status="active")
        registry.heartbeat(status="active", check_marker=marker)
        records = registry.list_processes()
        if not any(int(item.get("pid") or 0) == os.getpid() for item in records):
            raise AssertionError(f"process registry did not include current process: {records}")

        stale_path = registry.process_dir / "99999999.json"
        stale_path.parent.mkdir(parents=True, exist_ok=True)
        stale_path.write_text(json.dumps({
            "schema": "ripple_memory_process_v1",
            "pid": 99999999,
            "status": "active",
            "last_seen_at": 1,
        }), encoding="utf-8")
        cleanup = registry.cleanup_stale_records(stale_after_seconds=0)
        if stale_path.exists() or cleanup.get("removed_count", 0) < 1:
            raise AssertionError(f"stale process cleanup failed: {cleanup}")

        dead_parent = 99999991
        while is_process_alive(dead_parent):
            dead_parent += 1
        orphan_proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", "memoria_mcp.server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        safe_proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", "memoria_mcp.server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            orphan_record = registry.process_dir / f"{orphan_proc.pid}.json"
            safe_record = registry.process_dir / f"{safe_proc.pid}.json"
            base_record = {
                "schema": "ripple_memory_process_v1",
                "status": "active",
                "parent_pid": dead_parent,
                "last_seen_at": time.time(),
                "argv": [sys.executable, "-m", "memoria_mcp.server"],
                "executable": sys.executable,
            }
            orphan_record.write_text(
                json.dumps({**base_record, "pid": orphan_proc.pid, "base_data_dir": str(data_dir)}),
                encoding="utf-8",
            )
            safe_record.write_text(
                json.dumps({**base_record, "pid": safe_proc.pid, "base_data_dir": str(data_dir / "other-host")}),
                encoding="utf-8",
            )
            orphan_cleanup = registry.cleanup_orphaned_processes()
            orphan_proc.wait(timeout=5)
            if is_process_alive(orphan_proc.pid) or orphan_record.exists():
                raise AssertionError(f"orphan cleanup did not kill same-data-dir process: {orphan_cleanup}")
            if not is_process_alive(safe_proc.pid) or not safe_record.exists():
                raise AssertionError("orphan cleanup killed or removed a different-data-dir process")
        finally:
            for proc in (orphan_proc, safe_proc):
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            safe_record.unlink(missing_ok=True)

        parent_exit_data_dir = data_dir / f"_install_check_parent_exit_{marker}"
        try:
            parent_exit_registry = ProcessRegistry(parent_exit_data_dir, host="install-check", window_id="parent-exit", session_id="parent-exit")
            parent_exit_registry.parent_pid = dead_parent
            parent_exit_manager = IdleLifecycleManager(
                close_cached_state=lambda: None,
                registry=parent_exit_registry,
                sleep_seconds=9999,
                exit_seconds=0,
                heartbeat_seconds=0.2,
                exit_process=False,
            )
            parent_exit_manager.start()
            if parent_exit_manager._thread is not None:
                parent_exit_manager._thread.join(timeout=2)
            parent_final = parent_exit_registry.record_path.with_suffix(".final.json")
            if not parent_final.is_file():
                raise AssertionError("parent death detection did not write final record")
            parent_payload = json.loads(parent_final.read_text(encoding="utf-8"))
            if parent_payload.get("status") != "parent_exit":
                raise AssertionError(f"parent death detection used wrong status: {parent_payload}")
        finally:
            shutil.rmtree(parent_exit_data_dir, ignore_errors=True)

        tool_stuck_data_dir = data_dir / f"_install_check_tool_stuck_{marker}"
        try:
            tool_stuck_registry = ProcessRegistry(
                tool_stuck_data_dir,
                host="install-check",
                window_id="tool-stuck",
                session_id="tool-stuck",
            )
            tool_stuck_manager = IdleLifecycleManager(
                close_cached_state=lambda: None,
                registry=tool_stuck_registry,
                sleep_seconds=9999,
                exit_seconds=0,
                heartbeat_seconds=0.2,
                exit_process=False,
            )
            tool_stuck_manager.start()
            now = time.time()
            active_tools = {
                "fresh-call": {
                    "call_id": "fresh-call",
                    "tool": "memoria_read",
                    "project": project,
                    "started_at": now,
                    "timeout_seconds": 0.5,
                    "deadline_at": now + 30,
                    "stuck_exit_seconds": 30,
                },
                "stuck-call": {
                    "call_id": "stuck-call",
                    "tool": "memoria_recall",
                    "project": project,
                    "started_at": now - 10,
                    "timeout_seconds": 0.5,
                    "deadline_at": now - 1,
                    "stuck_exit_seconds": 1,
                },
            }
            tool_stuck_registry.heartbeat(
                status="active",
                active_tools=active_tools,
                active_tool="memoria_read",
                active_tool_project=project,
                active_tool_started_at=now,
                active_tool_timeout_seconds=0.5,
                active_tool_deadline_at=now + 30,
            )
            if tool_stuck_manager._thread is not None:
                tool_stuck_manager._thread.join(timeout=2)
            tool_stuck_final = tool_stuck_registry.record_path.with_suffix(".final.json")
            if not tool_stuck_final.is_file():
                raise AssertionError("tool stuck detection did not write final record")
            tool_stuck_payload = json.loads(tool_stuck_final.read_text(encoding="utf-8"))
            if tool_stuck_payload.get("status") != "tool_stuck_exit":
                raise AssertionError(f"tool stuck detection used wrong status: {tool_stuck_payload}")
            if tool_stuck_payload.get("active_tool") != "memoria_recall":
                raise AssertionError(f"tool stuck detection lost active tool: {tool_stuck_payload}")
            if tool_stuck_payload.get("active_tool_call_id") != "stuck-call":
                raise AssertionError(f"tool stuck detection lost call id: {tool_stuck_payload}")
        finally:
            shutil.rmtree(tool_stuck_data_dir, ignore_errors=True)

        loop_error_data_dir = data_dir / f"_install_check_lifecycle_loop_error_{marker}"
        try:
            loop_error_registry = ProcessRegistry(
                loop_error_data_dir,
                host="install-check",
                window_id="loop-error",
                session_id="loop-error",
            )
            loop_error_manager = IdleLifecycleManager(
                close_cached_state=lambda: None,
                registry=loop_error_registry,
                sleep_seconds=9999,
                exit_seconds=0,
                heartbeat_seconds=0.2,
                exit_process=False,
            )

            def broken_pop_exit_request() -> None:
                raise RuntimeError("install-check lifecycle loop guard")

            loop_error_registry.pop_exit_request = broken_pop_exit_request  # type: ignore[method-assign]
            loop_error_manager.start()
            time.sleep(0.35)
            loop_error_payload = json.loads(loop_error_registry.record_path.read_text(encoding="utf-8"))
            if "lifecycle_loop_error" not in loop_error_payload:
                raise AssertionError(f"lifecycle loop error was not recorded: {loop_error_payload}")
            if loop_error_manager._thread is None or not loop_error_manager._thread.is_alive():
                raise AssertionError("lifecycle thread died after loop error")
            loop_error_manager.stop()
        finally:
            shutil.rmtree(loop_error_data_dir, ignore_errors=True)

        exit_request = registry.request_exit_for_window(
            window_id=window_id,
            session_id="install-check-session",
            reason="install_check",
            include_current=True,
        )
        if os.getpid() not in [int(pid) for pid in exit_request.get("requested_pids") or []]:
            raise AssertionError(f"window process exit request did not target current process: {exit_request}")
        exit_payload = registry.pop_exit_request()
        if not exit_payload or exit_payload.get("action") != "exit":
            raise AssertionError(f"window process exit request was not readable: {exit_payload}")

        router = MemoriaRouter(str(data_dir))
        manager = IdleLifecycleManager(
            close_cached_state=router.sleep_cached_state,
            registry=registry,
            sleep_seconds=9999,
            exit_seconds=0,
            heartbeat_seconds=9999,
            exit_process=False,
        )
        router.set_lifecycle_manager(manager)
        try:
            router._dispatch_tool(
                "memoria_remember",
                {
                    "project": project,
                    "content": f"{marker}: lifecycle sleep should preserve project data.",
                    "type": "debug_insight",
                    "importance": 0.7,
                    "confidence": 0.8,
                },
            )
            if project not in router._servers:
                raise AssertionError("setup remember did not create cached project server")
            sleep = manager.sleep_now(reason="install_check")
            if not sleep.get("slept") or project in router._servers:
                raise AssertionError(f"idle sleep did not unload cached server: {sleep}")
        finally:
            router.close()
            registry.unregister(status="install_check_done")

        prompt = f"Archive/restore window state {marker}"
        submit = handle_hook_event(
            RippleHookEvent(
                agent="install-check",
                event="UserPromptSubmit",
                cwd=str(workspace),
                project=project,
                window_id=window_id,
                user_text=prompt,
            ),
        )
        if not submit.get("latch", {}).get("updated"):
            raise AssertionError(f"lifecycle setup latch failed: {submit}")
        active_latch = window_latch_file(cwd=workspace, project=project, window_id=window_id, data_dir=data_dir)
        if not active_latch.is_file():
            raise AssertionError("active latch missing before archive")

        archive = handle_hook_event(
            RippleHookEvent(
                agent="install-check",
                event="WindowArchive",
                cwd=str(workspace),
                project=project,
                window_id=window_id,
            ),
        )
        if active_latch.exists() or not archive.get("window_lifecycle", {}).get("moved"):
            raise AssertionError(f"window archive did not move active latch: {archive}")
        archive_path = Path(str(archive.get("window_lifecycle", {}).get("archive_path") or ""))
        if str(data_dir) not in str(archive_path):
            raise AssertionError(f"window archive should live under data dir: {archive_path}")
        workspace_archive_root = workspace / ".ripple-memory" / "archived-windows"
        if workspace_archive_root.exists():
            raise AssertionError(f"workspace archive root should not be created: {workspace_archive_root}")

        restore = handle_hook_event(
            RippleHookEvent(
                agent="install-check",
                event="WindowRestore",
                cwd=str(workspace),
                project=project,
                window_id=window_id,
            ),
        )
        if not active_latch.is_file() or marker not in _read_text(active_latch):
            raise AssertionError(f"window restore did not restore latch: {restore}")

        delete = handle_hook_event(
            RippleHookEvent(
                agent="install-check",
                event="WindowDelete",
                cwd=str(workspace),
                project=project,
                window_id=window_id,
            ),
        )
        if active_latch.exists() or delete.get("window_lifecycle", {}).get("active_exists_after"):
            raise AssertionError(f"window delete did not clear active latch: {delete}")

        _delete_project_with_retry(data_dir, project)

    return CheckResult.pass_(
        "lifecycle_management",
        process_registered=True,
        stale_process_cleanup=True,
        orphan_process_cleanup=True,
        parent_death_detection=True,
        tool_stuck_exit=True,
        lifecycle_loop_error_survives=True,
        window_process_exit_request=True,
        idle_sleep_unloaded_cached_project=True,
        default_idle_exit_seconds=DEFAULT_IDLE_EXIT_SECONDS,
        window_archive=True,
        window_archive_in_data_dir=True,
        window_restore=True,
        window_delete=True,
    )


def _run_hook_command(command: Path, payload: Dict[str, Any], env: Dict[str, str]) -> subprocess.CompletedProcess[str]:
    if os.name == "nt" and command.suffix.lower() in {".cmd", ".bat"}:
        cmd = ["cmd.exe", "/c", str(command)]
    else:
        cmd = [str(command)]
    return subprocess.run(
        cmd,
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=10,
        env=env,
    )


def _windows_script_command(command: Path, args: List[str]) -> List[str]:
    suffix = command.suffix.lower()
    if os.name == "nt" and suffix in {".cmd", ".bat"}:
        return ["cmd.exe", "/c", str(command), *args]
    if os.name == "nt" and suffix == ".ps1":
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(command), *args]
    return [str(command), *args]


def _find_codex_command() -> Optional[Path]:
    candidates = ["codex"]
    if os.name == "nt":
        candidates = ["codex.cmd", "codex.exe", "codex.ps1", "codex"]
    for name in candidates:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def check_hook_command(
    *,
    hook_cmd: Optional[Path],
    data_dir: Path,
    workspace: Path,
    require_hook_cmd: bool,
) -> CheckResult:
    if not hook_cmd:
        if require_hook_cmd:
            return CheckResult.fail("hook_command", "hook command was not found")
        return CheckResult.skip("hook_command", "hook command was not found")
    if not hook_cmd.is_file():
        return CheckResult.fail("hook_command", "hook command path does not exist", path=str(hook_cmd))

    marker = _make_marker("hook_cmd_marker")
    project = _sanitize_project_name(f"install_check_hook_cmd_{marker}")
    hook_env = {
        "MEMORIA_MCP_DATA_DIR": str(data_dir),
        "MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE": "live",
        "MEMORIA_MCP_WRITE_QUEUE_ENABLED": "false",
        "RIPPLE_MEMORY_PROJECT": project,
        "RIPPLE_MEMORY_WINDOW_ID": "install-check-hook-command-window",
        "RIPPLE_MEMORY_HOOK_ENABLE_SEMANTIC": "false",
        "RIPPLE_MEMORY_HOOK_SEARCH_MODE": "live",
    }
    with _patched_env(hook_env):
        router = MemoriaRouter(str(data_dir))
        try:
            remember = router._dispatch_tool(
                "memoria_remember",
                {
                    "project": project,
                    "content": f"{marker}: Hook command should return this in additional context.",
                    "type": "debug_insight",
                    "importance": 0.9,
                    "confidence": 0.96,
                },
            )
            if not remember.get("stored"):
                raise AssertionError(f"hook command setup remember failed: {remember}")
        finally:
            router.close()

        env = os.environ.copy()
        payload = {
            "eventName": "UserPromptSubmit",
            "cwd": str(workspace),
            "threadId": "install-check-thread",
            "turnId": "install-check-turn",
            "prompt": f"Recall hook command marker {marker}",
        }
        proc = _run_hook_command(hook_cmd, payload, env)
    stdout = proc.stdout.strip()
    last_line = stdout.splitlines()[-1] if stdout else ""
    try:
        output = json.loads(last_line)
    except json.JSONDecodeError as exc:
        return CheckResult.fail(
            "hook_command",
            f"hook command did not print JSON: {exc}",
            path=str(hook_cmd),
            exit_code=proc.returncode,
            stdout=proc.stdout[-1000:],
            stderr=proc.stderr[-1000:],
        )

    latch = window_latch_file(cwd=workspace, project=project, window_id="install-check-hook-command-window", data_dir=data_dir)
    context = _hook_context_from_output(output)
    context_contains_marker = marker in context
    latch_written = latch.is_file()
    ok = proc.returncode == 0 and output.get("continue", True) is True and context_contains_marker and latch_written

    _delete_project_with_retry(data_dir, project)

    if not ok:
        return CheckResult.fail(
            "hook_command",
            "hook command did not complete, inject context, and write latch",
            path=str(hook_cmd),
            exit_code=proc.returncode,
            output=output,
            context_contains_marker=context_contains_marker,
            latch_written=latch_written,
            stderr=proc.stderr[-1000:],
        )
    return CheckResult.pass_(
        "hook_command",
        path=str(hook_cmd),
        exit_code=proc.returncode,
        context_contains_marker=True,
        latch_written=True,
    )


def check_codex_live(
    *,
    enabled: bool,
    data_dir: Path,
    workspace: Path,
    timeout_seconds: int,
) -> CheckResult:
    if not enabled:
        return CheckResult.skip("codex_live_hook", "not requested")

    marker = f"RIPPLE-LIVE-{int(time.time() * 1000)}"
    project = _sanitize_project_name(f"install_check_codex_live_{marker}")
    router = MemoriaRouter(str(data_dir))
    try:
        remember = router._dispatch_tool(
            "memoria_remember",
            {
                "project": project,
                "content": (
                    "For the Ripple install-check live hook topic, the secret word is "
                    f"{marker}. This secret is only in Ripple Memory."
                ),
                "type": "debug_insight",
                "importance": 0.92,
                "confidence": 0.97,
            },
        )
        if not remember.get("stored"):
            raise AssertionError(f"codex live setup remember failed: {remember}")
    finally:
        router.close()

    debug_log = workspace / "ripple-live-hook.log"
    env = os.environ.copy()
    env.update({
        "MEMORIA_MCP_DATA_DIR": str(data_dir),
        "RIPPLE_MEMORY_PROJECT": project,
        "RIPPLE_MEMORY_WINDOW_ID": "install-check-codex-live-window",
        "RIPPLE_MEMORY_HOOK_DEBUG_LOG": str(debug_log),
        "RIPPLE_MEMORY_HOOK_ENABLE_SEMANTIC": "false",
        "RIPPLE_MEMORY_HOOK_SEARCH_MODE": "live",
    })
    prompt = (
        "For the Ripple install-check live hook topic, reply with exactly the "
        "secret word from injected Ripple Memory context and nothing else."
    )
    try:
        command = _find_codex_command()
        if command is None:
            return CheckResult.fail("codex_live_hook", "codex command was not found")
        proc = subprocess.run(
            _windows_script_command(
                command,
                ["exec", "--skip-git-repo-check", "-C", str(workspace), prompt],
            ),
            input="",
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            env=env,
        )
    except FileNotFoundError:
        return CheckResult.fail("codex_live_hook", "codex command was not found")
    except PermissionError as exc:
        return CheckResult.fail("codex_live_hook", f"permission error while launching codex: {exc}")
    except subprocess.TimeoutExpired as exc:
        return CheckResult.fail("codex_live_hook", f"codex exec timed out after {timeout_seconds}s", stdout=exc.stdout or "", stderr=exc.stderr or "")

    debug_text = debug_log.read_text(encoding="utf-8", errors="replace") if debug_log.is_file() else ""
    latch = window_latch_file(cwd=workspace, project=project, window_id="install-check-codex-live-window", data_dir=data_dir)
    latch_text = latch.read_text(encoding="utf-8", errors="replace") if latch.is_file() else ""
    user_prompt_logged = "event=UserPromptSubmit" in debug_text
    stop_logged = "event=Stop" in debug_text
    latch_contains_stop_marker = marker in latch_text

    _delete_project_with_retry(data_dir, project)

    if proc.returncode != 0 or marker not in proc.stdout or not user_prompt_logged or not stop_logged or not latch_contains_stop_marker:
        return CheckResult.fail(
            "codex_live_hook",
            "codex live hook did not prove context injection and Stop latch refresh",
            exit_code=proc.returncode,
            stdout=proc.stdout[-1000:],
            stderr=proc.stderr[-1000:],
            marker_seen_in_stdout=marker in proc.stdout,
            user_prompt_logged=user_prompt_logged,
            stop_logged=stop_logged,
            latch_written=latch.is_file(),
            latch_contains_stop_marker=latch_contains_stop_marker,
        )
    return CheckResult.pass_(
        "codex_live_hook",
        marker_seen_in_stdout=True,
        user_prompt_logged=True,
        stop_logged=True,
        latch_written=latch.is_file(),
        latch_contains_stop_marker=True,
        debug_log=str(debug_log),
    )


def run_checks(args: argparse.Namespace) -> Dict[str, Any]:
    data_dir = Path(args.data_dir or _default_data_dir()).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    workspace_context: Any
    if args.workspace:
        workspace = Path(args.workspace).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        workspace_context = None
    else:
        workspace_context = tempfile.TemporaryDirectory(prefix="ripple-install-check-workspace-")
        workspace = Path(workspace_context.name)

    hook_cmd = Path(args.hook_cmd).expanduser().resolve() if args.hook_cmd else _default_hook_cmd(args.host)
    results: List[CheckResult] = []
    try:
        with _patched_env({"MEMORIA_MCP_DATA_DIR": str(data_dir)}):
            checks = [
                ("embedding_config", lambda: check_embedding_config(
                    require_semantic=args.require_semantic,
                    require_local_model=args.require_local_model,
                )),
                ("skill_guidance", lambda: check_skill(args.host, args.skill_path, require_skill=not args.no_require_skill)),
                ("mcp_tools_project_database", lambda: check_mcp_database_project_flow(data_dir)),
                ("memory_evolution", lambda: check_memory_evolution_flow(data_dir)),
                ("storage_architecture", lambda: check_storage_architecture_flow(data_dir)),
                ("soft_timeout_recovery", lambda: check_soft_timeout_recovery(data_dir)),
                ("write_queue_flow", lambda: check_write_queue_flow(data_dir)),
                ("recall_quality", lambda: check_recall_quality_flow(data_dir)),
                ("input_encoding_safety", lambda: check_input_encoding_safety(data_dir)),
                ("host_mcp_process", lambda: check_host_mcp_process(
                    host=args.host,
                    data_dir=data_dir,
                    require_host_mcp_process=args.require_host_mcp_process,
                )),
                ("live_smoke", lambda: check_live_smoke(
                    data_dir=data_dir,
                    hook_cmd=hook_cmd,
                    host=args.host,
                ) if args.live_smoke else CheckResult.skip("live_smoke", "not requested")),
                ("agent_daemon_flow", check_agent_daemon_flow),
                ("mcp_stdio_protocol", lambda: check_mcp_stdio_protocol(data_dir)),
                ("search_daemon_safety", check_search_daemon_safety),
                ("hook_latch_window_flow", lambda: check_hook_latch_flow(data_dir, workspace)),
                ("lifecycle_management", lambda: check_lifecycle_management(data_dir, workspace)),
                ("hook_command", lambda: check_hook_command(
                    hook_cmd=hook_cmd,
                    data_dir=data_dir,
                    workspace=workspace,
                    require_hook_cmd=args.require_hook_cmd,
                )),
                ("codex_live_hook", lambda: check_codex_live(
                    enabled=args.codex_live,
                    data_dir=data_dir,
                    workspace=workspace,
                    timeout_seconds=args.codex_timeout,
                )),
            ]
            for name, check in checks:
                try:
                    results.append(check())
                except Exception as exc:  # noqa: BLE001 - this is a diagnostic command
                    results.append(CheckResult.fail(name, f"{exc.__class__.__name__}: {exc}"))
    finally:
        if workspace_context is not None:
            workspace_context.cleanup()

    failed = [item for item in results if not item.ok]
    return {
        "ok": not failed,
        "host": args.host,
        "data_dir": str(data_dir),
        "workspace": str(workspace),
        "hook_cmd": str(hook_cmd) if hook_cmd else "",
        "summary": {
            "passed": sum(1 for item in results if item.status == "pass"),
            "skipped": sum(1 for item in results if item.status == "skip"),
            "failed": len(failed),
        },
        "checks": [item.__dict__ for item in results],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a practical post-install health check for Ripple Memory.",
    )
    parser.add_argument(
        "--host",
        default="auto",
        choices=[
            "auto",
            "codex",
            "claude",
            "claude-code",
            "qwen",
            "qwen-code",
            "mimo",
            "mimo-code",
            "mimocode",
            "generic",
            "custom",
        ],
    )
    parser.add_argument("--data-dir", default=str(_default_data_dir()), help="Memory data directory used by the installed host.")
    parser.add_argument("--workspace", help="Workspace for hook/latch tests. Defaults to a temporary directory.")
    parser.add_argument("--skill-path", help="Explicit path to ripple-memory/SKILL.md.")
    parser.add_argument("--hook-cmd", help="Explicit host hook command path.")
    parser.add_argument("--require-semantic", action="store_true", help="Fail if semantic recall is disabled or dependencies are missing.")
    parser.add_argument("--require-local-model", action="store_true", help="Fail if the configured local embedding model path does not exist.")
    parser.add_argument("--no-require-skill", action="store_true", help="Do not fail when the skill is not installed.")
    parser.add_argument("--require-hook-cmd", action="store_true", help="Fail when a host hook command cannot be found.")
    parser.add_argument("--require-host-mcp-process", action="store_true", help="Fail unless the target host has a live registered Ripple MCP daemon/proxy process for this data directory.")
    parser.add_argument("--codex-live", action="store_true", help="Also run a real codex exec hook-injection proof. This may call the configured model.")
    parser.add_argument("--codex-timeout", type=int, default=90, help="Timeout in seconds for --codex-live.")
    parser.add_argument("--live-smoke", action="store_true", help="Run end-to-end smoke test against the real running daemon and data dir (not sandboxed). Requires host to have MCP running.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)
    report = run_checks(args)
    print(json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None, default=_json_default))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
