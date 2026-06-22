"""Offline checks for Ripple Memory hook adapters.

This does not launch Codex. It feeds Codex-like JSON into the adapter and
verifies that the adapter remains a thin plug over the core memory engine.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from memoria_mcp.codex_hook import handle_payload
from memoria_mcp.hook_core import RippleHookEvent, handle_hook_event
from memoria_mcp.lifecycle import window_latch_file
from memoria_mcp.server import MemoriaRouter


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_check() -> dict[str, Any]:
    marker = f"hook_marker_{int(time.time() * 1000)}"
    progress_marker = f"hook_progress_{int(time.time() * 1000)}"
    with tempfile.TemporaryDirectory(prefix="ripple-memory-hook-") as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        workspace.mkdir()
        data_dir = root / "memory-data"
        repo_root = Path(__file__).resolve().parents[1]
        hook_script_sources = [
            repo_root / "plugins" / "codex" / "ripple-memory-hooks" / "scripts" / "ripple-memory-codex-hook.cmd.template",
            repo_root / "plugins" / "claude-code" / "ripple-memory-hooks" / "scripts" / "ripple-memory-claude-hook.cmd",
            repo_root / "plugins" / "qwen-code" / "ripple-memory-hooks" / "scripts" / "ripple-memory-qwen-hook.cmd.template",
            repo_root / "plugins" / "mimocode" / "ripple-memory-hooks" / "scripts" / "ripple-memory-mimocode-hook.cmd.template",
        ]
        for script_path in hook_script_sources:
            script_bytes = script_path.read_bytes()
            _assert(not script_bytes.startswith(b"\xef\xbb\xbf"), f"hook script must be UTF-8 without BOM: {script_path}")
            _assert(script_bytes.startswith(b"@echo off"), f"hook script must start with @echo off: {script_path}")

        old_env = os.environ.copy()
        try:
            os.environ["MEMORIA_MCP_DATA_DIR"] = str(data_dir)
            os.environ["MEMORIA_MCP_ENABLE_SEMANTIC"] = "false"
            os.environ["MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE"] = "live"
            os.environ["MEMORIA_MCP_WRITE_QUEUE_ENABLED"] = "false"
            os.environ["RIPPLE_MEMORY_HOOK_ENABLE_SEMANTIC"] = "false"
            os.environ["RIPPLE_MEMORY_HOOK_SEARCH_MODE"] = "live"
            os.environ["RIPPLE_MEMORY_PROJECT"] = "adapter_project"
            os.environ["RIPPLE_MEMORY_WINDOW_ID"] = "adapter_window"
            os.environ.pop("RIPPLE_MEMORY_HOOK_ENABLED", None)

            router = MemoriaRouter(str(data_dir))
            try:
                remember = router._dispatch_tool(
                    "memoria_remember",
                    {
                        "project": "adapter_project",
                        "content": (
                            f"{marker}: Codex hook adapter should emit context "
                            "without owning memory business logic."
                        ),
                        "type": "arch_decision",
                        "importance": 0.9,
                        "confidence": 0.95,
                    },
                )
                _assert(remember.get("stored") is True, f"setup remember failed: {remember}")
                progress_remember = router._dispatch_tool(
                    "memoria_remember",
                    {
                        "project": "adapter_project",
                        "content": (
                            f"{progress_marker}: current phase progress latest commit "
                            "next step record for adapter_project hook natural-language recall."
                        ),
                        "type": "arch_decision",
                        "importance": 0.9,
                        "confidence": 0.95,
                    },
                )
                _assert(progress_remember.get("stored") is True, f"progress setup remember failed: {progress_remember}")
            finally:
                router.close()

            payload = {
                "eventName": "userPromptSubmit",
                "cwd": str(workspace),
                "threadId": "thread-adapter",
                "turnId": "turn-1",
                "prompt": f"继续测试 {marker} 的 Codex 插头",
            }
            output = handle_payload(payload)
            context = output.get("hookSpecificOutput", {}).get("additionalContext", "")
            _assert(output.get("continue") is True, f"adapter did not continue: {output}")
            _assert(marker in context, f"context missing recalled marker: {output}")
            _assert("adapter_window" in context, "context missing window identity")
            _assert("status: read_skipped" in context, "UserPromptSubmit latch read skip status is unclear")
            _assert("reason: user_prompt_submit_no_latch" in context, "UserPromptSubmit latch skip reason missing")
            _assert("status: disabled" not in context, "UserPromptSubmit context still says latch is disabled")

            progress_result = handle_hook_event(
                RippleHookEvent(
                    agent="codex",
                    event="UserPromptSubmit",
                    cwd=str(workspace),
                    project="adapter_project",
                    window_id="adapter_progress_window",
                    session_id="progress-session",
                    turn_id="progress-1",
                    user_text="任务进度到哪里了",
                ),
            )
            progress_context = str(progress_result.get("context") or "")
            _assert(progress_marker in progress_context, f"natural progress prompt missed memory: {progress_result}")
            _assert("phase progress" in progress_context, "expanded progress aliases missing from hook context query")
            progress_latch = window_latch_file(
                cwd=workspace,
                project="adapter_project",
                window_id="adapter_progress_window",
                data_dir=data_dir,
            )
            progress_latch_text = progress_latch.read_text(encoding="utf-8")
            _assert("任务进度到哪里了" in progress_latch_text, "progress latch lost original user words")
            _assert("phase progress" not in progress_latch_text, "expanded recall query polluted original-words latch")

            latch = window_latch_file(
                cwd=workspace,
                project="adapter_project",
                window_id="adapter_window",
                data_dir=data_dir,
            )
            _assert(latch.is_file(), f"latch not written: {latch}")
            latch_text = latch.read_text(encoding="utf-8")
            _assert(marker in latch_text, "latch missing original user prompt")
            _assert("## Agent Task Understanding" in latch_text, "latch missing task-understanding section")
            _assert("Hook seed only" not in latch_text, "latch still contains placeholder task understanding")

            stop_payload = {
                "eventName": "Stop",
                "cwd": str(workspace),
                "threadId": "thread-adapter",
                "turnId": "turn-1",
                "assistantText": f"Agent plan for {marker}: preserve the chosen next action after compression.",
            }
            stop_output = handle_payload(stop_payload)
            _assert(stop_output.get("continue") is True, f"adapter stop did not continue: {stop_output}")
            latch_text = latch.read_text(encoding="utf-8")
            _assert(
                f"Agent plan for {marker}" in latch_text,
                "Stop hook did not refresh agent task understanding",
            )
            _assert("- State: active" in latch_text, "ordinary Stop checkpoint should remain active")
            _assert(
                "Next action: Resume from the concrete next step" not in latch_text,
                "Stop hook wrote the old generic Resume next-action",
            )

            completed_marker = f"completed-stop {marker}"
            completed_stop = handle_hook_event(
                RippleHookEvent(
                    agent="codex",
                    event="Stop",
                    cwd=str(workspace),
                    project="adapter_project",
                    window_id="adapter_completed_window",
                    session_id="completed-session",
                    turn_id="completed-1",
                    assistant_text=(
                        f"{completed_marker}: 已提交到当前分支，验证通过，"
                        "当前工作树干净。"
                    ),
                ),
            )
            _assert(completed_stop.get("latch", {}).get("task_state") == "completed", f"completed Stop was not classified: {completed_stop}")
            completed_latch = window_latch_file(
                cwd=workspace,
                project="adapter_project",
                window_id="adapter_completed_window",
                data_dir=data_dir,
            )
            completed_latch_text = completed_latch.read_text(encoding="utf-8")
            _assert("- State: completed" in completed_latch_text, "completed latch missing completed state")
            _assert("Completed checkpoint" in completed_latch_text, "completed latch missing completed checkpoint")
            completed_session = handle_hook_event(
                RippleHookEvent(
                    agent="codex",
                    event="SessionStart",
                    cwd=str(workspace),
                    project="adapter_project",
                    window_id="adapter_completed_window",
                    session_id="completed-session",
                ),
            )
            completed_context = str(completed_session.get("context") or "")
            _assert("- State: completed" in completed_context, "SessionStart context lost completed state")
            completed_context_lower = completed_context.lower()
            _assert("progress anchor" in completed_context_lower, "SessionStart context lacks progress-anchor guidance")
            _assert("latch: original intent" in completed_context_lower, "SessionStart context lacks concise latch guidance")
            _assert(
                "Next action: Resume from the concrete next step" not in completed_context,
                "SessionStart context still contains old generic Resume next-action",
            )

            burst_window = "adapter_burst_window"
            long_one = f"burst-one {marker} " + ("A" * 700)
            long_two_marker = f"burst-two {marker}"
            long_two = long_two_marker + " " + ("B" * 700)
            burst_one = handle_hook_event(
                RippleHookEvent(
                    agent="codex",
                    event="UserPromptSubmit",
                    cwd=str(workspace),
                    project="adapter_project",
                    window_id=burst_window,
                    session_id="burst-session",
                    turn_id="burst-1",
                    user_text=long_one,
                ),
            )
            _assert(burst_one.get("latch", {}).get("updated"), f"first long prompt was not latched: {burst_one}")
            burst_two = handle_hook_event(
                RippleHookEvent(
                    agent="codex",
                    event="UserPromptSubmit",
                    cwd=str(workspace),
                    project="adapter_project",
                    window_id=burst_window,
                    session_id="burst-session",
                    turn_id="burst-2",
                    user_text=long_two,
                ),
            )
            _assert(
                burst_two.get("latch", {}).get("reason") == "latch_burst_suppressed",
                f"second long burst prompt was not suppressed: {burst_two}",
            )
            burst_latch = window_latch_file(
                cwd=workspace,
                project="adapter_project",
                window_id=burst_window,
                data_dir=data_dir,
            )
            burst_latch_text = burst_latch.read_text(encoding="utf-8")
            _assert("burst-one" in burst_latch_text, "first burst prompt missing from latch")
            _assert(long_two_marker not in burst_latch_text, "suppressed long prompt polluted latch")

            suppressed_stop_marker = f"suppressed-stop {marker}"
            burst_stop = handle_hook_event(
                RippleHookEvent(
                    agent="codex",
                    event="Stop",
                    cwd=str(workspace),
                    project="adapter_project",
                    window_id=burst_window,
                    session_id="burst-session",
                    turn_id="burst-2",
                    assistant_text=f"{suppressed_stop_marker}: should not overwrite the main task understanding.",
                ),
            )
            _assert(
                burst_stop.get("latch", {}).get("reason") == "latch_burst_suppressed_stop",
                f"suppressed burst Stop still refreshed latch: {burst_stop}",
            )
            burst_latch_text = burst_latch.read_text(encoding="utf-8")
            _assert(suppressed_stop_marker not in burst_latch_text, "suppressed Stop polluted latch understanding")

            short_marker = f"short-burst {marker}"
            short_submit = handle_hook_event(
                RippleHookEvent(
                    agent="codex",
                    event="UserPromptSubmit",
                    cwd=str(workspace),
                    project="adapter_project",
                    window_id=burst_window,
                    session_id="burst-session",
                    turn_id="burst-3",
                    user_text=short_marker,
                ),
            )
            _assert(short_submit.get("latch", {}).get("updated"), f"short prompt was incorrectly suppressed: {short_submit}")
            short_stop_marker = f"short-stop {marker}"
            handle_hook_event(
                RippleHookEvent(
                    agent="codex",
                    event="Stop",
                    cwd=str(workspace),
                    project="adapter_project",
                    window_id=burst_window,
                    session_id="burst-session",
                    turn_id="burst-3",
                    assistant_text=f"{short_stop_marker}: short prompt should refresh understanding.",
                ),
            )
            burst_latch_text = burst_latch.read_text(encoding="utf-8")
            _assert(short_marker in burst_latch_text, "short prompt did not enter latch after burst suppression")
            _assert(short_stop_marker in burst_latch_text, "short prompt Stop did not refresh latch understanding")

            os.environ["RIPPLE_MEMORY_HOOK_ENABLED"] = "0"
            disabled_output = handle_payload(payload)
            _assert(
                "hookSpecificOutput" not in disabled_output,
                f"disabled hook still emitted context: {disabled_output}",
            )

            return {
                "ok": True,
                "workspace": str(workspace),
                "data_dir": str(data_dir),
                "context_contains_marker": marker in context,
                "user_prompt_latch_status_read_skipped": "status: read_skipped" in context,
                "natural_progress_prompt_context_contains_marker": progress_marker in progress_context,
                "latch_written": latch.is_file(),
                "stop_refreshed_understanding": f"Agent plan for {marker}" in latch_text,
                "completed_stop_checkpoint": "completed checkpoint" in completed_context_lower,
                "compression_progress_anchor_guidance": "progress anchor" in completed_context_lower,
                "burst_gate_suppressed_second_long_prompt": long_two_marker not in burst_latch_text,
                "burst_gate_allows_short_prompt": short_marker in burst_latch_text,
                "disabled_no_context": "hookSpecificOutput" not in disabled_output,
                "hook_scripts_no_bom": True,
            }
        finally:
            os.environ.clear()
            os.environ.update(old_env)


def main() -> int:
    print(json.dumps(run_check(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
