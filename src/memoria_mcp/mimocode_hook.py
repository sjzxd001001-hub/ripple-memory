"""MiMo Code hook adapter for Ripple Memory.

Thin adapter: parses MiMo Code hook stdin JSON, normalizes it into a
RippleHookEvent, delegates to hook_core, and prints MiMo Code compatible hook
JSON. It must not own memory business logic.
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


MIMOCODE_EVENT_NAMES = {
    "session_start": "SessionStart",
    "user_prompt_submit": "UserPromptSubmit",
    "stop": "Stop",
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
    return _first_string(
        payload.get("prompt"),
        payload.get("user_text"),
        payload.get("userText"),
        payload.get("message"),
        _dig(payload, "input", "prompt"),
        _dig(payload, "params", "prompt"),
        _dig(payload, "params", "userText"),
    )


def _extract_assistant_text(payload: Dict[str, Any]) -> str:
    return _first_string(
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


def mimocode_payload_to_event(payload: Dict[str, Any]) -> RippleHookEvent:
    event_name = _first_string(
        payload.get("hook_event_name"),
        payload.get("hookEventName"),
        payload.get("event"),
        payload.get("eventName"),
        _dig(payload, "params", "event"),
        _dig(payload, "params", "eventName"),
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
        payload.get("sessionID"),
        _dig(payload, "params", "sessionId"),
    )
    return RippleHookEvent(
        agent="mimocode",
        event=event_name,
        cwd=cwd,
        project=_first_string(payload.get("project"), _dig(payload, "params", "project")),
        window_id=_first_string(
            payload.get("window_id"),
            payload.get("windowId"),
            _dig(payload, "params", "windowId"),
            session_id,
        ),
        user_text=_extract_user_text(payload),
        assistant_text=_extract_assistant_text(payload),
        session_id=session_id,
        turn_id=_first_string(payload.get("turn_id"), payload.get("turnId"), _dig(payload, "params", "turnId")),
        transcript_path=_first_string(payload.get("transcript_path"), payload.get("transcriptPath")),
        raw=payload,
    )


def result_to_mimocode_output(result: Dict[str, Any]) -> Dict[str, Any]:
    normalized = normalize_event_name(str(result.get("event") or ""))
    context = str(result.get("context") or "").strip()
    warnings = [str(item) for item in result.get("warnings") or [] if str(item).strip()]

    output: Dict[str, Any] = {}
    if context:
        output["context"] = context
        output["hookEventName"] = MIMOCODE_EVENT_NAMES.get(normalized, normalized)
    if warnings:
        output["warning"] = "Ripple Memory hook warning: " + "; ".join(warnings[:3])
    return output


def handle_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    event = mimocode_payload_to_event(payload)
    result = handle_hook_event(event)
    return result_to_mimocode_output(result)


def _read_stdin_with_timeout(timeout_seconds: float = STDIN_TIMEOUT_SECONDS) -> str:
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


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    raw = _read_stdin_with_timeout()
    _debug_log(f"stdin_chars={len(raw)}")
    if not raw.strip():
        print(json.dumps({}, ensure_ascii=False))
        return 0
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be a JSON object")
    except Exception as exc:
        print(json.dumps({
            "warning": f"Ripple Memory hook ignored invalid JSON: {exc.__class__.__name__}",
        }, ensure_ascii=False))
        return 0

    try:
        output = handle_payload(payload)
    except Exception as exc:
        output = {
            "warning": f"Ripple Memory hook failed open: {exc.__class__.__name__}",
        }
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
