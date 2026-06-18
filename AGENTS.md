# AGENTS.md - Ripple Memory (Open Source Release)

Startup card for AI coding agents and human contributors working on Ripple Memory.

## Project Boundary

Ripple Memory is a compact agent memory engine. It gives AI coding agents
long-term persistent memory via MCP (Model Context Protocol).

```text
ripple-memory MCP server
+ ripple-memory skill (SKILL.md)
+ thin hook adapters (Codex, Claude Code, Qwen Code, MiMo Code)
+ install verification suite
```

## Architecture Rule

```text
Agent native hook event
  -> thin adapter (translates host events)
  -> Ripple standard hook event
  -> Ripple Memory core
  -> standard output context / warnings / memory result
```

Adapters only translate host events. They must not contain memory business logic.

Core owns: project/window identity, Original Words Latch, recall/read navigation,
durable remember decisions, delete/forget semantics, memory evolution, hook kill
switches, timeout/fail-open behavior, lifecycle sleep/archive/restore.

## Memory Model

- Project memory is durable and shared by windows using the same `project`
  inside the same host data root.
- MCP exposes exactly four tools: `memoria_remember`, `memoria_recall`,
  `memoria_read`, `memoria_forget`.
- If old memory conflicts with the current user message, the current user
  message wins.
- `memoria_forget` is user-visible deletion, not a forensic secure erase.

## Storage Model

Dual-rail architecture:

- SQLite `memoria.db`: runtime truth, graph state, search metadata,
  memory-evolution state/edges.
- JSONL/archive files: frozen full-content history, snapshots, Dreamer archives.
- Obsolete SQL tables `memory_stream` and `archive_blocks` must be absent.
- Memory evolution uses `fact_key` and `supersedes_ref_ids`. Default recall
  hides superseded memories. `include_evolution=true` exposes history.

## Testing Strategy & Importance

Tests are the project's safety net. Code changes without running tests are not
considered done. Three-tier testing hierarchy:

### Tier 1: Source Regression Batch (MANDATORY)

Run before every commit and after every meaningful change. These are fast,
isolated, and require no external host. Failure in any of these is a
**release blocker**.

| # | Test File | Importance | What It Protects |
|---|-----------|------------|------------------|
| 1 | `baseline_engine_check.py` | **CRITICAL** | The four core MCP tools (remember/recall/read/forget) work at the router level. If this fails, nothing works. |
| 2 | `import_hygiene_check.py` | **HIGH** | No stale global editable install shadows the source tree. Silent wrong-code prevention. |
| 3 | `agent_daemon_check.py` | **CRITICAL** | Daemon lifecycle: singleton enforcement, reuse, restart, slow-response port preservation, owner-exit cleanup. |
| 4 | `model_baseline_check.py` | **HIGH** | Embedding model stays on the correct baseline. Blocks accidental model drift. |
| 5 | `memory_evolution_check.py` | **CRITICAL** | Supersession chain: fact_key, supersedes_ref_ids, default filtering, include_evolution. Core data integrity. |
| 6 | `storage_architecture_check.py` | **CRITICAL** | Dual-rail contract: SQLite runtime + JSONL frozen history. Obsolete tables absent. Dreamer cleanup. Restart safety. |
| 7 | `soft_timeout_recovery_check.py` | **HIGH** | Read/write independent timeout budgets. Recovery doesn't re-write committed data. |
| 8 | `write_queue_check.py` | **HIGH** | Durable queue-first remember. Out-of-process worker. Non-blocking recall. Budget exhaustion. |
| 9 | `recall_quality_check.py` | **CRITICAL** | Recall latency, exact match ranking, weak filtering, read-only boundary, search-index rebuild safety. |
| 10 | `hook_adapter_check.py` | **HIGH** | Codex hook adapter: context injection, latch creation, progress tracking, completed-stop anti-repeat. |
| 11 | `claude_code_hook_check.py` | **HIGH** | Claude Code hook adapter: hookSpecificOutput.additionalContext schema compliance. |
| 12 | `mimocode_hook_check.py` | **HIGH** | MiMo Code hook adapter: correct translation to Ripple standard protocol. |

