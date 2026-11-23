import pytest

from curio_learn.analysis.profiles import effective_weights
from curio_learn.analysis.redundancy import similarity_matrix
from curio_learn.analysis.scoring import ScoringConfig, classify, score_sections, weighted_rubric
from curio_learn.config import ScoringWeights
from curio_learn.domain import (
    Importance,
    LearningPreferences,
    LearningProfile,
    Rubric,
    Section,
    SectionEvaluation,
    SectionKind,
)

W = ScoringWeights().as_dict()


def section(i, kind=SectionKind.CORE, dur=180.0, concepts=None):
    return Section(
        id=f"S{i:02d}",
        start_block=i,
        end_block=i,
        start_s=i * dur,
        end_s=(i + 1) * dur,
        title=f"Section {i}",
        summary="s",
        kind=kind,
        key_concepts=concepts or [],
    )


def ev(sid, v=7.0, **kw):
    return SectionEvaluation(
        section_id=sid, rubric=Rubric(educational_value=v, novelty=v, practical=v, depth=v, relevance=v), **kw
    )


def test_weights_sum_to_one_for_every_profile():
    for profile in LearningProfile:
        w = effective_weights(W, LearningPreferences(profile=profile))
        assert sum(w.values()) == pytest.approx(1.0)
    w = effective_weights(W, LearningPreferences(custom_objective="agents"))
    assert w["relevance"] > W["relevance"]


def test_weighted_rubric_scale():
    assert weighted_rubric(ev("S01", 1), W) == 0
    assert weighted_rubric(ev("S01", 10), W) == 100
    assert weighted_rubric(ev("S01", 5.5), W) == 50


def test_weighted_rubric_uses_configured_weights():
    e = SectionEvaluation(
        section_id="S01", rubric=Rubric(educational_value=10, novelty=1, practical=1, depth=1, relevance=1)
    )
    assert weighted_rubric(e, W) == pytest.approx(30.0)


def test_sponsor_and_meta_always_skipped():
    cfg = ScoringConfig(weights=W)
    assert classify(99, SectionKind.SPONSOR, cfg) is Importance.SKIP
    assert classify(99, SectionKind.META, cfg) is Importance.SKIP
    assert classify(75, SectionKind.CORE, cfg) is Importance.ESSENTIAL
    assert classify(60, SectionKind.CORE, cfg) is Importance.RECOMMENDED
    assert classify(45, SectionKind.CORE, cfg) is Importance.OPTIONAL
    assert classify(10, SectionKind.CORE, cfg) is Importance.SKIP


def test_adjustments_are_explained():
    secs = [section(0, SectionKind.INTRO), section(1), section(2, dur=30)]
    evs = {s.id: ev(s.id) for s in secs}
    evs["S01"] = ev("S01", introduces_concepts=["gradient descent"])
    scored = score_sections(secs, evs, LearningPreferences(), ScoringConfig(weights=W))
    intro, core, short = scored
    assert any(a.rule == "section_kind" and a.delta < 0 for a in intro.score.adjustments)
    assert any(a.rule == "new_concepts" for a in core.score.adjustments)
    assert any(a.rule == "too_short" for a in short.score.adjustments)
    for s in scored:
        expected = s.score.weighted_rubric + sum(a.delta for a in s.score.adjustments)
        assert s.score.final == pytest.approx(max(0, min(100, expected)), abs=0.1)
        assert all(a.reason for a in s.score.adjustments)


def test_redundant_section_demoted_against_better_one():
    secs = [section(0), section(1)]
    evs = {"S01": ev("S01", 8), "S00": ev("S00", 6, redundant_with=["S01"])}
    sim = [[1.0, 0.9], [0.9, 1.0]]
    scored = score_sections(secs, evs, LearningPreferences(), ScoringConfig(weights=W), sim)
    worse = scored[0]
    rules = {a.rule for a in worse.score.adjustments}
    assert {"lexical_overlap", "covered_elsewhere"} <= rules
    assert not {"lexical_overlap", "covered_elsewhere"} & {a.rule for a in scored[1].score.adjustments}


