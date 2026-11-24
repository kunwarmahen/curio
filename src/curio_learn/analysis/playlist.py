"""Step 9: build a time-budgeted, chronological learning playlist."""

from __future__ import annotations

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


def select_sections(
    scored: list[ScoredSection],
    budget_s: float,
    config: PlaylistConfig,
    *,
    excludes: dict[str, set[str]] | None = None,
) -> set[str]:
    """Best sections that fit the budget. `excludes` maps a section to the ones it rules
    out once chosen (a course's repeats: the same material in another lecture)."""
    by_id = {s.section.id: s for s in scored}
    limit = budget_s * config.budget_tolerance
    chosen: set[str] = set()
    blocked: set[str] = set()
    excludes = excludes or {}
    used = 0.0

    def cost(sid: str) -> float:
        # Each segment gets a lead-in and a tail; counting them per section keeps the
        # finished playlist inside the budget (merged neighbours only make it shorter).
        return by_id[sid].section.duration_s + config.lead_in_s + config.tail_s

    def try_add(sid: str) -> None:
        nonlocal used
        s = by_id[sid]
        if sid in chosen or sid in blocked or s.importance == Importance.SKIP:
            return
        if used + cost(sid) > limit:
            return
        # Pull in the sections this one directly builds on, but only when the whole
        # bundle fits: prerequisites must never crowd out the section that needs them.
        prereqs = [
            p
            for p in s.evaluation.builds_on
            if p in by_id
            and p not in chosen
            and p not in blocked
            and _RANK[by_id[p].importance] >= _RANK[Importance.RECOMMENDED]
        ]
        bundle = [sid, *prereqs]
        if used + sum(cost(x) for x in bundle) > limit:
            bundle = [sid]  # prerequisites stay listed on the segment card instead
        if any(y in bundle for x in bundle for y in excludes.get(x, ())):
            bundle = [sid]  # a prerequisite that repeats the section itself
        for x in bundle:
            chosen.add(x)
            used += cost(x)
            blocked.update(excludes.get(x, ()))

    ranked = sorted(scored, key=lambda s: (_RANK[s.importance], s.score.final), reverse=True)
    for s in ranked:
        if s.importance != Importance.SKIP:
            try_add(s.section.id)
    if not chosen:  # nothing fits: take the single best section anyway
        best = next((s for s in ranked if s.importance != Importance.SKIP), None)
        if best:
            chosen.add(best.section.id)
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