Additional tests (not in mandatory batch but valuable):

| Test File | Importance | What It Protects |
|-----------|------------|------------------|
| `lifecycle_check.py` | **HIGH** | Process registry, heartbeat, idle sleep/exit, parent-death detection, orphan cleanup. |
| `design_contract_check.py` | **MEDIUM** | Architecture contract: tool names, module structure, required regression files. |

### Tier 2: Installed-Host Acceptance (MANDATORY before release/sync)

Run via `ripple-memory-install-check` after installing into a host environment.
18 named checks grouped by importance:

**CRITICAL (core functionality):**

| Check | What It Protects |
|-------|------------------|
| `mcp_tools_project_database` | Four canonical tools work end-to-end. Project isolation. |
| `agent_daemon_flow` | Daemon starts, reuses, restarts. Recall latency. Owner lifecycle. |
| `mcp_stdio_protocol` | Full MCP stdio round-trip. All four tools over stdio transport. |
| `recall_quality` | Latency, relevance ranking, weak filtering, index rebuild safety. |
| `memory_evolution` | Supersession, default filtering, Dreamer cleanup, restart safety. |

**HIGH (reliability and safety):**

| Check | What It Protects |
|-------|------------------|
| `storage_architecture` | Dual-rail contract. Dreamer purge. Restart safety. |
| `soft_timeout_recovery` | Independent read/write budgets. Queue-first return. |
| `write_queue_flow` | Queue drain, non-blocking recall, budget exhaustion, cleanup. |
| `hook_latch_window_flow` | Hook injection, latch isolation, burst suppression, completed-stop, kill switch. |
| `lifecycle_management` | Process registry, orphan cleanup, parent-death, idle sleep. |
| `search_daemon_safety` | Concurrent daemon safety, reserved dirs protection, dead port cleanup. |
| `input_encoding_safety` | Dangerous inputs don't crash. UTF-8 safety. Token length cap. |

**MEDIUM (configuration and integration):**

| Check | What It Protects |
|-------|------------------|
| `embedding_config` | Model path, name, importability. Blocks model drift. |
| `skill_guidance` | SKILL.md completeness: four tools, semantic categories, evolution/latch guidance. |

**CONDITIONAL (requires explicit flags or live host):**

| Check | Condition | What It Proves |
|-------|-----------|----------------|
| `host_mcp_process` | `--require-host-mcp-process` | Live daemon/proxy running under correct host. |
| `live_smoke` | `--live-smoke` | Real daemon, real data dir, real hooks. End-to-end. |
| `hook_command` | `--require-hook-cmd` | Hook script executes, returns JSON, injects context. |
| `codex_live_hook` | `--codex-live` | Real Codex session retrieves stored secret via hook. |

### How to Run

**Tier 1 — Source regression:**

```powershell
$env:PYTHONPATH = "<repo-root>\src"
python tests\baseline_engine_check.py
python tests\import_hygiene_check.py
python tests\agent_daemon_check.py
python tests\model_baseline_check.py
python tests\memory_evolution_check.py
python tests\storage_architecture_check.py
python tests\soft_timeout_recovery_check.py
python tests\write_queue_check.py
python tests\recall_quality_check.py
python tests\hook_adapter_check.py
python tests\claude_code_hook_check.py
python tests\mimocode_hook_check.py
```

**Tier 2 — Installed-host acceptance:**

```powershell
ripple-memory-install-check --host <host> --data-dir <path> --pretty
```

### Test Discipline Rules

1. **Never skip Tier 1 before committing.** If a Tier 1 test fails, the change
   is not ready.
2. **Never sync to host runtimes without Tier 2.** Source passing does not
   guarantee installed passing.
3. **Flaky tests must be investigated, not ignored.** Timing-sensitive tests
   should be understood and documented.
