"""SQLite runtime persistence for Ripple Memory.

SQLite owns the current runtime truth: graph state, search metadata, and
memory-evolution overlays. Frozen/full history lives on the JSON archive rail.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from contextlib import closing
from typing import Any, Dict, List, Optional

from .config import MemoriaConfig

logger = logging.getLogger("RippleMemory.Persistence")


class MemoriaPersistence:
    def __init__(self, config: MemoriaConfig):
        self.config = config
        self.db_path = config.db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """Create a new SQLite connection with WAL and busy_timeout."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self):
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS graph_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    state_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_evolution_state (
                    state_id TEXT PRIMARY KEY,
                    fact_key TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    ref_id TEXT NOT NULL,
                    claim_text TEXT,
                    status TEXT NOT NULL,
                    superseded_by_node_id TEXT,
                    superseded_by_ref_id TEXT,
                    reason TEXT,
                    confidence REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_evolution_node
                ON memory_evolution_state(node_id, status, fact_key)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_evolution_fact
                ON memory_evolution_state(fact_key, status, updated_at DESC)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_evolution_edges (
                    edge_id TEXT PRIMARY KEY,
                    fact_key TEXT NOT NULL,
                    from_ref_id TEXT NOT NULL,
                    from_claim_hash TEXT NOT NULL,
                    to_ref_id TEXT NOT NULL,
                    to_claim_hash TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    confidence REAL,
                    reason TEXT,
                    metadata_json TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE(
                        fact_key,
                        from_ref_id,
                        from_claim_hash,
                        to_ref_id,
                        to_claim_hash,
                        relation
                    )
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_evolution_edges_fact
                ON memory_evolution_edges(fact_key, created_at DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_evolution_edges_refs
                ON memory_evolution_edges(from_ref_id, to_ref_id, relation)
            """)
            # Earlier builds duplicated frozen-history payloads into SQLite.
            # New installs keep those payloads only on the JSON archive rail.
            conn.execute("DROP TABLE IF EXISTS archive_blocks")
            conn.execute("DROP TABLE IF EXISTS memory_stream")
            conn.commit()

    def save_graph(self, graph_dict: Dict[str, Any]) -> float:
        now = time.time()
        state_json = json.dumps(graph_dict, ensure_ascii=False)
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO graph_state (id, state_json, updated_at) VALUES (1, ?, ?)",
                (state_json, now),
            )
            conn.commit()
        logger.debug(f"Graph state saved ({len(state_json)} bytes)")
        return now

    def load_graph(self) -> Optional[Dict[str, Any]]:
        record = self.load_graph_record()
        if record:
            return record["state"]
        return None

    def load_graph_record(self) -> Optional[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT state_json, updated_at FROM graph_state WHERE id = 1").fetchone()
            if row:
                return {
                    "state": json.loads(row[0]),
                    "updated_at": float(row[1] or 0.0),
                }
        return None

    def graph_updated_at(self) -> float:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT updated_at FROM graph_state WHERE id = 1").fetchone()
            if row:
                return float(row[0] or 0.0)
        return 0.0

    @staticmethod
    def _memory_evolution_state_id(fact_key: str, node_id: str) -> str:
        raw = f"{fact_key}\0{node_id}".encode("utf-8")
        return "evo_" + hashlib.sha256(raw).hexdigest()[:24]

    @staticmethod
    def memory_evolution_claim_hash(value: Any) -> str:
        text = " ".join(str(value or "").split()).strip().lower()
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_memory_evolution_status(value: Any) -> str:
        status = str(value or "active").strip().lower()
        if status not in {"active", "superseded", "pending_conflict"}:
            status = "active"
        return status

    def upsert_memory_evolution_state(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Record lightweight current-vs-historical claim status for a memory."""
        fact_key = str(state.get("fact_key") or "").strip().lower()
        node_id = str(state.get("node_id") or "").strip()
        if not fact_key or not node_id:
            raise ValueError("memory_evolution_state requires fact_key and node_id")

        status = self._normalize_memory_evolution_status(state.get("status"))
        ref_id = str(state.get("ref_id") or f"memory_node:{node_id}").strip()
        claim_text = str(state.get("claim_text") or "")[:4000]
        superseded_by_node_id = str(state.get("superseded_by_node_id") or "").strip()
        superseded_by_ref_id = str(state.get("superseded_by_ref_id") or "").strip()
        reason = str(state.get("reason") or "")[:1000]
        confidence = state.get("confidence")
        try:
            confidence_value = None if confidence in (None, "") else float(confidence)
        except (TypeError, ValueError):
            confidence_value = None
        now = time.time()
        state_id = str(state.get("state_id") or self._memory_evolution_state_id(fact_key, node_id))

        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO memory_evolution_state (
                    state_id, fact_key, node_id, ref_id, claim_text, status,
                    superseded_by_node_id, superseded_by_ref_id, reason,
                    confidence, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(state_id) DO UPDATE SET
                    ref_id = excluded.ref_id,
                    claim_text = excluded.claim_text,
                    status = excluded.status,
                    superseded_by_node_id = excluded.superseded_by_node_id,
                    superseded_by_ref_id = excluded.superseded_by_ref_id,
                    reason = excluded.reason,
                    confidence = excluded.confidence,
                    updated_at = excluded.updated_at
                """,
                (
                    state_id,
                    fact_key,
                    node_id,
                    ref_id,
                    claim_text,
                    status,
                    superseded_by_node_id,
                    superseded_by_ref_id,
                    reason,
                    confidence_value,
                    now,
                    now,
                ),
            )
            conn.commit()
        return {
            "state_id": state_id,
            "fact_key": fact_key,
            "node_id": node_id,
            "ref_id": ref_id,
            "claim_text": claim_text,
            "status": status,
            "superseded_by_node_id": superseded_by_node_id,
            "superseded_by_ref_id": superseded_by_ref_id,
            "reason": reason,
            "confidence": confidence_value,
            "updated_at": now,
        }

    @staticmethod
    def _memory_evolution_edge_id(
        fact_key: str,
        from_ref_id: str,
        from_claim_hash: str,
        to_ref_id: str,
        to_claim_hash: str,
        relation: str,
    ) -> str:
        raw = json.dumps(
            {
                "fact_key": fact_key,
                "from_ref_id": from_ref_id,
                "from_claim_hash": from_claim_hash,
                "to_ref_id": to_ref_id,
                "to_claim_hash": to_claim_hash,
                "relation": relation,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "evo_edge_" + hashlib.sha256(raw).hexdigest()[:32]

    def insert_memory_evolution_edge(self, edge: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a claim-level evolution edge, e.g. old claim superseded_by new claim."""
        fact_key = str(edge.get("fact_key") or "").strip().lower()
        from_ref_id = str(edge.get("from_ref_id") or "").strip()
        to_ref_id = str(edge.get("to_ref_id") or "").strip()
        from_claim_hash = str(edge.get("from_claim_hash") or "").strip()
        to_claim_hash = str(edge.get("to_claim_hash") or "").strip()
        relation = str(edge.get("relation") or "superseded_by").strip().lower()
        if not fact_key or not from_ref_id or not to_ref_id or not from_claim_hash or not to_claim_hash:
            raise ValueError("memory_evolution_edge requires fact_key, refs, and claim hashes")

        confidence = edge.get("confidence")
        try:
            confidence_value = None if confidence in (None, "") else float(confidence)
        except (TypeError, ValueError):
            confidence_value = None
        reason = str(edge.get("reason") or "")[:1000]
        metadata = edge.get("metadata") if isinstance(edge.get("metadata"), dict) else {}
        now = time.time()
        edge_id = str(
            edge.get("edge_id")
            or self._memory_evolution_edge_id(
                fact_key,
                from_ref_id,
                from_claim_hash,
                to_ref_id,
                to_claim_hash,
                relation,
            )
        )

        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO memory_evolution_edges (
                    edge_id, fact_key, from_ref_id, from_claim_hash,
                    to_ref_id, to_claim_hash, relation, confidence,
                    reason, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    fact_key,
                    from_ref_id,
                    from_claim_hash,
                    to_ref_id,
                    to_claim_hash,
                    relation
                ) DO UPDATE SET
                    confidence = excluded.confidence,
                    reason = excluded.reason,
                    metadata_json = excluded.metadata_json
                """,
                (
                    edge_id,
                    fact_key,
                    from_ref_id,
                    from_claim_hash,
                    to_ref_id,
                    to_claim_hash,
                    relation,
                    confidence_value,
                    reason,
                    json.dumps(metadata, ensure_ascii=False),
                    now,
                ),
            )
            conn.commit()
        return {
            "edge_id": edge_id,
            "fact_key": fact_key,
            "from_ref_id": from_ref_id,
            "from_claim_hash": from_claim_hash,
            "to_ref_id": to_ref_id,
            "to_claim_hash": to_claim_hash,
            "relation": relation,
            "confidence": confidence_value,
            "reason": reason,
            "metadata": metadata,
            "created_at": now,
        }

    def list_memory_evolution_states(
        self,
        *,
        node_ids: Optional[List[str]] = None,
        fact_key: str = "",
        statuses: Optional[List[str]] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        clean_node_ids = [str(item).strip() for item in (node_ids or []) if str(item).strip()]
        if clean_node_ids:
            placeholders = ", ".join("?" for _ in clean_node_ids)
            clauses.append(f"node_id IN ({placeholders})")
            params.extend(clean_node_ids)
        clean_fact_key = str(fact_key or "").strip().lower()
        if clean_fact_key:
            clauses.append("fact_key = ?")
            params.append(clean_fact_key)
        clean_statuses = [
            self._normalize_memory_evolution_status(item)
            for item in (statuses or [])
            if str(item or "").strip()
        ]
        if clean_statuses:
            placeholders = ", ".join("?" for _ in clean_statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(clean_statuses)
        sql = (
            "SELECT state_id, fact_key, node_id, ref_id, claim_text, status, "
            "superseded_by_node_id, superseded_by_ref_id, reason, confidence, "
            "created_at, updated_at FROM memory_evolution_state"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC, state_id ASC"
        clean_limit = int(limit or 0)
        if clean_limit > 0:
            sql += " LIMIT ?"
            params.append(clean_limit)

        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_memory_evolution_edges(
        self,
        *,
        fact_key: str = "",
        ref_id: str = "",
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        clean_fact_key = str(fact_key or "").strip().lower()
        clean_ref_id = str(ref_id or "").strip()
        if clean_fact_key:
            clauses.append("fact_key = ?")
            params.append(clean_fact_key)
        if clean_ref_id:
            clauses.append("(from_ref_id = ? OR to_ref_id = ?)")
            params.extend([clean_ref_id, clean_ref_id])

        sql = (
            "SELECT edge_id, fact_key, from_ref_id, from_claim_hash, "
            "to_ref_id, to_claim_hash, relation, confidence, reason, "
            "metadata_json, created_at FROM memory_evolution_edges"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, edge_id ASC"
        clean_limit = int(limit or 0)
        if clean_limit > 0:
            sql += " LIMIT ?"
            params.append(clean_limit)

        with closing(self._connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()

        results: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            raw_metadata = item.pop("metadata_json", "") or ""
            try:
                item["metadata"] = json.loads(raw_metadata) if raw_metadata else {}
            except json.JSONDecodeError:
                item["metadata"] = {}
            results.append(item)
        return results

    def delete_memory_evolution_states(self, node_ids: List[str]) -> int:
        clean_node_ids = [str(item).strip() for item in (node_ids or []) if str(item).strip()]
        if not clean_node_ids:
            return 0
        placeholders = ", ".join("?" for _ in clean_node_ids)
        refs: List[str] = []
        for node_id in clean_node_ids:
            refs.extend([f"memory_node:{node_id}", f"memory_index:{node_id}", node_id])
        ref_placeholders = ", ".join("?" for _ in refs)
        with closing(self._connect()) as conn:
            edge_cursor = conn.execute(
                f"""
                DELETE FROM memory_evolution_edges
                WHERE from_ref_id IN ({ref_placeholders})
                   OR to_ref_id IN ({ref_placeholders})
                """,
                refs + refs,
            )
            cursor = conn.execute(
                f"DELETE FROM memory_evolution_state WHERE node_id IN ({placeholders})",
                clean_node_ids,
            )
            conn.commit()
            return int(cursor.rowcount or 0) + int(edge_cursor.rowcount or 0)
