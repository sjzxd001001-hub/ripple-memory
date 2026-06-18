"""Core data models for the memory graph."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class MemoryType(str, Enum):
    EVENT = "event"
    FACT = "fact"
    RULE = "rule"
    PREFERENCE = "preference"
    PROCEDURAL = "procedural"
    SEMANTIC = "semantic"
    CAUSAL = "causal"
    CODE_PATTERN = "code_pattern"
    DEBUG_INSIGHT = "debug_insight"
    ARCH_DECISION = "arch_decision"
    PLOT = "plot"
    CHARACTER = "character"
    LOCATION = "location"


class MemoryLayer(str, Enum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


@dataclass
class CausalLink:
    target: str
    weight: float
    delay: int
    curvature: float = 1.0
    phase: float = 0.0
    last_used_tick: int = 0
    hebbian_strength: float = 0.5

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target": self.target,
            "weight": self.weight,
            "delay": self.delay,
            "curvature": self.curvature,
            "phase": self.phase,
            "last_used_tick": self.last_used_tick,
            "hebbian_strength": self.hebbian_strength,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CausalLink:
        return cls(
            target=data["target"],
            weight=data["weight"],
            delay=data["delay"],
            curvature=data.get("curvature", 1.0),
            phase=data.get("phase", 0.0),
            last_used_tick=data.get("last_used_tick", 0),
            hebbian_strength=data.get("hebbian_strength", 0.5),
        )

    def update_hebbian(self, success: bool, learning_rate: float):
        if success:
            self.hebbian_strength = min(1.0, self.hebbian_strength + learning_rate)
        else:
            self.hebbian_strength = max(0.0, self.hebbian_strength - learning_rate * 0.5)


@dataclass
class Summary:
    locations: List[str] = field(default_factory=list)
    people: List[str] = field(default_factory=list)
    description: str = ""
    causal_chain: List[str] = field(default_factory=list)
    time_range: List[int] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Summary:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ArchivePointer:
    archive_id: str
    block_id: str
    content_kind: str
    version: int = 1
    checksum: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "archive_id": self.archive_id,
            "block_id": self.block_id,
            "content_kind": self.content_kind,
            "version": self.version,
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ArchivePointer:
        return cls(
            archive_id=str(data["archive_id"]),
            block_id=str(data["block_id"]),
            content_kind=str(data["content_kind"]),
            version=int(data.get("version", 1)),
            checksum=str(data.get("checksum", "")),
        )


@dataclass
class ArchiveBlock:
    """Archive block stored in the archive rail."""
    pointer: ArchivePointer
    payload: Dict[str, Any]
    created_at: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pointer": self.pointer.to_dict(),
            "payload": self.payload,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ArchiveBlock:
        return cls(
            pointer=ArchivePointer.from_dict(data["pointer"]),
            payload=dict(data.get("payload") or {}),
            created_at=str(data["created_at"]),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class MemoryNode:
    id: str
    timestamp: int
    type: MemoryType
    importance: float
    strength: float
    access_count: int = 0
    last_access_tick: float = 0.0
    summary: Summary = field(default_factory=Summary)
    links: List[CausalLink] = field(default_factory=list)
    vector: Optional[List[float]] = None
    layer: MemoryLayer = MemoryLayer.WARM
    muscle: bool = False
    is_muscle_memory: bool = False
    embedding: Optional[Any] = None
    auto_action_template: Optional[str] = None
    parent_ids: List[str] = field(default_factory=list)
    attractor_score: float = 0.0
    origin_kind: str = "primary"
    source_node_ids: List[str] = field(default_factory=list)
    archive_pointer: Optional[ArchivePointer] = None
    archive_tags: List[str] = field(default_factory=list)
    created_at_real: Optional[float] = None
    last_accessed_at_real: Optional[float] = None
    last_consolidated_at_real: Optional[float] = None

    def __post_init__(self):
        if self.muscle:
            self.is_muscle_memory = True

    def access(self, tick: float, current_real_time: Optional[float] = None):
        self.access_count += 1
        self.last_access_tick = float(tick)
        real_time = current_real_time or time.time()
        if self.created_at_real is None:
            self.created_at_real = real_time
        if self.last_accessed_at_real is None:
            self.last_accessed_at_real = real_time

    def latest_real_access_anchor(self) -> Optional[float]:
        if self.last_accessed_at_real is not None:
            return self.last_accessed_at_real
        return self.created_at_real

    def latest_real_write_anchor(self) -> Optional[float]:
        return self.created_at_real or self.latest_real_access_anchor()

    def real_access_age_seconds(self, current_real_time: Optional[float]) -> Optional[float]:
        anchor = self.latest_real_access_anchor()
        if anchor is None or current_real_time is None:
            return None
        return max(0.0, current_real_time - anchor)

    def real_write_age_seconds(self, current_real_time: Optional[float]) -> Optional[float]:
        anchor = self.latest_real_write_anchor()
        if anchor is None or current_real_time is None:
            return None
        return max(0.0, current_real_time - anchor)

    def is_phase_memory(self) -> bool:
        return self.origin_kind == "phase_transition"

    def should_raise_archive_attention(self) -> bool:
        if self.muscle or self.is_muscle_memory:
            return False
        if self.strength < 0.3 and self.importance >= 0.7:
            return True
        if self.access_count >= 5 and self.strength < 0.4:
            return True
        return False

    def promote_to_muscle_memory(self, curvature_muscle: float = 2.0) -> None:
        self.muscle = True
        self.is_muscle_memory = True
        self.strength = 1.0
        self.layer = MemoryLayer.HOT
        for link in self.links:
            link.curvature = curvature_muscle
            link.weight = 1.0
            link.hebbian_strength = 1.0

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "id": self.id,
            "timestamp": self.timestamp,
            "type": self.type.value,
            "importance": self.importance,
            "strength": self.strength,
            "access_count": self.access_count,
            "last_access_tick": self.last_access_tick,
            "summary": self.summary.to_dict(),
            "links": [link.to_dict() for link in self.links],
            "vector": self.vector,
            "layer": self.layer.value,
            "muscle": self.muscle,
            "is_muscle_memory": self.is_muscle_memory,
            "auto_action_template": self.auto_action_template,
            "parent_ids": self.parent_ids,
            "attractor_score": self.attractor_score,
            "origin_kind": self.origin_kind,
            "source_node_ids": self.source_node_ids,
            "archive_tags": list(self.archive_tags),
        }
        if self.created_at_real is not None:
            d["created_at_real"] = self.created_at_real
        if self.last_accessed_at_real is not None:
            d["last_accessed_at_real"] = self.last_accessed_at_real
        if self.last_consolidated_at_real is not None:
            d["last_consolidated_at_real"] = self.last_consolidated_at_real
        if self.archive_pointer is not None:
            d["archive_pointer"] = self.archive_pointer.to_dict()
        if self.embedding is not None:
            d["embedding"] = self.embedding if isinstance(self.embedding, list) else list(self.embedding)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> MemoryNode:
        node = cls(
            id=data["id"],
            timestamp=data["timestamp"],
            type=MemoryType(data["type"]),
            importance=data["importance"],
            strength=data["strength"],
            access_count=data.get("access_count", 0),
            last_access_tick=data.get("last_access_tick", 0),
            summary=Summary.from_dict(data.get("summary", {})),
            vector=data.get("vector"),
            layer=MemoryLayer(data.get("layer", "warm")),
            muscle=data.get("muscle", False),
            created_at_real=data.get("created_at_real"),
            last_accessed_at_real=data.get("last_accessed_at_real"),
            last_consolidated_at_real=data.get("last_consolidated_at_real"),
        )
        for link_data in data.get("links", []):
            node.links.append(CausalLink.from_dict(link_data))
        node.is_muscle_memory = data.get("is_muscle_memory", node.muscle)
        node.auto_action_template = data.get("auto_action_template")
        node.parent_ids = data.get("parent_ids", [])
        node.attractor_score = data.get("attractor_score", 0.0)
        node.origin_kind = data.get("origin_kind", "primary")
        node.source_node_ids = data.get("source_node_ids", [])
        archive_pointer = data.get("archive_pointer")
        if archive_pointer:
            node.archive_pointer = ArchivePointer.from_dict(archive_pointer)
        node.archive_tags = data.get("archive_tags", [])
        emb = data.get("embedding")
        if emb is not None:
            node.embedding = emb
        return node
