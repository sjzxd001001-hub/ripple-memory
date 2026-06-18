# Ripple Memory LLM-Guided Install

This is the portable install card for coding agents. The installing agent should
inspect the target machine, copy a host-owned runtime, render local paths, update
host config carefully, and run the unified check. Do not copy paths from the
source author's computer.

## Product Shape

```text
ripple-memory MCP
+ ripple-memory skill
+ ripple-memory launcher
+ thin host hook plug where the host exposes a supported hook system
```

Adapters must stay thin:

```text
host hook event -> Ripple standard hook event -> Ripple core -> context/latch/result
```

Memory behavior belongs in `src/memoria_mcp`, especially `server.py` and
`hook_core.py`.

## Installer Duty

Your job is to leave the user's host genuinely integrated, not merely to copy a
minimal MCP stanza and declare success. Before falling back to MCP-only:

- Inspect the host's docs, config schema, plugin API, local source, and
  installed examples.
- Prefer an existing safe hook/plugin/middleware surface over manual context
  paste.
- If a hook surface exists, create a host-specific thin adapter under
  `plugins/<host>/...`, add an adapter test, render the hook command, and run
  `install_check --hook-cmd ... --require-hook-cmd`.
- If the host has no executable hook surface, document that evidence and use
  the Generic MCP Agent path.

MiMo Code is the example: the public docs primarily described MCP, but the
installed MiMo plugin API exposed message/system/compaction hooks, so Ripple
ships a MiMo-specific plug instead of leaving users with MCP-only behavior.

## Storage Rules

- Each host gets its own runtime copy and data root.
- New installs must not use the old global `~/.memoria-mcp` layout.
- Recommended data roots:
  - Codex: `<CODEX_HOME>/mcp-data/ripple-memory`
  - Claude Code: `<CLAUDE_HOME>/mcp-data/ripple-memory`
  - Qwen Code: `<QWEN_HOME>/mcp-data/ripple-memory`
  - MiMo Code / generic hosts: a host-owned absolute path such as
    `<MIMOCODE_HOME>/data/ripple-memory` or `<HOST_HOME>/mcp-data/ripple-memory`
- SQLite `memoria.db` stores runtime truth: graph state, search index metadata, and memory-evolution state/edges.
- JSONL/archive files store frozen full-content history and Dreamer archive blocks.
- Obsolete SQL payload tables `memory_stream` and `archive_blocks` must be absent.
- Default recall returns current口径 only. `include_evolution=true` exposes superseded history and evolution chains.
- Original Words Latch is window-local and lives under:

```text
<data-dir>/_window_state/<project>/<window-id>/original-word-latch.md
```

## General Install Steps

Use these steps for every host.

1. Detect paths:

```powershell
$SourceRoot = "<ripple-memory source root>"
$Python = (Get-Command python).Source
```

2. Copy runtime:

```powershell
New-Item -ItemType Directory -Force -Path $Runtime,$DataDir | Out-Null
Copy-Item -LiteralPath (Join-Path $SourceRoot "src") -Destination $Runtime -Recurse -Force
Copy-Item -LiteralPath (Join-Path $SourceRoot "pyproject.toml") -Destination $Runtime -Force
if (Test-Path (Join-Path $SourceRoot "models")) {
  Copy-Item -LiteralPath (Join-Path $SourceRoot "models") -Destination $Runtime -Recurse -Force
}
# Optional: install dependencies/entry points. The host config below still uses
# PYTHONPATH so it does not depend on global console scripts.
python -m pip install -e $Runtime
```

Current default local embedding model is
`models\paraphrase-multilingual-MiniLM-L12-v2`. If this directory is missing,
copy or download it into the source `models` folder before running
`--require-local-model`.

When several hosts share one Python install, console entry points can point at
the most recently installed runtime. For verification, prefer explicit
`PYTHONPATH=<runtime>\src` plus `python -m memoria_mcp.install_check`.

3. Copy skill:

```powershell
New-Item -ItemType Directory -Force -Path $SkillTarget | Out-Null
Copy-Item -LiteralPath (Join-Path $SourceRoot "skills\ripple-memory\*") -Destination $SkillTarget -Recurse -Force
```

