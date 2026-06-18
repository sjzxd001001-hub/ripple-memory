"""BM25 retrieval implementation."""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

Segmenter = Optional[Callable[[str], List[str]]]
MAX_RETRIEVAL_TOKEN_CHARS = 128


def tokenize_retrieval_text(text: Any, *, segmenter: Segmenter = None, limit: Optional[int] = 64) -> List[str]:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return []

    tokens: List[str] = []
    seen: set[str] = set()

    def remember(raw_token: Any) -> None:
        token = str(raw_token or "").strip().lower()
        if len(token) > MAX_RETRIEVAL_TOKEN_CHARS:
            token = token[:MAX_RETRIEVAL_TOKEN_CHARS]
        if len(token) <= 1 or token in seen:
            return
        tokens.append(token)
        seen.add(token)

    # ASCII tokens
    for token in re.findall(r"[a-z0-9_]+", normalized):
        remember(token)

    # Chinese tokens (bigram fallback)
    for segment in re.findall(r"[一-鿿]+", normalized):
        parts: List[str] = []
        if segmenter is not None:
            try:
                parts = list(segmenter(segment) or [])
            except Exception:
                parts = []
        for part in parts:
            remember(part)
        if parts:
            continue
        if len(segment) <= 4:
            remember(segment)
        else:
            for index in range(len(segment) - 1):
                remember(segment[index: index + 2])

    # Full tokens (mixed)
    for token in re.findall(r"[\w一-鿿]+", normalized):
        remember(token)

    if limit is None:
        return tokens
    return tokens[:max(1, int(limit or 64))]


@dataclass(slots=True)
class BM25DocumentStats:
    doc_id: str
    term_freq: Dict[str, int]
    doc_len: int

    @classmethod
    def from_tokens(cls, doc_id: str, tokens: Iterable[str]) -> BM25DocumentStats:
        normalized = [str(token or "").strip().lower() for token in tokens if str(token or "").strip()]
        return cls(doc_id=str(doc_id), term_freq=dict(Counter(normalized)), doc_len=len(normalized))


class BM25Scorer:
    def __init__(self, *, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = float(k1 or 1.5)
        self.b = float(b or 0.75)

    def score(
        self,
        query_tokens: Iterable[str],
        doc_stats: BM25DocumentStats,
        *,
        doc_freq: Dict[str, int],
        total_docs: int,
        avg_doc_len: float,
    ) -> float:
        query_terms = list(dict.fromkeys(
            str(t or "").strip().lower() for t in query_tokens if str(t or "").strip()
        ))
        if not query_terms or doc_stats.doc_len <= 0 or total_docs <= 0:
            return 0.0
        avgdl = max(1e-9, float(avg_doc_len or 0.0))
        score = 0.0
        for term in query_terms:
            tf = int(doc_stats.term_freq.get(term, 0) or 0)
            if tf <= 0:
                continue
            df = max(0, int(doc_freq.get(term, 0) or 0))
            idf = math.log(1.0 + (float(total_docs) - float(df) + 0.5) / (float(df) + 0.5))
            denominator = tf + self.k1 * (1.0 - self.b + self.b * (float(doc_stats.doc_len) / avgdl))
            if denominator <= 0.0:
                continue
            score += idf * ((tf * (self.k1 + 1.0)) / denominator)
        return float(max(0.0, score))


class BM25Index:
    """In-memory BM25 index."""

    def __init__(self, *, k1: float = 1.5, b: float = 0.75, segmenter: Segmenter = None, token_limit: int = 64) -> None:
        self._scorer = BM25Scorer(k1=k1, b=b)
        self._segmenter = segmenter
        self._token_limit = int(token_limit or 64)
        self._term_freqs: Dict[str, Dict[str, int]] = {}
        self._doc_lengths: Dict[str, int] = {}
        self._doc_freqs: Dict[str, int] = {}
        self._avg_doc_len = 0.0

    def tokenize(self, text: Any) -> List[str]:
        return tokenize_retrieval_text(text, segmenter=self._segmenter, limit=self._token_limit)

    def add(self, doc_id: str, text: Any) -> None:
        normalized_id = str(doc_id or "").strip()
        if not normalized_id:
            return
        self.remove(normalized_id)
        tokens = self.tokenize(text)
        term_freq = dict(Counter(tokens))
        self._term_freqs[normalized_id] = term_freq
        self._doc_lengths[normalized_id] = len(tokens)
        for term in term_freq:
            self._doc_freqs[term] = int(self._doc_freqs.get(term, 0) or 0) + 1
        self._recompute_avg_doc_len()

    def remove(self, doc_id: str) -> None:
        normalized_id = str(doc_id or "").strip()
        if not normalized_id:
            return
        term_freq = self._term_freqs.pop(normalized_id, None)
        self._doc_lengths.pop(normalized_id, None)
        if not term_freq:
            self._recompute_avg_doc_len()
            return
        for term in term_freq:
            next_value = int(self._doc_freqs.get(term, 0) or 0) - 1
            if next_value <= 0:
                self._doc_freqs.pop(term, None)
            else:
                self._doc_freqs[term] = next_value
        self._recompute_avg_doc_len()

    def ranked(self, query: Any, *, limit: int | None = None) -> List[Tuple[str, float]]:
        query_tokens = self.tokenize(query)
        if not query_tokens or not self._term_freqs:
            return []
        total_docs = len(self._term_freqs)
        scores: Dict[str, float] = {}
        for doc_id, term_freq in self._term_freqs.items():
            stats = BM25DocumentStats(
                doc_id=doc_id,
                term_freq=dict(term_freq),
                doc_len=int(self._doc_lengths.get(doc_id, 0) or 0),
            )
            score = self._scorer.score(
                query_tokens, stats,
                doc_freq=dict(self._doc_freqs),
                total_docs=total_docs,
                avg_doc_len=self._avg_doc_len,
            )
            if score > 0.0:
                scores[doc_id] = float(score)
        rows = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        if limit is None:
            return rows
        return rows[:max(0, int(limit or 0))]

    def _recompute_avg_doc_len(self) -> None:
        if not self._doc_lengths:
            self._avg_doc_len = 0.0
            return
        self._avg_doc_len = sum(self._doc_lengths.values()) / max(1, len(self._doc_lengths))
