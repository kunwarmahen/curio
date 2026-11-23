"""Step 8: lexical redundancy between sections (TF-IDF cosine, no external deps).

This is deliberately simple and deterministic. It complements the LLM's
`redundant_with` judgment and can later be replaced by embedding similarity.
"""

from __future__ import annotations

import math
import re
from collections import Counter

_TOKEN_RE = re.compile(r"[a-z][a-z0-9'+-]{2,}")
_STOP = frozenset(
    """
the and that this with you for are was were but not have has had they them their there
what when where which who will would could should can just like so really very going
about into from your our its it's then than also because been being some any all more
most other such only over know think yeah okay right actually kind sort thing things way
well let get got one two here now see say said make made want need lot let's we're
you're i'm that's don't doesn't it'll gonna maybe much many mean even still these those
does did
""".split()
)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOP]


def similarity_matrix(texts: list[str]) -> list[list[float]]:
    docs = [Counter(tokenize(t)) for t in texts]
    n = len(docs)
    df: Counter[str] = Counter()
    for d in docs:
        df.update(d.keys())
    idf = {term: math.log((1 + n) / (1 + c)) + 1.0 for term, c in df.items()}
    vecs = []
    for d in docs:
        v = {t: (1 + math.log(c)) * idf[t] for t, c in d.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs.append({t: x / norm for t, x in v.items()})
    sim = [[0.0] * n for _ in range(n)]
    for i in range(n):
        sim[i][i] = 1.0
        for j in range(i + 1, n):
            a, b = vecs[i], vecs[j]
            if len(a) > len(b):
                a, b = b, a
            s = sum(x * b.get(t, 0.0) for t, x in a.items())
            sim[i][j] = sim[j][i] = s
    return sim
