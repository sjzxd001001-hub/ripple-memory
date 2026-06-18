"""Host-agnostic hook support for Ripple Memory.

Adapters should translate each host agent's native hook payload into
``RippleHookEvent`` and then call ``handle_hook_event``. The memory behavior
lives here so Codex/Claude/Qwen adapters stay thin and removable.
"""
from __future__ import annotations

import os
import re
import time
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .context_cli import (
    _default_data_dir,
    _default_latch_file,
    _infer_project_name,
    _infer_window_id,
    _read_original_words_latch,
    _read_other_latch_index,
    recall_project_memory,
    render_context_block,
)
from .lifecycle import ProcessRegistry, archive_window_state, restore_window_state
from .server import _sanitize_project_name


HOOK_TIMEOUT_SECONDS = 3.0
MAX_PROMPT_CHARS = 4000
MAX_LATCH_USER_TURNS = 10
MAX_CONTEXT_CHARS = 6000
MAX_TASK_UNDERSTANDING_CHARS = 1800
MAX_TRANSCRIPT_TAIL_BYTES = 262144
LATCH_BURST_LONG_PROMPT_CHARS = 600
LATCH_BURST_WINDOW_SECONDS = 5.0
LATCH_BURST_ALLOW_LONG_PROMPTS = 1
LATCH_BURST_STOP_SUPPRESS_SECONDS = 45.0


@dataclass
class RippleHookEvent:
    agent: str
    event: str
    cwd: str
    project: Optional[str] = None
    window_id: Optional[str] = None
    user_text: str = ""
    assistant_text: str = ""
    session_id: str = ""
    turn_id: str = ""
    transcript_path: str = ""
    raw: Optional[Dict[str, Any]] = None


def hook_enabled(cwd: str) -> bool:
    """Return False when a hot-unplug switch is active."""
    raw = os.environ.get("RIPPLE_MEMORY_HOOK_ENABLED")
    if raw is not None and raw.strip().lower() in {"0", "false", "no", "off"}:
        return False

    try:
        disabled = Path(cwd).resolve() / ".ripple-memory" / "hooks.disabled"
    except OSError:
        disabled = Path(cwd) / ".ripple-memory" / "hooks.disabled"
    return not disabled.exists()


def normalize_event_name(name: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9]+", "", str(name or "")).lower()
    aliases = {
        "sessionstart": "session_start",
        "startup": "session_start",
        "resume": "session_start",
        "userpromptsubmit": "user_prompt_submit",
        "promptsubmit": "user_prompt_submit",
        "stop": "stop",
        "sessionstop": "stop",
        "windowarchive": "window_archive",
        "threadarchive": "window_archive",
        "conversationarchive": "window_archive",
        "sessionarchive": "window_archive",
        "archive": "window_archive",
        "windowdelete": "window_delete",
        "threaddelete": "window_delete",
        "conversationdelete": "window_delete",
        "sessiondelete": "window_delete",
        "delete": "window_delete",
        "windowrestore": "window_restore",
        "threadrestore": "window_restore",
        "conversationrestore": "window_restore",
        "sessionrestore": "window_restore",
        "restore": "window_restore",
    }
    return aliases.get(clean, clean or "unknown")


def _now_label() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %z")


def _truncate(text: str, max_chars: int) -> str:
    text = str(text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 24].rstrip() + "\n...[truncated by hook]"


def _read_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _read_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _read_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _safe_state_name(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(value or "")).strip("._-")
    return (clean or "default")[:120]


def _latch_burst_state_path(data_dir: Path, project: str, window_id: str) -> Path:
    return (
        data_dir
        / "_runtime"
        / "latch_burst"
        / _safe_state_name(project)
        / f"{_safe_state_name(window_id)}.json"
    )


def _read_latch_burst_state(path: Path) -> Dict[str, Any]:
    try:
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
    except Exception:
        return {}
    return {}


def _write_latch_burst_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _prompt_size(text: str) -> int:
    return len(str(text or "").strip())


