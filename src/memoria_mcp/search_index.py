"""SQLite Search Index - auxiliary retrieval rail for dual-track storage.

- SQLite stores runtime retrieval metadata: keywords, ranking signals, dirty flags,
  JSONL pointers, and logical delete markers.
- JSONL/archive files store the frozen full-content rail.
- Rebuilds must preserve JSONL pointers and delete state so old口径 does not
  resurrect after process restart.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("RippleMemory.SearchIndex")

try:
    import jieba

    jieba.setLogLevel(logging.WARNING)
    _HAS_JIEBA = True
except ImportError:
    _HAS_JIEBA = False

_STOP_WORDS = frozenset({
    "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这", "他", "她", "它", "们", "那", "被",
    "把", "对", "用", "从", "与", "等", "之", "及",
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "and", "but", "or",
    "not", "no", "nor", "so", "if", "then", "than", "too", "very",
})


def extract_keywords(text: str, max_keywords: int = 10) -> List[str]:
    """Extract keywords from text using jieba when available, else regex."""
    if not text:
        return []

    if _HAS_JIEBA:
        words = list(jieba.cut(text))
    else:
        words = re.findall(r"[a-zA-Z_]\w+", text)
        for segment in re.findall(r"[\u4e00-\u9fff]+", text):
            for i in range(len(segment) - 1):
                words.append(segment[i:i + 2])

    filtered = [word.strip().lower() for word in words if word.strip() and len(word.strip()) > 1]
    filtered = [word for word in filtered if word not in _STOP_WORDS]

    freq: Dict[str, int] = {}
    for word in filtered:
        freq[word] = freq.get(word, 0) + 1

    sorted_words = sorted(freq.items(), key=lambda item: item[1], reverse=True)
    return [word for word, _ in sorted_words[:max_keywords]]


def compute_content_signature(node_dict: Dict[str, Any]) -> str:
    """SHA-256 of node content excluding volatile fields."""
    stable = {
        "id": node_dict.get("id", ""),
        "type": node_dict.get("type", ""),
        "description": node_dict.get("summary", {}).get("description", ""),
        "locations": node_dict.get("summary", {}).get("locations", []),
        "people": node_dict.get("summary", {}).get("people", []),
        "origin_kind": node_dict.get("origin_kind", ""),
        "source_node_ids": node_dict.get("source_node_ids", []),
    }
    raw = json.dumps(stable, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class SearchIndex:
    """SQLite-based search index with dual dirty tracking."""

    _LOCK_TIMEOUT = 8.0

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
        self._ensure_table()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def _ensure_table(self):
        with self._lock:
            conn = self._get_conn()
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS search_index (
                    node_id TEXT PRIMARY KEY,
                    keywords TEXT,
                    importance REAL,
                    strength REAL,
                    timestamp INTEGER,
                    layer TEXT,
                    content_signature TEXT,
                    json_file TEXT,
                    json_offset INTEGER,
                    content_dirty INTEGER DEFAULT 1,
                    index_dirty INTEGER DEFAULT 1,
                    deleted INTEGER DEFAULT 0,
                    deleted_reason TEXT,
                    deleted_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_search_keywords ON search_index(keywords);
                CREATE INDEX IF NOT EXISTS idx_search_content_dirty ON search_index(content_dirty, deleted);
                CREATE INDEX IF NOT EXISTS idx_search_index_dirty ON search_index(index_dirty, deleted);
                CREATE INDEX IF NOT EXISTS idx_search_deleted ON search_index(deleted);
            """)
            try:
                conn.execute("ALTER TABLE search_index ADD COLUMN deleted_at REAL")
                conn.commit()
            except Exception:
                pass

    def upsert_node(
        self,
        node_id: str,
        description: str,
        importance: float,
        strength: float,
        timestamp: int,
        layer: str,
        *,
        content_dirty: bool = True,
        index_dirty: bool = True,
    ):
        """Upsert an active node into the search index."""
        keywords = ", ".join(extract_keywords(description))
        with self._lock:
            conn = self._get_conn()
            conn.execute("""
                INSERT INTO search_index (node_id, keywords, importance, strength, timestamp, layer,
                    content_dirty, index_dirty, deleted)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(node_id) DO UPDATE SET
                    keywords = excluded.keywords,
                    importance = excluded.importance,
                    strength = excluded.strength,
                    timestamp = excluded.timestamp,
                    layer = excluded.layer,
                    content_dirty = CASE WHEN excluded.content_dirty = 1 THEN 1 ELSE search_index.content_dirty END,
                    index_dirty = CASE WHEN excluded.index_dirty = 1 THEN 1 ELSE search_index.index_dirty END,
                    deleted = 0,
                    deleted_reason = NULL,
                    deleted_at = NULL
            """, (node_id, keywords, importance, strength, timestamp, layer,
                  1 if content_dirty else 0, 1 if index_dirty else 0))
            conn.commit()

    def mark_content_synced(self, node_id: str, json_file: str, json_offset: int, content_signature: str):
        """Mark a node as content-synced with JSONL location."""
        with self._lock:
            conn = self._get_conn()
            conn.execute("""
                UPDATE search_index
                SET content_dirty = 0, json_file = ?, json_offset = ?, content_signature = ?
                WHERE node_id = ?
            """, (json_file, json_offset, content_signature, node_id))
            conn.commit()

    def mark_index_synced(self, node_id: str):
        """Mark a node as index-synced."""
        with self._lock:
            conn = self._get_conn()
            conn.execute("UPDATE search_index SET index_dirty = 0 WHERE node_id = ?", (node_id,))
            conn.commit()

    def mark_deleted(self, node_ids: List[str], reason: str = "compression"):
        """Logical delete for compressed or superseded nodes."""
        if not node_ids:
            return
        with self._lock:
            conn = self._get_conn()
            now = time.time()
            placeholders = ",".join("?" * len(node_ids))
            conn.execute(f"""
                UPDATE search_index SET deleted = 1, deleted_reason = ?, deleted_at = ?
                WHERE node_id IN ({placeholders})
            """, [reason, now] + node_ids)
            conn.commit()

    def purge_deleted(self, node_ids: Optional[List[str]] = None):
        """Hard-delete processed deleted entries from the retrieval index."""
        with self._lock:
            conn = self._get_conn()
            if node_ids:
                placeholders = ",".join("?" * len(node_ids))
                conn.execute(f"DELETE FROM search_index WHERE deleted = 1 AND node_id IN ({placeholders})", node_ids)
            else:
                conn.execute("DELETE FROM search_index WHERE deleted = 1")
            conn.commit()

    def purge_nodes(self, node_ids: List[str]) -> int:
        """Hard-delete any index rows for explicit user deletion."""
        node_ids = [str(node_id) for node_id in node_ids if str(node_id).strip()]
        if not node_ids:
            return 0
        with self._lock:
            conn = self._get_conn()
            placeholders = ",".join("?" * len(node_ids))
            cur = conn.execute(f"DELETE FROM search_index WHERE node_id IN ({placeholders})", node_ids)
            conn.commit()
            return int(cur.rowcount or 0)

    def get_content_dirty_nodes(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get nodes needing JSONL sync."""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute("""
                SELECT node_id, keywords, importance, strength, timestamp, layer
                FROM search_index
                WHERE content_dirty = 1 AND deleted = 0
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(row) for row in rows]

    def get_index_dirty_nodes(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get nodes needing index-only sync."""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute("""
                SELECT node_id, keywords, importance, strength, timestamp, layer
                FROM search_index
                WHERE index_dirty = 1 AND deleted = 0
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(row) for row in rows]

    def get_deleted_entries(self, limit: int = 1000) -> List[Dict[str, Any]]:
        """Get deleted entries with JSONL locations for Dreamer."""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute("""
                SELECT node_id, keywords, json_file, json_offset, deleted_reason, deleted_at
                FROM search_index
                WHERE deleted = 1 AND json_file IS NOT NULL
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(row) for row in rows]

    def get_deleted_count(self) -> int:
        """Count deleted entries for Dreamer trigger check."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute("SELECT COUNT(*) as cnt FROM search_index WHERE deleted = 1").fetchone()
            return int(row["cnt"])

    def query_keywords(self, keywords: List[str], top_k: int = 10) -> List[Dict[str, Any]]:
        """Query by keywords, sorted by importance*0.6 + strength*0.4."""
        if not keywords:
            return []
        with self._lock:
            conn = self._get_conn()
            conditions = " OR ".join(["keywords LIKE ?" for _ in keywords])
            params = [f"%{keyword}%" for keyword in keywords]
            rows = conn.execute(f"""
                SELECT node_id, keywords, importance, strength, timestamp, layer
                FROM search_index
                WHERE deleted = 0 AND ({conditions})
                ORDER BY (importance * 0.6 + strength * 0.4) DESC, timestamp DESC
                LIMIT ?
            """, params + [top_k]).fetchall()
            return [dict(row) for row in rows]

    def has_content_signature(self, signature: str) -> bool:
        """Check if a content signature already exists for an active row."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT 1 FROM search_index WHERE content_signature = ? AND deleted = 0",
                (signature,),
            ).fetchone()
            return row is not None

    def get_stats(self) -> Dict[str, int]:
        """Get index statistics."""
        with self._lock:
            conn = self._get_conn()
            total = conn.execute("SELECT COUNT(*) as c FROM search_index").fetchone()["c"]
            deleted = conn.execute("SELECT COUNT(*) as c FROM search_index WHERE deleted = 1").fetchone()["c"]
            content_dirty = conn.execute(
                "SELECT COUNT(*) as c FROM search_index WHERE content_dirty = 1 AND deleted = 0"
            ).fetchone()["c"]
            index_dirty = conn.execute(
                "SELECT COUNT(*) as c FROM search_index WHERE index_dirty = 1 AND deleted = 0"
            ).fetchone()["c"]
            return {
                "total": int(total),
                "active": int(total - deleted),
                "deleted": int(deleted),
                "content_dirty": int(content_dirty),
                "index_dirty": int(index_dirty),
            }

    def rebuild_from_nodes(self, nodes: Dict[str, Any]):
        """Incrementally update index text while preserving JSONL pointers and delete state.

        Only processes nodes whose description keywords differ from the current
        index, avoiding a full DELETE + re-INSERT on every graph reload.
        """
        if not self._lock.acquire(timeout=self._LOCK_TIMEOUT):
            logger.warning("rebuild_from_nodes: lock timeout after %.1fs, skipping", self._LOCK_TIMEOUT)
            return
        try:
            conn = self._get_conn()
            rows = conn.execute("""
                SELECT node_id, keywords, content_signature, json_file, json_offset,
                    content_dirty, index_dirty, deleted, deleted_reason, deleted_at
                FROM search_index
            """).fetchall()
            existing = {str(row["node_id"]): dict(row) for row in rows}

            node_ids_in_graph = set(nodes.keys())
            existing_active_ids = {nid for nid, r in existing.items() if not r.get("deleted")}

            # Mark nodes removed from graph as deleted
            removed_ids = existing_active_ids - node_ids_in_graph
            if removed_ids:
                now = time.time()
                placeholders = ",".join("?" * len(removed_ids))
                conn.execute(
                    f"UPDATE search_index SET deleted=1, deleted_reason='graph_pruned', deleted_at=? "
                    f"WHERE node_id IN ({placeholders}) AND deleted=0",
                    [now] + list(removed_ids),
                )

            # Upsert only changed nodes
            updated = 0
            for node_id, node in nodes.items():
                new_keywords = ", ".join(extract_keywords(node.summary.description))
                old = existing.get(str(node_id))
                if old and not old.get("deleted") and old.get("keywords") == new_keywords:
                    # Update mutable signals and clear index_dirty
                    conn.execute(
                        "UPDATE search_index SET importance=?, strength=?, timestamp=?, layer=?, index_dirty=0 "
                        "WHERE node_id=?",
                        (node.importance, node.strength, node.timestamp, node.layer.value, node_id),
                    )
                    continue

                json_file = old.get("json_file") if old else None
                json_offset = old.get("json_offset") if old else None
                deleted = int((old or {}).get("deleted") or 0)
                content_dirty = (old or {}).get("content_dirty")
                if content_dirty is None:
                    content_dirty = 0 if json_file else 1

                conn.execute("""
                    INSERT INTO search_index (node_id, keywords, importance, strength, timestamp, layer,
                        content_signature, json_file, json_offset, content_dirty, index_dirty,
                        deleted, deleted_reason, deleted_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(node_id) DO UPDATE SET
                        keywords=excluded.keywords,
                        importance=excluded.importance,
                        strength=excluded.strength,
                        timestamp=excluded.timestamp,
                        layer=excluded.layer,
                        content_signature=excluded.content_signature,
                        json_file=excluded.json_file,
                        json_offset=excluded.json_offset,
                        content_dirty=excluded.content_dirty,
                        index_dirty=0,
                        deleted=excluded.deleted,
                        deleted_reason=excluded.deleted_reason,
                        deleted_at=excluded.deleted_at
                """, (
                    node_id,
                    new_keywords,
                    node.importance,
                    node.strength,
                    node.timestamp,
                    node.layer.value,
                    (old or {}).get("content_signature"),
                    json_file,
                    json_offset,
                    int(content_dirty),
                    0,
                    deleted,
                    (old or {}).get("deleted_reason") if deleted else None,
                    (old or {}).get("deleted_at") if deleted else None,
                ))
                updated += 1

            conn.commit()
            logger.info("Incremental search index update: %d changed, %d removed, %d total",
                        updated, len(removed_ids), len(nodes))
        finally:
            self._lock.release()

    def close(self):
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None
