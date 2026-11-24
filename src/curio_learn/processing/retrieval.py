"""Finding the transcript passages that answer a question.

Used by Ask the Video when a transcript is too long to show the model in full.
Two rankers, fused by reciprocal rank:

- BM25 over transcript blocks: exact terms, names and numbers. No dependencies.
- Embedding similarity (optional): paraphrases and questions worded differently
  from the talk. Any `Embedder` works; the default is Ollama's `nomic-embed-text`.
  Block vectors are computed once per video and stored.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import Counter
from collections.abc import Sequence
from typing import Literal, Protocol

from curio_learn.analysis.redundancy import tokenize
from curio_learn.domain import Block

log = logging.getLogger(__name__)


class Embedder(Protocol):
    name: str  # e.g. "ollama/nomic-embed-text"

    def embed(self, texts: list[str], *, kind: Literal["query", "document"]) -> list[list[float]]: ...


class OllamaEmbedder:
    """nomic-embed-text expects task prefixes on queries and documents."""

    _PREFIX = {"query": "search_query: ", "document": "search_document: "}

    def __init__(self, host: str, model: str, *, timeout_s: float = 120.0, client=None, batch: int = 64):
        import ollama

        self._client = client or ollama.Client(host=host, timeout=timeout_s)
        self.model = model
        self.name = f"ollama/{model}"
        self._batch = batch

    def embed(self, texts: list[str], *, kind: Literal["query", "document"]) -> list[list[float]]:
        prefix = self._PREFIX[kind] if "nomic" in self.model else ""
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            chunk = [prefix + t for t in texts[i : i + self._batch]]
            out.extend(self._client.embed(model=self.model, input=chunk, truncate=True, keep_alive="10m").embeddings)
        return out


def blocks_key(blocks: Sequence[Block]) -> str:
    h = hashlib.sha256()
    for b in blocks:
        h.update(f"{b.start_s:.3f}|{b.text}\n".encode())
    return h.hexdigest()[:32]


def bm25_scores(query: str, docs: Sequence[str], *, k1: float = 1.5, b: float = 0.75) -> list[float]:
    q = set(tokenize(query))
    toks = [tokenize(d) for d in docs]
    if not q or not toks:
        return [0.0] * len(docs)
    n = len(toks)
    avg = sum(len(t) for t in toks) / n or 1.0
    df = Counter(term for t in toks for term in set(t) if term in q)
    idf = {term: math.log(1 + (n - c + 0.5) / (c + 0.5)) for term, c in df.items()}
    scores = []
    for t in toks:
        tf = Counter(w for w in t if w in idf)
        s = 0.0
        for term, f in tf.items():
            s += idf[term] * f * (k1 + 1) / (f + k1 * (1 - b + b * len(t) / avg))
        scores.append(s)
    return scores


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def rank(
    query: str,
    blocks: Sequence[Block],
    *,
    query_vector: Sequence[float] | None = None,
    block_vectors: Sequence[Sequence[float]] | None = None,
    rrf_k: int = 60,
) -> list[int]:
    """Block indices, most relevant first (reciprocal rank fusion of the rankers)."""
    rankings = []
    lexical = bm25_scores(query, [b.text for b in blocks])
    if any(lexical):
        rankings.append(sorted(range(len(blocks)), key=lambda i: -lexical[i]))
    if query_vector is not None and block_vectors is not None and len(block_vectors) == len(blocks):
        semantic = [cosine(query_vector, v) for v in block_vectors]
        rankings.append(sorted(range(len(blocks)), key=lambda i: -semantic[i]))
    if not rankings:
        return list(range(len(blocks)))
    fused: dict[int, float] = {}
    for r in rankings:
        for pos, i in enumerate(r):
            fused[i] = fused.get(i, 0.0) + 1.0 / (rrf_k + pos + 1)
    return sorted(fused, key=lambda i: -fused[i])


_CLOCK_RE = re.compile(r"\b(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\b")
_MINUTES_RE = re.compile(
    r"\b(?:(\d+)\s*(?:h|hr|hrs|hours?)\s*(?:and\s*)?)?(\d+)\s*(?:m|min|mins|minutes?)\b"
    r"|\bminute\s+(\d+)\b|\b(\d+)\s*(?:h|hr|hrs|hours?)\b",
    re.IGNORECASE,
)


def time_hints(question: str) -> list[float]:
    """Seconds the learner points at: "at 23:10", "around 1:05:00", "23 minutes in",
    "minute 23", "1 hour 5 minutes"."""
    out = []
    for h, m, s in _CLOCK_RE.findall(question):
        out.append(int(h or 0) * 3600 + int(m) * 60 + int(s))
    for h, m, minute, hours in _MINUTES_RE.findall(question):
        if minute:
            out.append(int(minute) * 60)
        elif m:
            out.append(int(h or 0) * 3600 + int(m) * 60)
        elif hours:
            out.append(int(hours) * 3600)
    return sorted(set(float(t) for t in out))