4. Configure MCP server with these env values:

```text
PYTHONPATH=<runtime>\src
MEMORIA_MCP_DATA_DIR=<host data dir>
MEMORIA_MCP_ENABLE_SEMANTIC=true
MEMORIA_MCP_EMBEDDING_MODEL=<runtime>\models\paraphrase-multilingual-MiniLM-L12-v2
MEMORIA_MCP_PRELOAD_EMBEDDING=true
MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE=live
MEMORIA_MCP_EXIT_ON_RUNTIME_CHANGE=false
MEMORIA_MCP_IDLE_EXIT_SECONDS=36000
RIPPLE_MEMORY_HOST=<codex|claude|qwen|mimocode|generic>
```

Use an absolute Python path when possible. GUI-launched hosts may not inherit the
same PATH as PowerShell.

### Transport modes: stdio proxy vs SSE direct-connect

**Default: stdio proxy** (works with all agents)
Each window spawns a thin proxy process that forwards MCP calls to the daemon.
```toml
[mcp_servers.ripple-memory]
command = "python"
args = ["-m", "memoria_mcp.server"]
```

**Optional: SSE direct-connect** (zero per-window processes)
If the agent supports URL-based MCP servers, set `MEMORIA_MCP_SSE_PORT` in the
daemon env and use `url` instead of `command`:
```toml
# Daemon env: add MEMORIA_MCP_SSE_PORT=0 (auto) or a fixed port
MEMORIA_MCP_SSE_PORT=0

# Agent config: use url instead of command
[mcp_servers.ripple-memory]
url = "http://127.0.0.1:<port>/sse"
```
The port is written to `<data_dir>/_runtime/agent_daemon/port.json` as
`sse_port`. Auto-assign (0) is recommended to avoid port conflicts. When SSE is
enabled, the daemon handles MCP protocol directly — no proxy processes needed.
The stdio proxy path continues to work alongside SSE.

5. Render hook scripts from `plugins/<host>/...` templates only when this release
ships a hook adapter for the target host. On Windows, render absolute paths. If
a `.cmd` contains non-ASCII paths, write it with the system ANSI/GBK encoding
and CRLF line endings.

For generic MCP-only hosts, skip hook rendering. They still get the four MCP
tools; they just do not get automatic startup context/latch injection until a
real host hook adapter exists. Do not render `plugins/mimocode` for non-MiMo hosts.

6. Restart the host after config changes. Some agent hosts keep old MCP or hook
processes alive until the app/process is fully restarted.

## Post-Install: Agent Self-Teaching

After install, in the first agent session, instruct the agent to write the
following Ripple Memory usage rules into its own long-term memory (e.g.
`memory/feedback_ripple_engine_rules.md`). This teaches the agent how to use
Ripple Memory correctly from the first session.

### Content to embed in agent memory

```text
## strength 语义
- strength = importance × decay × access_bonus，是带时间衰减的当前活跃度，不是静态标签。
- 衰减：每天约降 2%；被访问时重置计时器并获得对数级加成。
- score 是语义相关度，strength 是时间活跃度。两者完全不同。
- 判断新旧时看 strength 与 importance 差距，差距越大越需核实是否过期。

## 演化优先级
多条同主题但值不同的记录出现时，按以下优先级：
1. 用户本轮或更晚、更明确的口径
2. truth_projection.current_claims
3. evolution_status=active 的当前口径
4. 结构化长期记忆中的当前事实
5. 回读到的精确证据

## recall 后行为规范
- memoria_recall 是导航，不是真相源。memoria_read 才是。
- vec_sim > 0.5 或 score 最高的结果，必须用 memoria_read 拉原文后再使用。
- 低分/背景结果只是线索，精确原文未读之前不要当作证据。
- rollup、摘要、ContextPacket、topic、capsule、恢复卡、接力卡都是导航，不是完整事实。

## 上下文压缩恢复
上下文被压缩后，立刻做三步：
1. 读 Original Words Latch 恢复任务理解
2. memoria_recall 搜索与当前任务相关的记忆
3. 对高分结果 memoria_read 获取详情，快速重建上下文
```

## Codex

Paths:

