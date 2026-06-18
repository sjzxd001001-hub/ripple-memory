"""IPC client for hook → search daemon communication.

The hook subprocess connects to the MCP server's search daemon
via TCP localhost to get vector-reranked results.
"""
from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any, Dict, List, Optional

_PORT_FILE_NAME = ".search_port"
_CONNECT_TIMEOUT = 0.5
_RECV_TIMEOUT = 1.5


def _read_port_record(data_dir: Path) -> Optional[Dict[str, Any]]:
    """Read the search daemon port record from the port file."""
    port_file = data_dir / _PORT_FILE_NAME
    try:
        if port_file.is_file():
            raw = port_file.read_text(encoding="utf-8").strip()
            if raw.startswith("{"):
                record = json.loads(raw)
                port = int(record.get("port") or 0)
            else:
                port = int(raw)
                record = {"port": port}
            if 1 <= port <= 65535:
                record["port"] = port
                return record
    except (ValueError, OSError, json.JSONDecodeError):
        pass
    return None


def _remove_stale_port_file(data_dir: Path, record: Optional[Dict[str, Any]]) -> None:
    """Remove the port file only if it still points at the failed daemon."""
    if not record:
        return
    port_file = data_dir / _PORT_FILE_NAME
    try:
        current = _read_port_record(data_dir)
        if not current:
            return
        if current.get("port") != record.get("port"):
            return
        token = record.get("token")
        if token and current.get("token") != token:
            return
        port_file.unlink(missing_ok=True)
    except OSError:
        pass


def request_rerank(
    *,
    data_dir: Path,
    project: str,
    query: str,
    candidate_ids: List[str],
    top_k: int = 4,
) -> Optional[Dict[str, Any]]:
    """Send rerank request to search daemon. Returns None if unavailable."""
    record = _read_port_record(data_dir)
    if record is None:
        return None
    port = int(record["port"])

    request = json.dumps({
        "project": project,
        "query": query,
        "candidate_ids": candidate_ids,
        "top_k": top_k,
    }, ensure_ascii=False)

    sock: Optional[socket.socket] = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(_CONNECT_TIMEOUT)
        sock.connect(("127.0.0.1", port))
        sock.settimeout(_RECV_TIMEOUT)
        sock.sendall(request.encode("utf-8") + b"\n")

        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break

        if data:
            response = json.loads(data.decode("utf-8").strip())
            if isinstance(response, dict) and response.get("ok"):
                return response
    except ConnectionRefusedError:
        _remove_stale_port_file(data_dir, record)
    except socket.timeout:
        _remove_stale_port_file(data_dir, record)
    except (OSError, json.JSONDecodeError):
        _remove_stale_port_file(data_dir, record)
        pass
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    return None
