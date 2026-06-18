"""Inverted index with BM25 retrieval."""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Set

from .bm25 import BM25Index, tokenize_retrieval_text


class MemoryIndex:
    """Inverted index supporting keyword and BM25 retrieval."""

    def __init__(self, *, enable_bm25: bool = False, bm25_k1: float = 1.5, bm25_b: float = 0.75):
        self.enable_bm25 = bool(enable_bm25)
        self._index: Dict[str, Set[str]] = defaultdict(set)
        self._node_terms: Dict[str, List[str]] = {}
        self._node_texts: Dict[str, str] = {}
        self._bm25 = BM25Index(k1=bm25_k1, b=bm25_b) if self.enable_bm25 else None

    def add_node(self, node_id: str, text: str):
        normalized_id = str(node_id or "").strip()
        if not normalized_id:
            return
        if normalized_id in self._node_terms:
            self.remove_node(normalized_id, "")
        words = self._tokenize(text)
        for w in words:
            self._index[w].add(normalized_id)
        self._node_terms[normalized_id] = words
        self._node_texts[normalized_id] = str(text or "")
        if self._bm25 is not None:
            self._bm25.add(normalized_id, text)

    def remove_node(self, node_id: str, text: str):
        normalized_id = str(node_id or "").strip()
        if not normalized_id:
            return
        words = self._node_terms.pop(normalized_id, None) or self._tokenize(text)
        for w in words:
            self._index[w].discard(normalized_id)
            if not self._index[w]:
                self._index.pop(w, None)
        self._node_texts.pop(normalized_id, None)
        if self._bm25 is not None:
            self._bm25.remove(normalized_id)

    def search_ranked(self, query: str, top_k: int | None = None) -> List[str]:
        words = self._tokenize(query)
        if not words:
            return []

        if self._bm25 is not None:
            ranked = [node_id for node_id, _score in self._bm25.ranked(query, limit=top_k)]
            if ranked:
                return ranked

        scores: Dict[str, int] = defaultdict(int)
        first_hit_order: Dict[str, int] = {}
        for word_order, word in enumerate(words):
            for node_id in self._index.get(word, set()):
                scores[node_id] += 1
                first_hit_order.setdefault(node_id, word_order)

        ranked = sorted(
            scores.keys(),
            key=lambda node_id: (-scores[node_id], first_hit_order[node_id], node_id),
        )
        if top_k is not None:
            return ranked[:top_k]
        return ranked

    def search(self, query: str) -> Set[str]:
        return set(self.search_ranked(query))

    def _tokenize(self, text: str) -> List[str]:
        return tokenize_retrieval_text(text, limit=None)

    def clear(self):
        self._index.clear()
        self._node_terms.clear()
        self._node_texts.clear()
        if self._bm25 is not None:
            self._bm25 = BM25Index(k1=self._bm25._scorer.k1, b=self._bm25._scorer.b)
