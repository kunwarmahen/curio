"""Curricula: one goal across courses and videos, in the order things build on each other.

No model calls here. Each section's evaluation lists `prerequisites` (short phrases such
as "Basic neural network concepts") and `introduces_concepts`; a section needs another
source when a prerequisite isn't taught earlier in its own video but matches concepts
another source teaches. Those needs order the sources ("whole lectures") or the chosen
sections ("section by section"); where nothing is needed, the given order stands.

A course keeps its own lecture order (its instructor's): lectures of one course share
vocabulary, and matching their prerequisites put CS329Z's lecture 7 before lecture 2.
Needs only decide where other courses and videos go around it.
"""

from __future__ import annotations

import math
import operator
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel

from curio_learn.analysis import prompts
from curio_learn.domain import CurriculumCandidate, Importance, ScoredSection, VideoAnalysis
from curio_learn.errors import LLMError
from curio_learn.llm.base import LLMProvider, generate_structured

# Words that say how much is needed, not what: they never make a match on their own.
_FILLER = {
    "a", "an", "and", "the", "of", "in", "on", "for", "to", "with", "how", "what", "its", "their", "or", "vs",
    "basic", "basics", "general", "some", "prior", "previous", "earlier", "high", "level", "simple",
    "understanding", "understand", "knowledge", "familiarity", "familiar", "awareness", "concept", "concepts",
    "idea", "ideas", "experience", "background", "introduction", "intro", "overview", "fundamental",
    "fundamentals", "core", "key", "common", "standard", "notion", "principle", "principles", "using", "use",
}  # fmt: skip
# Words too broad to tie two lectures together alone ("model", "system", "agent" in an agents course).
_BROAD = {
    "model", "models", "system", "systems", "data", "learning", "method", "methods", "process", "task",
    "tasks", "problem", "problems", "approach", "technique", "techniques", "type", "types", "language",
    "agent", "ai", "network", "training", "performance", "evaluation", "design", "architecture",
}  # fmt: skip
_SKIPPED = {Importance.SKIP}


def _words(text: str) -> list[str]:
    """Content words of a phrase, in order, lowercase, roughly singular."""
    out = []
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in _FILLER:
            continue
        if len(w) > 4 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


def concept_tokens(text: str, acronyms: frozenset[str] = frozenset()) -> frozenset[str]:
    """Content words of a concept or prerequisite, lowercase, roughly singular. A run of
    two to four words whose initials are one of `acronyms` reads as that acronym, so
    "large language models" matches "LLM" ("Basic knowledge of LLMs" found nothing)."""
    words = _words(text)
    if not acronyms:
        return frozenset(words)
    out, i = [], 0
    while i < len(words):
        for n in (4, 3, 2):
            run = words[i : i + n]
            if len(run) == n and "".join(w[0] for w in run) in acronyms:
                out.append("".join(w[0] for w in run))
                i += n
                break
        else:
            out.append(words[i])
            i += 1
    return frozenset(out)


def library_acronyms(phrases: Sequence[str]) -> frozenset[str]:
    """Acronyms the library writes in capitals ("LLM", "RAGs" -> "rag"), lowercase."""
    found = set()
    for p in phrases:
        for w in re.findall(r"\b[A-Z]{2,5}s?\b", p):
            found.add(w.lower().rstrip("s") if len(w) > 2 and w.endswith("s") else w.lower())
    return frozenset(found)


Similar = Callable[[str, str], bool]  # (prerequisite, concept) -> close in meaning


class SemanticMatcher:
    """Prerequisite and concept phrases close in meaning: cosine of their embeddings at
    least `threshold`. On CS329Z, 0.8 and up were right ("Understanding of agent
    architectures" ~ "Agent Architecture", which the word rule misses: both words are
    broad); 0.75-0.8 mixed ("Policy Gradient methods" ~ "Feedback gradients")."""

    def __init__(self, vectors: dict[str, Sequence[float]], threshold: float = 0.8):
        self._v = {t: _unit(v) for t, v in vectors.items()}
        self._t = threshold
        self._memo: dict[tuple[str, str], bool] = {}

    def __call__(self, need: str, concept: str) -> bool:
        k = (need, concept)
        if k not in self._memo:
            a, b = self._v.get(need), self._v.get(concept)
            self._memo[k] = a is not None and b is not None and sum(map(operator.mul, a, b)) >= self._t
        return self._memo[k]


