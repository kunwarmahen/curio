"""Step 9: build a time-budgeted, chronological learning playlist."""

from __future__ import annotations

import math
from dataclasses import dataclass

from curio_learn.domain import (
    Importance,
    LearningSegment,
    Playlist,
    ScoreAdjustment,
    ScoredSection,
    SectionKind,
    SkippedSection,
)

_RANK = {Importance.ESSENTIAL: 3, Importance.RECOMMENDED: 2, Importance.OPTIONAL: 1, Importance.SKIP: 0}


@dataclass(frozen=True)
class PlaylistConfig:
    budget_fraction: float = 0.30
    budget_min_s: float = 600.0
    budget_max_s: float = 2100.0
    merge_gap_s: float = 15.0
    lead_in_s: float = 2.0  # start slightly before the first spoken word
    tail_s: float = 1.0
    budget_tolerance: float = 1.10


def compute_budget(
    duration_s: float, config: PlaylistConfig, *, target_minutes: float | None = None, fraction: float | None = None
) -> float:
    if target_minutes:
        return min(duration_s, target_minutes * 60)
    budget = duration_s * (fraction or config.budget_fraction)
    budget = max(config.budget_min_s, min(config.budget_max_s, budget))
    return min(budget, duration_s * 0.6)


def _value(s: ScoredSection) -> float:
    """Learning value used for the coverage estimate: quality² × time."""
    return (s.score.final / 100) ** 2 * s.section.duration_s


def _pick_value(s: ScoredSection) -> float:
    """What a section adds to a playlist: quality cubed (so a strong section beats
    several middling ones) times the square root of its length (a 10-minute section on
    one topic teaches more than a 5-minute one, but not twice as much)."""
    return (s.score.final / 100) ** 3 * math.sqrt(s.section.duration_s / 60)


def select_sections(
    scored: list[ScoredSection],
    budget_s: float,
    config: PlaylistConfig,
    *,
    excludes: dict[str, set[str]] | None = None,
) -> set[str]:
    """Best sections that fit the budget. `excludes` maps a section to the ones it rules
    out once chosen (a course's repeats: the same material in another lecture).

    A video's playlist is the set with the most total `_pick_value` that fits (a small
    knapsack), then the direct prerequisites of its sections that still fit. Before
    this, sections went in by score, and one long top section could crowd out several
    nearly as good (MIT 6.006 lecture 1: a 10-minute proof scored 100 left out
    efficiency and complexity classes, 87 and 93, from a 14-minute playlist).

    With `excludes` (a course's repeats), when the best set holds both halves of a
    repeat, the half worth less is set aside and the set is chosen again, until no
    repeat is left; a course has few repeats, so this ends after a few rounds."""
    excludes = excludes or {}
    banned: set[str] = set()
    by_id = {s.section.id: s for s in scored}
    while True:
        chosen = _knapsack(scored, budget_s, config, banned=banned, excludes=excludes)
        clashes = [(a, b) for a in sorted(chosen) for b in sorted(excludes.get(a, ())) if b in chosen and a < b]
        if not clashes:
            return chosen
        for a, b in clashes:
            if a not in banned and b not in banned:
                banned.add(a if _pick_value(by_id[a]) < _pick_value(by_id[b]) else b)


