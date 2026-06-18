"""MemoryGraph — directed graph memory engine with cognitive-inspired mechanisms.

Core mechanisms:
- Strength-based three-layer classification (HOT/WARM/COLD)
- Pulse propagation along causal links with delay ticks
- Hebbian learning on link strengths
- Attractor detection and auto-solidification to muscle memory
- Phase transition detection
- Semantic resonance
- Hybrid retrieval (BM25 + vector + RRF fusion)
- Compression of cold/warm nodes into summary nodes
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import threading
import time
from collections import OrderedDict, defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from .bm25 import tokenize_retrieval_text
from .config import MemoriaConfig
from .memory_index import MemoryIndex
from .models import CausalLink, MemoryLayer, MemoryNode, MemoryType, Summary

logger = logging.getLogger("RippleMemory.Graph")
_RNG = random.SystemRandom()

# Lazy embedding model — only import when actually needed
_embedding_model = None
_EMBEDDING_AVAILABLE = None  # None = not checked yet
_SentenceTransformer = None
_np = None
_embedding_load_lock = threading.Lock()
_embedding_loading = False


def _configure_embedding_logging():
    """Reduce model-library chatter without touching process stdout/stderr."""
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
    logging.getLogger("transformers").setLevel(logging.ERROR)
    try:
        from transformers.utils import logging as transformers_logging
        transformers_logging.set_verbosity_error()
        transformers_logging.disable_progress_bar()
    except Exception:
        pass


def _check_embedding_available() -> bool:
    global _EMBEDDING_AVAILABLE, _SentenceTransformer, _np
    if _EMBEDDING_AVAILABLE is not None:
        return _EMBEDDING_AVAILABLE
    try:
        _configure_embedding_logging()
        from sentence_transformers import SentenceTransformer
        import numpy as np
        _SentenceTransformer = SentenceTransformer
        _np = np
        _EMBEDDING_AVAILABLE = True
    except ImportError:
        _EMBEDDING_AVAILABLE = False
        _SentenceTransformer = None
        _np = None
    return _EMBEDDING_AVAILABLE


def _get_embedding_model(model_name: str):
    """Load embedding model synchronously. Thread-unsafe warmup removed."""
    global _embedding_model
    if not _check_embedding_available():
        return None
    if _embedding_model is False:
        return None
    if _embedding_model is not None:
        return _embedding_model
    try:
        _configure_embedding_logging()
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        import sys; print(f'[EMBED] loading {model_name}', file=sys.stderr, flush=True)
        _embedding_model = _SentenceTransformer(model_name, device="cpu", local_files_only=True)
        print(f'[EMBED] loaded OK', file=sys.stderr, flush=True)
        logger.info(f"Embedding model loaded: {model_name}")
    except Exception as e:
        print(f'[EMBED] FAILED: {e}', file=sys.stderr, flush=True)
        logger.warning(f"Embedding model unavailable: {e}")
        _embedding_model = False
    return _embedding_model if _embedding_model is not False else None


def _safe_cosine_similarity(a, b) -> float:
    if not _check_embedding_available() or _np is None or a is None or b is None:
        return 0.0
    try:
        a_vec = _np.array(a) if not isinstance(a, _np.ndarray) else a
        b_vec = _np.array(b) if not isinstance(b, _np.ndarray) else b
        if a_vec.ndim != 1 or b_vec.ndim != 1 or a_vec.size == 0 or a_vec.shape != b_vec.shape:
            return 0.0
        dot = float(_np.dot(a_vec, b_vec))
        norm_a = float(_np.linalg.norm(a_vec))
        norm_b = float(_np.linalg.norm(b_vec))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
    except Exception:
        return 0.0


class MemoryGraph:
    def __init__(self, config: MemoriaConfig):
        self.config = config
        self.nodes: Dict[str, MemoryNode] = {}
        self.links: Dict[str, List[CausalLink]] = defaultdict(list)
        self.pulse_queue: Dict[int, List[Tuple[str, str, float, CausalLink]]] = defaultdict(list)
        self.muscle_memory_ids: Set[str] = set()
        self.hot_cache: Set[str] = set()
        self.warm_cache: Set[str] = set()
        self.location_index: Dict[str, Set[str]] = defaultdict(set)
        self.people_index: Dict[str, Set[str]] = defaultdict(set)
        self.keyword_index = MemoryIndex(
            enable_bm25=config.enable_bm25,
            bm25_k1=config.bm25_k1,
            bm25_b=config.bm25_b,
        )
        self.incoming_links: Dict[str, Set[str]] = defaultdict(set)
        self._strength_history: Dict[str, List[float]] = defaultdict(list)
        self._summary_cache: OrderedDict = OrderedDict()
        self._current_tick: int = 0

    # ========== Node Management ==========

    def _ensure_node_embedding(self, node: MemoryNode):
        if not self.config.enable_semantic or node.embedding is not None or not node.summary.description:
            return
        model = _get_embedding_model(self.config.embedding_model)
        if model:
            node.embedding = model.encode(node.summary.description, show_progress_bar=False).tolist()

    def _register_node_storage(self, node: MemoryNode):
        self.nodes[node.id] = node
        if node.links:
            self.links[node.id] = node.links.copy()
            for link in node.links:
                self.incoming_links[link.target].add(node.id)

    def _bind_primary_parents(self, node: MemoryNode):
        if node.origin_kind != "primary" or len(self.nodes) <= 1:
            return
        parents = []
        if self.config.enable_semantic and node.embedding is not None:
            parents = self._find_semantic_parents(node, top_k=3)
        if not parents:
            candidates = [other for other in self.nodes.values() if other.id != node.id]
            if candidates:
                parents = [max(candidates, key=lambda n: n.timestamp)]
        for parent in parents[:self.config.min_parent_links]:
            self.add_link(parent.id, node.id, weight=0.2, delay=1, curvature=1.0)
            if parent.id not in node.parent_ids:
                node.parent_ids.append(parent.id)

    def _register_node_indexes(self, node: MemoryNode):
        for loc in node.summary.locations:
            self.location_index[loc].add(node.id)
        for person in node.summary.people:
            self.people_index[person].add(node.id)
        self.keyword_index.add_node(node.id, node.summary.description)

    def _finalize_added_node(self, node: MemoryNode, auto_link: Optional[bool]):
        if (auto_link if auto_link is not None else self.config.enable_auto_link) and self.config.enable_semantic:
            self._auto_link_node(node)
        self._update_node_layer(node.id)

    def add_node(self, node: MemoryNode, auto_link: bool = None) -> str:
        if node.id in self.nodes:
            raise ValueError(f"Node {node.id} already exists")
        self._ensure_node_embedding(node)
        self._register_node_storage(node)
        self._bind_primary_parents(node)
        self._register_node_indexes(node)
        self._finalize_added_node(node, auto_link)
        logger.debug(f"Added node {node.id}: {node.summary.description[:50]}")
        return node.id

    def _find_semantic_parents(self, node: MemoryNode, top_k: int = 3) -> List[MemoryNode]:
        model = _get_embedding_model(self.config.embedding_model)
        if model is None or node.embedding is None:
            return []
        candidates = [n for n in self.nodes.values() if n.id != node.id and n.strength > 0.4]
        if not candidates:
            return []
        scored = []
        for other in candidates:
            if other.embedding is not None:
                sim = _safe_cosine_similarity(node.embedding, other.embedding)
                if sim >= self.config.auto_link_threshold:
                    scored.append((sim, other))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [n for _, n in scored[:top_k]]

    def _auto_link_node(self, node: MemoryNode):
        if node.embedding is None:
            return
        candidates = [
            n for n in self.nodes.values()
            if n.id != node.id and not n.muscle and n.id not in self.muscle_memory_ids and n.strength > 0.4
        ]
        if not candidates:
            return
        sims = []
        for other in candidates:
            sim = _safe_cosine_similarity(node.embedding, other.embedding)
            sims.append((other, sim))
        sims.sort(key=lambda x: x[1], reverse=True)
        linked = 0
        for other, sim in sims:
            if linked >= self.config.auto_link_max:
                break
            if sim >= self.config.auto_link_threshold:
                self.add_link(other.id, node.id, weight=sim, delay=1, curvature=1.0)
                self.add_link(node.id, other.id, weight=sim, delay=0, curvature=1.0)
                linked += 1

    def add_link(self, from_id: str, to_id: str, weight: float = 0.5, delay: int = 0, curvature: float = 1.0):
        if from_id not in self.nodes or to_id not in self.nodes:
            return
        for link in self.links[from_id]:
            if link.target == to_id:
                link.weight = weight
                link.delay = delay
                link.curvature = curvature
                return
        link = CausalLink(target=to_id, weight=weight, delay=delay, curvature=curvature)
        self.links[from_id].append(link)
        self.nodes[from_id].links.append(link)
        self.incoming_links[to_id].add(from_id)

    def _update_node_layer(self, node_id: str):
        node = self.nodes.get(node_id)
        if not node:
            return
        self.hot_cache.discard(node_id)
        self.warm_cache.discard(node_id)
        if node.id in self.muscle_memory_ids or node.muscle:
            node.layer = MemoryLayer.HOT
            node.strength = 1.0
        else:
            if node.strength >= self.config.hot_threshold:
                node.layer = MemoryLayer.HOT
            elif node.strength >= self.config.warm_threshold:
                node.layer = MemoryLayer.WARM
            else:
                node.layer = MemoryLayer.COLD
        if node.layer == MemoryLayer.HOT:
            self.hot_cache.add(node_id)
        elif node.layer == MemoryLayer.WARM:
            self.warm_cache.add(node_id)

    # ========== Strength & Decay ==========

    def update_strength(self, node_id: str, current_tick: float, current_real_time: Optional[float] = None):
        node = self.nodes.get(node_id)
        if not node:
            return
        if node.id in self.muscle_memory_ids or node.muscle:
            node.strength = 1.0
            self._update_node_layer(node_id)
            return

        now = current_real_time or time.time()
        if self.config.enable_real_time_decay_anchor:
            anchor = node.latest_real_access_anchor()
            if anchor is not None:
                decay_per_day = self.config.real_time_decay_per_day
                grace = self.config.real_time_decay_grace_seconds
                elapsed_days = max(0.0, (now - anchor - grace) / 86400.0)
                time_elapsed = elapsed_days * decay_per_day
            else:
                time_elapsed = max(0, float(current_tick) - float(node.last_access_tick or node.timestamp))
        else:
            time_elapsed = max(0, float(current_tick) - float(node.last_access_tick or node.timestamp))

        decay = self.config.decay_factor ** time_elapsed
        access_bonus = 1 + self.config.access_gain * math.log(1 + node.access_count)
        new_strength = node.importance * decay * access_bonus
        node.strength = max(0.0, min(1.0, new_strength))
        self._update_node_layer(node_id)
        self._record_strength_history(node_id, node.strength)

    def _record_strength_history(self, node_id: str, strength: float):
        self._strength_history[node_id].append(strength)
        if len(self._strength_history[node_id]) > self.config.attractor_window:
            self._strength_history[node_id].pop(0)

    # ========== Pulse Propagation ==========

    def propagate(self, source_id: str, delta: float, current_tick: int, depth: int = 0, visited: Set[str] = None):
        if visited is None:
            visited = set()
        if depth > self.config.max_propagation_depth or source_id in visited:
            return
        visited.add(source_id)
        source = self.nodes.get(source_id)
        if not source or delta == 0:
            return

        # Semantic resonance
        if self.config.enable_semantic_resonance and self.config.enable_semantic and source.embedding is not None:
            self._semantic_resonance(source, delta, visited, current_tick)

        for link in self.links[source_id]:
            target_id = link.target
            if target_id in visited:
                continue
            target = self.nodes.get(target_id)
            if not target:
                continue
            pulse_value = delta * link.weight * link.curvature
            if pulse_value == 0:
                continue
            arrival_tick = current_tick + link.delay
            self.pulse_queue[arrival_tick].append((source_id, target_id, pulse_value, link))
            link.curvature = max(0.1, link.curvature * (1 - self.config.curvature_learning_rate))
            link.last_used_tick = current_tick

    def process_pulses(self, current_tick: int):
        pulses = self.pulse_queue.pop(current_tick, [])
        for (src, tgt, val, link) in pulses:
            target = self.nodes.get(tgt)
            if target:
                target.strength = max(0.0, min(1.0, target.strength + val))
                link.update_hebbian(True, self.config.hebbian_learning_rate)
                self.propagate(tgt, val, current_tick, depth=1)
        # Clean expired pulses
        expired = [t for t in self.pulse_queue.keys() if t < current_tick - 100]
        for t in expired:
            del self.pulse_queue[t]

    def _semantic_resonance(self, source: MemoryNode, delta: float, visited: Set[str], current_tick: int):
        model = _get_embedding_model(self.config.embedding_model)
        if model is None:
            return
        candidates = [
            n for n in self.nodes.values()
            if n.id not in visited and not n.muscle and n.id not in self.muscle_memory_ids
            and n.embedding is not None
        ]
        for target in candidates:
            sim = _safe_cosine_similarity(source.embedding, target.embedding)
            if sim >= self.config.auto_link_threshold:
                extra = delta * self.config.resonance_factor * (sim - self.config.auto_link_threshold)
                if extra > 0:
                    target.strength = min(1.0, target.strength + extra)

    # ========== Attractor Detection ==========

    def detect_attractors(self) -> List[str]:
        attractors = []
        for node_id, history in self._strength_history.items():
            if len(history) < self.config.attractor_window:
                continue
            if node_id not in self.nodes:
                continue
            node = self.nodes[node_id]
            mean_strength = sum(history) / len(history)
            variance = sum((s - mean_strength) ** 2 for s in history) / len(history)
            if mean_strength > self.config.attractor_threshold and variance < 0.01:
                node.attractor_score = mean_strength * (1 - variance * 100)
                if not node.muscle and node.attractor_score > 0.95:
                    self._auto_solidify(node)
                attractors.append(node_id)
        return attractors

    def _auto_solidify(self, node: MemoryNode):
        self.muscle_memory_ids.add(node.id)
        node.muscle = True
        node.is_muscle_memory = True
        node.strength = 1.0
        node.layer = MemoryLayer.HOT
        for link in self.links.get(node.id, []):
            link.curvature = self.config.curvature_muscle
            link.weight = 1.0
            link.hebbian_strength = 1.0
        self.hot_cache.add(node.id)
        logger.info(f"Attractor auto-solidified: {node.id}")

    # ========== Phase Transition Detection ==========

    def detect_phase_transition(self, current_tick: int, current_real_time: Optional[float] = None) -> Optional[Dict[str, Any]]:
        strengths = [n.strength for n in self.nodes.values()]
        if len(strengths) < 10:
            return None
        mean = sum(strengths) / len(strengths)
        variance = sum((s - mean) ** 2 for s in strengths) / len(strengths)
        if variance > self.config.phase_transition_threshold:
            event_id = f"phase_transition_{current_tick}"
            summary = Summary(
                description=f"Phase transition | variance:{variance:.3f} | mean:{mean:.3f}",
                locations=["global"],
            )
            event_node = MemoryNode(
                id=event_id,
                timestamp=current_tick,
                last_access_tick=current_tick,
                type=MemoryType.EVENT,
                importance=min(1.0, variance * 2),
                strength=min(1.0, variance * 2),
                summary=summary,
                origin_kind="phase_transition",
                created_at_real=current_real_time,
                last_accessed_at_real=current_real_time,
            )
            self.add_node(event_node)
            logger.warning(f"Phase transition detected: variance={variance:.3f}")
            return {"type": "phase_transition", "variance": variance, "event_id": event_id}
        return None

    # ========== Solidification ==========

    def solidify(self, node_id: str) -> bool:
        node = self.nodes.get(node_id)
        if not node:
            return False
        if (node.access_count >= self.config.muscle_visits and
                node.importance >= self.config.muscle_importance):
            self.muscle_memory_ids.add(node_id)
            node.muscle = True
            node.strength = 1.0
            node.layer = MemoryLayer.HOT
            for link in self.links.get(node_id, []):
                link.curvature = self.config.curvature_muscle
                link.weight = 1.0
                link.hebbian_strength = 1.0
            for src, links in self.links.items():
                for link in links:
                    if link.target == node_id:
                        link.curvature = 0.0
            self.hot_cache.add(node_id)
            logger.info(f"Muscle memory solidified: {node_id}")
            return True
        return False

    # ========== Consolidation ==========

    def _consolidation_stability_score(self, node_id: str, window: int) -> Optional[float]:
        history = list(self._strength_history.get(node_id, []))
        if len(history) < max(2, window):
            return None
        sample = history[-max(2, window):]
        mean_strength = sum(sample) / len(sample)
        variance = sum((value - mean_strength) ** 2 for value in sample) / len(sample)
        return max(0.0, min(1.0, 1.0 - min(1.0, variance / 0.02)))

    def consolidate(self, current_tick: float, current_real_time: Optional[float]) -> List[Dict[str, Any]]:
        now = current_real_time or time.time()
        if not self.config.enable_active_consolidation:
            return []

        batch_size = max(1, self.config.consolidation_batch_size)
        min_age = self.config.consolidation_min_age_seconds
        threshold = self.config.consolidation_threshold
        importance_boost = self.config.consolidation_importance_boost
        hebbian_lr = self.config.consolidation_hebbian_learning_rate
        history_window = self.config.consolidation_history_window

        scored: List[Tuple[float, float, float, MemoryNode]] = []
        for node in self.nodes.values():
            if node.muscle or node.id in self.muscle_memory_ids:
                continue
            if node.origin_kind in {"compressed_summary", "phase_transition", "triggered_event"}:
                continue
            access_age = node.real_access_age_seconds(now)
            if access_age is not None and access_age < min_age:
                continue
            last_consolidated = node.last_consolidated_at_real
            if last_consolidated is not None:
                if (now - last_consolidated) < self.config.consolidation_interval_seconds:
                    continue
            stability = self._consolidation_stability_score(node.id, history_window)
            if stability is None:
                continue
            maturity = 1.0 if access_age is None else min(1.0, access_age / max(1.0, min_age))
            link_density = min(1.0, len(self.links.get(node.id, [])) / 3.0)
            score = (
                float(node.strength) * 0.35
                + float(node.importance) * 0.30
                + stability * 0.25
                + maturity * 0.05
                + link_density * 0.05
            )
            if score < threshold:
                continue
            scored.append((score, float(node.importance), float(node.strength), node))

        scored.sort(key=lambda item: (item[0], item[1], item[2], item[3].id), reverse=True)
        records: List[Dict[str, Any]] = []
        for score, _imp, _str, node in scored[:batch_size]:
            boost = max(0.0, score - threshold) * importance_boost
            node.importance = min(1.0, float(node.importance) + boost)
            node.last_consolidated_at_real = now
            for link in self.links.get(node.id, [])[:3]:
                link.update_hebbian(True, hebbian_lr)
            self._update_node_layer(node.id)
            records.append({
                "node_id": node.id,
                "score": round(float(score), 4),
                "importance": float(node.importance),
            })
        return records

    # ========== Compression ==========

    def _compression_priority(self, node: MemoryNode) -> Tuple[int, float, float, float, float, int]:
        layer_priority = {MemoryLayer.COLD: 0, MemoryLayer.WARM: 1, MemoryLayer.HOT: 2}.get(node.layer, 1)
        last_seen = float(node.last_access_tick) if float(node.last_access_tick) > 0 else float(node.timestamp)
        return (layer_priority, float(node.strength), float(node.importance), last_seen, last_seen, int(node.timestamp))

    def _select_compression_candidates(
        self,
        current_tick: float,
        *,
        protected_node_ids: Optional[Set[str]] = None,
        recent_write_grace_ticks: float = 0.0,
        recent_access_grace_ticks: float = 0.0,
        current_real_time: Optional[float] = None,
        recent_write_grace_seconds: float = 0.0,
        recent_access_grace_seconds: float = 0.0,
    ) -> List[MemoryNode]:
        protected_ids = set(protected_node_ids or set())
        now = current_real_time or time.time()
        selected: List[MemoryNode] = []
        for node in self.nodes.values():
            if node.muscle or node.id in self.muscle_memory_ids or node.id in protected_ids:
                continue
            if self.config.enable_real_time_decay_anchor:
                write_age = node.real_write_age_seconds(now)
                if write_age is not None and write_age < recent_write_grace_seconds:
                    continue
                access_age = node.real_access_age_seconds(now)
                if access_age is not None and access_age < recent_access_grace_seconds:
                    continue
            else:
                write_age = float(current_tick) - float(node.timestamp)
                if write_age < recent_write_grace_ticks:
                    continue
                last_seen = float(node.last_access_tick) if float(node.last_access_tick) > 0 else float(node.timestamp)
                access_age = float(current_tick) - last_seen
                if access_age < recent_access_grace_ticks:
                    continue
            selected.append(node)
        return sorted(selected, key=self._compression_priority)

    def compress(
        self,
        current_tick: int,
        *,
        active_memory_limit: Optional[int] = None,
        compression_batch: Optional[int] = None,
        protected_node_ids: Optional[Set[str]] = None,
        recent_write_grace_seconds: float = 300.0,
        recent_access_grace_seconds: float = 60.0,
        current_real_time: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        active_limit = max(1, int(active_memory_limit or self.config.active_memory_limit))
        batch_size = max(1, int(compression_batch or self.config.compression_batch))
        candidates = [n for n in self.nodes.values() if not n.muscle and n.id not in self.muscle_memory_ids]
        if len(candidates) <= active_limit:
            return None
        stable = self._select_compression_candidates(
            current_tick,
            protected_node_ids=protected_node_ids,
            recent_write_grace_seconds=recent_write_grace_seconds,
            recent_access_grace_seconds=recent_access_grace_seconds,
            current_real_time=current_real_time,
        )
        to_compress = stable[:batch_size]
        if not to_compress:
            return None

        node_ids = [n.id for n in to_compress]
        node_id_set = set(node_ids)

        # Build summary text
        descriptions = [n.summary.description for n in to_compress if n.summary.description]
        summary_text = " | ".join(descriptions)[:500]

        summary_id = f"summary_{current_tick}_{len(self.nodes)}"
        avg_importance = sum(n.importance for n in to_compress) / len(to_compress)
        summary_node = MemoryNode(
            id=summary_id,
            timestamp=current_tick,
            last_access_tick=current_tick,
            type=MemoryType.PLOT,
            importance=avg_importance,
            strength=avg_importance,
            summary=Summary(description=summary_text),
            origin_kind="compressed_summary",
            source_node_ids=list(node_ids),
            created_at_real=current_real_time,
            last_accessed_at_real=current_real_time,
        )
        self.add_node(summary_node, auto_link=False)

        # Redirect links
        outgoing: Dict[str, Tuple[float, int, float]] = {}
        incoming: Dict[str, Tuple[float, int, float]] = {}
        for source_id, links in self.links.items():
            for link in links:
                if source_id in node_id_set and link.target not in node_id_set:
                    key = link.target
                    existing = outgoing.get(key)
                    candidate = (link.weight, link.delay, link.curvature)
                    if existing is None:
                        outgoing[key] = candidate
                    else:
                        outgoing[key] = (max(existing[0], candidate[0]), min(existing[1], candidate[1]), max(existing[2], candidate[2]))
                elif source_id not in node_id_set and link.target in node_id_set:
                    key = source_id
                    existing = incoming.get(key)
                    candidate = (link.weight, link.delay, link.curvature)
                    if existing is None:
                        incoming[key] = candidate
                    else:
                        incoming[key] = (max(existing[0], candidate[0]), min(existing[1], candidate[1]), max(existing[2], candidate[2]))

        for target_id, (weight, delay, curvature) in outgoing.items():
            self.add_link(summary_id, target_id, weight, delay, curvature)
        for source_id, (weight, delay, curvature) in incoming.items():
            self.add_link(source_id, summary_id, weight, delay, curvature)

        # Remove compressed nodes
        for node in to_compress:
            nid = node.id
            if nid in self.nodes:
                del self.nodes[nid]
            self.hot_cache.discard(nid)
            self.warm_cache.discard(nid)
            if nid in self._strength_history:
                del self._strength_history[nid]

        # Clean pulse queue
        for pulse_tick in list(self.pulse_queue.keys()):
            filtered = [p for p in self.pulse_queue[pulse_tick] if p[0] not in node_id_set and p[1] not in node_id_set]
            if filtered:
                self.pulse_queue[pulse_tick] = filtered
            else:
                del self.pulse_queue[pulse_tick]

        self._rebuild_indices()
        logger.info(f"Compressed {len(to_compress)} nodes -> {summary_id}")
        return {
            "summary_id": summary_id,
            "compressed_node_ids": list(node_ids),
            "source_count": len(node_ids),
        }

    # ========== Retrieval ==========

    def hybrid_retrieve(self, query: str, top_k: int = 5) -> tuple[List[MemoryNode], Dict[str, float]]:
        """BM25 keyword + optional vector + RRF fusion. Returns (nodes, vector_similarities)."""
        keyword_limit = max(top_k * 4, top_k)
        keyword_ids = self.keyword_index.search_ranked(query, top_k=keyword_limit)
        keyword_nodes = [self.nodes[nid] for nid in keyword_ids if nid in self.nodes]

        # Vector retrieval (skip if semantic disabled)
        vector_nodes: List[MemoryNode] = []
        vector_sims: Dict[str, float] = {}
        if self.config.enable_semantic:
            model = _get_embedding_model(self.config.embedding_model)
            if model is not None:
                try:
                    query_embedding = model.encode(query, show_progress_bar=False).tolist()
                    scored = []
                    for node in self.nodes.values():
                        # Lazy backfill: compute embedding if missing
                        if node.embedding is None and node.summary.description:
                            try:
                                node.embedding = model.encode(node.summary.description, show_progress_bar=False).tolist()
                            except Exception:
                                pass
                        if node.embedding is not None:
                            sim = _safe_cosine_similarity(node.embedding, query_embedding)
                            scored.append((sim, node))
                            vector_sims[node.id] = sim
                    scored.sort(key=lambda x: x[0], reverse=True)
                    vector_nodes = [n for _, n in scored[:top_k]]
                except Exception:
                    pass

        # RRF fusion
        scores: Dict[str, float] = {}
        for rank, node in enumerate(keyword_nodes):
            scores[node.id] = scores.get(node.id, 0) + 1 / (rank + 60)
        for rank, node in enumerate(vector_nodes):
            scores[node.id] = scores.get(node.id, 0) + 1 / (rank + 60)

        sorted_ids = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [self.nodes[nid] for nid, _ in sorted_ids[:top_k] if nid in self.nodes], vector_sims

    def get_hot_nodes(self) -> List[MemoryNode]:
        return [self.nodes[nid] for nid in self.hot_cache if nid in self.nodes]

    def get_warm_nodes(self) -> List[MemoryNode]:
        return [self.nodes[nid] for nid in self.warm_cache if nid in self.nodes]

    # ========== Temporal Order Validation ==========

    def validate_temporal_order(self) -> int:
        """Check for temporal violations in causal links and halve their weight."""
        violations = 0
        for src_id, links in self.links.items():
            src_node = self.nodes.get(src_id)
            if not src_node:
                continue
            for link in links:
                tgt_node = self.nodes.get(link.target)
                if not tgt_node:
                    continue
                if src_node.timestamp > tgt_node.timestamp:
                    link.weight *= 0.5
                    violations += 1
        if violations:
            logger.info(f"Temporal order: {violations} violations corrected")
        return violations

    # ========== Event Trigger (Region Energy) ==========

    def check_trigger(self, current_tick: int) -> Optional[str]:
        """If total strength of a region exceeds event_threshold, create triggered event."""
        if not self.nodes:
            return None

        total_strength = sum(n.strength for n in self.nodes.values() if not n.muscle)
        if total_strength < self.config.event_threshold:
            return None

        # Find the strongest non-muscle region
        candidates = sorted(
            [n for n in self.nodes.values() if not n.muscle],
            key=lambda n: n.strength,
            reverse=True,
        )[:5]

        if not candidates:
            return None

        # Create triggered event node
        now = int(time.time())
        event_id = f"trigger_{now}_{_RNG.randint(0, 9999):04d}"
        descriptions = [n.summary.description[:80] for n in candidates[:3]]
        event_node = MemoryNode(
            id=event_id,
            timestamp=now,
            type=MemoryType.EVENT,
            importance=min(1.0, total_strength * 0.3),
            strength=min(1.0, total_strength * 0.5),
            origin_kind="triggered_event",
            source_node_ids=[n.id for n in candidates],
            summary=Summary(
                description=f"Region trigger: {'; '.join(descriptions)}",
            ),
        )
        self.add_node(event_node)

        # Drain strength from source nodes
        drain = 0.1 / max(len(candidates), 1)
        for n in candidates:
            n.strength = max(0.0, n.strength - drain)
            self._update_node_layer(n.id)

        logger.info(f"Event triggered: {event_id} (total_strength={total_strength:.3f})")
        return event_id

    # ========== Propagation Hotspots ==========

    def find_propagation_hotspots(self, window_ticks: int = 5) -> List[Dict[str, Any]]:
        """Find nodes receiving pulse propagation in a recent time window."""
        recent_tick = self._current_tick - window_ticks
        hotspot_scores: Dict[str, float] = defaultdict(float)

        for arrival_tick, pulses in self.pulse_queue.items():
            if arrival_tick < recent_tick:
                continue
            for src, tgt, val, link in pulses:
                hotspot_scores[tgt] += abs(val)

        hotspots = []
        for node_id, score in sorted(hotspot_scores.items(), key=lambda x: x[1], reverse=True):
            node = self.nodes.get(node_id)
            if node:
                hotspots.append({
                    "node_id": node_id,
                    "description": node.summary.description[:100],
                    "score": round(score, 4),
                    "strength": round(node.strength, 3),
                })
        return hotspots[:20]

    # ========== Attractor Structure Analysis ==========

    def analyze_attractor_structure(self) -> Dict[str, Any]:
        """Analyze inter-attractor link structure and cluster density."""
        attractors = [nid for nid, node in self.nodes.items() if node.attractor_score > 0.5]
        if not attractors:
            return {"attractors": 0, "clusters": [], "density": 0.0}

        # Build subgraph of attractor-to-attractor links
        inter_links = []
        for src in attractors:
            for link in self.links.get(src, []):
                if link.target in attractors:
                    inter_links.append({
                        "from": src,
                        "to": link.target,
                        "weight": round(link.weight, 3),
                        "hebbian": round(link.hebbian_strength, 3),
                    })

        max_possible = len(attractors) * (len(attractors) - 1)
        density = len(inter_links) / max(max_possible, 1)

        return {
            "attractors": len(attractors),
            "inter_links": len(inter_links),
            "density": round(density, 4),
            "links": inter_links[:20],
        }

    # ========== Orphan Repair ==========

    def repair_orphans(self, current_tick: int) -> int:
        if not self.config.enable_semantic:
            return 0
        repaired = 0
        for node_id, node in self.nodes.items():
            if not self.incoming_links.get(node_id):
                neighbor = self._find_semantic_neighbor(node)
                if neighbor:
                    self.add_link(neighbor.id, node_id, weight=0.1, delay=0, curvature=0.5)
                    node.parent_ids.append(neighbor.id)
                    repaired += 1
        return repaired

    def _find_semantic_neighbor(self, node: MemoryNode) -> Optional[MemoryNode]:
        model = _get_embedding_model(self.config.embedding_model)
        if model is None or node.embedding is None:
            return None
        hot_nodes = [n for n in self.get_hot_nodes() if n.id != node.id and self.incoming_links.get(n.id)]
        if not hot_nodes:
            return None
        best_sim = 0.0
        best_node = None
        for other in hot_nodes:
            if other.embedding is not None:
                sim = _safe_cosine_similarity(node.embedding, other.embedding)
                if sim > best_sim:
                    best_sim = sim
                    best_node = other
        return best_node if best_sim > 0.3 else None

    # ========== Tick Management ==========

    def tick(self, current_real_time: Optional[float] = None) -> Dict[str, Any]:
        """Perform one lazy tick: process pulses, decay, detect attractors, consolidate."""
        now = current_real_time or time.time()
        self._current_tick += 1
        tick = self._current_tick

        # Process pulses
        self.process_pulses(tick)

        # Decay all nodes
        for node_id in list(self.nodes.keys()):
            self.update_strength(node_id, tick, now)

        # Detect attractors
        attractors = self.detect_attractors()

        # Consolidate
        consolidations = self.consolidate(tick, now)

        # Phase transition
        phase = self.detect_phase_transition(tick, now)

        # Orphan repair (every 10 ticks)
        orphans_repaired = 0
        if tick % 10 == 0:
            orphans_repaired = self.repair_orphans(tick)

        return {
            "tick": tick,
            "attractors": attractors,
            "consolidations": consolidations,
            "phase_transition": phase,
            "orphans_repaired": orphans_repaired,
            "node_count": len(self.nodes),
            "hot_count": len(self.hot_cache),
            "warm_count": len(self.warm_cache),
            "muscle_count": len(self.muscle_memory_ids),
        }

    # ========== Index Rebuild ==========

    def _rebuild_indices(self):
        self.hot_cache.clear()
        self.warm_cache.clear()
        self.location_index.clear()
        self.people_index.clear()
        self.keyword_index = MemoryIndex(
            enable_bm25=self.config.enable_bm25,
            bm25_k1=self.config.bm25_k1,
            bm25_b=self.config.bm25_b,
        )
        self.incoming_links.clear()
        rebuilt_links: Dict[str, List[CausalLink]] = defaultdict(list)
        existing_ids = set(self.nodes.keys())
        for node in self.nodes.values():
            filtered_links = []
            for link in node.links:
                if link.target not in existing_ids:
                    continue
                filtered_links.append(link)
                rebuilt_links[node.id].append(link)
                self.incoming_links[link.target].add(node.id)
            node.links = filtered_links
            for loc in node.summary.locations:
                self.location_index[loc].add(node.id)
            for person in node.summary.people:
                self.people_index[person].add(node.id)
            self.keyword_index.add_node(node.id, node.summary.description)
            if node.muscle or node.id in self.muscle_memory_ids:
                self.hot_cache.add(node.id)
                node.layer = MemoryLayer.HOT
            elif node.layer == MemoryLayer.HOT:
                self.hot_cache.add(node.id)
            elif node.layer == MemoryLayer.WARM:
                self.warm_cache.add(node.id)
        self.links = defaultdict(list, rebuilt_links)

    # ========== Serialization ==========

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": {nid: node.to_dict() for nid, node in self.nodes.items()},
            "links": {src: [link.to_dict() for link in links] for src, links in self.links.items()},
            "muscle_memory_ids": list(self.muscle_memory_ids),
            "current_tick": self._current_tick,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], config: MemoriaConfig) -> MemoryGraph:
        graph = cls(config)
        for nid, nd in data.get("nodes", {}).items():
            graph.nodes[nid] = MemoryNode.from_dict(nd)
        for src, links_data in data.get("links", {}).items():
            for ld in links_data:
                link = CausalLink.from_dict(ld)
                graph.links[src].append(link)
                graph.incoming_links[link.target].add(src)
        graph.muscle_memory_ids = set(data.get("muscle_memory_ids", []))
        graph._current_tick = data.get("current_tick", 0)
        graph._rebuild_indices()
        return graph
