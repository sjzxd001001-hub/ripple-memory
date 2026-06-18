# Ripple Memory - MiMo Code Plug

MiMo Code integration uses:

```text
ripple-memory MCP server
+ ripple-memory skill
+ MiMo Code plugin hook adapter
```

The MCP server exposes the four tools: `memoria_remember`, `memoria_recall`,
`memoria_read`, and `memoria_forget`. The MiMo plugin injects startup/prompt
context and refreshes the Original Words Latch through
`memoria_mcp.mimocode_hook`.

## Paths

```powershell
$SourceRoot = "<ripple-memory repo root>"
$Python = (Get-Command python).Source
$MimoHome = if ($env:MIMOCODE_HOME) { $env:MIMOCODE_HOME } else { Join-Path $env:USERPROFILE ".local\share\mimocode" }
$Runtime = Join-Path $MimoHome "mcp\ripple-memory-runtime"
$DataDir = Join-Path $MimoHome "mcp-data\ripple-memory"
$SkillTarget = Join-Path $MimoHome "skills\ripple-memory"
$PluginTarget = Join-Path $MimoHome "mcp\ripple-memory-hooks"
$HookCmd = Join-Path $PluginTarget "scripts\ripple-memory-mimocode-hook.cmd"
$MimoConfig = if ($env:MIMOCODE_CONFIG) { $env:MIMOCODE_CONFIG } else { Join-Path $env:USERPROFILE ".config\mimocode\mimocode.jsonc" }
```

Do not use the old global `~/.memoria-mcp` data directory.

## Install

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

Render `ripple-memory-hooks/scripts/ripple-memory-mimocode-hook.cmd.template`
to `$HookCmd`:

- `{{PYTHON}}` -> `$Python`
- `{{RUNTIME_SRC}}` -> `$Runtime\src`
- `{{DATA_DIR}}` -> `$DataDir`

On Windows, write the `.cmd` with CRLF line endings. If paths contain Chinese or
other non-ASCII characters, write it with the system ANSI/GBK encoding.

Merge these entries into `mimocode.jsonc` without removing existing servers or
plugins:

```jsonc
{
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
  ]
}
```

## Verify

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

Restart MiMo Code after config changes. After restart, confirm the four MCP
tools are visible and that a fresh session receives Ripple Memory context.

## Hot Unplug

```powershell
$env:RIPPLE_MEMORY_HOOK_ENABLED = "0"
```

Or create `.ripple-memory\hooks.disabled` in the project workspace.
