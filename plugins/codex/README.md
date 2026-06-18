# Codex Plugin Templates

This directory contains the portable Codex plugin templates for Ripple Memory.
Read the root `INSTALL_FOR_LLM.md` first.

## Important Rules

- Install into a Codex-owned runtime, not the desktop source tree.
- Use Codex data dir: `<CODEX_HOME>\mcp-data\ripple-memory`.
- Render absolute hook command paths on Windows.
- Render an absolute Python executable into the `.cmd`; Codex Desktop may not inherit terminal PATH.
- Codex hook JSON uses `"timeout": 3`; do not write `timeoutSec`.
- Prefer one hook source: the installed plugin. Do not also create global `hooks.json` or direct `[hooks]` config.
- Set `MEMORIA_MCP_EXIT_ON_RUNTIME_CHANGE=false`; Codex does not reliably hot-reconnect a closed MCP stdio transport mid-window.
- Set `MEMORIA_MCP_IDLE_EXIT_SECONDS=36000`; this keeps an idle MCP server alive for 10 hours before it exits.
- The user must trust hooks before live hook e2e is considered complete.

## Verify

```powershell
ripple-memory-install-check `
  --host codex `
  --data-dir "<CODEX_HOME>\mcp-data\ripple-memory" `
  --skill-path "<CODEX_HOME>\skills\ripple-memory\SKILL.md" `
  --hook-cmd "<CODEX_HOME>\ripple-memory-marketplace\plugins\ripple-memory-hooks\scripts\ripple-memory-codex-hook.cmd" `
  --require-hook-cmd `
  --pretty
```

Add `--codex-live` only after hooks are trusted.
