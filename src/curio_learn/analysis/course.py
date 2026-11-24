"""A course playlist: the best parts of several lectures under one time budget.

1. Collect every watchable section of every lecture (no intros, outros, housekeeping
   or sponsor reads, nothing rated skip, no optional section under 2 minutes). Scores come from each lecture's analysis;
   half of each is the section's rank within its lecture, so they compare across
   lectures.
2. Find sections in different lectures that may teach the same thing: embedding
   similarity of title, summary and concepts (TF-IDF without an embedder), best match
   per section, the closest pairs first.
3. The LLM judges those pairs from summaries and transcript excerpts: repeat (watch
   one), overlap (both add something) or different, and which one to keep.
4. One selection across the course (the same selector as a lecture's playlist): a
   repeat's other half is ruled out once one half is chosen, and the half to skip
   starts lower. Chosen sections play in lecture order, neighbours merged.
5. A checklist of what's easy to miss for the learner's goal, written from the
   course outline; every point cites sections, and code keeps only real ones.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from curio_learn.analysis import prompts
from curio_learn.analysis.playlist import PlaylistConfig, _segment, select_sections
from curio_learn.analysis.profiles import learner_brief
from curio_learn.analysis.redundancy import similarity_matrix
from curio_learn.domain import (
    AnalysisStatus,
    ChecklistItem,
    CoursePair,
    CoursePart,
    CoursePlan,
    CourseRef,
    CourseRepeat,
    Importance,
    LearningPreferences,
    LLMInfo,
    ScoredSection,
    SectionKind,
    VideoAnalysis,
    fmt_ts,
)
from curio_learn.errors import LLMError
from curio_learn.llm.base import LLMProvider, generate_structured
from curio_learn.processing.normalize import Utterance, approx_tokens
from curio_learn.processing.retrieval import Embedder, cosine

log = logging.getLogger(__name__)

Progress = Callable[[str], None]

NOT_WATCHABLE = {SectionKind.INTRO, SectionKind.OUTRO, SectionKind.META, SectionKind.SPONSOR}
# Optional sections shorter than this only fill leftover time, out of context.
MIN_OPTIONAL_S = 120.0
# A repeat's weaker half starts this many points lower, so the stronger half is tried first.
REPEAT_PENALTY = 15.0
PAIRS_PER_CALL = 12


class PairVerdictOut(BaseModel):
    pair: str
    relation: Literal["repeat", "overlap", "different"]
    keep: Literal["A", "B"]
    reason: str


class OverlapOut(BaseModel):
    verdicts: list[PairVerdictOut]


class ChecklistPointOut(BaseModel):
    text: str
    why: str
    sections: list[str]


class ChecklistOut(BaseModel):
    points: list[ChecklistPointOut]


@dataclass
class Lecture:
    position: int
    analysis: VideoAnalysis
    units: Sequence[Utterance]
    label: str = ""  # "Lecture 3", "Guest lecture"; defaults to the position

    @property
    def name(self) -> str:
        return self.label or f"Lecture {self.position}"


@dataclass
class Item:
    key: str  # "L3.S05"
    lecture: Lecture
    scored: ScoredSection

    @property
    def section(self):
        return self.scored.section

    def ref(self) -> CourseRef:
        s = self.section
        return CourseRef(
            lecture=self.lecture.position,
            video_id=self.lecture.analysis.video.video_id,
            analysis_id=self.lecture.analysis.id,
            section_id=s.id,
            title=s.title,
            start_s=s.start_s,
            end_s=s.end_s,
        )

    def describe(self) -> str:
        s, ev = self.section, self.scored.evaluation
        concepts = f" Concepts: {', '.join(s.key_concepts)}." if s.key_concepts else ""
        learn = f" Teaches: {ev.what_you_learn}" if ev.what_you_learn else ""
        return f"{s.title}. {s.summary}{concepts}{learn}"


@dataclass
class Pair:
    id: str
    a: Item  # the earlier lecture
    b: Item
    similarity: float
    relation: str = ""
    keep: str = ""
    reason: str = ""

    @property
    def kept(self) -> Item:
        return self.a if self.keep == "A" else self.b

    @property
    def other(self) -> Item:
        return self.b if self.keep == "A" else self.a


def course_items(lectures: Sequence[Lecture]) -> list[Item]:
    return [
        Item(f"L{lec.position}.{s.section.id}", lec, s)
        for lec in lectures
        for s in lec.analysis.sections
        if s.importance != Importance.SKIP
        and s.section.kind not in NOT_WATCHABLE
        and not (s.importance == Importance.OPTIONAL and s.section.duration_s < MIN_OPTIONAL_S)
    ]


def _similarities(items: list[Item], embedder: Embedder | None) -> list[list[float]]:
    texts = [it.describe() for it in items]
    if embedder is not None:
        try:
            vecs = embedder.embed(texts, kind="document")
            return [[cosine(a, b) for b in vecs] for a in vecs]
        except Exception as exc:  # embeddings are an optional improvement
            log.warning("Embeddings unavailable for the course (%s); using word overlap", exc)
    return similarity_matrix(texts)


def candidate_pairs(items: list[Item], sim: list[list[float]], *, max_pairs: int) -> list[Pair]:
    """Each section's closest section in another lecture; the closest pairs overall."""
    best: dict[tuple[int, int], float] = {}
    for i, it in enumerate(items):
        others = [j for j, o in enumerate(items) if o.lecture.position != it.lecture.position]
        if not others:
            continue
        j = max(others, key=lambda j: sim[i][j])
        a, b = sorted((i, j), key=lambda k: (items[k].lecture.position, items[k].section.start_s))
        best[(a, b)] = sim[i][j]
    ranked = sorted(best.items(), key=lambda kv: -kv[1])[:max_pairs]
    return [Pair(f"p{n}", items[a], items[b], s) for n, ((a, b), s) in enumerate(ranked, 1)]


