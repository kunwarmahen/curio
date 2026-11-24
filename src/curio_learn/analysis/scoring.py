"""Step 7: explainable segment scoring.

final = weighted LLM rubric (0-100) + rule-based adjustments, clamped to 0-100.
Every adjustment is recorded with its reason so the UI can show why a section
ranked where it did. The LLM rubric is one input, not the verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from curio_learn.analysis.profiles import effective_weights, spec_for
from curio_learn.domain import (
    Importance,
    LearningPreferences,
    ScoreAdjustment,
    ScoreBreakdown,
    ScoredSection,
    Section,
    SectionEvaluation,
    SectionKind,
)

# Base priors by section kind, in points out of 100.
KIND_PRIOR: dict[SectionKind, float] = {
    SectionKind.CORE: 0,
    SectionKind.EXAMPLE: 2,
    SectionKind.DEMO: 3,
    SectionKind.QA: -3,
    SectionKind.INTRO: -12,
    SectionKind.RECAP: -10,
    SectionKind.TANGENT: -10,
    SectionKind.OUTRO: -15,
    SectionKind.META: -30,
    SectionKind.SPONSOR: -40,
}
ALWAYS_SKIP = {SectionKind.SPONSOR, SectionKind.META}
CONTENT_KINDS = {SectionKind.CORE, SectionKind.EXAMPLE, SectionKind.DEMO}
# Sections ranked against each other for calibration: the teaching, including Q&A.
RANKED_KINDS = CONTENT_KINDS | {SectionKind.QA}


@dataclass(frozen=True)
class ScoringConfig:
    weights: dict[str, float]
    threshold_essential: float = 70.0
    threshold_recommended: float = 55.0
    threshold_optional: float = 40.0
    short_section_s: float = 60.0
    lexical_redundancy_threshold: float = 0.45
    # Content the AI rated at least this well (0-100) is never labeled "skip" just
    # because it ranks low within this video; it stays optional.
    content_floor_rating: float = 50.0


def weighted_rubric(ev: SectionEvaluation, weights: dict[str, float]) -> float:
    r = ev.rubric.model_dump()
    raw = sum(weights[k] * r[k] for k in weights)  # weights are normalized: 1..10
    return round((raw - 1) / 9 * 100, 1)


def _norm_concept(c: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", c.lower()).strip()


def score_sections(
    sections: list[Section],
    evaluations: dict[str, SectionEvaluation],
    prefs: LearningPreferences,
    config: ScoringConfig,
    similarity: list[list[float]] | None = None,
) -> list[ScoredSection]:
    weights = effective_weights(config.weights, prefs)
    spec = spec_for(prefs)
    base = {s.id: weighted_rubric(evaluations[s.id], weights) for s in sections}
    index = {s.id: i for i, s in enumerate(sections)}
    # Rank only the teaching against itself: intros, recaps, outros and housekeeping
    # have their own kind priors and would stretch the range.
    relative, rank_of = _relative_scores({s.id: base[s.id] for s in sections if s.kind in RANKED_KINDS})
    pool = len(relative)

    foundations = _foundation_reach(sections, evaluations) if spec.foundation_bonus else {}
    teaching = sum(1 for s in sections if s.kind in RANKED_KINDS) or 1

    seen_concepts: set[str] = set()
    scored: list[ScoredSection] = []
    for i, s in enumerate(sections):
        ev = evaluations[s.id]
        adj: list[ScoreAdjustment] = []

        prior = KIND_PRIOR[s.kind] + spec.kind_bias.get(s.kind, 0)
        if prior:
            adj.append(
                ScoreAdjustment(
                    rule="section_kind", delta=prior, reason=f"{s.kind.value} section ({spec.label} profile)"
                )
            )

        if s.duration_s < config.short_section_s:
            adj.append(
                ScoreAdjustment(
                    rule="too_short",
                    delta=-6,
                    reason=f"Only {int(s.duration_s)}s long; little room for a complete explanation",
                )
            )

        # Lexical redundancy against a higher-rated section.
        if similarity is not None:
            best_j, best_sim = -1, 0.0
            for j, other in enumerate(sections):
                if j != i and base[other.id] > base[s.id] and similarity[i][j] > best_sim:
                    best_j, best_sim = j, similarity[i][j]
            if best_sim >= config.lexical_redundancy_threshold:
                delta = -round(min(12.0, (best_sim - config.lexical_redundancy_threshold) * 60 + 4), 1)
                adj.append(
                    ScoreAdjustment(
                        rule="lexical_overlap",
                        delta=delta,
                        reason=f"Wording overlaps {sections[best_j].id} ('{sections[best_j].title}') by {best_sim:.0%}",
                    )
                )

        # LLM-identified redundancy with a higher-rated section.
        better = [r for r in ev.redundant_with if r in base and base[r] > base[s.id]]
        if better:
            adj.append(
                ScoreAdjustment(
                    rule="covered_elsewhere", delta=-8, reason=f"Content is covered by higher-rated {', '.join(better)}"
                )
            )

        # Calibration: half of the base score comes from the section's rank within this
        # video, placed on the range of ratings the model gave this video's content.
        # That spreads out ratings bunched together, without dragging down solid
        # sections of a video whose content is all strong (a 0-100 percentile turned a
        # Kubernetes basics section rated 82 into 65).
        if pool > 1 and s.id in relative:
            delta = round(0.5 * (relative[s.id] - base[s.id]), 1)
            if abs(delta) >= 0.5:
                adj.append(
                    ScoreAdjustment(
                        rule="within_video_rank",
                        delta=delta,
                        reason=f"Ranked {rank_of[s.id]} of {pool} sections in this video",
                    )
                )

        # Novelty: concepts this section introduces that no earlier section mentioned.
        new_unique = list(
            dict.fromkeys(n for n in (_norm_concept(c) for c in ev.introduces_concepts) if n and n not in seen_concepts)
        )
        if s.kind not in ALWAYS_SKIP | {SectionKind.INTRO, SectionKind.OUTRO}:
            if new_unique:
                bonus = min(4.0, 1.5 * len(new_unique))
                adj.append(
                    ScoreAdjustment(
                        rule="new_concepts", delta=bonus, reason=f"Introduces {len(new_unique)} new concept(s)"
                    )
                )
            elif s.kind in (SectionKind.CORE, SectionKind.RECAP):
                adj.append(
                    ScoreAdjustment(
                        rule="no_new_concepts", delta=-3, reason="Introduces no concept not covered earlier"
                    )
                )
        seen_concepts.update(_norm_concept(c) for c in ev.introduces_concepts + s.key_concepts)

        if not ev.standalone and not any(b in index for b in ev.builds_on):
            adj.append(ScoreAdjustment(rule="needs_context", delta=-4, reason="Hard to follow on its own"))

        reach = foundations.get(s.id, 0)
        if s.kind in CONTENT_KINDS and reach:
            bonus = round(spec.foundation_bonus * min(1.0, reach / teaching), 1)
            if bonus >= 0.5:
                adj.append(
                    ScoreAdjustment(
                        rule="foundation",
                        delta=bonus,
                        reason=f"{reach} later section(s) build on this; a beginner needs it first",
                    )
                )

        final = round(min(100.0, max(0.0, base[s.id] + sum(a.delta for a in adj))), 1)
        redundant = any(a.rule in ("covered_elsewhere", "lexical_overlap") for a in adj)
        if (
            s.kind in CONTENT_KINDS
            and base[s.id] >= config.content_floor_rating
            and final < config.threshold_optional
            and not redundant
        ):
            lift = round(config.threshold_optional - final, 1)
            adj.append(
                ScoreAdjustment(
                    rule="content_floor",
                    delta=lift,
                    reason=f"Kept as optional: the AI rated it {base[s.id]:.0f}/100; it ranks low only "
                    "compared with the rest of this video",
                )
            )
            final = config.threshold_optional
        breakdown = ScoreBreakdown(weighted_rubric=base[s.id], adjustments=adj, final=final)
        scored.append(
            ScoredSection(section=s, evaluation=ev, score=breakdown, importance=classify(final, s.kind, config))
        )
    return scored


def _foundation_reach(sections: list[Section], evaluations: dict[str, SectionEvaluation]) -> dict[str, int]:
    """For each section, how many teaching sections build on it, directly or through
    others (from the evaluations' `builds_on`, which only point to earlier sections)."""
    teaching = {s.id for s in sections if s.kind in RANKED_KINDS}
    users: dict[str, set[str]] = {s.id: set() for s in sections}
    for s in sections:
        for b in evaluations[s.id].builds_on:
            if b in users and b != s.id:
                users[b].add(s.id)
    reach: dict[str, int] = {}
    for s in sections:
        seen: set[str] = set()
        stack = list(users[s.id])
        while stack:
            x = stack.pop()
            if x not in seen:
                seen.add(x)
                stack.extend(users[x])
        reach[s.id] = len(seen & teaching)
    return reach


def _relative_scores(base: dict[str, float]) -> tuple[dict[str, float], dict[str, int]]:
    """Each base score's percentile within the video, placed on the range of the base
    scores (lowest to highest); ties share a percentile. Also each one's rank."""
    if not base:
        return {}, {}
    ordered = sorted(base.values())
    n, lo, hi = len(ordered), ordered[0], ordered[-1]
    relative, rank_of = {}, {}
    for sid, value in base.items():
        below = sum(1 for v in ordered if v < value)
        equal = sum(1 for v in ordered if v == value)
        pct = (below + (equal - 1) / 2) / (n - 1) if n > 1 else 0.5
        relative[sid] = lo + pct * (hi - lo)
        rank_of[sid] = 1 + sum(1 for v in ordered if v > value)
    return relative, rank_of


def classify(score: float, kind: SectionKind, config: ScoringConfig) -> Importance:
    if kind in ALWAYS_SKIP:
        return Importance.SKIP
    if score >= config.threshold_essential:
        return Importance.ESSENTIAL
    if score >= config.threshold_recommended:
        return Importance.RECOMMENDED
    if score >= config.threshold_optional:
        return Importance.OPTIONAL
    return Importance.SKIP
