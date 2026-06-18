# Claude Code + Ripple Memory

For a full portable install, start from the root `INSTALL_FOR_LLM.md` or
`plugins/claude-code/README.md`.

## Runtime Shape

```text
Claude MCP server
+ Claude skill
+ Claude Code hook adapter
```

Recommended paths:

```text
<CLAUDE_HOME>/mcp/ripple-memory-runtime
<CLAUDE_HOME>/mcp-data/ripple-memory
<CLAUDE_HOME>/skills/ripple-memory
<CLAUDE_HOME>/ripple-memory-hooks/ripple-memory-claude-hook.cmd
```

Do not point Claude directly at the desktop source tree, and do not use the old
global `~/.memoria-mcp` data directory for new installs.

## MCP Defaults

Use:

```text
MEMORIA_MCP_ENABLE_SEMANTIC=true
MEMORIA_MCP_PRELOAD_EMBEDDING=true
MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE=live
```

Semantic recall should use the local MiniLM model. MCP server configs preload it
at startup so the first recall does not pay the model-load cost inside a tool
call.

## Hooks

Supported events:

- `SessionStart`
- `UserPromptSubmit`
- `Stop`

The hook adapter updates the Original Words Latch and injects lightweight memory
context. The MCP server provides active memory tools.

Use Claude Code's exec-form hook config for the Windows batch wrapper:

```json
{
  "command": "cmd.exe",
  "args": ["/d", "/c", "<CLAUDE_HOME>\\ripple-memory-hooks\\ripple-memory-claude-hook.cmd"],
  "timeout": 3
}
```

Injected context must appear in the hook stdout as
`hookSpecificOutput.additionalContext`. A top-level `context` field is not
enough for Claude Code.

After changing Claude Code config, fully restart Claude Code or reload VS Code.
Closing one panel can leave stale `claude.exe` / MCP processes alive.

## Verify

```powershell
ripple-memory-install-check `
  --host claude `
  --data-dir "<CLAUDE_HOME>\mcp-data\ripple-memory" `
  --skill-path "<CLAUDE_HOME>\skills\ripple-memory\SKILL.md" `
  --hook-cmd "<CLAUDE_HOME>\ripple-memory-hooks\ripple-memory-claude-hook.cmd" `
  --require-hook-cmd `
  --pretty
```

## Hot Unplug

```text
RIPPLE_MEMORY_HOOK_ENABLED=0
<workspace>/.ripple-memory/hooks.disabled
remove the Claude hooks block
```
