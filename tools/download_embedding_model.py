"""Download the baseline Ripple Memory embedding model into a runtime folder."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_DIR_NAME = "paraphrase-multilingual-MiniLM-L12-v2"


def _model_ready(path: Path) -> bool:
    return (path / "config.json").is_file()


def _safe_model_target(runtime_dir: Path, dir_name: str) -> Path:
    """Return <runtime>/models/<dir_name> while rejecting path traversal."""
    name = str(dir_name or "").strip()
    raw = Path(name)
    if (
        not name
        or raw.is_absolute()
        or raw.drive
        or len(raw.parts) != 1
        or raw.parts[0] in {".", ".."}
    ):
        raise ValueError("--dir-name must be one plain directory name under <runtime>/models")

    models_root = (runtime_dir / "models").resolve()
    target = (models_root / raw.parts[0]).resolve()
    try:
        target.relative_to(models_root)
    except ValueError as exc:
        raise ValueError("--dir-name resolved outside <runtime>/models") from exc
    return target


def _download_with_huggingface_hub(model_name: str, target: Path) -> dict[str, Any]:
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=model_name,
        local_dir=str(target),
    )
    return {"method": "huggingface_hub.snapshot_download"}


def _download_with_sentence_transformers(model_name: str, target: Path) -> dict[str, Any]:
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_name)
    model.save(str(target))
    return {"method": "sentence_transformers.save"}


def download_model(model_name: str, target: Path, *, force: bool = False) -> dict[str, Any]:
    if _model_ready(target) and not force:
        return {
            "ok": True,
            "status": "already_exists",
            "model": model_name,
            "target": str(target),
        }
    if target.exists() and force:
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    for downloader in (_download_with_huggingface_hub, _download_with_sentence_transformers):
        try:
            detail = downloader(model_name, target)
            if not _model_ready(target):
                raise RuntimeError("download finished but config.json is missing")
            return {
                "ok": True,
                "status": "downloaded",
                "model": model_name,
                "target": str(target),
                **detail,
            }
        except Exception as exc:  # noqa: BLE001 - report both fallback failures.
            errors.append(f"{downloader.__name__}: {exc.__class__.__name__}: {exc}")

    return {
        "ok": False,
        "status": "failed",
        "model": model_name,
        "target": str(target),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download the Ripple Memory baseline embedding model into a host runtime.",
    )
    parser.add_argument(
        "--runtime-dir",
        required=True,
        help="Runtime directory that contains src/ and will receive models/<model-dir>.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model repo id. Default: {DEFAULT_MODEL}",
    )
    parser.add_argument(
        "--dir-name",
        default=DEFAULT_DIR_NAME,
        help=f"Directory name under <runtime>/models. Default: {DEFAULT_DIR_NAME}",
    )
    parser.add_argument("--force", action="store_true", help="Remove and redownload an existing model directory.")
    args = parser.parse_args()

    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    try:
        target = _safe_model_target(runtime_dir, args.dir_name)
    except ValueError as exc:
        parser.error(str(exc))
    result = download_model(args.model, target, force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