def _iso_from_timestamp(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(timestamp))


def _review_latch_burst_gate(
    *,
    data_dir: Path,
    project: str,
    window_id: str,
    event: RippleHookEvent,
    user_text: str,
) -> Dict[str, Any]:
    """Throttle high-frequency long prompt latch writes without blocking recall."""
    enabled = _read_bool_env("RIPPLE_MEMORY_LATCH_BURST_GATE_ENABLED", True)
    long_chars = _read_int_env("RIPPLE_MEMORY_LATCH_BURST_LONG_CHARS", LATCH_BURST_LONG_PROMPT_CHARS)
    window_seconds = _read_float_env("RIPPLE_MEMORY_LATCH_BURST_SECONDS", LATCH_BURST_WINDOW_SECONDS)
    allow_long = _read_int_env("RIPPLE_MEMORY_LATCH_BURST_ALLOW", LATCH_BURST_ALLOW_LONG_PROMPTS)
    now = time.time()
    prompt_chars = _prompt_size(user_text)
    long_prompt = prompt_chars >= long_chars
    decision: Dict[str, Any] = {
        "enabled": enabled,
        "allowed": True,
        "suppressed": False,
        "reason": "allowed",
        "prompt_chars": prompt_chars,
        "long_prompt": long_prompt,
        "long_prompt_chars": long_chars,
        "window_seconds": window_seconds,
        "allow_long_prompts": allow_long,
    }
    if not enabled:
        decision["reason"] = "disabled"
        return decision

    path = _latch_burst_state_path(data_dir, project, window_id)
    state = _read_latch_burst_state(path)
    burst_count = int(state.get("long_burst_count") or 0)
    last_long_at = float(state.get("last_long_at") or 0.0)
    if long_prompt:
        if last_long_at > 0 and now - last_long_at <= window_seconds:
            burst_count += 1
        else:
            burst_count = 1
        decision["burst_count"] = burst_count
        if burst_count > allow_long:
            decision["allowed"] = False
            decision["suppressed"] = True
            decision["reason"] = "latch_burst_suppressed"
        state["last_long_at"] = now
        state["last_long_at_label"] = _iso_from_timestamp(now)
        state["long_burst_count"] = burst_count
    else:
        decision["burst_count"] = burst_count

    latest_event = {
        "timestamp": now,
        "timestamp_label": _iso_from_timestamp(now),
        "turn_id": event.turn_id,
        "session_id": event.session_id,
        "agent": event.agent,
        "prompt_chars": prompt_chars,
        "long_prompt": long_prompt,
        "suppressed": decision["suppressed"],
        "reason": decision["reason"],
    }
    state.update({
        "schema": "ripple_memory_latch_burst_v1",
        "project": project,
        "window_id": window_id,
        "updated_at": now,
        "updated_at_label": _iso_from_timestamp(now),
        "latest_event": latest_event,
    })
    if decision["suppressed"]:
        state["last_suppressed_event"] = latest_event

    try:
        _write_latch_burst_state(path, state)
        decision["state_path"] = str(path)
    except OSError as exc:
        decision.update({
            "allowed": True,
            "suppressed": False,
            "reason": "state_write_failed_open",
            "error": str(exc),
            "state_path": str(path),
        })
    return decision