def test_repeated_concepts_get_no_novelty_bonus():
    secs = [section(0, concepts=["GRPO"]), section(1)]
    evs = {"S00": ev("S00", introduces_concepts=["GRPO"]), "S01": ev("S01", introduces_concepts=["grpo"])}
    scored = score_sections(secs, evs, LearningPreferences(), ScoringConfig(weights=W))
    assert any(a.rule == "new_concepts" for a in scored[0].score.adjustments)
    assert not any(a.rule == "new_concepts" for a in scored[1].score.adjustments)
    assert any(a.rule == "no_new_concepts" for a in scored[1].score.adjustments)


def test_outline_key_concepts_alone_earn_no_bonus():
    scored = score_sections(
        [section(0, concepts=["GRPO"])], {"S00": ev("S00")}, LearningPreferences(), ScoringConfig(weights=W)
    )
    assert not any(a.rule == "new_concepts" for a in scored[0].score.adjustments)


def test_within_video_rank_spreads_inflated_scores():
    # A model that rates everything 8-9 should not make every section essential.
    secs = [section(i) for i in range(8)]
    evs = {s.id: ev(s.id, 8 + i * 0.14) for i, s in enumerate(secs)}
    scored = score_sections(secs, evs, LearningPreferences(), ScoringConfig(weights=W))
    tiers = [s.importance for s in scored]
    assert tiers.count(Importance.ESSENTIAL) <= len(secs) / 2
    best = max(scored, key=lambda s: s.score.weighted_rubric)
    worst = min(scored, key=lambda s: s.score.weighted_rubric)
    assert best.score.final - worst.score.final > 40
    assert any(a.rule == "within_video_rank" and "Ranked 1 of 8" in a.reason for a in best.score.adjustments)


def test_profile_changes_ranking():
    demo = section(0, SectionKind.DEMO)
    talk = section(1, SectionKind.CORE)
    evs = {
        "S00": SectionEvaluation(
            section_id="S00", rubric=Rubric(educational_value=6, novelty=6, practical=10, depth=7, relevance=5)
        ),
        "S01": SectionEvaluation(
            section_id="S01", rubric=Rubric(educational_value=8, novelty=6, practical=3, depth=6, relevance=9)
        ),
    }
    cfg = ScoringConfig(weights=W)
    tech = score_sections([demo, talk], evs, LearningPreferences(profile="technical"), cfg)
    exe = score_sections([demo, talk], evs, LearningPreferences(profile="executive"), cfg)
    assert tech[0].score.final > tech[1].score.final
    assert exe[0].score.final < exe[1].score.final


def test_similarity_matrix():
    sim = similarity_matrix(
        [
            "gradient descent updates weights using the gradient of the loss",
            "the loss gradient drives weight updates in gradient descent",
            "prompt optimization searches over instructions and few-shot examples",
        ]
    )
    assert sim[0][0] == pytest.approx(1.0)
    assert sim[0][1] > sim[0][2]
    assert sim[1][0] == sim[0][1]


def test_solid_content_ranked_low_stays_optional_not_skip():
    # Regression from the podcast run: a founding story rated 51/100 ranked 17 of 18
    # and fell to "skip" purely through within-video calibration.
    secs = [section(i) for i in range(10)]
    evs = {s.id: ev(s.id, 9.5) for s in secs}
    evs["S00"] = ev("S00", 5.6)  # weighted rubric ≈ 51
    scored = score_sections(secs, evs, LearningPreferences(), ScoringConfig(weights=W))
    low = scored[0]
    assert low.importance is Importance.OPTIONAL
    floor = next(a for a in low.score.adjustments if a.rule == "content_floor")
    assert "rated it 51/100" in floor.reason
    assert low.score.final == pytest.approx(
        low.score.weighted_rubric + sum(a.delta for a in low.score.adjustments), abs=0.15
    )


def test_weak_or_redundant_content_can_still_be_skipped():
    secs = [section(i) for i in range(6)]
    evs = {s.id: ev(s.id, 9) for s in secs}
    evs["S00"] = ev("S00", 3)  # genuinely weak
    evs["S01"] = ev("S01", 6, redundant_with=["S02"])  # decent but covered elsewhere
    scored = score_sections(secs, evs, LearningPreferences(), ScoringConfig(weights=W))
    assert scored[0].importance is Importance.SKIP
    assert not any(a.rule == "content_floor" for a in scored[1].score.adjustments)
