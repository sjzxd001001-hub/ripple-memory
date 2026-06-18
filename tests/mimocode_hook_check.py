"""Offline checks for the MiMo Code hook adapter."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from memoria_mcp.lifecycle import window_latch_file
from memoria_mcp.mimocode_hook import handle_payload
from memoria_mcp.server import MemoriaRouter


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run_check() -> dict[str, Any]:
    marker = f"mimocode_hook_marker_{int(time.time() * 1000)}"
    with tempfile.TemporaryDirectory(prefix="ripple-memory-mimocode-hook-") as tmp:
        root = Path(tmp)
        workspace = root / "workspace"
        workspace.mkdir()
        data_dir = root / "memory-data"

        old_env = os.environ.copy()
        try:
            os.environ["MEMORIA_MCP_DATA_DIR"] = str(data_dir)
            os.environ["MEMORIA_MCP_ENABLE_SEMANTIC"] = "false"
            os.environ["MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE"] = "live"
            os.environ["MEMORIA_MCP_WRITE_QUEUE_ENABLED"] = "false"
            os.environ["RIPPLE_MEMORY_HOOK_ENABLE_SEMANTIC"] = "false"
            os.environ["RIPPLE_MEMORY_HOOK_SEARCH_MODE"] = "live"
            os.environ["RIPPLE_MEMORY_PROJECT"] = "mimocode_adapter_project"
            os.environ["RIPPLE_MEMORY_WINDOW_ID"] = "mimocode_adapter_window"
            os.environ.pop("RIPPLE_MEMORY_HOOK_ENABLED", None)

            router = MemoriaRouter(str(data_dir))
            try:
                remember = router._dispatch_tool(
                    "memoria_remember",
                    {
                        "project": "mimocode_adapter_project",
                        "content": (
                            f"{marker}: MiMo Code hook adapter should emit context "
                            "through the MiMo plugin context field."
                        ),
                        "type": "arch_decision",
                        "importance": 0.9,
                        "confidence": 0.95,
                    },
                )
                _assert(remember.get("stored") is True, f"setup remember failed: {remember}")
            finally:
                router.close()

            payload = {
                "hook_event_name": "user_prompt_submit",
                "cwd": str(workspace),
                "session_id": "mimocode-session",
                "turn_id": "mimocode-turn",
                "user_text": f"请回忆 MiMo hook 标记 {marker}",
            }
            output = handle_payload(payload)
            context = str(output.get("context") or "")
            _assert(marker in context, f"MiMo context missing recalled marker: {output}")
            _assert(output.get("hookEventName") == "UserPromptSubmit", f"MiMo event name not normalized: {output}")

            latch = window_latch_file(
                cwd=workspace,
                project="mimocode_adapter_project",
                window_id="mimocode-session",
                data_dir=data_dir,
            )
            _assert(latch.is_file(), f"latch not written: {latch}")
            latch_text = latch.read_text(encoding="utf-8")
            _assert(marker in latch_text, "latch missing original user prompt")

            stop_output = handle_payload({
                "event": "Stop",
                "cwd": str(workspace),
                "sessionId": "mimocode-session",
                "assistantText": f"MiMo stop checkpoint for {marker}",
            })
            _assert("context" not in stop_output, f"Stop hook should not inject context: {stop_output}")
            latch_text = latch.read_text(encoding="utf-8")
            _assert(f"MiMo stop checkpoint for {marker}" in latch_text, "Stop hook did not refresh latch")

            os.environ["RIPPLE_MEMORY_HOOK_ENABLED"] = "0"
            disabled_output = handle_payload(payload)
            _assert("context" not in disabled_output, f"disabled hook still emitted context: {disabled_output}")

            return {
                "ok": True,
                "workspace": str(workspace),
                "data_dir": str(data_dir),
                "context_contains_marker": marker in context,
                "latch_written": latch.is_file(),
                "disabled_no_context": "context" not in disabled_output,
            }
        finally:
            os.environ.clear()
            os.environ.update(old_env)


def main() -> int:
    print(json.dumps(run_check(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