def _should_suppress_stop_for_latch(
    *,
    data_dir: Path,
    project: str,
    window_id: str,
    event: RippleHookEvent,
) -> Dict[str, Any]:
    enabled = _read_bool_env("RIPPLE_MEMORY_LATCH_BURST_GATE_ENABLED", True)
    decision: Dict[str, Any] = {"enabled": enabled, "suppress": False, "reason": "allowed"}
    if not enabled:
        decision["reason"] = "disabled"
        return decision

    path = _latch_burst_state_path(data_dir, project, window_id)
    state = _read_latch_burst_state(path)
    latest = state.get("latest_event") if isinstance(state, dict) else None
    if not isinstance(latest, dict) or not latest.get("suppressed"):
        return decision

    now = time.time()
    latest_ts = float(latest.get("timestamp") or 0.0)
    max_age = _read_float_env(
        "RIPPLE_MEMORY_LATCH_BURST_STOP_SECONDS",
        max(LATCH_BURST_STOP_SUPPRESS_SECONDS, LATCH_BURST_WINDOW_SECONDS * 6),
    )
    if latest_ts <= 0 or now - latest_ts > max_age:
        decision["reason"] = "suppressed_event_expired"
        return decision

    latest_turn = str(latest.get("turn_id") or "")
    latest_session = str(latest.get("session_id") or "")
    event_turn = str(event.turn_id or "")
    event_session = str(event.session_id or "")
    if event_turn and latest_turn and event_turn != latest_turn:
        decision["reason"] = "turn_id_mismatch"
        return decision
    if event_session and latest_session and event_session != latest_session:
        decision["reason"] = "session_id_mismatch"
        return decision

    decision.update({
        "suppress": True,
        "reason": "latch_burst_suppressed_stop",
        "state_path": str(path),
        "latest_event": latest,
    })
    return decision


def _default_window_id(event: RippleHookEvent) -> str:
    explicit = event.window_id or os.environ.get("RIPPLE_MEMORY_WINDOW_ID")
    if explicit:
        return _infer_window_id(explicit)
    if event.session_id:
        return _infer_window_id(event.session_id)
    return _infer_window_id(None, generate_if_missing=True)


def _latch_path(cwd: str, project: str, window_id: str, data_dir: Path) -> Path:
    raw = os.environ.get("RIPPLE_MEMORY_LATCH_FILE")
    if raw:
        return Path(raw).expanduser()
    return _default_latch_file(cwd, window_id, data_dir=data_dir, project=project)


def _parse_latch_turns(text: str) -> list[str]:
    turns: list[str] = []
    in_turns = False
    for line in text.splitlines():
        if line.strip() == "## Recent User Turns":
            in_turns = True
            continue
        if in_turns and line.startswith("## "):
            break
        if in_turns and line.startswith("- "):
            turns.append(line)
    return turns[-MAX_LATCH_USER_TURNS:]