```powershell
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE ".codex" }
$Runtime = Join-Path $CodexHome "mcp\ripple-memory-runtime"
$DataDir = Join-Path $CodexHome "mcp-data\ripple-memory"
$SkillTarget = Join-Path $CodexHome "skills\ripple-memory"
$PluginTarget = Join-Path $CodexHome "plugins\ripple-memory-hooks"
$Marketplace = Join-Path $CodexHome "ripple-memory-marketplace"
$MarketplacePlugin = Join-Path $Marketplace "plugins\ripple-memory-hooks"
```

Copy the runtime and skill with the general steps.

Render the Codex plugin:

- Copy `plugins/codex/ripple-memory-hooks` to `$PluginTarget`.
- Replace `{{PYTHON}}`, `{{RUNTIME_SRC}}`, and `{{DATA_DIR}}` in the hook script.
- Replace `{{PLUGIN_TARGET_JSON}}` in `hooks.json.template` with a JSON-escaped `$PluginTarget`.
- Write the rendered file as `$PluginTarget\hooks.json`.
- Copy/render `plugins/codex/marketplace.json.template` into `$Marketplace\marketplace.json`.

Config requirements:

```toml
[features]
plugins = true
hooks = true
plugin_hooks = true
```

Use only one hook source. Prefer the plugin; do not also create
`<CODEX_HOME>\hooks.json` or direct `[hooks]` blocks in `config.toml`.

Codex hook JSON uses `"timeout": 3`. Do not write `timeoutSec`; `hooks/list`
will render `timeoutSec=3` by itself.

Set `MEMORIA_MCP_EXIT_ON_RUNTIME_CHANGE=false` in the Codex MCP server env.
Codex does not reliably hot-reconnect an MCP stdio transport after the server
exits mid-window; restart Codex to load synced runtime code.
Set `MEMORIA_MCP_IDLE_EXIT_SECONDS=36000` so the MCP server waits 10 hours of
idle time before exiting.

After installation, the user must trust the plugin hooks in Codex. Do not claim
live Codex hook integration is complete until `codex hooks/list` shows trusted
hooks and a real `codex exec` smoke test passes.

**→ Restart Codex, then run the Post-Restart Live Verification section below.**

## Claude Code

Paths:

```powershell
$ClaudeHome = if ($env:CLAUDE_HOME) { $env:CLAUDE_HOME } else { Join-Path $env:USERPROFILE ".claude" }
$Runtime = Join-Path $ClaudeHome "mcp\ripple-memory-runtime"
$DataDir = Join-Path $ClaudeHome "mcp-data\ripple-memory"
$SkillTarget = Join-Path $ClaudeHome "skills\ripple-memory"
$HookDir = Join-Path $ClaudeHome "ripple-memory-hooks"
$HookCmd = Join-Path $HookDir "ripple-memory-claude-hook.cmd"
```

Copy the runtime and skill with the general steps.

Render:

- `plugins/claude-code/ripple-memory-hooks/scripts/ripple-memory-claude-hook.cmd`
  to `$HookCmd`.
- `plugins/claude-code/.mcp.json.template` into `$ClaudeHome\.mcp.json`.
  Merge, do not overwrite. Preserve existing MCP servers.
- `plugins/claude-code/settings.json.template` into the existing Claude Code
  settings for hooks. Merge, do not overwrite. Preserve existing model,
  permissions, and other hooks.

Claude Code hook commands should use the exec form from the template:

```json
{
  "command": "cmd.exe",
  "args": ["/d", "/c", "<CLAUDE_HOME>\\ripple-memory-hooks\\ripple-memory-claude-hook.cmd"],
  "timeout": 3
}
```

Do not pass a raw Windows `.cmd` path through Claude Code's default shell. On a
Windows machine with Git Bash, shell hooks can mangle `C:\Users\...` into
`C:Users...` and fail with `command not found`.

The Claude Code MCP server and hook must both point at `$DataDir`, not
`~/.memoria-mcp`.

**→ Restart Claude Code, then run the Post-Restart Live Verification section below.**

## Qwen Code

Paths:

