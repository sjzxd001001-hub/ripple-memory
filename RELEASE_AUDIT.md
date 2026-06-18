# Release Audit

Purpose: this file records the package scope and release verification evidence.
The user entry point is `README.md`, the installer entry point is
`INSTALL_FOR_LLM.md`, and maintainer guidance is in `DEVELOPMENT.md`.

This release package is built from an allowlist. It is not a full repository
copy.

## Excluded From The Release Package

- `models/` embedding model weights
- `.git/`
- project instruction files
- construction ledger / internal work logs
- `.codegraph/`
- `.ripple-memory/`
- `.test-data/`
- `.pytest_cache/`
- `tmp/`
- `tests/diag_*`
- `tests/pollution_recall_test.py`
- `__pycache__/` and `.pyc`

## Required Pre-Release Checks

```powershell
$env:PYTHONPATH = "<release-dir>\src"
python -m compileall -q src tests
python tests\design_contract_check.py
python tests\baseline_engine_check.py
python tests\agent_daemon_check.py
python tests\hook_adapter_check.py
python tests\claude_code_hook_check.py
python tests\mimocode_hook_check.py
```

After downloading the embedding model, also run:

```powershell
python tests\model_baseline_check.py
```

## Model Policy

Current public release baseline model:

```text
sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

The release package does not include model weights. An installer must download
the model into the host runtime:

```text
<runtime>/models/paraphrase-multilingual-MiniLM-L12-v2
```

Supported helper:

```powershell
python tools\download_embedding_model.py --runtime-dir "<runtime>"
```

## Verification History

### 2026-06-16 Initial Release Package

- Internal release scan passed: no personal paths, private project names, model
  weights, caches, or development ledger were found.
- `tests/design_contract_check.py` passed. It protects the four MCP tools,
  agent daemon, dual-rail storage, hook/latch behavior, write queue, model
  baseline, and test-suite boundaries.
- `compileall` passed.
- Core regression checks passed: baseline, import hygiene, agent daemon,
  memory evolution, storage architecture, soft-timeout recovery, write queue,
  recall quality, hook adapter, Claude hook, and lifecycle.
- A temporary ASCII runtime with rendered Codex hook passed `install_check`.
- `tools/download_embedding_model.py` can download MiniLM and then
  `tests/model_baseline_check.py` can run. The final package still excludes
  model weights.

### 2026-06-17 Package Layout Review

- The release package uses `plugins/` instead of source-tree `plug/`; document
  references were synchronized.
- Codex plugin manifest passed `plugin-creator`'s `validate_plugin.py`.
- Codex marketplace template was updated to the current `source` / `policy` /
  `category` structure.
- `compileall` passed.
- Non-model regressions passed: design contract, baseline, agent daemon, hook
  adapter, Claude hook, import hygiene, memory evolution, storage architecture,
  soft-timeout recovery, write queue, recall quality, and lifecycle.
- The package still excludes `models/`; `tests/model_baseline_check.py`
  requires a downloaded local model.

### 2026-06-17 MiMo Code Hook Promotion

- The package now includes `src/memoria_mcp/mimocode_hook.py` and
  `plugins/mimocode/ripple-memory-hooks`; MiMo tutorial references now point
  at shipped files.
- MiMo Code docs now describe MCP + plugin hook support. Generic MCP Agent
  remains MCP-only unless a real host-specific adapter exists.
- Non-MiMo hosts must not install `plugins/mimocode` or `mimocode_hook.py`.
- The package includes `integrations/generic-agent-ripple-memory.md`.
- `INSTALL_FOR_LLM.md` and the Generic guide require installers to inspect host
  docs/config/source/plugin APIs before downgrading to MCP-only. If a safe hook
  surface exists, the installer should create a host-specific adapter and tests;
  otherwise it must record why the host remains MCP-only.
- `README.md`, `DEVELOPMENT.md`, and `RELEASE_AUDIT.md` now have distinct
  English roles: product entry point, maintainer guide, and release evidence.
- `compileall -q src tests` passed.
- `tests/design_contract_check.py` passed.
- `tests/mimocode_hook_check.py` passed.
- A temporary render of `plugins/mimocode`'s `.cmd.template` passed
  `install_check --host mimocode --require-hook-cmd`:

```text
15 passed, 3 skipped, 0 failed
```