def _excerpt(item: Item, words: int = 160) -> str:
    """What is said in the section: its opening and a passage from the middle."""
    s = item.section
    text = " ".join(u.text for u in item.lecture.units if s.start_s <= u.start_s < s.end_s).split()
    if len(text) <= words:
        return " ".join(text)
    half = words // 2
    mid = len(text) // 2
    return " ".join(text[:half]) + " … " + " ".join(text[mid : mid + half])


def _render_pair(p: Pair) -> str:
    def side(label: str, it: Item) -> str:
        s = it.section
        return (
            f"{label}: {it.lecture.name} ({it.lecture.analysis.video.title}), "
            f'{fmt_ts(s.start_s)}–{fmt_ts(s.end_s)}\n{it.describe()}\nExcerpt: "{_excerpt(it)}"'
        )

    return f"### Pair {p.id}\n{side('A', p.a)}\n{side('B', p.b)}"


def judge_pairs(
    provider: LLMProvider,
    pairs: list[Pair],
    *,
    model: str,
    shared: str,
    retries: int,
    progress: Progress,
) -> int:
    """Fill in each pair's verdict. Returns how many pairs got none."""
    by_id = {p.id: p for p in pairs}
    batches = [pairs[i : i + PAIRS_PER_CALL] for i in range(0, len(pairs), PAIRS_PER_CALL)]
    for n, batch in enumerate(batches, 1):
        progress(f"Checking {len(pairs)} possible repeats across lectures (batch {n} of {len(batches)})")
        out = generate_structured(
            provider,
            OverlapOut,
            model=model,
            system=prompts.COURSE_OVERLAP_SYSTEM,
            user=prompts.course_overlap_user(
                pairs="\n\n".join(_render_pair(p) for p in batch), ids=[p.id for p in batch]
            ),
            shared_prefix=shared,
            max_tokens=90 * len(batch) + 300,
            retries=retries,
            task=f"course[overlap {n}]",
        )
        for v in out.verdicts:
            p = by_id.get(v.pair.strip().lower())
            if p is not None and not p.relation:
                p.relation, p.keep, p.reason = v.relation, v.keep, " ".join(v.reason.split())
    return sum(not p.relation for p in pairs)