```powershell
$QwenHome = if ($env:QWEN_HOME) { $env:QWEN_HOME } else { Join-Path $env:USERPROFILE ".qwen" }
$Runtime = Join-Path $QwenHome "mcp\ripple-memory-runtime"
$DataDir = Join-Path $QwenHome "mcp-data\ripple-memory"
$SkillTarget = Join-Path $QwenHome "skills\ripple-memory"
$HookCmd = Join-Path $QwenHome "ripple-memory-hook.cmd"
```

Copy the runtime and skill with the general steps.

Render:

- `plugins/qwen-code/ripple-memory-hooks/scripts/ripple-memory-qwen-hook.cmd.template`
  to `$HookCmd`.
- `plugins/qwen-code/settings.json.template` into `$QwenHome\settings.json`.
  Merge, do not overwrite. Preserve existing model, permissions, and MCP servers.

The Qwen Code MCP server and hook must both point at `$DataDir`, not
`~/.memoria-mcp`.

### Qwen Code Post-Install Optimizations

Qwen Code has platform-specific friction that Codex/Claude Code do not:
MCP tools are "deferred" (need `tool_search` before use), and a built-in
"auto memory" system competes with Ripple Memory. After install, apply
the three optimizations documented in `integrations/qwen-code-ripple-memory.md`
(Post-Install Optimizations section) to the **installed copies** under
`<QWEN_HOME>`. Do NOT modify the source `plugins/` templates.

**→ Restart Qwen Code, then run the Post-Restart Live Verification section below.**

## MiMo Code

MiMo Code supports Ripple Memory through both a local MCP server and a MiMo
plugin hook. MCP exposes the four tools; the plugin injects startup/prompt
context and updates the Original Words Latch through
`memoria_mcp.mimocode_hook`.

Only MiMo Code installs should render `plugins/mimocode` and install
`mimocode_hook.py`. Generic MCP hosts must not install the MiMo plug.

Official MiMo Code docs currently describe MCP servers under `mcp` in
`mimocode.jsonc`, with local servers using `"type": "local"`, a command array,
an `environment` object, `enabled`, and an optional millisecond `timeout`.
Local MiMo configs also accept `plugin` entries pointing at plugin directories.

Paths:

```powershell
$MimoHome = if ($env:MIMOCODE_HOME) { $env:MIMOCODE_HOME } else { Join-Path $env:USERPROFILE ".local\share\mimocode" }
$Runtime = Join-Path $MimoHome "mcp\ripple-memory-runtime"
$DataDir = Join-Path $MimoHome "mcp-data\ripple-memory"
$SkillTarget = Join-Path $MimoHome "skills\ripple-memory"
$PluginTarget = Join-Path $MimoHome "mcp\ripple-memory-hooks"
$HookCmd = Join-Path $PluginTarget "scripts\ripple-memory-mimocode-hook.cmd"
$MimoConfig = if ($env:MIMOCODE_CONFIG) { $env:MIMOCODE_CONFIG } else { Join-Path $env:USERPROFILE ".config\mimocode\mimocode.jsonc" }
```

Copy the runtime and skill with the general steps.

Render the MiMo plugin:

- Copy `plugins/mimocode/ripple-memory-hooks` to `$PluginTarget`.
- Replace `{{PYTHON}}`, `{{RUNTIME_SRC}}`, and `{{DATA_DIR}}` in
  `scripts\ripple-memory-mimocode-hook.cmd.template`.
- Write the rendered file as `$HookCmd`.
- Keep `index.ts` and `package.json` beside the `scripts` directory.

