"""Regression check for Ripple Memory import-path hygiene.

Host runtimes must be loaded explicitly from their copied runtime `src`
directory. A global editable install can silently shadow source tests or host
runtime checks with an old copy, so this test blocks that failure mode.
"""
from __future__ import annotations

import importlib.util
import json
import site
from pathlib import Path
from typing import Iterable


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _site_roots() -> Iterable[Path]:
    roots: list[str] = []
    try:
        roots.extend(site.getsitepackages())
    except Exception:
        pass
    try:
        roots.append(site.getusersitepackages())
    except Exception:
        pass
    seen: set[Path] = set()
    for raw in roots:
        if not raw:
            continue
        path = Path(raw).expanduser()
        if path in seen or not path.is_dir():
            continue
        seen.add(path)
        yield path


def _global_ripple_editable_refs() -> list[str]:
    matches: list[str] = []
    suspicious_name_parts = (
        "_editable_impl_ripple_memory",
        "__editable__.ripple",
        "__editable___ripple",
    )
    suspicious_text_parts = (
        "ripple-memory-runtime",
        "ripple-memory-engine",
    )
    for root in _site_roots():
        for path in sorted(root.glob("*.pth")) + sorted(root.glob("__editable__*.py")):
            name = path.name.lower()
            text = ""
            try:
                text = path.read_text(encoding="utf-8", errors="replace").lower()
            except OSError:
                pass
            if any(part in name for part in suspicious_name_parts):
                matches.append(str(path))
                continue
            if any(part in text for part in suspicious_text_parts):
                matches.append(str(path))
    return matches


def run_check() -> dict:
    repo_root = Path(__file__).resolve().parents[1]
    source_root = repo_root / "src"
    package_root = source_root / "memoria_mcp"

    server_spec = importlib.util.find_spec("memoria_mcp.server")
    _assert(server_spec is not None and server_spec.origin, "memoria_mcp.server is not importable")
    server_origin = Path(str(server_spec.origin)).resolve()
    _assert(
        _is_relative_to(server_origin, source_root.resolve()),
        f"memoria_mcp.server resolved outside source src: {server_origin}",
    )

    tool_worker_spec = importlib.util.find_spec("memoria_mcp.tool_worker")
    _assert(tool_worker_spec is None, f"legacy memoria_mcp.tool_worker is importable: {tool_worker_spec}")

    residual_tool_worker_files = sorted(str(path) for path in package_root.rglob("tool_worker*"))
    _assert(
        not residual_tool_worker_files,
        f"legacy tool_worker files remain in source tree: {residual_tool_worker_files}",
    )

    global_editable_refs = _global_ripple_editable_refs()
    _assert(
        not global_editable_refs,
        f"global Ripple Memory editable installs can shadow source/runtime imports: {global_editable_refs}",
    )

    return {
        "ok": True,
        "source_root": str(source_root),
        "server_origin": str(server_origin),
        "tool_worker_importable": False,
        "global_ripple_editable_refs": [],
    }


def main() -> int:
    result = run_check()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
