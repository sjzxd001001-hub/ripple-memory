"""Configuration for RippleMemory."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

DEFAULT_EMBEDDING_MODEL_DIR = "paraphrase-multilingual-MiniLM-L12-v2"
_MODEL_DIR_NAME = DEFAULT_EMBEDDING_MODEL_DIR


def _resolve_embedding_model_path() -> str:
    """Resolve embedding model path: env var > data_dir/models/ > package-relative fallback."""
    # Env var takes priority
    env_model = os.environ.get("MEMORIA_MCP_EMBEDDING_MODEL")
    if env_model and Path(env_model).expanduser().is_dir():
        return str(Path(env_model).expanduser())
    data_dir = (
        os.environ.get("MEMORIA_MCP_DATA_DIR")
        or os.environ.get("RIPPLE_MEMORY_DATA_DIR")
        or os.path.expanduser("~/.ripple-memory")
    )
    data_model = Path(data_dir).expanduser() / "models" / _MODEL_DIR_NAME
    if (data_model / "config.json").is_file():
        return str(data_model)
    pkg_root = Path(__file__).resolve().parent.parent.parent
    pkg_model = pkg_root / "models" / _MODEL_DIR_NAME
    if (pkg_model / "config.json").is_file():
        return str(pkg_model)
    return str(data_model)


@dataclass
class MemoriaConfig:
    # === Decay & Strength ===
    decay_factor: float = 0.9
    access_gain: float = 0.1

    # === Memory Layers ===
    hot_threshold: float = 0.8
    warm_threshold: float = 0.3

    # === Muscle Memory (Solidification) ===
    muscle_visits: int = 5
    muscle_importance: float = 0.7
    muscle_auto_trigger_threshold: float = 0.8
    muscle_auto_trigger_prob: float = 0.8

    # === Pulse Propagation ===
    max_propagation_depth: int = 10
    curvature_learning_rate: float = 0.01
    curvature_recovery_rate: float = 0.001
    curvature_critical: float = 2.0
    curvature_muscle: float = 0.8

    # === Hebbian Learning ===
    hebbian_learning_rate: float = 0.05

    # === Auto-Linking ===
    enable_auto_link: bool = True
    auto_link_threshold: float = 0.65
    auto_link_max: int = 3
    min_parent_links: int = 1

    # === Semantic Resonance ===
    enable_semantic_resonance: bool = True
    resonance_factor: float = 0.3

    # === Attractor Detection ===
    attractor_window: int = 10
    attractor_threshold: float = 0.7

    # === Phase Transition Detection ===
    phase_transition_threshold: float = 0.15
    event_threshold: float = 0.8

    # === Compression ===
    active_memory_limit: int = 100
    compression_batch: int = 5
    compression_cache_size: int = 100
    enable_llm_compression: bool = False
    compression_prompt_template: str = ""

    # === BM25 ===
    enable_bm25: bool = True
    bm25_k1: float = 1.5
    bm25_b: float = 0.75

    # === Semantic / Embedding ===
    enable_semantic: bool = True
    embedding_model: str = field(default_factory=lambda: _resolve_embedding_model_path())

    # === FAISS (disabled by default for MCP — heavy dependency) ===
    enable_faiss: bool = False
    faiss_index_threshold: int = 50
    faiss_rebuild_interval: int = 50

    # === Real-time Decay ===
    enable_real_time_decay_anchor: bool = True
    real_time_decay_per_day: float = 0.02
    real_time_decay_grace_seconds: float = 3600.0

    # === Consolidation ===
    enable_active_consolidation: bool = True
    consolidation_batch_size: int = 1
    consolidation_min_age_seconds: float = 600.0
    consolidation_threshold: float = 0.5
    consolidation_importance_boost: float = 0.05
    consolidation_hebbian_learning_rate: float = 0.04
    consolidation_interval_seconds: float = 300.0
    consolidation_history_window: int = 5

    # === Search Index (auxiliary retrieval rail) ===
    enable_search_index: bool = True
    search_index_query_mode: str = field(default_factory=lambda: os.environ.get("MEMORIA_MCP_SEARCH_INDEX_QUERY_MODE", "shadow"))  # off / shadow / live
    search_index_sync_interval_ticks: float = 10.0
    search_index_rebuild_on_boot: bool = True
    search_index_batch_size: int = 64

    # === SQLite Persistence ===
    db_path: str = field(default_factory=lambda: os.path.join(
        os.environ.get("MEMORIA_MCP_DATA_DIR")
        or os.environ.get("RIPPLE_MEMORY_DATA_DIR")
        or os.path.expanduser("~/.ripple-memory"),
        "memoria.db",
    ))

    # === Dreamer (Background Compaction) ===
    enable_dreamer: bool = True
    dreamer_interval_days: float = 7.0
    dreamer_idle_hours: float = 1.0
    dreamer_batch_threshold: int = 100
    dreamer_max_rows_per_run: int = 200
    dreamer_min_entry_age_hours: float = 1.0
    dreamer_allowed_delete_reasons: List[str] = field(
        default_factory=lambda: ["compression", "memory_evolution_superseded"]
    )

    # === JSONL Memory Stream ===
    enable_memory_jsonl_stream: bool = True
    jsonl_flush_immediately: bool = True
    jsonl_rotate_days: int = 1

    # === WriteGate Thresholds (adapted for programming agent) ===
    min_event_confidence: float = 0.3
    min_stable_confidence: float = 0.4
    min_stable_evidence: float = 0.4

    def __post_init__(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