Merge this block into the target MiMo `mimocode.jsonc`:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "ripple-memory": {
      "type": "local",
      "command": [
        "<absolute-python>",
        "-m",
        "memoria_mcp.server"
      ],
      "enabled": true,
      "timeout": 30000,
      "environment": {
        "PYTHONPATH": "<runtime>\\src",
        "MEMORIA_MCP_DATA_DIR": "<host data dir>",
        "MEMORIA_MCP_ENABLE_SEMANTIC": "true",
        "MEMORIA_MCP_EMBEDDING_MODEL": "<runtime>\\models\\paraphrase-multilingual-MiniLM-L12-v2",
        "MEMORIA_MCP_PRELOAD_EMBEDDING": "true",
        "MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE": "live",
        "MEMORIA_MCP_EXIT_ON_RUNTIME_CHANGE": "false",
        "MEMORIA_MCP_IDLE_EXIT_SECONDS": "36000",
        "RIPPLE_MEMORY_HOST": "mimocode"
      }
    }
  },
  "plugin": [
    "<plugin target>"
  ],
  "skills": {
    "paths": [
      "<host skill>"
    ]
  }
}
```

If the MiMo config already has `mcp`, `plugin`, or `skills`, merge instead of
overwriting. Preserve existing plugin entries and append the Ripple plugin path.

Verify before restart:

```powershell
$env:PYTHONPATH = "<runtime>\src"
python -m memoria_mcp.install_check `
  --host mimocode `
  --data-dir "<host data dir>" `
  --skill-path "<host skill>\SKILL.md" `
  --hook-cmd "<plugin target>\scripts\ripple-memory-mimocode-hook.cmd" `
  --require-hook-cmd `
  --pretty
```

After restarting MiMo, ask it to list available tools and confirm
`ripple-memory_memoria_remember`, `ripple-memory_memoria_recall`,
`ripple-memory_memoria_read`, and `ripple-memory_memoria_forget` or equivalent
MCP-prefixed tool names are visible. Start a fresh session and confirm the MiMo
plugin injects `[Ripple Memory Context]`; if no context appears, the `plugin`
entry or rendered hook command path is wrong.

## Generic MCP Agent

Use this path for any host that supports local stdio MCP servers but does not
have a dedicated Ripple hook adapter.

Treat this as a starting point, not permission to stop early. First look for a
host plugin, message-transform hook, pre-prompt hook, command hook, middleware
API, or launcher wrapper that can insert `context_cli` output. If one is safe
and testable, create a host-specific adapter and keep it out of the generic
files. Only stay MCP-only when no practical host hook surface exists.

Minimum host requirements:

- A way to configure a local MCP server command.
- A way to pass environment variables to that MCP server.
- A place to store a host-owned runtime copy and data directory.
- Optional: an instruction/skill mechanism so the agent learns when to call the
  four Ripple tools.

Configure the MCP server with the same command and environment from the general
steps:

```text
command: <absolute-python> -m memoria_mcp.server
env:
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

If the host has no hook system, do not invent one. The agent can still use
`memoria_recall` at the start of a task and `memoria_remember` for durable
facts. For manual context injection, run:

```powershell
$env:PYTHONPATH = "<runtime>\src"
$env:MEMORIA_MCP_DATA_DIR = "<host data dir>"
python -m memoria_mcp.context_cli "current task or prompt"
```

Verify:

```powershell
$env:PYTHONPATH = "<runtime>\src"
python -m memoria_mcp.install_check `
  --host generic `
  --data-dir "<host data dir>" `
  --skill-path "<host skill>\SKILL.md" `
  --pretty
```

Post-restart acceptance for a generic host:

- Confirm the four Ripple MCP tools are visible from inside the host session.
- Call a Ripple tool and check `<data-dir>\_runtime\agent_daemon\port.json`.
- Store, recall, read, and forget a temporary memory from inside the host when
  the host exposes MCP tool calling.
- For MCP-only hosts, run `python -m memoria_mcp.context_cli` and paste the
  context into the first prompt as the manual fallback.
- For any newly created host adapter, rerun `install_check` with
  `--hook-cmd` and `--require-hook-cmd`, then run the post-restart hook/latch
  checks below.

## Post-Install Functional Check (sandbox, before restart)

Run from the installed runtime after every host install, BEFORE restarting the
host. This is a sandbox test that creates temp dirs and patches env vars — it
proves the code is correct but does NOT verify the live host connection.

```powershell
$env:PYTHONPATH = "<host runtime>\src"
python -m memoria_mcp.install_check `
  --host <codex|claude|qwen|mimocode|generic> `
  --data-dir "<host data dir>" `
  --skill-path "<host skill>\SKILL.md" `
  --hook-cmd "<absolute hook command>" `
  --require-hook-cmd `
  --pretty
```

Add `--require-semantic --require-local-model` when the local embedding model is
expected to be present. After the host has been fully restarted and a host
window is open, add `--require-host-mcp-process` to prove the host itself owns a
live Ripple MCP process. Add `--codex-live` only after Codex hooks are trusted.

