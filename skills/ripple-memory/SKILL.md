---
name: ripple-memory
description: "Durable MCP project memory. Use before each phase/step, repo start, compression recovery, prior-work recall, or lasting rules/decisions/fixes. Cues: 继续/上次/记得吗/按原方案/别忘/上下文没了."
---

# Ripple Memory Skill

Ripple Memory is long-term project memory for coding agents. It helps with
continuity, but it does not replace reading current files, tests, logs, or the
latest user message.

For anti-compression work, also maintain the Original Words Latch. Read
`references/original-word-latch.md` when starting a task, after context
compression, or when the user says to continue from prior context.

## Task Analysis

Before using memory, decide what the user is really asking for:

- Recall: the user references earlier work, asks whether you remember something,
  resumes a task, or asks you to continue after context loss.
- Read: a recalled memory is vague, truncated, contested, or will be used as
  evidence for a code, architecture, install, or debugging decision.
- Remember: the user gives a durable rule, correction, preference, architecture
  decision, setup fact, or repeated failure fix.
- Forget: the user explicitly asks to delete a memory.
- Latch: the current task, acceptance criteria, or agent understanding changed
  and should survive context compression inside this window.

Memory is navigation, not proof. After context compression, Codex/host recovery
and the conversation summary are the progress anchor; the latch preserves
original intent, boundaries, and task-relevant user wording that summaries may
flatten.
During long tasks, keep visible checkpoints clear enough that latch recovery can
connect the next step without turning old original words into a fresh start.

## Default Rhythm

At the start of a new task or after context compression:

1. Infer the stable `project` name from the repository or user-provided project name.
2. Infer the stable `window_id` / task identity. Prefer `RIPPLE_MEMORY_WINDOW_ID` when present.
3. Read or refresh this window's Original Words Latch as intent/boundary context, not as the main progress checkpoint when a newer summary exists.
4. Call `memoria_recall` with a concise query built from the user's request, current repo/module, latch summary, and visible failure or goal.
5. Treat recall results as navigation. Use `memoria_read` when exact evidence matters.

## Recall Triggers

Call `memoria_recall` when the user references prior context, even without an
exact keyword. Treat these as semantic trigger categories:

- Continuation / resume: "继续", "接着", "接着来", "往下做", "继续上次", "恢复上下文", "从断点继续", "接上".
- Prior work / earlier turns: "上次", "之前", "刚才", "前面", "前一次", "上一轮", "刚刚说的", "前文".
- Memory questions: "你记得吗", "还记得吗", "记不记得", "你应该记得", "我之前说过", "我们不是说过".
- Existing plan / decision reuse: "按原方案", "按刚才的方案", "照之前的", "沿用那个方案", "不要重新来", "别改口径".
- Corrections / preferences / avoid repeat: "别又忘了", "别再忘", "不要再", "上次踩过坑", "之前错过", "记住这个", "以后都这样".
- Compression / lost context: "上下文没了", "压缩后", "断片了", "你丢上下文了", "接回记忆".

English equivalents such as "continue", "last time", "previously",
"do you remember", "use the prior plan", "don't forget again", and
"we already decided" should also trigger `memoria_recall`.

## Tool Rules

- Use only the four normal MCP tools: `memoria_remember`, `memoria_recall`, `memoria_read`, and `memoria_forget`.
- Call `memoria_read` after recall when the memory is truncated, contested, needed as exact evidence, or used to justify code/architecture changes.
- Call `memoria_remember` only for durable items: user/project rules, architecture decisions, setup quirks, repeated failure fixes, and important preferences.
- When a new memory clearly replaces an old recalled口径, use `memoria_remember` with `fact_key` and `supersedes_ref_ids`. Do not invent a new tool.
- Default `memoria_recall` returns current口径. Use `include_evolution=true` only for audits or when explaining what changed.
- Use `evolution_status="pending_conflict"` only when old and new口径 are both visible but not safely resolved.
- If recalled memory conflicts with the latest user message, the latest user message wins. Remember the correction when it is durable.
- Use `memoria_forget` only when the user explicitly asks to delete memory. Treat it as user-visible deletion, not forensic secure erase.

## Recall Discipline

- For Chinese projects or Chinese user questions, build recall queries with Chinese terms plus English technical aliases, fact_key words, and module names.
- Treat `memoria_recall` results as structured index cards that save context, not as the truth source. Original memory text from `memoria_read` is the source of truth.
- High-score recall results must be read before being used as evidence or as the basis for an architecture, debugging, installation, or project-history conclusion. This does not mean lower-score results never need reading.
- Always call `memoria_read` when a result is truncated, contested, has `read_hint`, has JSONL source pointers such as `json_file`/`json_offset`, or will affect a decision.
- Use score, coverage, overlap, fact_key, importance, and strength to choose what to read first. Weak or background results are clues only until exact text is read.

## Project, Window, Storage

- Project memory is durable and shared by windows that use the same `project` inside the same host data directory.
- Original Words Latch is window-local current-task state. Do not treat another window's latch as the current task.
- Installed hosts should use their own data roots, for example:
  - Codex: `<CODEX_HOME>/mcp-data/ripple-memory`
  - Claude Code: `<CLAUDE_HOME>/mcp-data/ripple-memory`
  - Qwen Code: `<QWEN_HOME>/mcp-data/ripple-memory`
- Do not rely on the old global `~/.memoria-mcp` layout.
- SQLite stores runtime truth: graph state, search metadata, and memory evolution state/edges.
- JSONL/archive files store frozen full-content history and Dreamer archive blocks.
- `memoria_remember` may return `commit_state="queued"` when another MCP process is already writing the same project. Treat that as durably accepted but not searchable until a later committed/drained result.
- `memoria_recall` is navigation, not proof. Prefer active/current tracked claims over untracked legacy notes when both appear, and use `memoria_read` for exact evidence before acting.

## Window Identity

Recommended launcher form:

```bash
ripple-memory-run --agent codex --project <project> --window-id <window-or-task-id> "<task>"
```

The launcher sets:

```text
RIPPLE_MEMORY_PROJECT=<project>
RIPPLE_MEMORY_WINDOW_ID=<window-or-task-id>
```

Default latch path:

```text
<data-dir>/_window_state/<project>/<window-id>/original-word-latch.md
```

Only use `RIPPLE_MEMORY_LATCH_FILE` when a host explicitly overrides the latch
path. Other same-project latch files may be read for awareness, but they must
remain clearly labeled as other windows.

## Memory Quality

Good memory:

```text
Project uses X because Y; when touching Z, run command A and avoid B.
```

Bad memory:

```text
User asked about the project today.
```

Use `importance` around:

- `0.9`: user rule, architecture decision, repeated failure fix.
- `0.7`: project convention or important setup detail.
- `0.5`: useful but ordinary fact.

Use memory `type`:

- `rule` for user/project rules.
- `arch_decision` for architecture choices.
- `debug_insight` for root causes and fixes.
- `code_pattern` for reusable implementation patterns.
- `preference` for user preferences.
- `fact` for stable facts.

Before finishing a meaningful task, write one short durable memory only if the
task changed project understanding. If nothing durable was learned, do not
write.
