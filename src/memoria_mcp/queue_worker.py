"""Short-lived worker that drains one project's durable remember queue."""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from typing import Any, Dict

from .server import MemoriaRouter, _sanitize_project_name
from .write_queue import ProjectWriteQueue


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Drain a Ripple Memory remember write queue.")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--budget-seconds", type=float, default=8.0)
    parser.add_argument("--pretty", action="store_true")
    return parser


def run_worker(*, data_dir: str, project: str, budget_seconds: float) -> Dict[str, Any]:
    project = _sanitize_project_name(project)
    router = MemoriaRouter(data_dir)
    try:
        result = router._flush_write_queue(project, budget_seconds=max(0.0, float(budget_seconds)))
    finally:
        router.close()
    result["project"] = project
    result["data_dir"] = data_dir
    result["follow_up_worker"] = _start_follow_up_worker_if_needed(
        data_dir=data_dir,
        project=project,
        budget_seconds=budget_seconds,
        drain_result=result,
    )
    return result


def _start_follow_up_worker_if_needed(
    *,
    data_dir: str,
    project: str,
    budget_seconds: float,
    drain_result: Dict[str, Any],
) -> Dict[str, Any]:
    if os.environ.get("MEMORIA_MCP_WRITE_QUEUE_FOLLOW_UP_WORKER", "true").lower() in {"0", "false", "no", "off"}:
        return {"started": False, "reason": "disabled"}
    if not drain_result.get("acquired"):
        return {"started": False, "reason": "writer_lock_not_acquired"}
    if int(drain_result.get("processed") or 0) + int(drain_result.get("failed") or 0) <= 0:
        return {"started": False, "reason": "no_progress"}

    counts = ProjectWriteQueue(data_dir, project).counts()
    if counts.get("ready", 0) <= 0:
        return {"started": False, "reason": "queue_empty", "counts": counts}

    env = os.environ.copy()
    env["MEMORIA_MCP_DATA_DIR"] = data_dir
    cmd = [
        sys.executable,
        "-m",
        "memoria_mcp.queue_worker",
        "--data-dir",
        data_dir,
        "--project",
        project,
        "--budget-seconds",
        str(max(0.1, float(budget_seconds))),
    ]
    kwargs: Dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": env,
        "cwd": os.getcwd(),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except Exception as exc:  # noqa: BLE001 - caller records the failed follow-up.
        return {"started": False, "reason": "spawn_failed", "error": f"{exc.__class__.__name__}: {exc}", "counts": counts}
    return {"started": True, "pid": proc.pid, "counts": counts}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    args = _build_parser().parse_args(argv)
    os.environ["MEMORIA_MCP_DATA_DIR"] = args.data_dir
    try:
        result = run_worker(
            data_dir=args.data_dir,
            project=args.project,
            budget_seconds=args.budget_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - worker failure is inspectable by process exit.
        payload = {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
        print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))
        return 1
    payload = {"ok": True, **result}
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
