# Ripple Memory - Qwen Code Plug

Qwen Code integration uses:

```text
ripple-memory MCP server
+ ripple-memory skill
+ Qwen hook adapter
```

The hook adapter injects memory context and updates the Original Words Latch.
The MCP server exposes the four tools: `memoria_remember`, `memoria_recall`,
`memoria_read`, and `memoria_forget`.

## Paths

```powershell
$SourceRoot = "<ripple-memory repo root>"
$Python = (Get-Command python).Source
$QwenHome = if ($env:QWEN_HOME) { $env:QWEN_HOME } else { Join-Path $env:USERPROFILE ".qwen" }
$Runtime = Join-Path $QwenHome "mcp\ripple-memory-runtime"
$DataDir = Join-Path $QwenHome "mcp-data\ripple-memory"
$SkillTarget = Join-Path $QwenHome "skills\ripple-memory"
$HookCmd = Join-Path $QwenHome "ripple-memory-hook.cmd"
```

Do not use the old global `~/.memoria-mcp` data directory.

## Install

```powershell
New-Item -ItemType Directory -Force -Path $Runtime,$DataDir,$SkillTarget | Out-Null
Copy-Item -LiteralPath (Join-Path $SourceRoot "src") -Destination $Runtime -Recurse -Force
Copy-Item -LiteralPath (Join-Path $SourceRoot "pyproject.toml") -Destination $Runtime -Force
if (Test-Path (Join-Path $SourceRoot "models")) {
  Copy-Item -LiteralPath (Join-Path $SourceRoot "models") -Destination $Runtime -Recurse -Force
}
# Optional: install dependencies/entry points. Qwen still loads this runtime
# through PYTHONPATH in `settings.json`.
python -m pip install -e $Runtime
Copy-Item -LiteralPath (Join-Path $SourceRoot "skills\ripple-memory\*") -Destination $SkillTarget -Recurse -Force
```

Render `ripple-memory-hooks/scripts/ripple-memory-qwen-hook.cmd.template` to
`$HookCmd`:

- `{{PYTHON}}` -> `$Python`
- `{{RUNTIME_SRC}}` -> `$Runtime\src`
- `{{DATA_DIR}}` -> `$DataDir`

On Windows, write the `.cmd` with CRLF line endings. If paths contain Chinese or
other non-ASCII characters, write it with the system ANSI/GBK encoding.

Merge `settings.json.template` into `$QwenHome\settings.json`. Preserve existing
model, permissions, and MCP servers. Replace:

- `{{PYTHON}}`
- `{{RUNTIME_SRC}}`
- `{{DATA_DIR}}`
- `{{HOOK_CMD_PATH}}`

The template sets `MEMORIA_MCP_EXIT_ON_RUNTIME_CHANGE=false` for Qwen Code.
Qwen runs inside VS Code and may surface an MCP process exit as an interrupted
agent turn, so runtime updates should take effect on the next Qwen restart
instead of killing the current MCP server mid-session.

## Verify

```powershell
$env:PYTHONPATH = "$Runtime\src"
python -m memoria_mcp.install_check `
  --host qwen `
  --data-dir "$DataDir" `
  --skill-path "$SkillTarget\SKILL.md" `
  --hook-cmd "$HookCmd" `
  --require-hook-cmd `
  --pretty
```

Restart Qwen Code after config changes. After restart, keep a Qwen Code window
open and rerun the same check with `--require-host-mcp-process`; this proves the
live Qwen host owns a Ripple MCP process. The normal `mcp_stdio_protocol` check
starts a temporary server and is not enough to prove host integration.

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

## Hot Unplug

```powershell
$env:RIPPLE_MEMORY_HOOK_ENABLED = "0"
```

Or create `.ripple-memory\hooks.disabled` in the project workspace.