def _knapsack(
    scored: list[ScoredSection],
    budget_s: float,
    config: PlaylistConfig,
    *,
    banned: set[str] | None = None,
    excludes: dict[str, set[str]] | None = None,
) -> set[str]:
    banned, excludes = banned or set(), excludes or {}
    items = [s for s in scored if s.importance != Importance.SKIP and s.section.id not in banned]
    if not items:
        return set()
    limit = budget_s * config.budget_tolerance
    # 5 s steps, or coarser for a long course playlist (at most ~1,500 steps).
    unit = max(5.0, limit / 1500)
    lead = config.lead_in_s + config.tail_s
    cost = [math.ceil((s.section.duration_s + lead) / unit) for s in items]
    # A section that better sections build on is worth a little more (10% per such
    # section, up to two), so a prerequisite wins a near tie with an unrelated section.
    score_of = {s.section.id: s.score.final for s in items}
    dependants: dict[str, int] = {}
    for s in items:
        for p in set(s.evaluation.builds_on):
            if p in score_of and s.score.final > score_of[p]:
                dependants[p] = dependants.get(p, 0) + 1
    value = [_pick_value(s) * (1 + 0.1 * min(2, dependants.get(s.section.id, 0))) for s in items]
    cap = int(limit // unit)
    best = [0.0] * (cap + 1)
    take = [[False] * (cap + 1) for _ in items]
    for i, (c, v) in enumerate(zip(cost, value, strict=True)):
        for w in range(cap, c - 1, -1):
            if best[w - c] + v > best[w] + 1e-12:
                best[w] = best[w - c] + v
                take[i][w] = True
    chosen: set[str] = set()
    w = cap
    for i in range(len(items) - 1, -1, -1):
        if take[i][w]:
            chosen.add(items[i].section.id)
            w -= cost[i]
    if not chosen:  # nothing fits: take the single best section anyway
        return {max(items, key=lambda s: s.score.final).section.id}

    # Direct prerequisites of the chosen sections, best sections first, when they fit.
    by_id = {s.section.id: s for s in scored}
    used = sum(by_id[x].section.duration_s + lead for x in chosen)
    for sid in sorted(chosen, key=lambda x: -by_id[x].score.final):
        for p in by_id[sid].evaluation.builds_on:
            pre = by_id.get(p)
            if pre is None or p in chosen or p in banned or _RANK[pre.importance] < _RANK[Importance.RECOMMENDED]:
                continue
            if any(p in excludes.get(x, ()) or x in excludes.get(p, ()) for x in chosen):
                continue  # a prerequisite that repeats a chosen section
            if used + pre.section.duration_s + lead <= limit:
                chosen.add(p)
                used += pre.section.duration_s + lead
    return chosen


def _segment(
    group: list[ScoredSection], n: int, in_playlist: bool, duration_s: float, config: PlaylistConfig
) -> LearningSegment:
    top = max(group, key=lambda s: s.score.final)
    total = sum(s.section.duration_s for s in group) or 1.0
    score = sum(s.score.final * s.section.duration_s for s in group) / total
    importance = max((s.importance for s in group), key=lambda i: _RANK[i])

    def join(attr: str) -> str:
        parts = [getattr(s.evaluation, attr) or "" for s in group]
        return " ".join(dict.fromkeys(p for p in parts if p))

    title = top.section.title if len(group) == 1 else " → ".join(s.section.title for s in group[:3])
    prereqs = list(dict.fromkeys(p for s in group for p in s.evaluation.prerequisites))
    return LearningSegment(
        id=f"P{n:02d}" if in_playlist else f"X{n:02d}",
        section_ids=[s.section.id for s in group],
        title=title,
        start_s=round(max(0.0, group[0].section.start_s - config.lead_in_s), 2),
        end_s=round(min(duration_s, group[-1].section.end_s + config.tail_s), 2),
        summary=" ".join(s.section.summary for s in group),
        what_you_learn=join("what_you_learn") or top.section.summary,
        why_valuable=join("why_valuable"),
        prerequisites=prereqs[:5],
        importance=importance,
        score=round(score, 1),
        kind=top.section.kind,
        in_playlist=in_playlist,
    )


BEYOND_BUDGET = "beyond_budget"


def fit_labels(scored: list[ScoredSection], chosen: set[str], budget_s: float) -> list[ScoredSection]:
    """Keep "essential" for sections in the playlist. A section that scored as essential
    but didn't fit the time budget is labeled recommended, with the reason, so the label
    never says "must watch" about a part the playlist leaves out. The score is unchanged."""
    minutes = max(1, round(budget_s / 60))
    out = []
    for s in scored:
        if s.importance == Importance.ESSENTIAL and s.section.id not in chosen:
            note = ScoreAdjustment(
                rule=BEYOND_BUDGET,
                delta=0,
                reason=f"Scored as essential, but didn't fit in your {minutes}-minute playlist "
                "after the higher-scored parts",
            )
            s = s.model_copy(
                update={
                    "importance": Importance.RECOMMENDED,
                    "score": s.score.model_copy(update={"adjustments": [*s.score.adjustments, note]}),
                }
            )
        out.append(s)
    return out


def build_playlist(
    scored: list[ScoredSection],
    *,
    duration_s: float,
    budget_s: float,
    config: PlaylistConfig,
    chosen: set[str] | None = None,
) -> tuple[Playlist, list[LearningSegment]]:
    """Return the playlist and the non-selected (but watchable) segments. `chosen` is the
    selection when the caller already made it (see `fit_labels`)."""
    if chosen is None:
        chosen = select_sections(scored, budget_s, config)
    ordered = sorted(scored, key=lambda s: s.section.start_s)

    groups: list[list[ScoredSection]] = []
    for s in ordered:
        if s.section.id not in chosen:
            continue
        if groups and s.section.start_s - groups[-1][-1].section.end_s <= config.merge_gap_s:
            groups[-1].append(s)
        else:
            groups.append([s])
    segments = [_segment(g, n, True, duration_s, config) for n, g in enumerate(groups, 1)]

    others = [
        _segment([s], n, False, duration_s, config)
        for n, s in enumerate((s for s in ordered if s.section.id not in chosen and s.importance != Importance.SKIP), 1)
    ]
    skipped = [
        SkippedSection(
            section_id=s.section.id,
            title=s.section.title,
            start_s=s.section.start_s,
            end_s=s.section.end_s,
            reason=s.evaluation.skip_reason or _skip_reason(s),
        )
        for s in ordered
        if s.importance == Importance.SKIP
    ]

    total_value = sum(_value(s) for s in scored if s.importance != Importance.SKIP) or 1.0
    covered = sum(_value(s) for s in scored if s.section.id in chosen)
    playlist = Playlist(
        segments=segments,
        budget_s=round(budget_s, 1),
        total_s=round(sum(seg.duration_s for seg in segments), 1),
        original_s=duration_s,
        coverage_estimate=round(min(1.0, covered / total_value), 3),
        skipped=skipped,
    )
    return playlist, others


_KIND_LABEL = {
    SectionKind.INTRO: "Introduction",
    SectionKind.OUTRO: "Closing remarks",
    SectionKind.SPONSOR: "Sponsor or self-promotion",
    SectionKind.META: "Housekeeping",
    SectionKind.TANGENT: "Off-topic tangent",
    SectionKind.RECAP: "Recap of earlier material",
    SectionKind.QA: "Audience Q&A",
}


def _skip_reason(s: ScoredSection) -> str:
    """Explain a skip when the LLM gave no reason of its own."""
    label = _KIND_LABEL.get(s.section.kind)
    if label:
        return f"{label}: {s.section.summary}"
    covered = next((a for a in s.score.adjustments if a.rule in ("covered_elsewhere", "lexical_overlap")), None)
    if covered:
        return covered.reason
    rank = next((a for a in s.score.adjustments if a.rule == "within_video_rank"), None)
    detail = f"; {rank.reason.lower()}" if rank else ""
    return (
        f"Less valuable for this objective than the rest of the video (score {s.score.final:.0f}/100{detail}). "
        f"Covers: {s.section.summary}"
    )
