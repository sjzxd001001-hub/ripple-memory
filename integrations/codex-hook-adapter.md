# Codex Hook Adapter

The Codex adapter is a thin translator:

```text
Codex hook JSON
  -> memoria_mcp.codex_hook
  -> RippleHookEvent
  -> memoria_mcp.hook_core
  -> Codex hook JSON output
```

Memory business logic belongs in `hook_core.py` and the MCP engine, not in the
Codex adapter.

## Events

Supported events:

- `SessionStart`
- `UserPromptSubmit`
- `Stop`

Do not start with `PreToolUse` / `PostToolUse`; those are execution
interception points and would couple memory to tool runtime.

## Timeout

Hook config uses `"timeout": 3`. Codex displays this as `timeoutSec=3`.

Do not write `timeoutSec` directly in hook JSON.

On timeout or error, the hook should fail open and return no memory context
rather than blocking the agent.

## Windows Path Rule

Render absolute hook command paths for the target machine. Relative plugin
commands may appear valid in `hooks/list` but fail under real `codex exec`.

Render an absolute Python path into the `.cmd`; Codex Desktop may not inherit
PowerShell PATH.

## Trust

Codex requires manual hook trust. If hook JSON changes, Codex may mark hooks as
modified. Re-trust the final hook before claiming live integration is active.

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
