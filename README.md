# Ripple Memory

> Not a text store — a memory system. Facts decay, insights propagate, decisions evolve.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

Ripple Memory gives your AI coding agent long-term memory. Without it, every session starts from scratch — the agent forgets your architecture decisions, debugging insights, and project conventions the moment the context window closes. Ripple Memory changes that. It runs as a local MCP server alongside your coding agent, automatically capturing and retrieving project knowledge across sessions. It can also be installed as a plugin directly into supported agent hosts.

## What Makes It Different

**Cognitive-inspired memory model.** Ripple Memory doesn't just store and retrieve text. It models how human memory actually works:

- **Strength decay** — unused memories fade naturally, so stale information doesn't clutter results
- **Pulse propagation** — accessing one memory boosts related memories through causal links, enabling associative recall
- **Hebbian learning** — frequently traversed memory paths strengthen over time ("cells that fire together wire together")
- **Muscle memory** — repeatedly accessed facts become permanently locked at full strength
- **Consolidation** — stable, well-linked memories get importance boosts, simulating how sleep consolidates learning
- **Compression** — when memory grows too large, cold memories are summarized into compact nodes, preserving the essence without the bulk

**Hybrid retrieval.** Every recall combines BM25 keyword search with vector semantic search (sentence-transformers), fused via Reciprocal Rank Fusion. You get both exact-match precision and semantic understanding.

**Memory evolution.** Facts change over time. Ripple Memory tracks how claims evolve — old versions are hidden by default but remain auditable. When a new architecture decision supersedes an old one, the system knows.

**Fail-open design.** If the embedding model fails, keyword search still works. If the search daemon dies, hooks fall back to BM25. If a hook times out, it returns empty context rather than blocking your agent. The system degrades gracefully, never catastrophically.

## Supported Hosts

| Host | Hook Adapter | Status |
|---|---|---|
| [OpenAI Codex](integrations/codex-ripple-memory.md) | Plugin-based | Stable |
| [Claude Code](integrations/claude-code-ripple-memory.md) | Exec hook | Stable |
| [Qwen Code](integrations/qwen-code-ripple-memory.md) | Exec hook | Stable |
| [MiMo Code](integrations/mimo-code-ripple-memory.md) | Plugin-based | Stable |
| Generic MCP Agent | MCP only | Stable |

Each host gets a thin adapter that translates native events into Ripple's standard hook protocol. Memory logic stays in the core engine — adapters only translate.

## Quick Start

### 1. Install

```bash
git clone https://github.com/sjzxd001001-hub/ripple-memory.git
cd ripple-memory
pip install -e .
```

### 2. Download the embedding model

Ripple Memory needs a sentence-transformer model for semantic search. It's not bundled — download it once:

```bash
python tools/download_embedding_model.py
```

This downloads `paraphrase-multilingual-MiniLM-L12-v2` (multilingual, compact).

### 3. Configure your host