The check verifies:

- installed skill guidance and Chinese/English trigger categories
- the four MCP tools
- real MCP stdio protocol calls
- optional live host-owned MCP process check
- project isolation
- same-project memory visibility across windows
- window-local latch separation and 10-turn pruning
- `SessionStart`, `UserPromptSubmit`, and `Stop` hook behavior
- hook kill switches and hook command JSON output
- SQLite runtime truth tables
- absence of obsolete SQL `memory_stream` / `archive_blocks`
- JSONL frozen-content rail
- memory evolution state/edges, default filtering, `include_evolution=true`, and `memoria_read` historical labels
- Dreamer archive/cleanup behavior
- restart safety: old superseded claims do not resurrect after search-index rebuild
- soft-timeout recovery for stuck MCP response paths
- per-project durable write queue: queued `memoria_remember` returns quickly under writer contention, drains later, and does not block recall/read
- recall quality/read-only boundary: `memoria_recall` and `memoria_read` do not run write-side maintenance, code-name queries filter weak old口径, and search-index rebuild clears stale `index_dirty`
- runtime refresh safety: host configs keep live stdio transports open; restart the host to load synced runtime code

## Post-Restart Live Verification (MANDATORY)

The install_check above is a **sandbox test** — it creates temp dirs, patches
env vars, and runs MCP via subprocess. It proves the code is correct, but does
NOT prove the host actually connects to MCP, fires hooks, or writes the latch.

After restarting the host, the installing agent MUST run this sequence:

### Step 1: Verify MCP tools are visible

Ask the host agent to list its available tools. Confirm `memoria_remember`,
`memoria_recall`, `memoria_read`, `memoria_forget` appear. If they don't, the
MCP config path is wrong or the host didn't reload config on restart.

### Step 2: Verify daemon is running

```powershell
$portFile = "<data-dir>\_runtime\agent_daemon\port.json"
if (Test-Path $portFile) {
  Get-Content $portFile | ConvertFrom-Json | Format-List
  # Verify: pid is alive, port is nonzero
} else {
  Write-Output "FAIL: daemon port.json not found"
}
```

### Step 3: Verify hooks inject context

Ask the host agent to start a new session. The hook should inject a
`<ripple_memory_context>` block containing the project name, current time,
and latch content. If no context appears, the hook plugin/config is not loaded.

### Step 4: Verify Original Words Latch

After the host agent has been running for a few turns, check:

```powershell
$latchDir = "<data-dir>\_window_state\<project>\<window-id>"
$latchFile = Join-Path $latchDir "original-word-latch.md"
if (Test-Path $latchFile) {
  Get-Content $latchFile | Select-String "Task:" "Goal:" "Task State"
} else {
  Write-Output "FAIL: latch file not created"
}
```

### Step 5: Run live smoke test (optional but recommended)

```powershell
$env:PYTHONPATH = "<host runtime>\src"
python -m memoria_mcp.install_check `
  --host <host> `
  --data-dir "<host data dir>" `
  --live-smoke --pretty
```

This connects to the real running daemon, runs remember/recall/read/forget on
the real data dir, verifies the hook produces context, and checks the latch
file — all without creating temp directories.

### Why every agent skips this

The root cause: `install_check.py` is a sandbox test. Every test function
creates temporary data directories, patches environment variables, and runs MCP
via subprocess. It proves the **code logic** is correct, but:

1. It does NOT verify the host's **real config** points to the right paths
2. It does NOT verify the host can **actually start and connect** to MCP
3. It does NOT verify hooks fire in the host's **real hook system**
4. It does NOT verify the latch is written during **real sessions**

The sandbox tests pass → the installing agent declares success → nobody checks
the live system → the user discovers MCP tools are missing or hooks don't fire.

**Always do the post-restart live verification above.**

## Hot Unplug

Disable hooks without removing config:

```powershell
$env:RIPPLE_MEMORY_HOOK_ENABLED = "0"
```

Or create a project-local kill switch:

```powershell
New-Item -ItemType File -Path ".ripple-memory\hooks.disabled" -Force
```
