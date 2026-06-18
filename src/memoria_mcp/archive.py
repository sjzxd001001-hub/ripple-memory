"""JSON Archive Storage — frozen-history side rail for dual-track memory architecture.

- Snapshots: core state snapshots with tiered retention
- Content blocks: thick archive blocks with checksum validation
- Memory stream: JSONL append-only log with byte-offset addressing
- Exports: manual JSON exports

Directory structure:
  {data_dir}/archives/
    snapshots/{YYYY}/{MM}/snapshot_{timestamp}_{suffix}.json
    content/{archive_id}/{block_id}.json
    streams/{YYYY}/{MM}/memories_{YYYYMMDD}.jsonl
    exports/
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import ArchiveBlock, ArchivePointer

logger = logging.getLogger("RippleMemory.Archive")

VALID_CONTENT_KINDS = frozenset({
    "context_block", "event_cluster", "relation_arc", "archive_volume",
    "compressed_summary", "dreamer_compaction",
})


class ArchiveStorage:
    """JSON-based frozen-history archive. No SQLite dependency."""

    def __init__(self, data_dir: str):
        self.base = Path(data_dir) / "archives"
        self.snapshots_dir = self.base / "snapshots"
        self.content_dir = self.base / "content"
        self.streams_dir = self.base / "streams"
        self.exports_dir = self.base / "exports"
        for d in (self.snapshots_dir, self.content_dir, self.streams_dir, self.exports_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._stream_io_lock = threading.Lock()

    # ========== Snapshots ==========

    def archive_core_snapshot(
        self,
        state: Dict[str, Any],
        *,
        milestone: bool = False,
        label: str = "",
    ) -> str:
        now = datetime.now()
        ts = int(now.timestamp() * 1000)
        suffix = label or ("milestone" if milestone else "snapshot")
        rel = f"{now.year}/{now.month:02d}"
        dir_path = self.snapshots_dir / rel
        dir_path.mkdir(parents=True, exist_ok=True)
        filename = f"snapshot_{ts}_{suffix}.json"
        filepath = dir_path / filename

        envelope = {
            "archive_type": "core_snapshot",
            "created_at": now.isoformat(),
            "milestone": milestone,
            "label": label,
            "state": state,
        }
        filepath.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")

        if not milestone:
            self._prune_archives()

        return str(filepath.relative_to(self.base))

    def list_archives(self, limit: int = 50) -> List[Dict[str, Any]]:
        results = []
        for dirpath, _, filenames in os.walk(self.snapshots_dir):
            for fn in sorted(filenames, reverse=True):
                if not fn.endswith(".json"):
                    continue
                fp = Path(dirpath) / fn
                try:
                    data = json.loads(fp.read_text(encoding="utf-8"))
                    results.append({
                        "path": str(fp.relative_to(self.base)),
                        "created_at": data.get("created_at", ""),
                        "milestone": data.get("milestone", False),
                        "label": data.get("label", ""),
                    })
                except Exception:
                    continue
                if len(results) >= limit:
                    return results
        return results

    def export_to_json(self, export_path: str) -> str:
        archives = self.list_archives(limit=1)
        if not archives:
            return ""
        latest_path = self.base / archives[0]["path"]
        dest = self.exports_dir / export_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(latest_path.read_text(encoding="utf-8"), encoding="utf-8")
        return str(dest.relative_to(self.base))

    def _prune_archives(self):
        """Tiered retention: 7 daily, 8 weekly, 12 monthly, then yearly."""
        now = datetime.now()
        keep: Dict[str, str] = {}  # bucket -> newest filepath

        for dirpath, _, filenames in os.walk(self.snapshots_dir):
            for fn in sorted(filenames):
                if not fn.endswith(".json"):
                    continue
                fp = Path(dirpath) / fn
                try:
                    data = json.loads(fp.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if data.get("milestone"):
                    continue  # never prune milestones

                created = data.get("created_at", "")
                try:
                    dt = datetime.fromisoformat(created)
                except Exception:
                    continue

                age = now - dt
                if age < timedelta(days=7):
                    bucket = f"day:{dt.strftime('%Y-%m-%d')}"
                elif age < timedelta(weeks=8):
                    iso = dt.isocalendar()
                    bucket = f"week:{iso[0]}-W{iso[1]:02d}"
                elif age < timedelta(days=365):
                    bucket = f"month:{dt.strftime('%Y-%m')}"
                else:
                    bucket = f"year:{dt.year}"

                keep[bucket] = str(fp)

        all_snapshots = set()
        for dirpath, _, filenames in os.walk(self.snapshots_dir):
            for fn in filenames:
                if fn.endswith(".json"):
                    all_snapshots.add(str(Path(dirpath) / fn))

        to_delete = all_snapshots - set(keep.values())
        for fp in to_delete:
            try:
                os.remove(fp)
            except OSError:
                pass

    # ========== Archive Blocks ==========

    def store_archive_block(
        self,
        content_kind: str,
        payload: Dict[str, Any],
        *,
        archive_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ArchivePointer:
        if content_kind not in VALID_CONTENT_KINDS:
            logger.warning(f"Unknown content_kind: {content_kind}")

        block_id = f"{content_kind}_{int(time.time() * 1000)}"
        payload_bytes = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        checksum = hashlib.sha256(payload_bytes).hexdigest()

        pointer = ArchivePointer(
            archive_id=archive_id,
            block_id=block_id,
            content_kind=content_kind,
            version=1,
            checksum=checksum,
        )

        block = ArchiveBlock(
            pointer=pointer,
            payload=payload,
            created_at=datetime.now().isoformat(),
            metadata=metadata or {},
        )

        dir_path = self.content_dir / archive_id
        dir_path.mkdir(parents=True, exist_ok=True)
        filepath = dir_path / f"{block_id}.json"
        filepath.write_text(
            json.dumps(block.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return pointer

    def load_archive_block(self, pointer: ArchivePointer) -> ArchiveBlock:
        filepath = self.content_dir / pointer.archive_id / f"{pointer.block_id}.json"
        if not filepath.exists():
            raise FileNotFoundError(f"Archive block not found: {filepath}")

        data = json.loads(filepath.read_text(encoding="utf-8"))
        block = ArchiveBlock.from_dict(data)

        # Validate pointer fields
        if block.pointer.archive_id != pointer.archive_id:
            raise ValueError(f"Archive ID mismatch: {block.pointer.archive_id} != {pointer.archive_id}")
        if block.pointer.block_id != pointer.block_id:
            raise ValueError(f"Block ID mismatch: {block.pointer.block_id} != {pointer.block_id}")
        if block.pointer.content_kind != pointer.content_kind:
            raise ValueError(f"Content kind mismatch: {block.pointer.content_kind} != {pointer.content_kind}")

        # Validate checksum
        if pointer.checksum:
            payload_bytes = json.dumps(block.payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
            actual = hashlib.sha256(payload_bytes).hexdigest()
            if actual != pointer.checksum:
                raise ValueError(f"Checksum mismatch: {actual} != {pointer.checksum}")

        return block

    def list_archive_block_pointers(
        self,
        archive_id: str,
        *,
        content_kind: Optional[str] = None,
    ) -> List[ArchivePointer]:
        dir_path = self.content_dir / archive_id
        if not dir_path.exists():
            return []
        pointers = []
        for fn in sorted(dir_path.iterdir()):
            if not fn.name.endswith(".json"):
                continue
            try:
                data = json.loads(fn.read_text(encoding="utf-8"))
                block = ArchiveBlock.from_dict(data)
                if content_kind and block.pointer.content_kind != content_kind:
                    continue
                pointers.append(block.pointer)
            except Exception:
                continue
        return pointers

    # ========== Memory Stream (JSONL) ==========

    def append_memory_stream_entry(
        self,
        payload: Dict[str, Any],
        *,
        flush: bool = True,
    ) -> Dict[str, Any]:
        now = datetime.now()
        rel = f"{now.year}/{now.month:02d}"
        dir_path = self.streams_dir / rel
        dir_path.mkdir(parents=True, exist_ok=True)
        filename = f"memories_{now.strftime('%Y%m%d')}.jsonl"
        filepath = dir_path / filename

        entry = {
            **payload,
            "stream_recorded_at": now.isoformat(),
            "stream_schema": "memory_jsonl_v1",
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        line_bytes = line.encode("utf-8")

        with self._stream_io_lock:
            with open(filepath, "ab") as f:
                offset = f.tell()
                f.write(line_bytes)
                if flush:
                    f.flush()
                    os.fsync(f.fileno())

        return {
            "json_file": str(filepath.relative_to(self.base)),
            "json_offset": offset,
            "created_at": now.isoformat(),
            "size_bytes": len(line_bytes),
        }

    def read_memory_stream_entry(self, json_file: str, json_offset: int) -> Dict[str, Any]:
        filepath = self.base / json_file
        with open(filepath, "rb") as f:
            if json_offset > 0:
                f.seek(json_offset - 1)
                prev = f.read(1)
                if prev != b"\n":
                    raise ValueError(f"Offset {json_offset} is not at a line boundary")
            else:
                f.seek(0)
            line = f.readline()
        return json.loads(line.decode("utf-8"))

    def read_memory_stream_entries(
        self,
        json_file: str,
        start_offset: int = 0,
    ) -> List[Dict[str, Any]]:
        filepath = self.base / json_file
        entries = []
        with open(filepath, "rb") as f:
            f.seek(start_offset)
            while True:
                offset = f.tell()
                line = f.readline()
                if not line:
                    break
                try:
                    entry = json.loads(line.decode("utf-8"))
                    entry["_json_file"] = json_file
                    entry["_json_offset"] = offset
                    entries.append(entry)
                except json.JSONDecodeError:
                    continue
        return entries

    def purge_memory_stream_entries(self, node_ids: List[str]) -> Dict[str, Any]:
        """Remove user-deleted memory content from JSONL streams.

        JSONL stream offsets are used by the search index, so this redacts matching
        lines in place with equal-length tombstones instead of shrinking files.
        """
        targets = {str(node_id) for node_id in node_ids if str(node_id).strip()}
        if not targets:
            return {"purged": 0, "files_touched": 0}

        purged = 0
        files_touched = 0
        for json_file in self.list_memory_stream_files():
            filepath = self.base / json_file
            changed = False
            with self._stream_io_lock:
                try:
                    with open(filepath, "r+b") as f:
                        while True:
                            offset = f.tell()
                            line = f.readline()
                            if not line:
                                break
                            try:
                                entry = json.loads(line.decode("utf-8"))
                            except json.JSONDecodeError:
                                continue
                            if str(entry.get("id") or "") not in targets:
                                continue
                            tombstone = {
                                "purged": True,
                                "stream_schema": "memory_jsonl_v1",
                                "purged_at": datetime.now().isoformat(),
                            }
                            raw = (json.dumps(tombstone, ensure_ascii=False) + "\n").encode("utf-8")
                            if len(raw) > len(line):
                                logger.warning(f"Cannot in-place purge short JSONL record in {json_file} at {offset}")
                                continue
                            if raw.endswith(b"\n"):
                                raw = raw[:-1] + (b" " * (len(line) - len(raw))) + b"\n"
                            f.seek(offset)
                            f.write(raw)
                            purged += 1
                            changed = True
                    if changed:
                        files_touched += 1
                except FileNotFoundError:
                    continue
        return {"purged": purged, "files_touched": files_touched}

    def list_memory_stream_files(self) -> List[str]:
        files = []
        for dirpath, _, filenames in os.walk(self.streams_dir):
            for fn in sorted(filenames):
                if fn.endswith(".jsonl"):
                    fp = Path(dirpath) / fn
                    files.append(str(fp.relative_to(self.base)))
        return files
