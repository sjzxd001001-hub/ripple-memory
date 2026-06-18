"""Search daemon: TCP server for vector reranking.

Runs as a background thread inside the MCP server process.
The hook subprocess connects via TCP to get vector-reranked results.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_PORT_FILE_NAME = ".search_port"
_REQUEST_TIMEOUT = 5.0
_MAX_RESULTS = 10
_RESERVED_DATA_DIRS = {"models", "archives"}


def _port_file_path(data_dir: str) -> Path:
    return Path(data_dir) / _PORT_FILE_NAME


def _write_port_file(path: Path, port: int, *, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {
                "schema": "ripple_memory_search_daemon_v1",
                "host": "127.0.0.1",
                "port": int(port),
                "pid": os.getpid(),
                "token": token,
                "started_at": time.time(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tmp.replace(path)


def _remove_port_file(path: Path, *, token: Optional[str] = None) -> None:
    if token is not None:
        try:
            raw = path.read_text(encoding="utf-8").strip()
            record = json.loads(raw) if raw.startswith("{") else {}
            if record.get("token") != token:
                return
        except (OSError, json.JSONDecodeError):
            return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _project_name_allowed(project: str) -> bool:
    name = str(project or "").strip()
    return bool(name) and not name.startswith("_") and name not in _RESERVED_DATA_DIRS


def _project_dir_looks_real(data_dir: str, project: str) -> bool:
    if not _project_name_allowed(project):
        return False
    project_dir = Path(data_dir) / project
    return project_dir.is_dir() and (project_dir / "memoria.db").is_file()


class SearchDaemon:
    """TCP server for vector reranking, runs in a background thread."""

    def __init__(self, router: Any, data_dir: str):
        self._router = router
        self._data_dir = data_dir
        self._port_file = _port_file_path(data_dir)
        self._token = f"{os.getpid()}-{uuid.uuid4().hex}"
        self._server_socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._model_loaded = False
        # Pre-cache servers for thread-safe access
        self._server_cache: Dict[str, Any] = {}
        self._cache_lock = threading.RLock()

    def _build_server_cache(self) -> None:
        """Cache servers already opened by the router.

        The data directory also contains non-project folders such as ``models``.
        Startup must not scan every folder through ``_get_server`` because that
        creates bogus project databases and makes MCP startup heavier.
        """
        try:
            for project, server in dict(getattr(self._router, "_servers", {})).items():
                if _project_name_allowed(project):
                    self._server_cache[str(project)] = server
        except Exception:
            pass

    def _get_server_for_project(self, project: str) -> Any:
        if not _project_dir_looks_real(self._data_dir, project):
            return None
        with self._cache_lock:
            server = self._server_cache.get(project)
            if server is not None:
                return server
            try:
                server = self._router._get_server(project)
            except Exception:
                return None
            self._server_cache[project] = server
            return server

    def _ensure_model_loaded(self, server: Any) -> None:
        """Load embedding model on first rerank request."""
        import sys
        if self._model_loaded:
            return
        try:
            from .graph import _get_embedding_model, _check_embedding_available
            if not _check_embedding_available():
                print("[DAEMON] embedding not available", file=sys.stderr, flush=True)
                return
            if server.config.enable_semantic:
                print(f"[DAEMON] loading model for request: {server.config.embedding_model[-40:]}", file=sys.stderr, flush=True)
                _get_embedding_model(server.config.embedding_model)
                self._model_loaded = True
                print("[DAEMON] model loaded OK", file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"[DAEMON] model load failed: {exc}", file=sys.stderr, flush=True)

    def _handle_rerank_request(
        self,
        project: str,
        query: str,
        candidate_ids: List[str],
        top_k: int,
    ) -> Dict[str, Any]:
        """Rerank BM25 candidates using vector similarity."""
        server = self._get_server_for_project(project)
        if server is None:
            return {"ok": False, "error": f"project_not_cached: {project}"}

        graph = server.graph
        nodes = [graph.nodes[cid] for cid in candidate_ids if cid in graph.nodes]
        if not nodes:
            return {"ok": True, "results": [], "count": 0}

        model = None
        if server.config.enable_semantic:
            self._ensure_model_loaded(server)
            from .graph import _get_embedding_model
            model = _get_embedding_model(server.config.embedding_model)

        results = []
        query_embedding = None
        if model is not None:
            try:
                query_embedding = model.encode(query, show_progress_bar=False).tolist()
            except Exception:
                query_embedding = None

        for node in nodes:
            item: Dict[str, Any] = {
                "id": node.id,
                "description": node.summary.description or "",
                "type": str(getattr(node.summary, "type", "")),
                "importance": float(node.importance),
                "strength": float(node.strength),
                "vec_sim": 0.0,
            }
            if query_embedding is not None and node.embedding is not None:
                from .graph import _safe_cosine_similarity
                item["vec_sim"] = round(_safe_cosine_similarity(node.embedding, query_embedding), 4)
            elif query_embedding is not None and node.summary.description:
                try:
                    emb = model.encode(node.summary.description, show_progress_bar=False).tolist()
                    node.embedding = emb
                    from .graph import _safe_cosine_similarity
                    item["vec_sim"] = round(_safe_cosine_similarity(emb, query_embedding), 4)
                except Exception:
                    pass
            results.append(item)

        results.sort(key=lambda x: x["vec_sim"], reverse=True)
        results = results[:top_k]

        return {"ok": True, "results": results, "count": len(results)}

    def start(self) -> None:
        """Start the search daemon in a background thread."""
        try:
            self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_socket.bind(("127.0.0.1", 0))
            self._server_socket.listen(4)
            self._server_socket.settimeout(1.0)
            port = self._server_socket.getsockname()[1]
            _write_port_file(self._port_file, port, token=self._token)
            self._running = True
            self._build_server_cache()
            self._thread = threading.Thread(target=self._run, daemon=True, name="ripple-search-daemon")
            self._thread.start()
            logger.info(f"Search daemon listening on 127.0.0.1:{port}")
        except Exception as exc:
            logger.warning(f"Search daemon failed to start: {exc}")
            self._cleanup()

    def stop(self) -> None:
        """Stop the search daemon."""
        self._running = False
        self._cleanup()

    def _cleanup(self) -> None:
        _remove_port_file(self._port_file, token=self._token)
        if self._server_socket:
            try:
                self._server_socket.close()
            except OSError:
                pass
            self._server_socket = None

    def _run(self) -> None:
        while self._running:
            try:
                conn, addr = self._server_socket.accept()
                conn.settimeout(_REQUEST_TIMEOUT)
                threading.Thread(target=self._handle_connection, args=(conn,), daemon=True).start()
            except socket.timeout:
                continue
            except OSError:
                if self._running:
                    time.sleep(0.1)
                continue

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            data = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break
            if not data:
                return

            request = json.loads(data.decode("utf-8").strip())
            project = str(request.get("project") or "default")
            query = str(request.get("query") or "")
            candidate_ids = list(request.get("candidate_ids") or [])
            top_k = min(int(request.get("top_k") or 4), _MAX_RESULTS)

            response = self._handle_rerank_request(project, query, candidate_ids, top_k)
            conn.sendall(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
        except Exception as exc:
            try:
                error_resp = json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
                conn.sendall(error_resp.encode("utf-8") + b"\n")
            except OSError:
                pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