def _parse_latch_goal(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- Goal:"):
            return stripped.split(":", 1)[1].strip()
    return ""


def _parse_latch_task_understanding(text: str) -> str:
    for header in ("## Agent Task Understanding", "## Current Task Understanding"):
        marker = text.find(header)
        if marker < 0:
            continue
        after = text[marker + len(header):].lstrip()
        next_header = after.find("\n## ")
        if next_header >= 0:
            after = after[:next_header]
        cleaned = after.strip()
        if cleaned and "Hook seed only:" not in cleaned:
            return _truncate(cleaned, MAX_TASK_UNDERSTANDING_CHARS)
    return ""


def _squash(text: str, max_chars: int) -> str:
    return _truncate(re.sub(r"\s+", " ", str(text or "")).strip(), max_chars)


def _looks_like_short_continuation(text: str) -> bool:
    clean = re.sub(r"\s+", "", str(text or ""))
    if len(clean) > 80:
        return False
    cues = (
        "继续", "接着", "开工", "开始", "做吧", "动手", "往下做",
        "这五条一起做", "按这个做", "就这样做", "go", "continue",
    )
    lower = clean.lower()
    return any(cue in lower for cue in cues)


def _dedupe_terms(terms: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        clean = re.sub(r"\s+", " ", str(term or "")).strip()
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(clean)
    return result


def _hook_prompt_matches_any(clean_compact: str, lower_text: str, cues: tuple[str, ...]) -> bool:
    for cue in cues:
        lowered = cue.lower()
        if lowered in lower_text or re.sub(r"\s+", "", lowered) in clean_compact:
            return True
    return False


def _expand_hook_recall_query(*, project: str, cwd: str, user_text: str) -> str:
    """Expand short natural prompts for hook recall without changing latch text."""
    raw = _truncate(user_text, MAX_PROMPT_CHARS) or "project context"
    compact = re.sub(r"\s+", "", raw).lower()
    lower = raw.lower()
    if len(compact) > 160:
        return raw

    progress_cues = (
        "任务进度", "进度", "到哪里了", "到哪了", "做到哪", "做到哪里",
        "当前阶段", "当前进展", "目前进展", "现在进展", "下一步", "下步",
        "还剩", "施工到", "任务状态", "progress", "status", "where are we",
        "current phase", "next step", "what's next", "latest status",
    )
    resume_cues = (
        "继续", "接着", "接上", "往下做", "恢复上下文", "上次", "之前",
        "刚才", "前面", "按原方案", "照之前", "不要重新来", "上下文没了",
        "压缩后", "continue", "resume", "last time", "previously",
        "prior work", "use the prior plan",
    )
    memory_cues = (
        "记得吗", "还记得", "记不记得", "我之前说过", "我们不是说过",
        "别忘", "记住", "不要再", "上次踩过坑", "do you remember",
        "don't forget", "remember this",
    )

    progress = _hook_prompt_matches_any(compact, lower, progress_cues)
    resume = _looks_like_short_continuation(raw) or _hook_prompt_matches_any(compact, lower, resume_cues)
    memory = _hook_prompt_matches_any(compact, lower, memory_cues)
    if not (progress or resume or memory):
        return raw

    terms = [raw, project]
    try:
        cwd_name = Path(cwd).name
        if cwd_name and cwd_name != project:
            terms.append(cwd_name)
    except OSError:
        pass

    if progress:
        terms.extend([
            time.strftime("%Y-%m-%d"),
            "当前任务进度", "最新进度", "当前阶段", "最近进展", "最近完成", "下一步",
            "phase progress", "current phase", "latest progress",
            "latest commit", "latest completed", "next step", "roadmap",
            "current baseline", "baseline", "completed", "closed",
        ])
    if resume:
        terms.extend([
            "继续上次", "断点续接", "当前任务", "上次计划", "下一步",
            "resume prior work", "current task", "prior plan",
            "latest status", "next action",
        ])
    if memory:
        terms.extend([
            "历史口径", "已定规则", "用户偏好", "避免重复踩坑",
            "prior decision", "lasting rule", "preference", "pitfall",
        ])

    expanded = " ".join(_dedupe_terms(terms))
    return _truncate(expanded, MAX_PROMPT_CHARS)


def _seed_task_understanding(*, cwd: str, project: str, user_text: str) -> str:
    prompt = _squash(user_text, 520)
    scope = _squash(cwd, 180)
    return "\n".join([
        f"- Goal: {prompt or 'Continue the current user-requested task.'}",
        f"- Scope: Project `{project}` in `{scope}`; verify against current files, logs, and tests.",
        "- Boundaries: Latest user message wins; Ripple memory/latch are navigation, not proof.",
        "- Acceptance: Complete the requested scope and run or record the relevant checks.",
        "- Progress rule: After compression, resume only from the latest unfinished step; do not repeat completed setup, reads, edits, tests, or commits.",
    ])


def _contains_any(text: str, cues: tuple[str, ...]) -> bool:
    lower = str(text or "").lower()
    return any(cue.lower() in lower for cue in cues)


def _classify_task_state_from_agent_text(text: str) -> str:
    lower = str(text or "").lower()
    if not lower.strip():
        return "active"
    completed_cues = (
        "已完成", "已经完成", "完成了", "已处理", "已修复", "修好了",
        "已提交", "提交成功", "工作树干净", "验证通过", "测试通过",
        "passed", "complete", "completed", "done", "committed", "worktree clean",
    )
    awaiting_cues = (
        "等待用户", "等你", "需要你确认", "请确认", "需要用户确认",
        "awaiting user", "waiting for user", "needs your confirmation",
    )
    blocked_cues = (
        "无法继续", "不能继续", "被阻塞", "卡住了", "blocked",
        "cannot proceed", "unable to continue",
    )
    if _contains_any(lower, completed_cues):
        return "completed"
    if _contains_any(lower, awaiting_cues):
        return "awaiting_user"
    if _contains_any(lower, blocked_cues):
        return "blocked"
    return "active"


def _next_action_for_task_state(state: str) -> str:
    if state == "completed":
        return "Completed checkpoint."
    if state == "awaiting_user":
        return "Awaiting user decision or confirmation."
    if state == "blocked":
        return "Blocked checkpoint; keep the blocker visible."
    return "Continue from the visible summary/checkpoint."


def _compression_rule_for_task_state(state: str) -> str:
    if state == "completed":
        return "Completed checkpoint."
    if state == "awaiting_user":
        return "Awaiting checkpoint."
    if state == "blocked":
        return "Blocked checkpoint."
    return "Summary/checkpoint carries progress; latch carries intent, boundaries, and recent guidance."


def _agent_task_understanding_from_text(text: str) -> str:
    summary = _squash(text, 1000)
    if not summary:
        return ""
    return "\n".join([
        f"- Latest agent checkpoint: {summary}",
        "- Boundaries: Preserve explicit user constraints; do not treat memory/latch as code truth.",
        "- Recovery rule: Use the task state above before acting. If completed, report status instead of repeating work.",
    ])


def _text_from_content(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "\n".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        return str(value.get("text") or value.get("content") or "").strip()
    return ""


def _assistant_text_from_raw(raw: Optional[Dict[str, Any]]) -> str:
    if not isinstance(raw, dict):
        return ""
    for key in (
        "assistant_text", "assistantText", "assistant_response",
        "assistantResponse", "response", "completion", "output",
    ):
        text = _text_from_content(raw.get(key))
        if text:
            return text
    content = raw.get("content")
    if isinstance(content, list):
        assistant_parts = []
        for item in content:
            if isinstance(item, dict) and str(item.get("role") or "").lower() == "assistant":
                assistant_parts.append(_text_from_content(item.get("content") or item.get("text")))
        if assistant_parts:
            return "\n".join(part for part in assistant_parts if part).strip()
    return ""


def _assistant_text_from_transcript(path: str) -> str:
    if not path:
        return ""
    try:
        transcript = Path(path).expanduser()
        if not transcript.is_file():
            return ""
        with transcript.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - MAX_TRANSCRIPT_TAIL_BYTES))
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""

    latest = ""
    for line in raw.splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        candidates = []
        if isinstance(obj, dict):
            candidates.append(obj)
            payload = obj.get("payload")
            if isinstance(payload, dict):
                candidates.append(payload)
            message = obj.get("message")
            if isinstance(message, dict):
                candidates.append(message)
        for item in candidates:
            if str(item.get("role") or "").lower() == "assistant":
                text = _text_from_content(item.get("content") or item.get("text"))
                if text:
                    latest = text
            elif item.get("type") == "message" and str(item.get("role") or "").lower() == "assistant":
                text = _text_from_content(item.get("content") or item.get("text"))
                if text:
                    latest = text
    return latest.strip()


def update_original_words_latch(
    *,
    cwd: str,
    project: str,
    data_dir: Path,
    window_id: str,
    user_text: str,
    task_seed: Optional[str],
    task_understanding: Optional[str] = None,
    task_state: str = "active",
    last_outcome: str = "",
    next_action: str = "",
    append_user_turn: bool = True,
) -> Dict[str, Any]:
    """Update the window-local latch while keeping user turns bounded."""
    path = _latch_path(cwd, project, window_id, data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    except OSError as exc:
        return {"updated": False, "path": str(path), "error": str(exc)}

    turns = _parse_latch_turns(existing)
    safe_user = _truncate(user_text.replace("\r", " ").replace("\n", " "), 1200)
    if append_user_turn and safe_user:
        turns.append(f"- {_now_label()} | {safe_user}")
    turns = turns[-MAX_LATCH_USER_TURNS:]

    existing_goal = _parse_latch_goal(existing)
    goal_source = task_seed if task_seed is not None else existing_goal
    goal = _squash(goal_source or existing_goal, 520)
    existing_understanding = _parse_latch_task_understanding(existing)
    if task_understanding:
        understanding = _truncate(task_understanding.strip(), MAX_TASK_UNDERSTANDING_CHARS)
    elif safe_user and not (_looks_like_short_continuation(safe_user) and existing_understanding):
        understanding = _seed_task_understanding(cwd=cwd, project=project, user_text=safe_user)
    else:
        understanding = existing_understanding or _seed_task_understanding(cwd=cwd, project=project, user_text=safe_user)

    normalized_state = str(task_state or "active").strip().lower()
    if normalized_state not in {"active", "completed", "blocked", "awaiting_user"}:
        normalized_state = "active"
    outcome = _squash(
        last_outcome
        or ("User prompt captured; no completion checkpoint recorded yet." if safe_user else "Agent checkpoint refreshed."),
        700,
    )
    action = _squash(next_action or _next_action_for_task_state(normalized_state), 700)
    compression_rule = _compression_rule_for_task_state(normalized_state)

    lines = [
        "# Original Words Latch",
        "",
        f"Updated: {_now_label()}",
        f"Project: {project}",
        f"Window: {window_id}",
        "Task:",
        f"- Goal: {goal or 'No current prompt captured.'}",
        "",
        "## Task State",
        "",
        f"- State: {normalized_state}",
        f"- Last outcome: {outcome}",
        f"- Next action: {action}",
        f"- Compression rule: {compression_rule}",
        "",
        "## Agent Task Understanding",
        "",
        understanding,
        "",
        "## Recent User Turns",
        "",
    ]
    lines.extend(turns or ["- No user turns captured yet."])
    lines.append("")

    try:
        path.write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        return {"updated": False, "path": str(path), "error": str(exc)}
    return {
        "updated": True,
        "path": str(path),
        "turn_count": len(turns),
        "understanding_chars": len(understanding),
        "task_state": normalized_state,
    }


def update_latch_from_agent_stop(
    event: RippleHookEvent,
    *,
    cwd: str,
    project: str,
    data_dir: Path,
    window_id: str,
) -> Dict[str, Any]:
    assistant_text = (
        event.assistant_text
        or _assistant_text_from_raw(event.raw)
        or _assistant_text_from_transcript(event.transcript_path)
    )
    understanding = _agent_task_understanding_from_text(assistant_text)
    if not understanding:
        return {"updated": False, "reason": "no_agent_summary_available"}
    task_state = _classify_task_state_from_agent_text(assistant_text)
    last_outcome = _squash(assistant_text, 700)
    return update_original_words_latch(
        cwd=cwd,
        project=project,
        data_dir=data_dir,
        window_id=window_id,
        user_text="",
        task_seed=None,
        task_understanding=understanding,
        task_state=task_state,
        last_outcome=last_outcome,
        next_action=_next_action_for_task_state(task_state),
        append_user_turn=False,
    )


def _should_emit_context(context: str) -> bool:
    if "relevant_memories: []" not in context:
        return True
    if "current_window_latch:" in context and "status: loaded" in context:
        return True
    if "other_window_latches:\n  []" not in context:
        return True
    return False


def handle_hook_event(event: RippleHookEvent, *, deadline_seconds: float = HOOK_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """Handle a standard hook event and return a host-neutral result."""
    start = time.monotonic()
    cwd = event.cwd or os.getcwd()
    normalized = normalize_event_name(event.event)

    if not hook_enabled(cwd):
        return {
            "ok": True,
            "enabled": False,
            "event": normalized,
            "context": "",
            "warnings": [],
            "latch": {"updated": False, "reason": "disabled"},
        }

    project = _sanitize_project_name(event.project or os.environ.get("RIPPLE_MEMORY_PROJECT") or _infer_project_name(cwd, None))
    window_id = _default_window_id(event)
    data_dir = _default_data_dir()
    warnings: list[str] = []
    latch_update: Dict[str, Any] = {"updated": False}

    if normalized in {"window_archive", "window_delete", "window_restore"}:
        registry = ProcessRegistry(data_dir, host=event.agent, window_id=window_id, session_id=event.session_id)
        process_exit_request: Dict[str, Any] = {"requested_count": 0, "requested_pids": []}
        if normalized == "window_restore":
            lifecycle = restore_window_state(
                cwd=cwd,
                project=project,
                window_id=window_id,
                data_dir=data_dir,
                agent=event.agent,
            )
        else:
            lifecycle = archive_window_state(
                cwd=cwd,
                project=project,
                window_id=window_id,
                action=normalized,
                data_dir=data_dir,
                agent=event.agent,
                reason=normalized,
            )
            process_exit_request = registry.request_exit_for_window(
                window_id=window_id,
                session_id=event.session_id,
                reason=normalized,
            )
            lifecycle["process_exit_request"] = process_exit_request
        registry.record_window_event(
            agent=event.agent,
            project=project,
            window_id=window_id,
            action=normalized,
            cwd=cwd,
            details=lifecycle,
        )
        return {
            "ok": True,
            "enabled": True,
            "event": normalized,
            "project": project,
            "window_id": window_id,
            "context": "",
            "warnings": [],
            "latch": {"updated": False, "reason": normalized},
            "window_lifecycle": lifecycle,
            "process_exit_request": process_exit_request,
            "memory_written": False,
            "note": "Window lifecycle events archive/restore window-local latch state; project memory stays shared.",
        }

    query = "project context"
    recall_query = query
    if normalized == "user_prompt_submit":
        query = _truncate(event.user_text, MAX_PROMPT_CHARS) or "project context"
        recall_query = _expand_hook_recall_query(project=project, cwd=cwd, user_text=query)
        burst_gate = _review_latch_burst_gate(
            data_dir=data_dir,
            project=project,
            window_id=window_id,
            event=event,
            user_text=query,
        )
        if burst_gate.get("suppressed"):
            latch_update = {
                "updated": False,
                "reason": "latch_burst_suppressed",
                "burst_gate": burst_gate,
            }
        else:
            latch_update = update_original_words_latch(
                cwd=cwd,
                project=project,
                data_dir=data_dir,
                window_id=window_id,
                user_text=query,
                task_seed=query,
            )
            latch_update["burst_gate"] = burst_gate
        if not latch_update.get("updated") and latch_update.get("reason") != "latch_burst_suppressed":
            warnings.append(f"latch_update_failed:{latch_update.get('error', 'unknown')}")
    elif normalized == "session_start":
        query = f"{project} startup context"
        recall_query = query
    elif normalized == "stop":
        stop_gate = _should_suppress_stop_for_latch(
            data_dir=data_dir,
            project=project,
            window_id=window_id,
            event=event,
        )
        if stop_gate.get("suppress"):
            latch_update = {
                "updated": False,
                "reason": "latch_burst_suppressed_stop",
                "burst_gate": stop_gate,
            }
            return {
                "ok": True,
                "enabled": True,
                "event": normalized,
                "context": "",
                "warnings": [],
                "latch": latch_update,
                "memory_written": False,
                "note": "Stop hook skipped latch refresh because the matching long prompt was burst-suppressed.",
            }
        latch_update = update_latch_from_agent_stop(event, cwd=cwd, project=project, data_dir=data_dir, window_id=window_id)
        return {
            "ok": True,
            "enabled": True,
            "event": normalized,
            "context": "",
            "warnings": [],
            "latch": latch_update,
            "memory_written": False,
            "note": "Stop hook only refreshes window-local latch understanding; it does not write durable memory.",
        }
    else:
        return {
            "ok": True,
            "enabled": True,
            "event": normalized,
            "context": "",
            "warnings": [f"unsupported_event:{normalized}"],
            "latch": latch_update,
        }

    elapsed = time.monotonic() - start
    if elapsed >= max(0.1, deadline_seconds - 0.25):
        return {
            "ok": True,
            "enabled": True,
            "event": normalized,
            "context": "",
            "warnings": ["deadline_reached_before_recall"],
            "latch": latch_update,
        }

    # BM25 fast recall for candidates, then vector rerank via MCP search daemon.
    semantic = False
    search_mode = os.environ.get("RIPPLE_MEMORY_HOOK_SEARCH_MODE", "live").strip().lower() or "live"
    if search_mode not in {"off", "shadow", "live"}:
        search_mode = "live"

    final_top_k = int(os.environ.get("RIPPLE_MEMORY_HOOK_TOP_K", "4"))
    # Get more BM25 candidates for reranking
    bm25_top_k = max(final_top_k * 3, 10)

    try:
        recall = recall_project_memory(
            project=project,
            query=recall_query,
            top_k=bm25_top_k,
            data_dir=data_dir,
            create_project=False,
            semantic=semantic,
            search_mode=search_mode,
        )
    except Exception as exc:
        recall = {"query": query, "count": 0, "results": [], "status": f"recall_error:{exc.__class__.__name__}"}
        warnings.append(str(exc))

    # Try vector reranking via search daemon IPC
    bm25_results = list(recall.get("results") or [])
    if bm25_results and len(bm25_results) > final_top_k:
        try:
            from .search_ipc import request_rerank
            candidate_ids = [str(r.get("id") or "") for r in bm25_results if r.get("id")]
            rerank_response = request_rerank(
                data_dir=data_dir,
                project=project,
                query=recall_query,
                candidate_ids=candidate_ids,
                top_k=final_top_k,
            )
            if rerank_response and rerank_response.get("results"):
                # Merge vec_sim scores into original results, reorder by vec_sim
                reranked = rerank_response["results"]
                reranked_ids = [r["id"] for r in reranked]
                vec_sims = {r["id"]: r.get("vec_sim", 0.0) for r in reranked}
                # Reorder original results by reranked order
                id_to_result = {str(r.get("id")): r for r in bm25_results}
                merged = []
                for rid in reranked_ids:
                    if rid in id_to_result:
                        result = dict(id_to_result[rid])
                        result["vec_sim"] = vec_sims.get(rid, 0.0)
                        merged.append(result)
                if merged:
                    recall["results"] = merged[:final_top_k]
                    recall["count"] = len(recall["results"])
                    recall["reranked"] = True
        except Exception:
            pass  # Fall back to BM25 results

    # Trim to final top_k if not reranked
    if not recall.get("reranked") and len(bm25_results) > final_top_k:
        recall["results"] = bm25_results[:final_top_k]
        recall["count"] = len(recall["results"])

    latch = _read_original_words_latch(
        cwd=cwd,
        project=project,
        data_dir=data_dir,
        window_id=window_id,
        latch_file=os.environ.get("RIPPLE_MEMORY_LATCH_FILE"),
        no_latch=(normalized == "user_prompt_submit"),
        max_chars=2500,
    )
    other_latches = _read_other_latch_index(
        cwd=cwd,
        project=project,
        data_dir=data_dir,
        window_id=window_id,
        no_other_latches=False,
    )
    context = render_context_block(
        project, window_id, recall_query, recall, latch, other_latches,
        is_session_start=(normalized == "session_start"),
    )
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[: MAX_CONTEXT_CHARS - 24].rstrip() + "\n...[truncated by hook]"

    if not _should_emit_context(context):
        context = ""

    return {
        "ok": True,
        "enabled": True,
        "event": normalized,
        "project": project,
        "window_id": window_id,
        "context": context,
        "warnings": warnings,
        "latch": latch_update,
        "memory_written": False,
        "duration_ms": int((time.monotonic() - start) * 1000),
    }
