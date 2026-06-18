# Ripple Memory Hooks

Portable Codex plugin template for Ripple Memory.

Render `hooks.json.template` and `scripts/ripple-memory-codex-hook.cmd.template`
before installing. Do not ship another user's absolute paths.

The command path in rendered `hooks.json` should point to the target machine's
installed `ripple-memory-codex-hook.cmd`. Use JSON-escaped backslashes in
`hooks.json`, for example:

```json
"C:\\Users\\me\\.codex\\ripple-memory-marketplace\\plugins\\ripple-memory-hooks\\scripts\\ripple-memory-codex-hook.cmd"
```

The hook events are intentionally limited to:

- `SessionStart`
- `UserPromptSubmit`
- `Stop`

Do not add tool interception hooks unless a later design explicitly needs them.
