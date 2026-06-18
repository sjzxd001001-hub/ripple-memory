# Codex + Ripple Memory

For a full portable install, start from the root `INSTALL_FOR_LLM.md`.

## Runtime Shape

```text
Codex MCP server
+ Codex skill
+ Codex plugin hook adapter
```

Recommended paths:

```text
<CODEX_HOME>/mcp/ripple-memory-runtime
<CODEX_HOME>/mcp-data/ripple-memory
<CODEX_HOME>/skills/ripple-memory
<CODEX_HOME>/ripple-memory-marketplace/plugins/ripple-memory-hooks
```

Do not point Codex directly at the desktop source tree, and do not use the old
global `~/.memoria-mcp` data directory for new installs.

Set `MEMORIA_MCP_EXIT_ON_RUNTIME_CHANGE=false` in Codex MCP env. Codex does not
reliably hot-reconnect a closed stdio transport inside an already-open window;
restart Codex to load a synced runtime.
Set `MEMORIA_MCP_IDLE_EXIT_SECONDS=36000` so idle MCP servers wait 10 hours
before exiting.

## Hooks

Supported events:

- `SessionStart`
- `UserPromptSubmit`
- `Stop`

Codex hook JSON uses `"timeout": 3`; `hooks/list` displays this as
`timeoutSec=3`.

Use absolute hook command paths on Windows. Codex may list relative plugin paths
but fail to execute them under real `codex exec`.

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

## Hot Unplug

```text
RIPPLE_MEMORY_HOOK_ENABLED=0
<workspace>/.ripple-memory/hooks.disabled
disable/remove ripple-memory-hooks plugin
disable features.hooks / features.plugin_hooks
```