def _outline(items: list[Item], lectures: Sequence[Lecture], budget_tokens: int) -> str:
    """The course outline for the checklist; summaries of weaker sections are dropped
    when it doesn't fit."""

    def render(full: set[str]) -> str:
        parts = []
        for lec in lectures:
            parts.append(f"## {lec.name}: {lec.analysis.video.title}")
            for it in items:
                if it.lecture is not lec:
                    continue
                s = it.section
                line = f"{it.key} [{fmt_ts(s.start_s)}] {s.title}"
                if it.key in full:
                    concepts = f" ({', '.join(s.key_concepts)})" if s.key_concepts else ""
                    line += f": {s.summary}{concepts}"
                parts.append(line)
        return "\n".join(parts)

    ranked = sorted(items, key=lambda it: -it.scored.score.final)
    keep = len(ranked)
    text = render({it.key for it in ranked})
    while approx_tokens(text) > budget_tokens and keep > 0:
        keep = int(keep * 0.8)
        text = render({it.key for it in ranked[:keep]})
    return text


def write_checklist(
    provider: LLMProvider,
    items: list[Item],
    lectures: Sequence[Lecture],
    *,
    chosen: set[str],
    model: str,
    shared: str,
    budget_tokens: int,
    retries: int,
) -> tuple[list[ChecklistItem], int]:
    """The checklist and how many points cited no real section (dropped)."""
    by_key = {it.key: it for it in items}
    out = generate_structured(
        provider,
        ChecklistOut,
        model=model,
        system=prompts.COURSE_CHECKLIST_SYSTEM,
        user=prompts.course_checklist_user(outline=_outline(items, lectures, budget_tokens)),
        shared_prefix=shared,
        max_tokens=2500,
        retries=retries,
        task="course[checklist]",
    )
    points, dropped = [], 0
    for p in out.points[:12]:
        keys = list(dict.fromkeys(k.strip().upper().replace(" ", "") for k in p.sections))
        refs = [by_key[k] for k in keys if k in by_key][:3]
        text = " ".join(p.text.split())
        if not refs or not text:
            dropped += 1
            continue
        refs.sort(key=lambda it: (it.lecture.position, it.section.start_s))
        points.append(
            ChecklistItem(
                text=text,
                why=" ".join(p.why.split()),
                refs=[it.ref() for it in refs],
                in_playlist=any(it.key in chosen for it in refs),
            )
        )
    return points, dropped


def _for_course(item: Item, *, penalty: float = 0.0) -> ScoredSection:
    """A copy of the section keyed for the whole course, for the selector."""
    s = item.scored
    prefix = f"L{item.lecture.position}."
    score = s.score.model_copy(update={"final": max(0.0, s.score.final - penalty)})
    importance = s.importance
    if penalty and importance == Importance.ESSENTIAL:
        importance = Importance.RECOMMENDED
    return s.model_copy(
        update={
            "section": s.section.model_copy(update={"id": item.key}),
            "evaluation": s.evaluation.model_copy(update={"builds_on": [prefix + b for b in s.evaluation.builds_on]}),
            "score": score,
            "importance": importance,
        }
    )


