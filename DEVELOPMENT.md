# Ripple Memory Development Notes

Purpose: this file is for maintainers and maintenance agents. It is not the
user install entry point and it is not the release audit log. Installers should
start with `INSTALL_FOR_LLM.md`; release evidence belongs in
`RELEASE_AUDIT.md`.

Before changing the engine, adapters, tests, or install templates, read this
file and run:

```powershell
$env:PYTHONPATH = "<release-dir>\src"
python tests\design_contract_check.py
```

After a change, run the tests that cover the touched layer. Do not treat
"the code imports" as proof that the package works.

## Product Boundary

Ripple Memory is an external memory suite for coding agents and other agent
hosts. It has four layers:

```text
ripple-memory MCP tools
+ ripple-memory skill
+ ripple-memory launcher/context helper
+ thin host hook adapters
```

It is not a broad knowledge-base platform and it does not replace current
source files, logs, tests, or the latest user message. Its job is to help
agents keep important project context across compression, windows, and
sessions.

## Directory Responsibilities

```text
src/memoria_mcp/              core engine, MCP proxy, daemon, hook core, storage
skills/ripple-memory/         teaches LLMs when and how to use memory
plugins/                      Codex / Claude Code / Qwen Code / MiMo Code templates
integrations/                 host-specific install notes and Generic MCP guide
tools/download_embedding_model.py
tests/                        design contracts, core checks, post-install checks
INSTALL_FOR_LLM.md            main install card for AI installers
DEVELOPMENT.md                maintainer notes
RELEASE_AUDIT.md              release contents and verification evidence
```

The release package must not contain user memories, SQLite databases, JSONL
archives, window latches, local caches, compiled bytecode, or model weights.

## End-to-End Flow

Normal MCP tool calls follow this path:

```text
Agent host
  -> stdio MCP proxy: python -m memoria_mcp.server
  -> agent daemon: memoria_mcp.agent_daemon
  -> MemoriaRouter
  -> per-project RippleMemoryServer
  -> SQLite + JSONL/archive + search index
```

Key boundaries:

- The stdio MCP proxy exposes tools to the host and forwards calls to the
  daemon.
- The agent daemon owns runtime state for one host data directory.
- `MemoriaRouter` routes calls by project.
- The per-project `RippleMemoryServer` owns graph state, search, the write
  queue, storage, and memory evolution.

Do not move memory business logic back into the stdio proxy or hook adapters.
That would recreate window-local state, multi-window races, repeated cold
starts, and stale graph overwrite risks.

## MCP Tools

Only four normal MCP tools are exposed by default:

```text
memoria_remember
memoria_recall
memoria_read
memoria_forget
```

### memoria_remember

Use this tool for durable project memory:

- user rules
- project rules
- architecture decisions
- repeated failure causes and fixes
- stable preferences
- important code patterns

Do not use it for ordinary chat logs, large raw logs, unconfirmed guesses, or
short-lived state that can be read directly from current files.

Write semantics:

- Writes go through the durable per-project write queue by default.
- The MCP request durably enqueues work before returning.
- `commit_state="queued"` means the write has been accepted but may not be
  searchable until the project writer drains the queue.
- Do not bypass the write queue with direct multi-process database writes.

Memory evolution:

- When a new claim replaces an old claim, use `fact_key` and
  `supersedes_ref_ids`.
- Superseded claims are hidden from default recall.
- Use `include_evolution=true` for audits and history.
- Use `pending_conflict` when the correct claim is not yet clear.

### memoria_recall

Recall returns structured navigation cards, not final truth. Common fields
include `ref_id`, `description`, `score`, `importance`, `strength`,
`fact_key`, `read_hint`, and JSONL pointers.

Rules:

- Treat recall as navigation.
- Read high-impact or high-score results with `memoria_read` before using them
  as evidence.
- Low-score results may still need reading if they affect architecture,
  install, or debugging decisions.
- For Chinese projects, query with Chinese terms plus English technical aliases,
  module names, and fact-key words.

### memoria_read

Use this tool to expand a recalled memory into exact text. It is the evidence
path for project-history claims, architecture decisions, install decisions, and
debugging conclusions.

### memoria_forget

Use this only when the user explicitly asks to delete memory. It is
user-visible deletion from active graph/search/readable JSONL records, not a
forensic secure-erase guarantee.

## Storage Model

Ripple uses two storage rails:

```text
SQLite memoria.db
  -> runtime truth
  -> graph_state
  -> search_index
  -> memory_evolution_state
  -> memory_evolution_edges

JSONL / archive
  -> frozen full-content history
  -> memory stream
  -> snapshots
  -> Dreamer compacted archive blocks
```

SQLite owns current runtime state and retrieval metadata. The old SQL payload
tables must not return:

```text
memory_stream
archive_blocks
```

JSONL/archive stores frozen full content and long-term archives. Recall usually
returns compact cards; exact evidence should be loaded through `memoria_read`.

## Retrieval Model

