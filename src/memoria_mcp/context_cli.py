"""Lightweight startup memory injection for coding agents.

This is intentionally not a full memory proxy. It recalls relevant project
memory before launching a coding agent and injects the result into the initial
prompt. The MCP server remains the runtime read/write interface.
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .lifecycle import WINDOW_STATE_STORE_DIR_NAME, window_latch_file
from .server import ARCHIVE_DIR_NAME, MemoriaRouter, _sanitize_project_name


AGENT_COMMANDS = {
    "codex": "codex",
    "claude": "claude",
    "qwen": "qwen",
}

MAX_LATCH_CHARS = 6000
MAX_OTHER_LATCH_CHARS = 1200
MAX_OTHER_LATCHES = 8
MAX_LATCH_CONTEXT_TURNS = 10


def _configure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def _default_data_dir() -> Path:
    raw = (
        os.environ.get("MEMORIA_MCP_DATA_DIR")
        or os.environ.get("RIPPLE_MEMORY_DATA_DIR")
        or os.path.expanduser("~/.ripple-memory")
    )
    return Path(raw).expanduser()


def _infer_project_name(cwd: Optional[str], explicit: Optional[str]) -> str:
    if explicit and explicit.strip():
        return _sanitize_project_name(explicit)
    root = Path(cwd or os.getcwd()).resolve()
    return _sanitize_project_name(root.name)


def _infer_window_id(explicit: Optional[str], *, generate_if_missing: bool = False) -> str:
    raw = (
        explicit
        or os.environ.get("RIPPLE_MEMORY_WINDOW_ID")
        or os.environ.get("MEMORIA_WINDOW_ID")
        or os.environ.get("CODEX_WINDOW_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
        or os.environ.get("QWEN_SESSION_ID")
    )
    if not raw and generate_if_missing:
        raw = time.strftime("session-%Y%m%d-%H%M%S")
    if not raw:
        raw = "default"
    return _sanitize_project_name(raw)


def _project_exists(data_dir: Path, project: str) -> bool:
    return (data_dir / project).is_dir() or (data_dir / ARCHIVE_DIR_NAME / project).is_dir()


def _set_optional_env_bool(name: str, value: Optional[bool]) -> None:
    if value is None:
        return
    os.environ[name] = "true" if value else "false"


def _latch_root(cwd: Optional[str], *, data_dir: Optional[Path] = None, project: str = "") -> Path:
    if data_dir:
        return data_dir.expanduser().resolve() / WINDOW_STATE_STORE_DIR_NAME / _sanitize_project_name(project or "default")
    return Path(cwd or os.getcwd()).resolve() / ".ripple-memory" / "windows"


def _default_latch_file(
    cwd: Optional[str],
    window_id: str,
    *,
    data_dir: Optional[Path] = None,
    project: str = "",
) -> Path:
    if data_dir:
        return window_latch_file(
            cwd=cwd or os.getcwd(),
            project=_sanitize_project_name(project or "default"),
            window_id=window_id,
            data_dir=data_dir,
        )
    return _latch_root(cwd) / window_id / "original-word-latch.md"


def _extract_latch_preview(text: str) -> Dict[str, str]:
    preview: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        lower = line.lower()
        if lower.startswith("updated:"):
            preview["updated"] = line.split(":", 1)[1].strip()
        elif lower.startswith("task:"):
            preview["task"] = line.split(":", 1)[1].strip()
        elif line.startswith("- Goal:"):
            preview["goal"] = line.split(":", 1)[1].strip()
        elif line.startswith("- State:"):
            preview["state"] = line.split(":", 1)[1].strip()
        if {"updated", "task", "goal", "state"}.issubset(preview):
            break
    return preview


def _compact_latch_turn(line: str, max_chars: int = 360) -> str:
    if len(line) <= max_chars:
        return line
    return line[: max_chars - 24].rstrip() + " ...[turn truncated]"


def _compact_latch_for_context(text: str, max_chars: int) -> str:
    """Keep restart-critical latch sections instead of blindly tail-truncating."""
    if len(text) <= max_chars:
        return text

    lines = text.splitlines()
    top: List[str] = []
    turns: List[str] = []
    in_turns = False
    for line in lines:
        if line.strip() == "## Recent User Turns":
            in_turns = True
            continue
        if in_turns:
            if line.startswith("## "):
                in_turns = False
                top.append(line)
            elif line.startswith("- "):
                turns.append(line)
            continue
        top.append(line)

    compact_turns = [_compact_latch_turn(line) for line in turns[-MAX_LATCH_CONTEXT_TURNS:]]
    compact = "\n".join(top).strip()
    if compact_turns:
        compact = compact.rstrip() + "\n\n## Recent User Turns\n\n" + "\n".join(compact_turns)

    if len(compact) <= max_chars:
        return compact

    # Prefer preserving the metadata and task-understanding head; reduce turns first.
    compact_turns = [_compact_latch_turn(line, max_chars=220) for line in turns[-5:]]
    compact = "\n".join(top).strip()
    if compact_turns:
        compact = compact.rstrip() + "\n\n## Recent User Turns\n\n" + "\n".join(compact_turns)
    if len(compact) <= max_chars:
        return compact

    return compact[: max_chars - 32].rstrip() + "\n...[latch context truncated]"


def _read_original_words_latch(
    *,
    cwd: Optional[str],
    project: str,
    data_dir: Optional[Path],
    window_id: str,
    latch_file: Optional[str],
    no_latch: bool,
    max_chars: int = MAX_LATCH_CHARS,
) -> Dict[str, Any]:
    if no_latch:
        return {"status": "disabled", "path": "", "window_id": window_id}

    path = Path(latch_file).expanduser() if latch_file else _default_latch_file(
        cwd,
        window_id,
        data_dir=data_dir,
        project=project,
    )
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path

    if not resolved.is_file():
        return {"status": "not_found", "path": str(resolved), "window_id": window_id}

    try:
        text = resolved.read_text(encoding="utf-8-sig").strip()
    except UnicodeDecodeError:
        text = resolved.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        return {"status": f"read_error:{exc.__class__.__name__}", "path": str(resolved)}

    truncated = False
    if len(text) > max_chars:
        text = _compact_latch_for_context(text, max_chars)
        truncated = True

    return {
        "status": "loaded",
        "path": str(resolved),
        "window_id": window_id,
        "text": text,
        "truncated": truncated,
    }


MAX_POPULATED_LATCHES = 4


def _read_other_latch_index(
    *,
    cwd: Optional[str],
    project: str,
    data_dir: Optional[Path],
    window_id: str,
    no_other_latches: bool,
) -> List[Dict[str, Any]]:
    if no_other_latches:
        return []

    root = _latch_root(cwd, data_dir=data_dir, project=project)
    if not root.is_dir():
        return []

    entries: List[Dict[str, Any]] = []
    for path in sorted(root.glob("*/original-word-latch.md")):
        other_window_id = path.parent.name
        if other_window_id == window_id:
            continue
        try:
            text = path.read_text(encoding="utf-8-sig").strip()
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        preview = _extract_latch_preview(text[-MAX_OTHER_LATCH_CHARS:])
        goal = preview.get("goal", "")
        if not goal:
            continue
        entries.append({
            "window_id": other_window_id,
            "path": str(path.resolve()),
            "updated": preview.get("updated", ""),
            "task": preview.get("task", ""),
            "goal": goal,
        })
        if len(entries) >= MAX_POPULATED_LATCHES:
            break
    return entries


def recall_project_memory(
    *,
    project: str,
    query: str,
    top_k: int,
    data_dir: Path,
    create_project: bool = False,
    semantic: Optional[bool] = None,
    search_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """Recall memories without going through MCP stdio.

    The launcher is a local helper, so direct library access is simpler and
    avoids needing a second MCP client just to build the initial prompt.
    """
    _set_optional_env_bool("MEMORIA_MCP_ENABLE_SEMANTIC", semantic)
    if search_mode:
        os.environ["MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE"] = search_mode

    if not create_project and not _project_exists(data_dir, project):
        return {
            "query": query,
            "count": 0,
            "results": [],
            "status": "project_not_found",
        }

    router = MemoriaRouter(str(data_dir))
    try:
        server = router._get_server(project)
        return server._tool_recall({"query": query, "top_k": top_k})
    finally:
        router.close()


def _created_label_from_id(mem_id: str) -> str:
    """Extract human-readable creation date from memory ID like mem_1779836283952_0624."""
    try:
        parts = str(mem_id).split("_")
        if len(parts) >= 2:
            ts_ms = int(parts[1])
            return time.strftime("%Y-%m-%d", time.localtime(ts_ms / 1000.0))
    except (ValueError, IndexError, OSError):
        pass
    return ""


_FIELD_GUIDE = (
    "field_guide: strength=time-decayed activity (~2%/day, resets on access); "
    "score=semantic relevance to query; importance=inherent priority (stable); "
    "id=mem_{created_ms}_{rand}; "
    "type: fact/rule/preference/arch_decision/debug_insight/event/procedural/semantic/causal/code_pattern"
)


def render_context_block(
    project: str,
    window_id: str,
    query: str,
    recall: Dict[str, Any],
    latch: Optional[Dict[str, Any]] = None,
    other_latches: Optional[List[Dict[str, Any]]] = None,
    *,
    is_session_start: bool = False,
) -> str:
    results: List[Dict[str, Any]] = list(recall.get("results") or [])
    now_label = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime())
    lines = [
        "<ripple_memory_context>",
        f"project: {project}",
        f"current_time: {now_label}",
        f"window_id: {window_id}",
        f"query: {query}",
    ]
    if is_session_start:
        lines.append(_FIELD_GUIDE)

    if not results:
        status = recall.get("status", "no_hits")
        lines.append(f"status: {status}")
        lines.append("relevant_memories: []")
    else:
        lines.append("relevant_memories:")
        for index, item in enumerate(results, 1):
            desc = str(item.get("description", "")).replace("\n", " ").strip()
            mem_id = item.get("id", "")
            mem_type = item.get("type", "")
            importance = item.get("importance", "")
            strength = item.get("strength", "")
            created = _created_label_from_id(mem_id)
            created_part = f" (created {created})" if created else ""
            lines.append(
                f"- {index}. id={mem_id}{created_part}; type={mem_type}; importance={importance}; "
                f"strength={strength}; text={desc}"
            )

    if latch:
        lines.extend([
            "current_window_latch:",
            f"  window_id: {latch.get('window_id', window_id)}",
            f"  status: {latch.get('status', 'unknown')}",
        ])
        if latch.get("path"):
            lines.append(f"  path: {latch.get('path')}")
        if latch.get("text"):
            suffix = " (tail only)" if latch.get("truncated") else ""
            lines.append(f"  content{suffix}: |")
            for raw_line in str(latch["text"]).splitlines():
                lines.append(f"    {raw_line}")

    if other_latches is not None:
        lines.append("other_window_latches:")
        if not other_latches:
            lines.append("  []")
        else:
            for item in other_latches:
                lines.append(
                    f"  - window_id: {item.get('window_id', '')}; "
                    f"state: {item.get('state', '')}; task: {item.get('task', '')}; updated: {item.get('updated', '')}; "
                    f"goal: {item.get('goal', '')}; path: {item.get('path', '')}"
                )

    lines.extend([
        "usage:",
        "- Progress anchor: conversation summary/latest checkpoint.",
        "- Latch: original intent, boundaries, and recent task guidance.",
        "</ripple_memory_context>",
    ])
    return "\n".join(lines)


def build_injected_prompt(user_prompt: str, context_block: str) -> str:
    return (
        f"{context_block}\n\n"
        "Use the memory context above when it is relevant. Continue to use the "
        "ripple-memory MCP tools during the task when more detail or new durable "
        "memory is needed.\n\n"
        "User request:\n"
        f"{user_prompt.strip()}\n"
    )


def _read_prompt(parts: List[str]) -> str:
    if parts:
        return " ".join(parts).strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return ""


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("prompt", nargs="*", help="User prompt / memory query")
    parser.add_argument("--project", help="Stable memory project name. Defaults to current directory name.")
    parser.add_argument("--window-id", help="Stable task/window identity. Defaults to RIPPLE_MEMORY_WINDOW_ID; launcher generates a session id if missing.")
    parser.add_argument("--cwd", default=os.getcwd(), help="Workspace used to infer the project name.")
    parser.add_argument("--data-dir", default=str(_default_data_dir()), help="Ripple memory data directory.")
    parser.add_argument("--latch-file", help="Original Words Latch path. Defaults to <data-dir>/_window_state/<project>/<window-id>/original-word-latch.md.")
    parser.add_argument("--no-latch", action="store_true", help="Do not read the Original Words Latch into the startup context.")
    parser.add_argument("--no-other-latches", action="store_true", help="Do not list other window latch indexes in the startup context.")
    parser.add_argument("--top-k", type=int, default=6, help="Number of memories to recall.")
    parser.add_argument("--create-project", action="store_true", help="Create an empty memory project if missing.")
    semantic_group = parser.add_mutually_exclusive_group()
    semantic_group.add_argument("--semantic", action="store_true", default=None, help="Enable embedding retrieval for recall.")
    semantic_group.add_argument("--no-semantic", action="store_false", dest="semantic", help="Disable embedding retrieval for recall.")
    parser.add_argument("--search-mode", choices=["off", "shadow", "live"], default=None)


def context_main(argv: Optional[List[str]] = None) -> int:
    _configure_stdout()
    parser = argparse.ArgumentParser(description="Print a Ripple Memory context block for a coding-agent prompt.")
    _add_common_args(parser)
    args = parser.parse_args(argv)

    query = _read_prompt(args.prompt) or "project context"
    project = _infer_project_name(args.cwd, args.project)
    window_id = _infer_window_id(args.window_id)
    data_dir = Path(args.data_dir).expanduser()
    recall = recall_project_memory(
        project=project,
        query=query,
        top_k=args.top_k,
        data_dir=data_dir,
        create_project=args.create_project,
        semantic=args.semantic,
        search_mode=args.search_mode,
    )
    latch = _read_original_words_latch(
        cwd=args.cwd,
        project=project,
        data_dir=data_dir,
        window_id=window_id,
        latch_file=args.latch_file,
        no_latch=args.no_latch,
    )
    other_latches = _read_other_latch_index(
        cwd=args.cwd,
        project=project,
        data_dir=data_dir,
        window_id=window_id,
        no_other_latches=args.no_other_latches,
    )
    print(render_context_block(project, window_id, query, recall, latch, other_latches))
    return 0


def _agent_command(agent: str, explicit_command: Optional[str]) -> str:
    if explicit_command:
        return explicit_command
    env_name = f"RIPPLE_{agent.upper()}_COMMAND"
    return os.environ.get(env_name) or AGENT_COMMANDS.get(agent, agent)


def run_main(argv: Optional[List[str]] = None) -> int:
    _configure_stdout()
    parser = argparse.ArgumentParser(description="Launch a coding agent with Ripple Memory injected into the first prompt.")
    _add_common_args(parser)
    parser.add_argument("--agent", choices=["codex", "claude", "qwen", "custom"], default="codex")
    parser.add_argument("--command", help="Agent command. Defaults: codex, claude, qwen; custom requires this.")
    parser.add_argument("--dry-run", action="store_true", help="Print command and injected prompt instead of launching.")
    parser.add_argument("--print-prompt", action="store_true", help="Only print the injected prompt.")
    args = parser.parse_args(argv)

    user_prompt = _read_prompt(args.prompt)
    if not user_prompt:
        parser.error("prompt is required, either as arguments or stdin")

    project = _infer_project_name(args.cwd, args.project)
    window_id = _infer_window_id(args.window_id, generate_if_missing=True)
    data_dir = Path(args.data_dir).expanduser()
    recall = recall_project_memory(
        project=project,
        query=user_prompt,
        top_k=args.top_k,
        data_dir=data_dir,
        create_project=args.create_project,
        semantic=args.semantic,
        search_mode=args.search_mode,
    )
    latch = _read_original_words_latch(
        cwd=args.cwd,
        project=project,
        data_dir=data_dir,
        window_id=window_id,
        latch_file=args.latch_file,
        no_latch=args.no_latch,
    )
    other_latches = _read_other_latch_index(
        cwd=args.cwd,
        project=project,
        data_dir=data_dir,
        window_id=window_id,
        no_other_latches=args.no_other_latches,
    )
    context_block = render_context_block(project, window_id, user_prompt, recall, latch, other_latches)
    injected_prompt = build_injected_prompt(user_prompt, context_block)

    if args.print_prompt:
        print(injected_prompt)
        return 0

    command = _agent_command(args.agent, args.command)
    if args.agent == "custom" and not args.command:
        parser.error("--command is required when --agent custom")

    cmd = [command, injected_prompt]
    if args.dry_run:
        print("command:")
        print(" ".join(shlex.quote(part) for part in cmd))
        print("\nenv:")
        print(f"RIPPLE_MEMORY_PROJECT={project}")
        print(f"RIPPLE_MEMORY_WINDOW_ID={window_id}")
        if latch.get("path"):
            print(f"RIPPLE_MEMORY_LATCH_FILE={latch.get('path')}")
        print("\ninjected_prompt:\n")
        print(injected_prompt)
        return 0

    child_env = os.environ.copy()
    child_env["RIPPLE_MEMORY_PROJECT"] = project
    child_env["RIPPLE_MEMORY_WINDOW_ID"] = window_id
    if latch.get("path"):
        child_env["RIPPLE_MEMORY_LATCH_FILE"] = str(latch.get("path"))
    return subprocess.run(cmd, check=False, env=child_env).returncode


if __name__ == "__main__":
    raise SystemExit(context_main())
