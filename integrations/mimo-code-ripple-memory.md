# MiMo Code + Ripple Memory

MiMo Code supports Ripple Memory through both a local MCP server and a MiMo
plugin hook. MCP exposes the four memory tools; the MiMo plugin injects startup
and prompt context and refreshes the Original Words Latch.

Only render the MiMo-specific files for MiMo Code installs. Generic MCP hosts
must not install `plugins/mimocode` or `memoria_mcp.mimocode_hook`.

MiMo config is loaded from `mimocode.jsonc` / `mimocode.json` under the MiMo
config root, or from paths selected by `MIMOCODE_HOME`, `MIMOCODE_CONFIG`, and
`MIMOCODE_CONFIG_DIR`. Preserve existing `mcp`, `plugin`, and `skills` entries.

## Runtime Shape

```text
MiMo Code MCP server
+ ripple-memory skill as instruction source
+ MiMo plugin hook adapter
```

Recommended paths:

```text
<MIMOCODE_HOME or host root>/mcp/ripple-memory-runtime
<MIMOCODE_HOME or host root>/mcp-data/ripple-memory
<MIMOCODE_HOME or host root>/skills/ripple-memory
<MIMOCODE_HOME or host root>/mcp/ripple-memory-hooks
```

For the default Windows MiMo profile, a practical host root is:

```text
%USERPROFILE%\.local\share\mimocode
```

## Install

```powershell
$SourceRoot = "<ripple-memory source root>"
$Python = (Get-Command python).Source
$MimoHome = if ($env:MIMOCODE_HOME) { $env:MIMOCODE_HOME } else { Join-Path $env:USERPROFILE ".local\share\mimocode" }
$Runtime = Join-Path $MimoHome "mcp\ripple-memory-runtime"
$DataDir = Join-Path $MimoHome "mcp-data\ripple-memory"
$SkillTarget = Join-Path $MimoHome "skills\ripple-memory"
$PluginTarget = Join-Path $MimoHome "mcp\ripple-memory-hooks"
$HookCmd = Join-Path $PluginTarget "scripts\ripple-memory-mimocode-hook.cmd"
$MimoConfig = if ($env:MIMOCODE_CONFIG) { $env:MIMOCODE_CONFIG } else { Join-Path $env:USERPROFILE ".config\mimocode\mimocode.jsonc" }
```

Copy the runtime, skill, and MiMo plugin template:

```powershell
New-Item -ItemType Directory -Force -Path $Runtime,$DataDir,$SkillTarget,$PluginTarget | Out-Null
Copy-Item -LiteralPath (Join-Path $SourceRoot "src") -Destination $Runtime -Recurse -Force
Copy-Item -LiteralPath (Join-Path $SourceRoot "pyproject.toml") -Destination $Runtime -Force
if (Test-Path (Join-Path $SourceRoot "models")) {
  Copy-Item -LiteralPath (Join-Path $SourceRoot "models") -Destination $Runtime -Recurse -Force
}
python -m pip install -e $Runtime
Copy-Item -LiteralPath (Join-Path $SourceRoot "skills\ripple-memory\*") -Destination $SkillTarget -Recurse -Force
Copy-Item -LiteralPath (Join-Path $SourceRoot "plugins\mimocode\ripple-memory-hooks\*") -Destination $PluginTarget -Recurse -Force
```

Render `scripts\ripple-memory-mimocode-hook.cmd.template` to
`scripts\ripple-memory-mimocode-hook.cmd`:

- `{{PYTHON}}` -> `$Python`
- `{{RUNTIME_SRC}}` -> `$Runtime\src`
- `{{DATA_DIR}}` -> `$DataDir`

On Windows, write the rendered `.cmd` with CRLF line endings. If paths contain
Chinese or other non-ASCII characters, write it with the system ANSI/GBK
encoding.

## MiMo Config

Merge this into the MiMo config file. Do not overwrite existing MCP servers,
plugins, model settings, permissions, or skills.

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "ripple-memory": {
      "type": "local",
      "command": ["<absolute-python>", "-m", "memoria_mcp.server"],
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

If MiMo displays MCP tools with a server-name prefix, expect names such as
`ripple-memory_memoria_recall`.

## Verify

Run the sandbox check before restarting MiMo:

```powershell
$env:PYTHONPATH = "$Runtime\src"
python -m memoria_mcp.install_check `
  --host mimocode `
  --data-dir "$DataDir" `
  --skill-path "$SkillTarget\SKILL.md" `
  --hook-cmd "$HookCmd" `
  --require-hook-cmd `
  --pretty
```

Restart MiMo after config changes, then verify from a real MiMo session:

- The four Ripple MCP tools are visible.
- A new session receives `[Ripple Memory Context]`.
- The latch appears under `<host data dir>\_window_state\<project>\<window-id>\original-word-latch.md`.

For a stronger installed-host check, keep MiMo open after restart and rerun:

```powershell
$env:PYTHONPATH = "$Runtime\src"
python -m memoria_mcp.install_check `
  --host mimocode `
  --data-dir "$DataDir" `
  --skill-path "$SkillTarget\SKILL.md" `
  --hook-cmd "$HookCmd" `
  --require-hook-cmd `
  --require-host-mcp-process `
  --pretty
```

Use `--live-smoke` only after the real MiMo session has started Ripple's MCP
daemon. The sandbox check proves the package and hook command work; the
post-restart checks prove MiMo actually loaded the config and plugin.

## References

- https://mimo.xiaomi.com/mimocode/tools
- https://mimo.xiaomi.com/mimocode/configuration
- https://mimo.xiaomi.com/mimocode/config-overrides
