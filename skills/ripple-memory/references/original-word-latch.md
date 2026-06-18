# Original Words Latch

The Original Words Latch is a window-local anti-compression note. It preserves
the user's current task intent and the agent's current interpretation so a
compressed or resumed session can restart without inventing context.

It is not long-term memory or the main progress record. After context
compression, the conversation summary/checkpoint is the progress anchor; the
latch preserves original intent, boundaries, and task-relevant user wording
that summaries may flatten.

## Project vs Window

Ripple Memory has two layers:

- `project`: shared durable memory for one repository/product inside the same host data directory.
- `window_id`: current task/window identity. Each active task window has its own latch.

Never use one shared latch file for different active windows. That causes task
mixing after compression.

Default latch path:

```text
<data-dir>/_window_state/<project>/<window-id>/original-word-latch.md
```

Launcher sessions should expose:

```text
RIPPLE_MEMORY_PROJECT=<project>
RIPPLE_MEMORY_WINDOW_ID=<window-id>
```

Read and update only the current window's latch. Other latch files under the
same project are other windows and must stay labeled as other-window context.

## When to Maintain It

Use this latch at the start of a task, after context compression, after a
"continue" request, and whenever the user changes constraints, priority,
acceptance criteria, correction direction, or gives task-relevant mid-task
guidance.

If the current window has no latch, create one before substantial work. If you
cannot write it, say where the active restart note is being kept.

Do not store secrets.

## What to Store

Store only task-relevant material:

- User original words: exact or near-exact wording that affects what should be done.
- Recent user turns: task-relevant history and mid-task guidance, not the main progress anchor.
- Agent task understanding: what you believe the user wants now.
- Window identity: project, window id, role/task line.
- Boundaries: what not to touch, what must be preserved, what source is authoritative.
- Acceptance check: how you will know this task is done.
- Current next action: the next concrete move after resume.

Do not store filler, raw logs, huge pasted documents, or irrelevant chat.

## Ten-Turn Retention

Keep at most the latest 10 task-relevant user turns for this window. When the
latch grows beyond 10 turns:

1. Drop old resolved turns that no longer affect execution.
2. Keep any active hard rule, boundary, or acceptance criterion.
3. Preserve the latest user correction over older summaries.
4. Keep the task interpretation short and current.

## Required Template

```markdown
# Original Words Latch

Updated: <YYYY-MM-DD HH:mm local>
Project: <stable project name>
Window ID: <stable window/task id>
Task: <short task name>

## User Original Words

1. <latest relevant user wording>
2. <previous relevant user wording>

## Agent Task Understanding

- Goal: <what the user wants now>
- Scope: <files/systems/areas involved>
- Boundaries: <what not to change or assume>
- Acceptance: <what counts as done>
- Next action: <specific next step>

## Other Windows Note

- Shared project memory may contain facts from other windows.
- Other window latch files are context only, not this window's task state.

## Compression Restart Checklist

- Use the conversation summary/checkpoint as the progress anchor.
- Confirm `Project` and `Window ID`.
- Read this window's latch.
- Treat latch original words and recent turns as intent/process context, not as a restart command.
- Recall durable project memory with `memoria_recall`.
- Read current code/files/logs needed for truth.
- If anything conflicts, the latest user message wins.
```

## Relationship to Durable Memory

Use `memoria_remember` only when the latch reveals something durable, such as a
stable user preference, project rule, architecture decision, or repeated
failure fix.

Do not dump the whole latch into long-term memory. Compress durable lessons
into one short memory.