def build_course_plan(
    provider: LLMProvider,
    *,
    model: str,
    plan_id: str,
    course_id: str,
    lectures: Sequence[Lecture],
    prefs: LearningPreferences,
    budget_s: float,
    config: PlaylistConfig,
    context_tokens: int,
    embedder: Embedder | None = None,
    retries: int = 2,
    progress: Progress = lambda _: None,
) -> CoursePlan:
    started = time.monotonic()
    warnings: list[str] = []
    lectures = sorted(lectures, key=lambda lec: lec.position)
    items = course_items(lectures)
    shared = learner_brief(prefs)
    goals = {lec.analysis.preferences.cache_key() for lec in lectures}
    for lec in lectures:
        if lec.analysis.preferences.cache_key() != prefs.cache_key():
            warnings.append(f"{lec.name} was analyzed for another goal; its sections are scored for that goal.")
    if len(goals) > 1:
        log.info("Course %s mixes %d goals", course_id, len(goals))

    # 2-3. Repeats across lectures.
    progress("Comparing sections across lectures")
    pairs: list[Pair] = []
    if len(lectures) > 1 and items:
        sim = _similarities(items, embedder)
        pairs = candidate_pairs(items, sim, max_pairs=min(48, max(12, 4 * len(lectures))))
        try:
            missing = judge_pairs(provider, pairs, model=model, shared=shared, retries=retries, progress=progress)
            if missing:
                warnings.append(f"{missing} possible repeat(s) weren't checked; both parts stay in the running.")
        except LLMError as exc:
            warnings.append(f"Couldn't check for repeats across lectures ({exc}); some parts may repeat.")
    repeats = [p for p in pairs if p.relation == "repeat"]

    # 4. One selection across the course.
    progress("Picking the best parts across the course")
    weaker = {p.other.key for p in repeats}
    merged = [_for_course(it, penalty=REPEAT_PENALTY if it.key in weaker else 0.0) for it in items]
    excludes: dict[str, set[str]] = {}
    for p in repeats:
        excludes.setdefault(p.a.key, set()).add(p.b.key)
        excludes.setdefault(p.b.key, set()).add(p.a.key)
    chosen = select_sections(merged, budget_s, config, excludes=excludes) if merged else set()

    related: dict[str, list[CourseRef]] = {}
    for p in pairs:
        if p.relation in ("overlap", "repeat"):
            related.setdefault(p.a.key, []).append(p.b.ref())
            related.setdefault(p.b.key, []).append(p.a.ref())

    parts: list[CoursePart] = []
    for lec in lectures:
        mine = sorted((it for it in items if it.lecture is lec and it.key in chosen), key=lambda it: it.section.start_s)
        groups: list[list[Item]] = []
        for it in mine:
            if groups and it.section.start_s - groups[-1][-1].section.end_s <= config.merge_gap_s:
                groups[-1].append(it)
            else:
                groups.append([it])
        for n, group in enumerate(groups, 1):
            seg = _segment([it.scored for it in group], n, True, lec.analysis.video.duration_s, config)
            seen: set[tuple[str, str]] = set()
            also = []
            for it in group:
                for ref in related.get(it.key, []):
                    if (ref.video_id, ref.section_id) not in seen:
                        seen.add((ref.video_id, ref.section_id))
                        also.append(ref)
            parts.append(
                CoursePart(
                    key=f"{lec.analysis.video.video_id}@{int(seg.start_s)}",
                    lecture=lec.position,
                    video_id=lec.analysis.video.video_id,
                    analysis_id=lec.analysis.id,
                    section_ids=seg.section_ids,
                    title=seg.title,
                    start_s=seg.start_s,
                    end_s=seg.end_s,
                    what_you_learn=seg.what_you_learn,
                    why_valuable=seg.why_valuable,
                    importance=seg.importance,
                    score=seg.score,
                    also_in=also[:3],
                )
            )

    skipped: list[CourseRepeat] = []
    listed: set[str] = set()
    for p in repeats:  # closest pairs first: a section left out is listed once, with its closest match
        a_in, b_in = p.a.key in chosen, p.b.key in chosen
        if a_in != b_in:
            kept, left = (p.a, p.b) if a_in else (p.b, p.a)
            if left.key in listed:
                continue
            listed.add(left.key)
            skipped.append(CourseRepeat(section=left.ref(), covered_by=kept.ref(), reason=p.reason))
    skipped.sort(key=lambda r: (r.section.lecture, r.section.start_s))

    # 5. What's easy to miss.
    checklist: list[ChecklistItem] = []
    if items:
        progress("Writing what's easy to miss across the course")
        try:
            checklist, dropped = write_checklist(
                provider,
                items,
                lectures,
                chosen=chosen,
                model=model,
                shared=shared,
                budget_tokens=max(2000, context_tokens - 5000),
                retries=retries,
            )
            if dropped:
                warnings.append(f"Dropped {dropped} checklist point(s) that cited no section of the course.")
        except LLMError as exc:
            warnings.append(f"Couldn't write the checklist ({exc}).")

    return CoursePlan(
        id=plan_id,
        course_id=course_id,
        status=AnalysisStatus.DONE,
        progress="Done",
        budget_s=round(budget_s, 1),
        total_s=round(sum(p.duration_s for p in parts), 1),
        original_s=round(sum(lec.analysis.video.duration_s for lec in lectures), 1),
        analysis_ids=[lec.analysis.id for lec in lectures],
        parts=parts,
        repeats=skipped,
        pairs=[
            CoursePair(
                a=p.a.ref(),
                b=p.b.ref(),
                relation=p.relation,
                keep=p.keep,
                reason=p.reason,
                similarity=round(p.similarity, 3),
                a_in_playlist=p.a.key in chosen,
                b_in_playlist=p.b.key in chosen,
            )
            for p in pairs
        ],
        checklist=checklist,
        warnings=warnings,
        llm=LLMInfo(provider=provider.name, models={"course": model}),
        elapsed_s=round(time.monotonic() - started, 1),
    )
