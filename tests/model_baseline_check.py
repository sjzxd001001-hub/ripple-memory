"""Verify the local runtime embedding baseline stays on MiniLM."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from memoria_mcp.config import DEFAULT_EMBEDDING_MODEL_DIR, MemoriaConfig
from memoria_mcp.install_check import check_embedding_config


EXPECTED_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"
FORBIDDEN_MODEL_DIRS = {
    "bge-base-zh-v1.5",
    "paraphrase-multilingual-mpnet-base-v2",
}


def _assert_rejected_model_dir(model_name: str) -> None:
    with tempfile.TemporaryDirectory(prefix=f"ripple-{model_name}-drift-") as tmp:
        model_dir = Path(tmp) / model_name
        model_dir.mkdir(parents=True)
        (model_dir / "config.json").write_text(
            json.dumps({"hidden_size": 768}),
            encoding="utf-8",
        )
        os.environ["MEMORIA_MCP_EMBEDDING_MODEL"] = str(model_dir)
        failed = check_embedding_config(require_semantic=False, require_local_model=True)
        assert not failed.ok, failed
        assert "current baseline" in failed.error, failed


def main() -> int:
    assert DEFAULT_EMBEDDING_MODEL_DIR == EXPECTED_MODEL, DEFAULT_EMBEDDING_MODEL_DIR

    root = Path(__file__).resolve().parents[1]
    source_models_dir = root / "models"
    model_dir = source_models_dir / EXPECTED_MODEL
    assert model_dir.is_dir(), model_dir
    assert (model_dir / "config.json").is_file(), model_dir

    source_model_names = {
        path.name
        for path in source_models_dir.iterdir()
        if path.is_dir()
    }
    assert EXPECTED_MODEL in source_model_names, source_model_names
    assert not (source_model_names & FORBIDDEN_MODEL_DIRS), source_model_names

    old_env = dict(os.environ)
    try:
        os.environ.pop("MEMORIA_MCP_EMBEDDING_MODEL", None)
        os.environ["MEMORIA_MCP_DATA_DIR"] = str(root / ".test-data" / "model-baseline")
        config = MemoriaConfig()
        assert Path(config.embedding_model).name == EXPECTED_MODEL, config.embedding_model

        os.environ["MEMORIA_MCP_EMBEDDING_MODEL"] = str(model_dir)
        ok = check_embedding_config(require_semantic=False, require_local_model=True)
        assert ok.ok, ok

        for model_name in FORBIDDEN_MODEL_DIRS:
            _assert_rejected_model_dir(model_name)
    finally:
        os.environ.clear()
        os.environ.update(old_env)

    print(
        json.dumps(
            {
                "ok": True,
                "expected_model_dir": EXPECTED_MODEL,
                "source_model": str(model_dir),
                "forbidden_model_dirs_absent": sorted(FORBIDDEN_MODEL_DIRS),
                "forbidden_model_config_rejected": sorted(FORBIDDEN_MODEL_DIRS),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