Retrieval is a combined path:

```text
query
  -> token/BM25/search_index candidates
  -> current/evolution/default filtering
  -> optional MiniLM semantic rerank
  -> weak-match filtering
  -> ranked recall cards
```

Principles:

- BM25/search_index provides fast keyword anchoring.
- MiniLM provides semantic reranking without becoming a repeated cold-start
  tax.
- Current active claims outrank superseded historical claims.
- Weak one-token matches and stale background should be suppressed.
- The skill must teach LLMs to read exact memory text when evidence matters.

Do not add a second complex intelligence layer just to look smarter. Prefer
better query expansion, index fields, rerank thresholds, weak-match filters,
and skill discipline.

## Hooks and Original Words Latch

Hooks are used to:

- inject relevant project memory and the current window latch at session start
- refresh the latch and recall related memory when the user submits a prompt
- record task state at stop/compression boundaries

Standard hook path:

```text
host native hook event
  -> thin adapter
  -> RippleHookEvent
  -> hook_core.handle_hook_event
  -> host-compatible hook JSON
```

Adapters translate events only. They must not own memory business logic.

The Original Words Latch is window-local task state, not durable project
memory. It keeps:

- important user wording for the current task
- the agent's task understanding
- boundaries
- acceptance criteria
- task state
- last outcome
- next action

Task states matter:

- `active`: resume from the next step; do not redo completed work.
- `completed`: the task is done unless the user reopens it.
- `blocked` / `awaiting_user`: the agent is waiting or stuck.

The latch has a burst gate to stop long sub-agent prompts from overwriting the
main window's original words.

## Agent Daemon Lifecycle

The current architecture is agent-level, not window-level. The daemon:

- owns `MemoriaRouter` and project caches
- caches/preloads the embedding model
- serializes MCP tool execution
- starts and manages the search daemon
- writes `port.json` for proxy reuse
- enforces a singleton per host data directory

Lifecycle rules:

- A live agent may keep the daemon idle for a long time.
- After the owning agent exits, the daemon exits after a grace period.
- A later MCP call should restart the daemon after a crash.
- Response timeouts must not delete a live daemon's `port.json`.
- Do not restore the old read-only one-shot worker path.

## Model Strategy

Baseline model:

```text
sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

The release package does not include model weights. Install them into the host
runtime with:

```powershell
python tools\download_embedding_model.py --runtime-dir "<runtime>"
```

Expected location:

```text
<runtime>/models/paraphrase-multilingual-MiniLM-L12-v2
```

Do not bundle model weights in the release zip. Do not switch to a larger
default model without proving cold-start time, hot-call time, recall quality,
and install size.

## Host Adapter Layer

Codex, Claude Code, Qwen Code, and MiMo Code differences belong in
`plugins/` templates and thin host hook adapters.

Adapters may:

- locate host config
- render MCP config
- render hook commands
- set host-local data directories
- set runtime `PYTHONPATH` and environment variables
- translate host hook payloads into `RippleHookEvent`

Adapters must not:

- write SQLite directly
- implement recall/remember
- maintain graph state
- load embedding models
- decide memory evolution rules

## Test Layers

Common checks:

```powershell
$env:PYTHONPATH = "<repo-or-release>\src"
python tests\design_contract_check.py
python tests\baseline_engine_check.py
python tests\import_hygiene_check.py
python tests\agent_daemon_check.py
python tests\memory_evolution_check.py
python tests\storage_architecture_check.py
python tests\soft_timeout_recovery_check.py
python tests\write_queue_check.py
python tests\recall_quality_check.py
python tests\hook_adapter_check.py
python tests\claude_code_hook_check.py
python tests\mimocode_hook_check.py
```

After downloading the local model:

```powershell
python tests\model_baseline_check.py
```

After installing into a host:

```powershell
python -m memoria_mcp.install_check `
  --host codex `
  --data-dir "<host-data-dir>" `
  --skill-path "<host-skill>\SKILL.md" `
  --hook-cmd "<absolute-hook-cmd>" `
  --require-hook-cmd `
  --pretty
```

`tests/design_contract_check.py` is a guardrail for design intent. It does not
replace real read/write tests or installed-host checks.

## Change Rules

Before changing anything, identify the layer:

```text
adapter / hook / MCP proxy / daemon / router / storage / search / skill / install docs
```

Keep these contracts:

- Default MCP tools remain the four normal memory tools.
- Adapters only translate host events.
- The proxy only exposes tools and forwards to the daemon.
- The daemon is the state owner.
- `remember` goes through the write queue.
- `recall` and `read` do not run write-side maintenance.
- Recall results are navigation; read output is evidence.
- SQLite stores runtime truth; JSONL/archive stores frozen full content.
- Model weights are not shipped in the release package.
- New installs do not use the old global data directory.
- Personal paths, private project names, caches, and test data stay out of the
  release package.

When a refactor feels risky, add a test for the intended contract before
changing the implementation.
