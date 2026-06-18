# Qwen Code + Ripple Memory

For a full portable install, start from the root `INSTALL_FOR_LLM.md` or
`plugins/qwen-code/README.md`.

## Runtime Shape

```text
Qwen MCP server
+ Qwen skill
+ Qwen hook adapter
```

Recommended paths:

```text
<QWEN_HOME>/mcp/ripple-memory-runtime
<QWEN_HOME>/mcp-data/ripple-memory
<QWEN_HOME>/skills/ripple-memory
<QWEN_HOME>/ripple-memory-hook.cmd
```

Do not use the old global `~/.memoria-mcp` data directory for new installs.

## Hooks

Supported events:

- `SessionStart`
- `UserPromptSubmit`
- `Stop`

The hook adapter updates the Original Words Latch and injects lightweight memory
context. The MCP server provides the active tools:
`memoria_remember`, `memoria_recall`, `memoria_read`, `memoria_forget`.

Both hook and MCP config should point at the same Qwen-owned `$DataDir`.

## Post-Install Optimizations

After a fresh install, apply these optimizations to the **installed copies**
under `<QWEN_HOME>`. Do NOT modify the source `plugins/` templates.

### 1. Trim SKILL.md description

Qwen Code shows the `description` field from SKILL.md in the model's
`<available_skills>` list. The default description is long and mentions
multiple agent hosts (Codex, Claude Code, etc.) which is irrelevant noise
for the Qwen Code model.

Edit `<QWEN_HOME>/skills/ripple-memory/SKILL.md` frontmatter:

```yaml
---
name: ripple-memory
description: "Durable project memory via MCP. Triggers: 继续/上次/之前/记得吗/别忘了/上下文没了/别又忘了/按原方案/continue/previously/don't forget. Use memoria_remember for decisions/rules, memoria_recall for prior context. Hook auto-injects context on every prompt."
---
```

### 2. Strengthen hook usage directives

Edit `<QWEN_HOME>/mcp/ripple-memory-runtime/src/memoria_mcp/context_cli.py`.
In the `render_context_block` function, add these lines at the **top** of the
`usage:` block (before the existing lines):

```python
"- The hook already recalled for you. Use the injected context above directly. Only call memoria_recall manually for deeper or follow-up searches.",
"- Do NOT call tool_search for Ripple Memory MCP tools unless you need to write memory (memoria_remember) or do a deeper recall (memoria_recall).",
"- Memory hierarchy: use memoria_remember for project decisions, architecture rules, debug insights, code patterns, and important facts. Use the host built-in auto-memory ONLY for core user constitution/rules and explicitly user-emphasized points.",
```

These three lines address the key friction points in Qwen Code:

- **Deferred tools**: MCP tools require `tool_search` before use. The first line
  tells the model it doesn't need to — the hook already recalled.
- **Tool discovery**: The second line prevents wasted `tool_search` calls.
- **Memory hierarchy**: Qwen Code has a built-in "auto memory" system (writes
  `.qwen/projects/.../memory/*.md` via `write_file`). The third line establishes
  Ripple Memory as the primary project memory and reserves auto-memory for
  "core constitution" and user-emphasized rules only.

### 3. Improve SessionStart recall query

Edit `<QWEN_HOME>/mcp/ripple-memory-runtime/src/memoria_mcp/hook_core.py`.
In the `handle_hook_event` function, replace the SessionStart query:

```python
# Before (generic, often returns no hits):
elif normalized == "session_start":
    query = f"{project} startup context"

# After (reads existing latch goal for targeted recall):
elif normalized == "session_start":
    existing_latch = _read_original_words_latch(
        cwd=cwd, project=project, data_dir=data_dir,
        window_id=window_id, latch_file=None, no_latch=False, max_chars=800,
    )
    latch_goal = ""
    if isinstance(existing_latch, dict):
        latch_text = str(existing_latch.get("text") or "")
        latch_goal = _parse_latch_goal(latch_text)
    query = latch_goal or f"{project} recent work decisions architecture"
```

This uses the window's existing latch goal (from a previous session) as the
recall query, falling back to a more specific default than "startup context".

## Known Qwen Code Limitations

- **Deferred tools**: All MCP tools in Qwen Code are "deferred" — the model
  must call `tool_search` first to get the schema. This is a platform limitation,
  not configurable. The hook mitigates this by auto-recalling on every prompt.
- **Auto-memory coexistence**: Qwen Code's built-in auto-memory writes simple
  markdown files. It cannot be disabled via config. The hook usage directives
  steer the model to prefer Ripple Memory for project-level decisions.
- **Model behavior**: Different models (mimo-v2.5-pro, qwen3-coder-plus, etc.)
  have varying instruction-following tendencies. The usage directives help but
  cannot guarantee consistent behavior across all models.

## Verify

```powershell
ripple-memory-install-check `
  --host qwen `
  --data-dir "<QWEN_HOME>\mcp-data\ripple-memory" `
  --skill-path "<QWEN_HOME>\skills\ripple-memory\SKILL.md" `
  --hook-cmd "<QWEN_HOME>\ripple-memory-hook.cmd" `
  --require-hook-cmd `
  --pretty
```

Restart Qwen Code after config changes.