def _unit(v: Sequence[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def phrases(sources: Sequence[Source]) -> list[str]:
    """Every prerequisite and concept phrase of the sources, once."""
    out: dict[str, None] = {}
    for src in sources:
        for s in src.analysis.sections:
            for p in [*s.evaluation.prerequisites, *s.evaluation.introduces_concepts, *s.section.key_concepts]:
                if p.strip():
                    out[p] = None
    return list(out)


def matches(need: frozenset[str], concept: frozenset[str]) -> bool:
    """A concept covers a need when it holds every content word of the need ("Neural
    network as a function" covers "Basic neural network concepts"), or the need holds the
    whole concept of two or more words; and they share a word that isn't broad. "Vectors"
    doesn't cover "basic vector search"; "model" alone covers nothing."""
    if not need or not concept:
        return False
    covered = need <= concept or (len(concept) >= 2 and concept <= need)
    return covered and bool((need & concept) - _BROAD)


@dataclass
class Source:
    """One video in the curriculum, at its place in the given order."""

    key: str  # an id for the caller (a video id)
    analysis: VideoAnalysis
    rank: int  # position in the given order
    group: str = ""  # its course; a lone video is its own group (defaults to its key)

    @property
    def unit(self) -> str:
        return self.group or self.key


@dataclass
class Need:
    """A section of `source` that needs a concept another source teaches."""

    section_id: str
    prerequisite: str
    taught_by: dict[str, str] = field(default_factory=dict)  # source key -> its section that teaches it


def _taught(sections: Sequence[ScoredSection], acronyms: frozenset[str]) -> list[tuple[str, str, frozenset[str]]]:
    """(section id, concept, its words) for every concept a teaching section introduces
    or covers."""
    out = []
    for s in sections:
        if s.importance in _SKIPPED and s.section.kind.value in ("sponsor", "meta"):
            continue
        for c in [*s.evaluation.introduces_concepts, *s.section.key_concepts]:
            t = concept_tokens(c, acronyms)
            if t:
                out.append((s.section.id, c, t))
    return out


def outside_needs(sources: Sequence[Source], similar: Similar | None = None) -> dict[str, list[Need]]:
    """For each source, the prerequisites of its sections that neither it (earlier) nor an
    earlier lecture of its own course teaches, with the sources of other courses (and
    their sections) that teach them. A concept teaches a prerequisite when its words
    cover it (`matches`, with the library's acronyms) or, given `similar`, when the two
    are close in meaning."""
    acronyms = library_acronyms(phrases(sources))
    taught = {src.key: _taught(src.analysis.sections, acronyms) for src in sources}
    out: dict[str, list[Need]] = {}
    for src in sources:
        order = [s.section.id for s in src.analysis.sections]
        before_me = [o for o in sources if o.unit == src.unit and o.rank < src.rank]
        needs = []
        for i, s in enumerate(src.analysis.sections):
            earlier = set(order[: i + 1])
            for p in s.evaluation.prerequisites:
                t = concept_tokens(p, acronyms)
                if not t:
                    continue

                def covers(c: str, ct: frozenset[str], p: str = p, t: frozenset[str] = t) -> bool:
                    return matches(t, ct) or (similar is not None and similar(p, c))

                if any(sid in earlier and covers(c, ct) for sid, c, ct in taught[src.key]):
                    continue  # taught earlier in this video, or by this very section
                if any(covers(c, ct) for o in before_me for _, c, ct in taught[o.key]):
                    continue  # an earlier lecture of the same course teaches it
                by = {}
                for other in sources:
                    if other.unit == src.unit:
                        continue
                    hit = next((sid for sid, c, ct in taught[other.key] if covers(c, ct)), None)
                    if hit:
                        by[other.key] = hit
                if by:
                    needs.append(Need(section_id=s.section.id, prerequisite=p, taught_by=by))
        out[src.key] = needs
    return out


@dataclass
class Edge:
    before: str  # source key
    after: str
    weight: float  # `after`'s needs that `before` teaches, each split among its teachers
    examples: list[str]  # those prerequisites, for the reason shown to the learner


def source_edges(sources: Sequence[Source], *, min_weight: float = 1.5, similar: Similar | None = None) -> list[Edge]:
    """`A` goes before the first lecture of a course (or a lone video) that needs it when
    A teaches enough of that course's outside needs: each distinct need of a lecture
    counts 1, split among the sources that teach it (a need seven lectures teach, "basic
    knowledge of LLMs", says little about order; one lecture's five "Basic Python"s are
    one need), summed over the course's lectures (3Blue1Brown teaches lectures 2 and 5 of
    CS329Z one need each), and A must teach clearly more of the course's needs than the
    course teaches of A's."""
    needs = outside_needs(sources, similar)
    unit = {s.key: s.unit for s in sources}
    rank = {s.key: s.rank for s in sources}
    weight: dict[tuple[str, str], float] = {}  # (teacher, needing unit)
    first: dict[tuple[str, str], str] = {}  # its first lecture that needs the teacher
    examples: dict[tuple[str, str], list[str]] = {}
    for src in sources:
        seen: set[tuple[frozenset[str], str]] = set()
        for n in needs[src.key]:
            for other in n.taught_by:
                k = (other, unit[src.key])
                dedup = (concept_tokens(n.prerequisite), other)
                if dedup in seen:
                    continue
                seen.add(dedup)
                weight[k] = weight.get(k, 0.0) + 1 / len(n.taught_by)
                examples.setdefault(k, []).append(n.prerequisite)
                if k not in first or rank[src.key] < rank[first[k]]:
                    first[k] = src.key
    edges = []
    for (a, u), w in weight.items():
        back = sum(v for (b, uu), v in weight.items() if uu == unit[a] and unit[b] == u)
        if w >= min_weight and w >= 2 * back:
            edges.append(Edge(a, first[(a, u)], round(w, 2), list(dict.fromkeys(examples[(a, u)]))[:3]))
    return edges


def order_sources(sources: Sequence[Source], edges: Sequence[Edge]) -> tuple[list[str], list[Edge]]:
    """Source keys in an order that respects the edges and every course's own order,
    otherwise the given order (`rank`), and the edges it kept. An edge that would close
    a cycle is ignored, weakest first."""
    ordered = sorted(sources, key=lambda s: s.rank)
    keys = [s.key for s in ordered]
    rank = {s.key: s.rank for s in sources}
    fixed: list[Edge] = []
    for unit in dict.fromkeys(s.unit for s in ordered):
        mine = [s.key for s in ordered if s.unit == unit]
        fixed += [Edge(a, b, float("inf"), []) for a, b in zip(mine, mine[1:], strict=False)]
    kept = list(fixed)
    for e in sorted(edges, key=lambda e: -e.weight):
        if not _reaches(e.after, e.before, kept):
            kept.append(e)
    return _stable_topo(keys, [(e.before, e.after) for e in kept], rank), kept[len(fixed) :]


def _reaches(start: str, goal: str, edges: Sequence[Edge]) -> bool:
    seen, stack = set(), [start]
    while stack:
        x = stack.pop()
        if x == goal:
            return True
        if x not in seen:
            seen.add(x)
            stack.extend(e.after for e in edges if e.before == x)
    return False


def _stable_topo(keys: list[str], edges: list[tuple[str, str]], rank: dict[str, float]) -> list[str]:
    """Keys in `rank` order, each one's predecessors pulled in just before it (their own
    first, lowest rank first). A source needed by a later lecture moves up to just before
    it; everything else keeps its place. Picking the lowest-ranked ready key instead let
    an unrelated video jump ahead whenever a course waited on a foundation."""
    preds: dict[str, set[str]] = {k: set() for k in keys}
    for a, b in edges:
        if a in preds and b in preds and a != b:
            preds[b].add(a)
    out: list[str] = []
    placed: set[str] = set()
    visiting: set[str] = set()

    def place(k: str) -> None:
        if k in placed or k in visiting:  # visiting: a cycle the caller didn't remove
            return
        visiting.add(k)
        for p in sorted(preds[k], key=lambda x: rank[x]):
            place(p)
        visiting.discard(k)
        placed.add(k)
        out.append(k)

    for k in sorted(keys, key=lambda x: rank[x]):
        place(k)
    return out


@dataclass
class Placed:
    """A chosen section, for ordering section by section."""

    key: str  # "L3.S05"
    source: str  # its source key
    section_id: str
    start_s: float


def order_sections(
    placed: Sequence[Placed],
    sources: Sequence[Source],
    source_order: Sequence[str],
    similar: Similar | None = None,
) -> list[str]:
    """Chosen section keys, section by section: a section moves ahead of a section in
    another source that needs what it teaches; otherwise sources in `source_order`, and
    within a source always in time order."""
    pos = {k: i for i, k in enumerate(source_order)}
    rank = {p.key: pos.get(p.source, len(pos)) * 1e6 + p.start_s for p in placed}
    by_section = {(p.source, p.section_id): p.key for p in placed}
    edges: list[tuple[str, str]] = []
    # Within a source: time order, always; and a course's lectures in the course's order
    # (lecture 3's sections pulled ahead of a video bring lecture 2's along first).
    unit = {s.key: s.unit for s in sources}
    last_of: dict[str, str] = {}
    for src in source_order:
        mine = sorted((p for p in placed if p.source == src), key=lambda p: p.start_s)
        edges += [(a.key, b.key) for a, b in zip(mine, mine[1:], strict=False)]
        if mine:
            u = unit.get(src, src)
            if u in last_of:
                edges.append((last_of[u], mine[0].key))
            last_of[u] = mine[-1].key
    # Across sources: a chosen teacher before the chosen section that needs it.
    needs = outside_needs(sources, similar)
    for src in sources:
        for n in needs[src.key]:
            after = by_section.get((src.key, n.section_id))
            if after is None:
                continue
            for other, sid in n.taught_by.items():
                before = by_section.get((other, sid))
                if before is not None and rank[before] > rank[after]:
                    edges.append((before, after))
    # Drop cross edges that would close a cycle (the order edges come first and stay).
    kept: list[tuple[str, str]] = []
    for a, b in edges:
        if not _reaches_pairs(b, a, kept):
            kept.append((a, b))
    return _stable_topo([p.key for p in sorted(placed, key=lambda p: rank[p.key])], kept, rank)


def _reaches_pairs(start: str, goal: str, edges: Sequence[tuple[str, str]]) -> bool:
    seen, stack = set(), [start]
    while stack:
        x = stack.pop()
        if x == goal:
            return True
        if x not in seen:
            seen.add(x)
            stack.extend(b for a, b in edges if a == x)
    return False


# ------------------------------------------------------------------ suggesting sources


class PickOut(BaseModel):
    id: str
    include: bool
    reason: str


class PicksOut(BaseModel):
    picks: list[PickOut]


def suggest_sources(
    provider: LLMProvider,
    *,
    goal: str,
    candidates: Sequence[CurriculumCandidate],
    about: dict[str, str],
    model: str,
    retries: int = 2,
) -> list[CurriculumCandidate]:
    """Mark the candidates that serve `goal`, with a reason, in one model call (ids are
    short numbers so the model can't garble them). If the model fails, candidates sharing
    a content word with the goal are suggested instead."""
    short = {f"c{i}": c for i, c in enumerate(candidates, 1)}
    text = "\n".join(
        f"[{sid}] {c.kind}: {c.title} ({c.detail}). {about.get(c.id, '')[:500]}" for sid, c in short.items()
    )
    out = [c.model_copy() for c in candidates]
    by_short = dict(zip(short, out, strict=True))
    try:
        res = generate_structured(
            provider,
            PicksOut,
            model=model,
            system=prompts.CURRICULUM_SUGGEST_SYSTEM,
            user=prompts.curriculum_suggest_user(goal=goal, candidates=text, ids=list(short)),
            max_tokens=80 * len(short) + 300,
            retries=retries,
            task="curriculum[suggest]",
            temperature=0.0,
        )
        for p in res.picks:
            c = by_short.get(p.id.strip().strip("[]").lower())
            if c is not None:
                c.suggested, c.reason = p.include, " ".join(p.reason.split())
    except LLMError:
        want = concept_tokens(goal) - _BROAD
        for c in out:
            words = concept_tokens(f"{c.title} {about.get(c.id, '')}")
            c.suggested = bool(want & words)
            c.reason = "Shares words with your goal (Curio couldn't ask the model)." if c.suggested else ""
    return sorted(out, key=lambda c: not c.suggested)  # stable: suggestions first, library order otherwise