Tell your coding agent to use Ripple Memory as an MCP server. The exact steps depend on your host — see the integration guides linked in the [Supported Hosts](#supported-hosts) table above.

For a guided installation that handles all hosts automatically, see [INSTALL_FOR_LLM.md](INSTALL_FOR_LLM.md).

### 4. Verify

```bash
ripple-memory-install-check --host codex --data-dir "<your-host>/mcp-data/ripple-memory" --pretty
```

That's it. Your agent now has persistent memory.

## How It Works

```
Your Coding Agent (Codex / Claude Code / Qwen / MiMo)
        |
        |  stdio MCP protocol
        v
  MCP Proxy (per window, stateless)
        |
        |  TCP localhost IPC
        v
  Agent Daemon (one per host, owns all state)
        |
        +-- MemoriaRouter (routes by project)
        |       |
        |       +-- Per-Project Server
        |               |-- Causal Graph (nodes + links)
        |               |-- BM25 + Vector Search Index
        |               |-- Write Queue (durable, non-blocking)
        |               |-- Memory Evolution Tracker
        |               +-- SQLite + JSONL Archive
        |
        +-- Search Daemon (vector reranking)
        +-- Dreamer (background compaction)
        +-- Lifecycle Manager (idle sleep, orphan cleanup)
```

The agent daemon is the heart of the system. It loads the embedding model once (not per-window), manages all project caches, and serializes writes through a durable queue so reads never block.

## The Four Tools

Ripple Memory exposes exactly four MCP tools. This minimalism is deliberate — no tool proliferation, no hidden complexity.

| Tool | What It Does |
|---|---|
| `memoria_remember` | Store a durable fact, decision, rule, or insight. Supports `fact_key` for tracking how a topic evolves over time. |
| `memoria_recall` | Search project memory by natural language query. Returns ranked results with references. Old superseded claims are hidden by default. |
| `memoria_read` | Read the full content of a specific memory by its reference ID. Use this when a recall result is truncated or needs verification. |
| `memoria_forget` | Delete a memory from the active graph. The content remains in the archive for audit purposes. |

## Key Concepts

### Project Memory vs. Window Latch

**Project memory** is durable and shared across all sessions for a given project. Architecture decisions, debugging insights, coding conventions — this is long-term knowledge that persists indefinitely.

**Window latch** is short-lived, per-window state. It preserves the user's current task intent so that when context is compressed or a session resumes, the agent can pick up exactly where it left off. The latch is not long-term memory — it's a "don't forget what we're doing right now" note.

### Dual-Rail Storage

Ripple Memory separates runtime state from frozen history:

- **SQLite** (`memoria.db`) — the runtime truth: graph structure, search index, evolution state
- **JSONL/Archive** — the frozen record: full memory content, tiered snapshots, Dreamer compaction blocks

This separation keeps retrieval fast while preserving a complete audit trail.

### Memory Evolution

When a fact changes — say, an architecture decision is revised — use `fact_key` to mark it as the same topic and `supersedes_ref_ids` to link to the old version. Default recall shows only the current claim. Set `include_evolution=true` to see the full history.

## Configuration

### Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `RIPPLE_MEMORY_PROJECT` | *(required)* | Project name for memory isolation |
| `RIPPLE_MEMORY_WINDOW_ID` | *(auto)* | Window identifier for latch isolation |
| `RIPPLE_MEMORY_HOOK_ENABLED` | `1` | Set to `0` to disable hooks without uninstalling |
| `RIPPLE_MEMORY_DATA_DIR` | *(host-specific)* | Override the default data root |
| `MEMORIA_MCP_SSE_PORT` | *(unset)* | Enable SSE transport (eliminates per-window proxy processes) |
| `MEMORIA_MCP_DEBUG_TIMING` | `false` | Log timing diagnostics |

### Hot Unplug

Disable hooks without uninstalling:

```bash
# Via environment variable
export RIPPLE_MEMORY_HOOK_ENABLED=0

# Or via project-local kill switch
mkdir -p .ripple-memory && touch .ripple-memory/hooks.disabled
```

## Development

See [DEVELOPMENT.md](DEVELOPMENT.md) for maintainer notes on the engine architecture, adapter contracts, test layers, and change rules.

### Running Tests

```bash
python tests/baseline_engine_check.py
python tests/memory_evolution_check.py
python tests/recall_quality_check.py
python tests/write_queue_check.py
python tests/hook_adapter_check.py
# ... and more
```

### Project Structure

```
src/memoria_mcp/     Core engine (graph, search, persistence, daemon, hooks)
plugins/             Host-specific hook adapters (Codex, Claude, Qwen, MiMo)
skills/              LLM instruction files (SKILL.md)
integrations/        Per-host installation guides
tests/               Regression checks (14 suites)
tools/               Utilities (embedding model downloader)
```

## License

MIT. See [LICENSE](LICENSE) for details.
