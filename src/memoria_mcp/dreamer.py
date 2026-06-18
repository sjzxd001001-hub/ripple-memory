"""Dreamer — background memory compaction and archival.

- Groups deleted memory entries by time window and content overlap
- Finds surviving compressed_summary nodes as archive targets
- Builds archive payloads with summaries and source metadata
- Stores archive blocks and patches survivor nodes with pointers
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from .archive import ArchiveStorage
from .config import MemoriaConfig
from .models import MemoryNode

logger = logging.getLogger("RippleMemory.Dreamer")


def _content_signature(node: MemoryNode) -> str:
    """SHA-256 of node content excluding volatile fields."""
    stable = {
        "id": node.id,
        "type": node.type.value,
        "description": node.summary.description,
        "locations": node.summary.locations,
        "people": node.summary.people,
        "origin_kind": node.origin_kind,
        "source_node_ids": node.source_node_ids,
    }
    raw = json.dumps(stable, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class Dreamer:
    """Background compaction: archives deleted entries into JSON blocks."""

    def __init__(self, config: MemoriaConfig, archive: ArchiveStorage):
        self.config = config
        self.archive = archive
        self._last_run_real: float = 0.0

    def should_run(
        self,
        deleted_count: int,
        *,
        last_activity_real: Optional[float] = None,
    ) -> bool:
        if not self.config.enable_dreamer:
            return False
        now = time.time()
        # Interval gate: must wait at least dreamer_interval_days since last run
        interval = self.config.dreamer_interval_days * 86400.0
        if now - self._last_run_real < interval:
            return False
        # Idle gate: if last_activity provided, must be idle for dreamer_idle_hours
        if last_activity_real is not None:
            idle_seconds = now - last_activity_real
            if idle_seconds < self.config.dreamer_idle_hours * 3600.0:
                return False
        # Batch gate: must have enough deleted entries
        if deleted_count < self.config.dreamer_batch_threshold:
            return False
        return True

    def run(
        self,
        deleted_entries: List[Dict[str, Any]],
        surviving_nodes: Dict[str, MemoryNode],
    ) -> Dict[str, Any]:
        """Execute dreamer compaction.

        Args:
            deleted_entries: List of dicts with at least {id, description, source_node_ids, timestamp}
            surviving_nodes: Map of node_id -> MemoryNode for compressed_summary nodes

        Returns:
            Report dict with archive results
        """
        if not deleted_entries:
            return {"archived": 0, "reason": "no entries"}

        self._last_run_real = time.time()
        max_rows = self.config.dreamer_max_rows_per_run
        entries = deleted_entries[:max_rows]

        # Group by time window
        groups = self._build_groups(entries)
        logger.info(f"Dreamer: {len(entries)} entries -> {len(groups)} groups")

        # Find survivors and archive
        archived = 0
        patches = []
        audit_events = []
        processed_node_ids: List[str] = []
        for group_id, group_entries in groups.items():
            survivor = self._find_survivor(group_entries, surviving_nodes)
            payload = self._build_payload(group_id, group_entries)
            archive_id = f"dreamer_{datetime.now().strftime('%Y%m%d')}"

            pointer = self.archive.store_archive_block(
                "dreamer_compaction",
                payload,
                archive_id=archive_id,
                metadata={
                    "mode": "dreamer",
                    "group_id": group_id,
                    "survivor_node_id": survivor.id if survivor else None,
                    "entry_count": len(group_entries),
                },
            )

            if survivor:
                # Formal pointer patch through engine method
                self.apply_archive_pointer_patch(
                    survivor, pointer,
                    tags=["dreamer", "compressed"],
                    reason="dreamer_archive_compaction",
                )
                patches.append({
                    "survivor_id": survivor.id,
                    "archive_id": archive_id,
                    "block_id": pointer.block_id,
                })
                audit_events.append({
                    "event": "dreamer_pointer_patch",
                    "node_id": survivor.id,
                    "archive_id": archive_id,
                })
            else:
                audit_events.append({
                    "event": "dreamer_skip_no_survivor",
                    "group_id": group_id,
                    "archive_id": archive_id,
                })

            archived += 1
            processed_node_ids.extend([
                str(entry.get("id") or "")
                for entry in group_entries
                if str(entry.get("id") or "").strip()
            ])

        return {
            "archived": archived,
            "groups": len(groups),
            "patches": patches,
            "processed_node_ids": sorted(set(processed_node_ids)),
            "max_rows_used": min(len(deleted_entries), max_rows),
            "audit_events": audit_events,
        }

    @staticmethod
    def apply_archive_pointer_patch(
        node: MemoryNode,
        pointer: Any,
        *,
        tags: List[str],
        reason: str = "dreamer",
    ) -> None:
        """Formal method to attach archive pointer to a survivor node.

        Per design blueprint: must go through formal engine method,
        never directly modify search_index or JSONL.
        """
        node.archive_pointer = pointer
        for tag in tags:
            if tag not in node.archive_tags:
                node.archive_tags.append(tag)
        logger.info(f"Archive pointer patched: {node.id} ({reason})")

    def _build_groups(
        self, entries: List[Dict[str, Any]]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Group entries by time window and content overlap."""
        buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        for entry in entries:
            ts = entry.get("timestamp", 0)
            try:
                dt = datetime.fromtimestamp(ts)
                window = dt.strftime("%Y%m%d-%H")
            except Exception:
                window = "unknown"
            key = f"{window}"
            buckets[key].append(entry)

        # Merge overlapping groups within each bucket
        merged: Dict[str, List[Dict[str, Any]]] = {}
        for bucket_key, bucket_entries in buckets.items():
            groups_in_bucket = self._merge_overlapping(bucket_entries)
            for i, group in enumerate(groups_in_bucket):
                group_id = f"{bucket_key}:{i}"
                merged[group_id] = group

        return merged

    def _merge_overlapping(
        self, entries: List[Dict[str, Any]]
    ) -> List[List[Dict[str, Any]]]:
        """Merge entries with shared source_node_ids or >= 35% token overlap."""
        groups: List[List[Dict[str, Any]]] = []
        used = set()

        for i, entry in enumerate(entries):
            if i in used:
                continue
            group = [entry]
            used.add(i)
            entry_tokens = self._tokenize(entry.get("description", ""))

            for j in range(i + 1, len(entries)):
                if j in used:
                    continue
                other = entries[j]
                # Check source_node_ids overlap
                src_a = set(entry.get("source_node_ids", []))
                src_b = set(other.get("source_node_ids", []))
                if src_a & src_b:
                    group.append(other)
                    used.add(j)
                    continue
                # Check token overlap
                other_tokens = self._tokenize(other.get("description", ""))
                if entry_tokens and other_tokens:
                    overlap = len(entry_tokens & other_tokens) / max(len(entry_tokens | other_tokens), 1)
                    if overlap >= 0.35:
                        group.append(other)
                        used.add(j)

            groups.append(group)
        return groups

    def _find_survivor(
        self,
        group_entries: List[Dict[str, Any]],
        surviving_nodes: Dict[str, MemoryNode],
    ) -> Optional[MemoryNode]:
        """Find best compressed_summary node to attach archive pointer."""
        if not surviving_nodes:
            return None

        group_source_ids: Set[str] = set()
        for entry in group_entries:
            group_source_ids.update(entry.get("source_node_ids", []))

        best_score = (-1, 0.0, 0.0, 0.0, 0.0)
        best_node = None

        for node in surviving_nodes.values():
            node_sources = set(node.source_node_ids)
            overlap = len(group_source_ids & node_sources)
            if overlap == 0:
                continue
            coverage = overlap / max(len(group_source_ids), 1)
            score = (overlap, coverage, node.importance, node.strength, node.timestamp)
            if score > best_score:
                best_score = score
                best_node = node

        return best_node

    def _build_payload(
        self, group_id: str, entries: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Build archive payload from group entries."""
        summaries = []
        source_ids = []
        samples = []

        for entry in entries:
            desc = entry.get("description", "")
            if desc and desc not in summaries:
                summaries.append(desc)
            source_ids.extend(entry.get("source_node_ids", []))
            if len(samples) < 6:
                samples.append({
                    "id": entry.get("id", ""),
                    "description": desc[:200],
                    "timestamp": entry.get("timestamp", 0),
                })

        return {
            "group_id": group_id,
            "summary": " | ".join(summaries[:3])[:1000],
            "source_node_ids": list(set(source_ids)),
            "source_count": len(entries),
            "sample_memories": samples,
        }

    @staticmethod
    def _tokenize(text: str) -> Set[str]:
        tokens = set()
        for char_group in text.lower().split():
            if len(char_group) > 1:
                tokens.add(char_group)
            for i in range(len(char_group) - 1):
                tokens.add(char_group[i:i+2])
        return tokens
