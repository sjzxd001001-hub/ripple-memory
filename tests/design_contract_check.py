"""Design-contract checks for Ripple Memory.

This test is deliberately broad and shallow. It documents architecture choices
that are product contracts, so future AI-assisted edits can improve internals
without quietly breaking the memory engine's shape.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "memoria_mcp"

EXPECTED_CORE_TOOLS = [
    "memoria_remember",
    "memoria_recall",
    "memoria_read",
    "memoria_forget",
]

REQUIRED_REGRESSION_TESTS = [
    "baseline_engine_check.py",
    "import_hygiene_check.py",
    "agent_daemon_check.py",
    "model_baseline_check.py",
    "memory_evolution_check.py",
    "storage_architecture_check.py",
    "soft_timeout_recovery_check.py",
    "write_queue_check.py",
    "recall_quality_check.py",
    "hook_adapter_check.py",
    "claude_code_hook_check.py",
    "mimocode_hook_check.py",
]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _literal_assignment(relative: str, name: str) -> Any:
    tree = ast.parse(_read(relative), filename=relative)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{relative} does not define literal assignment {name}")


def _find_source_token(token: str) -> list[str]:
    hits: list[str] = []
    for path in SRC.rglob("*.py"):
        if path.name in {"install_check.py"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if token in text:
            hits.append(str(path.relative_to(ROOT)))
    return hits


def check_tool_surface() -> dict[str, Any]:
    tools = _literal_assignment("src/memoria_mcp/tool_specs.py", "EXPECTED_CORE_TOOLS")
    _require(tools == EXPECTED_CORE_TOOLS, f"canonical MCP tools drifted: {tools}")

    tool_specs = _read("src/memoria_mcp/tool_specs.py")
    _require("def build_memory_tools(*, expose_project_tools: bool = False)" in tool_specs, "tool builder must hide project-management tools by default")
    _require("if expose_project_tools:" in tool_specs, "optional project tools must stay opt-in")
    for name in EXPECTED_CORE_TOOLS:
        _require(tool_specs.count(f'name="{name}"') == 1, f"{name} tool spec should appear exactly once")
    _require("fact_key" in tool_specs and "supersedes_ref_ids" in tool_specs, "remember must preserve memory-evolution inputs")
    _require("include_evolution" in tool_specs, "recall must preserve evolution audit option")
    _require("read_hint" in _read("src/memoria_mcp/server.py"), "recall results must keep read navigation hints")
    return {"tools": tools, "optional_project_tools": "opt_in"}


def check_daemon_architecture() -> dict[str, Any]:
    server = _read("src/memoria_mcp/server.py")
    proxy = _read("src/memoria_mcp/mcp_stdio_proxy.py")
    daemon = _read("src/memoria_mcp/agent_daemon.py")
    daemon_client = _read("src/memoria_mcp/daemon_client.py")

    _require("from .mcp_stdio_proxy import main as proxy_main" in server and "await proxy_main()" in server, "server entry must delegate to the stdio proxy")
    _require("call_daemon_tool" in proxy and "ensure_agent_daemon" in proxy, "stdio proxy must forward execution to the agent daemon")
    _require("build_memory_tools" in proxy, "stdio proxy should expose the canonical tool specs only")
    for forbidden in ("MemoryGraph", "MemoriaPersistence", "ProjectWriteQueue", "_get_embedding_model"):
        _require(forbidden not in proxy, f"stdio proxy must not own core memory state: {forbidden}")

    _require('DAEMON_START_LOCK_NAME = "daemon.start.lock"' in daemon, "daemon must own a singleton start lock")
    _require("_preload_embedding_model_if_requested()" in daemon, "daemon should preload/cache the embedding model when requested")
    _require("SearchDaemon" in daemon, "agent daemon must preserve the search/rerank daemon integration")
    _require("self.tool_lock" in daemon and "call_tool" in daemon, "daemon must serialize tool execution")
    _require("agent_exit_grace_seconds" in daemon, "daemon lifetime must be tied to the owning agent, not a single window")
    _require("superseded_daemon" in daemon, "daemon must self-exit when another live singleton takes over")

    _require("RECV_TIMEOUT_SECONDS = 30.0" in daemon_client, "daemon client response timeout must not regress to the old 8s value")
    timeout_match = re.search(r"except socket\.timeout:(.*?)(?:\n    except |\n    finally:)", daemon_client, re.S)
    _require(timeout_match is not None, "daemon client must handle socket response timeout explicitly")
    _require("remove_port_record" not in timeout_match.group(1), "response timeout must preserve port.json instead of deleting a live daemon")

    for token in ("tool_worker", "MEMORIA_MCP_READONLY_PROCESS_GUARD"):
        hits = _find_source_token(token)
        _require(not hits, f"window-era read-only worker residue found for {token}: {hits}")
    return {
        "server_entry": "stdio_proxy",
        "state_owner": "agent_daemon",
        "readonly_worker_removed": True,
    }


def check_storage_and_evolution() -> dict[str, Any]:
    config = _read("src/memoria_mcp/config.py")
    persistence = _read("src/memoria_mcp/persistence.py")
    search_index = _read("src/memoria_mcp/search_index.py")
    archive = _read("src/memoria_mcp/archive.py")
    dreamer = _read("src/memoria_mcp/dreamer.py")
    server = _read("src/memoria_mcp/server.py")

    for table in ("graph_state", "memory_evolution_state", "memory_evolution_edges"):
        _require(f"CREATE TABLE IF NOT EXISTS {table}" in persistence, f"SQLite runtime table missing: {table}")
    for obsolete in ("archive_blocks", "memory_stream"):
        _require(f"CREATE TABLE IF NOT EXISTS {obsolete}" not in persistence, f"obsolete SQL payload table must not be recreated: {obsolete}")
        _require(f"DROP TABLE IF EXISTS {obsolete}" in persistence, f"old {obsolete} table should be dropped during migration")

    _require("CREATE TABLE IF NOT EXISTS search_index" in search_index, "search_index table must exist")
    for column in ("json_file", "json_offset", "deleted_reason", "index_dirty", "content_signature"):
        _require(column in search_index, f"search_index must retain {column} metadata")
    _require("append_memory_stream_entry" in archive and "read_memory_stream_entry" in archive, "JSONL archive rail must store and hydrate full content")
    _require("purge_memory_stream_entries" in archive, "forget must be able to remove readable JSONL content")
    _require("memory_evolution_superseded" in config, "Dreamer must allow superseded evolution rows to compact")
    _require("_load_deleted_entries_for_dreamer" in server and "self.dreamer.run(" in server, "server must feed deleted search rows into Dreamer")
    _require("self.search_index.purge_deleted(processed_ids)" in server, "Dreamer-processed search rows must be purged")
    _require("dreamer_compaction" in dreamer and "processed_node_ids" in dreamer, "Dreamer must archive processed deleted rows")
    _require("supersedes_ref_ids" in server and "pending_conflict" in server, "server must preserve memory evolution conflict/replacement semantics")
    return {
        "sqlite_runtime_tables": ["graph_state", "search_index", "memory_evolution_state", "memory_evolution_edges"],
        "jsonl_archive_rail": True,
    }


def check_write_queue_and_timeouts() -> dict[str, Any]:
    server = _read("src/memoria_mcp/server.py")
    write_queue = _read("src/memoria_mcp/write_queue.py")
    soft_timeout = _read("tests/soft_timeout_recovery_check.py")

    _require("ProjectWriteQueue" in server and "_dispatch_remember_with_write_queue" in server, "remember must go through the durable write queue")
    _require('"commit_state": "queued"' in server, "remember must be allowed to return queued after durable enqueue")
    _require("queue_first=True" in server, "queued remember must not run the soft-timeout thread path")
    _require("wait_for_result" in write_queue and "process_ready" in write_queue, "write queue must support async drain and result recovery")
    _require("heartbeat_lock" in write_queue and '"writer.lock"' in write_queue, "write queue must keep writer-lock health visible")
    _require("MEMORIA_MCP_READONLY_SOFT_TIMEOUT_SECONDS" in server, "read-only tools need their own soft-timeout budget")
    _require("MEMORIA_MCP_READONLY_SOFT_TIMEOUT_RETRY" in server, "read-only soft-timeout retry must stay opt-in/configurable")
    _require("queue-first" in soft_timeout or "queue_first" in soft_timeout, "soft-timeout tests must cover queue-first remember behavior")
    return {"remember": "queue_first", "read_timeout_budget": "separate"}


def check_hooks_and_latch() -> dict[str, Any]:
    hook_core = _read("src/memoria_mcp/hook_core.py")
    context_cli = _read("src/memoria_mcp/context_cli.py")
    skill = _read("skills/ripple-memory/SKILL.md")
    latch_ref = _read("skills/ripple-memory/references/original-word-latch.md")

    for adapter in ("codex_hook.py", "claude_code_hook.py", "qwen_code_hook.py", "mimocode_hook.py"):
        text = _read(f"src/memoria_mcp/{adapter}")
        _require("RippleHookEvent" in text and "handle_hook_event" in text, f"{adapter} must remain a thin adapter")
        for forbidden in ("MemoriaRouter(", "MemoryGraph", "ProjectWriteQueue"):
            _require(forbidden not in text, f"{adapter} must not own memory business logic: {forbidden}")

    for adapter in ("codex_hook.py", "claude_code_hook.py", "qwen_code_hook.py"):
        text = _read(f"src/memoria_mcp/{adapter}")
        _require("hookSpecificOutput" in text and "additionalContext" in text, f"{adapter} must emit host hook context")
    _require('"context"' in _read("src/memoria_mcp/mimocode_hook.py"), "mimocode_hook.py must emit MiMo plugin context")

    for event_name in ("SessionStart", "UserPromptSubmit", "Stop"):
        _require(event_name in _read("src/memoria_mcp/codex_hook.py"), f"Codex hook must preserve {event_name}")

    for token in (
        "LATCH_BURST_WINDOW_SECONDS = 5.0",
        "LATCH_BURST_LONG_PROMPT_CHARS = 600",
        "latch_burst_suppressed",
        "## Task State",
        "completed",
        "progress anchor",
        "Latch: original intent, boundaries",
        "task-relevant history and mid-task guidance",
        "Completed checkpoint",
    ):
        _require(token in hook_core or token in context_cli or token in skill or token in latch_ref, f"latch contract missing token: {token}")
    _require("Original Words Latch" in latch_ref and "Agent Task Understanding" in latch_ref, "latch reference must explain user words plus agent understanding")
    _require("High-score recall results must be read" in skill, "skill must teach high-score recall -> memoria_read")
    _require("Chinese terms plus English technical aliases" in skill, "skill must teach Chinese/English alias recall queries")
    _require("Original memory text from `memoria_read` is the source of truth" in skill, "skill must teach recall-as-navigation/read-as-truth")
    return {"hook_adapters": "thin", "latch": "task_state_and_burst_gate"}


def check_model_and_test_suite() -> dict[str, Any]:
    default_model = _literal_assignment("src/memoria_mcp/config.py", "DEFAULT_EMBEDDING_MODEL_DIR")
    _require(default_model == "paraphrase-multilingual-MiniLM-L12-v2", f"default model drifted: {default_model}")
    model_root = ROOT / "models"
    if model_root.exists():
        forbidden = [
            path.name
            for path in model_root.iterdir()
            if path.is_dir() and path.name in {"bge-base-zh-v1.5", "paraphrase-multilingual-mpnet-base-v2"}
        ]
        _require(not forbidden, f"forbidden non-baseline model dirs present: {forbidden}")

    missing_tests = [name for name in REQUIRED_REGRESSION_TESTS if not (ROOT / "tests" / name).is_file()]
    _require(not missing_tests, f"required regression tests missing: {missing_tests}")
    return {"default_model": default_model, "required_tests": len(REQUIRED_REGRESSION_TESTS)}


def check_model_downloader_safety() -> dict[str, Any]:
    tool_path = ROOT / "tools" / "download_embedding_model.py"
    _require(tool_path.is_file(), "model downloader tool is missing")
    spec = importlib.util.spec_from_file_location("ripple_download_embedding_model", tool_path)
    _require(spec is not None and spec.loader is not None, "model downloader tool cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    runtime_dir = Path(tempfile.gettempdir()) / "ripple-runtime-contract"
    target = module._safe_model_target(runtime_dir, "paraphrase-multilingual-MiniLM-L12-v2")
    _require(target.name == "paraphrase-multilingual-MiniLM-L12-v2", "model target changed the directory name")
    _require((runtime_dir / "models").resolve() in target.parents, "model target must stay under runtime/models")

    bad_names = ["", ".", "..", "../escape", r"..\escape", "nested/model", str(Path(tempfile.gettempdir()).resolve())]
    rejected: list[str] = []
    accepted: list[str] = []
    for name in bad_names:
        try:
            module._safe_model_target(runtime_dir, name)
        except ValueError:
            rejected.append(name)
        else:
            accepted.append(name)
    _require(not accepted, f"model downloader accepted unsafe dir names: {accepted}")
    return {"safe_target": str(target), "rejected_unsafe_names": len(rejected)}


def run_check() -> dict[str, Any]:
    return {
        "ok": True,
        "tool_surface": check_tool_surface(),
        "daemon_architecture": check_daemon_architecture(),
        "storage_and_evolution": check_storage_and_evolution(),
        "write_queue_and_timeouts": check_write_queue_and_timeouts(),
        "hooks_and_latch": check_hooks_and_latch(),
        "model_and_test_suite": check_model_and_test_suite(),
        "model_downloader_safety": check_model_downloader_safety(),
    }


def main() -> int:
    print(json.dumps(run_check(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
