"""Codex hook adapter for Ripple Memory.

This module is intentionally thin: it parses Codex hook stdin, normalizes it
into a Ripple hook event, delegates to hook_core, and prints Codex-compatible
hook JSON. It must not own memory business logic.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any, Dict

from .hook_core import RippleHookEvent, handle_hook_event, normalize_event_name

STDIN_TIMEOUT_SECONDS = 2.0


CODEX_EVENT_NAMES = {
    "session_start": "SessionStart",
    "user_prompt_submit": "UserPromptSubmit",
    "stop": "Stop",
    "window_archive": "WindowArchive",
    "window_delete": "WindowDelete",
    "window_restore": "WindowRestore",
}


def _configure_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def _first_string(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _dig(payload: Dict[str, Any], *keys: str) -> Any:
    cur: Any = payload
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _extract_user_text(payload: Dict[str, Any]) -> str:
    direct = _first_string(
        payload.get("user_text"),
        payload.get("userText"),
        payload.get("prompt"),
        payload.get("user_prompt"),
        payload.get("userPrompt"),
        payload.get("message"),
        _dig(payload, "input", "prompt"),
        _dig(payload, "params", "prompt"),
        _dig(payload, "params", "userPrompt"),
    )
    if direct:
        return direct

    content = payload.get("content") or _dig(payload, "params", "content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = _first_string(item.get("text"), item.get("content"))
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()
    return ""


def _extract_assistant_text(payload: Dict[str, Any]) -> str:
    direct = _first_string(
        payload.get("assistant_text"),
        payload.get("assistantText"),
        payload.get("assistant_response"),
        payload.get("assistantResponse"),
        payload.get("response"),
        payload.get("completion"),
        payload.get("output"),
        _dig(payload, "params", "assistantText"),
        _dig(payload, "params", "response"),
        _dig(payload, "result", "text"),
    )
    if direct:
        return direct

    content = payload.get("content") or _dig(payload, "params", "content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and str(item.get("role") or "").lower() == "assistant":
                text = _first_string(item.get("text"), item.get("content"))
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()
    return ""


def codex_payload_to_event(payload: Dict[str, Any]) -> RippleHookEvent:
    event_name = _first_string(
        payload.get("event"),
        payload.get("eventName"),
        payload.get("hook_event_name"),
        payload.get("hookEventName"),
        _dig(payload, "params", "event"),
        _dig(payload, "params", "eventName"),
        _dig(payload, "hook", "eventName"),
    )
    cwd = _first_string(
        payload.get("cwd"),
        payload.get("workingDirectory"),
        payload.get("workspace"),
        _dig(payload, "params", "cwd"),
        _dig(payload, "workspace", "cwd"),
        os.getcwd(),
    )
    session_id = _first_string(
        payload.get("session_id"),
        payload.get("sessionId"),
        payload.get("thread_id"),
        payload.get("threadId"),
        _dig(payload, "params", "sessionId"),
        _dig(payload, "params", "threadId"),
    )
    turn_id = _first_string(
        payload.get("turn_id"),
        payload.get("turnId"),
        _dig(payload, "params", "turnId"),
    )
    return RippleHookEvent(
        agent="codex",
        event=event_name,
        cwd=cwd,
        project=_first_string(payload.get("project"), _dig(payload, "params", "project")),
        window_id=_first_string(payload.get("window_id"), payload.get("windowId"), _dig(payload, "params", "windowId")),
        user_text=_extract_user_text(payload),
        assistant_text=_extract_assistant_text(payload),
        session_id=session_id,
        turn_id=turn_id,
        transcript_path=_first_string(payload.get("transcript_path"), payload.get("transcriptPath")),
        raw=payload,
    )


def result_to_codex_output(result: Dict[str, Any]) -> Dict[str, Any]:
    normalized = normalize_event_name(str(result.get("event") or ""))
    codex_event = CODEX_EVENT_NAMES.get(normalized, normalized)
    context = str(result.get("context") or "").strip()
    warnings = [str(item) for item in result.get("warnings") or [] if str(item).strip()]

    output: Dict[str, Any] = {
        "continue": True,
        "suppressOutput": True,
    }
    if warnings:
        output["systemMessage"] = "Ripple Memory hook warning: " + "; ".join(warnings[:3])
        output["suppressOutput"] = False
    if context:
        output["hookSpecificOutput"] = {
            "hookEventName": codex_event,
            "additionalContext": context,
        }
    return output


def _read_stdin_with_timeout(timeout_seconds: float = STDIN_TIMEOUT_SECONDS) -> str:
    """Read hook payload without letting a missing EOF hang the host agent."""
    box: Dict[str, Any] = {"value": "", "error": None}

    def reader() -> None:
        try:
            box["value"] = sys.stdin.read()
        except Exception as exc:
            box["error"] = exc

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        return ""
    if box.get("error"):
        return ""
    return str(box.get("value") or "")


def _debug_log(message: str) -> None:
    path = os.environ.get("RIPPLE_MEMORY_HOOK_DEBUG_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def handle_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    event = codex_payload_to_event(payload)
    _debug_log(
        "event="
        f"{event.event or '<missing>'} cwd={event.cwd or '<missing>'} "
        f"session={event.session_id or '<missing>'} turn={event.turn_id or '<missing>'} "
        f"user_text_chars={len(event.user_text or '')}"
    )
    result = handle_hook_event(event)
    _debug_log(
        "result="
        f"enabled={result.get('enabled')} event={result.get('event')} "
        f"context_chars={len(str(result.get('context') or ''))} "
        f"warnings={len(result.get('warnings') or [])}"
    )
    return result_to_codex_output(result)


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    raw = _read_stdin_with_timeout()
    _debug_log(f"stdin_chars={len(raw)}")
    if not raw.strip():
        print(json.dumps({"continue": True, "suppressOutput": True}, ensure_ascii=False))
        return 0
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be a JSON object")
    except Exception as exc:
        print(json.dumps({
            "continue": True,
            "suppressOutput": False,
            "systemMessage": f"Ripple Memory hook ignored invalid JSON: {exc.__class__.__name__}",
        }, ensure_ascii=False))
        return 0

    try:
        output = handle_payload(payload)
    except Exception as exc:
        output = {
            "continue": True,
            "suppressOutput": False,
            "systemMessage": f"Ripple Memory hook failed open: {exc.__class__.__name__}",
        }
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
