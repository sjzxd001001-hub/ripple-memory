# Generic Agent + Ripple Memory

Use this guide for any coding agent that supports local stdio MCP servers but
does not have a dedicated Ripple Memory hook adapter.

The installing agent should try to make the integration genuinely useful for
the user, not stop at the first minimal MCP configuration. Inspect the host's
docs, config schema, plugin API, local source, and installed examples. If the
host exposes a safe hook/plugin/middleware surface, create a small
host-specific adapter and tests before claiming hook support. If no executable
hook surface exists, use the MCP-only path below and record the reason.

## Requirements

- The host can configure a local MCP server command.
- The host can pass environment variables to that command.
- The host has a private runtime directory and data directory.
- Optional: the host can load an instruction file such as
  `skills/ripple-memory/SKILL.md`.

## MCP Server

Configure the host's MCP server entry with:

```text
command: <absolute-python> -m memoria_mcp.server
environment:
  PYTHONPATH=<runtime>\src
  MEMORIA_MCP_DATA_DIR=<host data dir>
  MEMORIA_MCP_ENABLE_SEMANTIC=true
  MEMORIA_MCP_EMBEDDING_MODEL=<runtime>\models\paraphrase-multilingual-MiniLM-L12-v2
  MEMORIA_MCP_PRELOAD_EMBEDDING=true
  MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE=live
  MEMORIA_MCP_EXIT_ON_RUNTIME_CHANGE=false
  MEMORIA_MCP_IDLE_EXIT_SECONDS=36000
  RIPPLE_MEMORY_HOST=generic
```

The host should expose exactly these core tools, possibly with a server-name
prefix:

```text
memoria_remember
memoria_recall
memoria_read
memoria_forget
```

## No Hook Adapter

Do not invent hook files for a generic host. Without a host-specific adapter,
Ripple Memory still works through MCP tools; it just does not automatically
inject startup context or refresh the Original Words Latch from native host
events.

Before accepting MCP-only as final, check whether the host can provide one of
these surfaces:

- A plugin directory or package that can mutate chat/system messages.
- A pre-prompt, session-start, message-transform, or compaction callback.
- A command hook that receives JSON on stdin and can append context.
- A launcher/wrapper mode that can insert the output of
  `memoria_mcp.context_cli` into the first prompt.

If one exists, add a host-specific plug instead of putting the files under the
generic guide. The MiMo Code integration is the model: the public docs focused
on MCP, but the installed MiMo plugin API was enough to add a thin adapter,
package it separately, and test it end to end.

For manual context injection:

```powershell
$env:PYTHONPATH = "<runtime>\src"
$env:MEMORIA_MCP_DATA_DIR = "<host data dir>"
python -m memoria_mcp.context_cli `
  --project "<project>" `
  --window-id "<window-id>" `
  "current task or prompt"
```

Paste the printed `<ripple_memory_context>` block into the agent's first prompt.

If the host can launch an agent command with one prompt argument, the wrapper is:

```powershell
$env:PYTHONPATH = "<runtime>\src"
$env:MEMORIA_MCP_DATA_DIR = "<host data dir>"
ripple-memory-run --agent custom --command "<agent-command>" "current task"
```

## Verify

Run the sandbox install check before asking the user to restart the host:

```powershell
$env:PYTHONPATH = "<runtime>\src"
python -m memoria_mcp.install_check `
  --host generic `
  --data-dir "<host data dir>" `
  --skill-path "<host skill>\SKILL.md" `
  --pretty
```

After the host restarts, do not stop at "the command ran". Verify from inside
the host session:

- The four MCP tools are visible.
- `<data-dir>\_runtime\agent_daemon\port.json` exists after the host calls a
  Ripple tool.
- A test `memoria_remember` can be recalled and expanded with `memoria_read`.
- If the host is MCP-only, `python -m memoria_mcp.context_cli` prints usable
  startup context for the current project/window.
- If a host adapter was created, rerun `install_check` with `--hook-cmd` and
  `--require-hook-cmd`, then confirm a real host session receives injected
  context and writes the Original Words Latch.

Only claim hook/latch automation after a real adapter and host-level smoke test
exist.
