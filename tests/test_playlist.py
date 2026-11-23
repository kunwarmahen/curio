import pytest

from curio_learn.analysis.playlist import PlaylistConfig, build_playlist, compute_budget, select_sections
from curio_learn.analysis.validation import validate
from curio_learn.domain import (
    Block,
    Importance,
    Rubric,
    ScoreBreakdown,
    ScoredSection,
    Section,
    SectionEvaluation,
    SectionKind,
)

CFG = PlaylistConfig(budget_min_s=60, budget_max_s=3000)


def scored(i, score, importance=None, dur=120.0, kind=SectionKind.CORE, builds_on=None, gap=0.0):
    start = i * (dur + gap)
    sec = Section(
        id=f"S{i:02d}",
        start_block=i,
        end_block=i,
        start_s=start,
        end_s=start + dur,
        title=f"Section {i}",
        summary=f"Summary {i}.",
        kind=kind,
    )
    if importance is None:
        importance = (
            Importance.ESSENTIAL
            if score >= 70
            else Importance.RECOMMENDED
            if score >= 55
            else Importance.OPTIONAL
            if score >= 40
            else Importance.SKIP
        )
    ev = SectionEvaluation(
        section_id=sec.id,
        rubric=Rubric(educational_value=5, novelty=5, practical=5, depth=5, relevance=5),
        what_you_learn=f"Learn {i}.",
        why_valuable=f"Why {i}.",
        builds_on=builds_on or [],
    )
    return ScoredSection(
        section=sec, evaluation=ev, score=ScoreBreakdown(weighted_rubric=score, final=score), importance=importance
    )


def test_budget_defaults_and_overrides():
    cfg = PlaylistConfig()
    assert compute_budget(5400, cfg) == pytest.approx(1620)  # 30% of 90 min
    assert compute_budget(36000, cfg) == cfg.budget_max_s
    assert compute_budget(1200, cfg) == pytest.approx(600)  # min 10 min
    assert compute_budget(600, cfg) == pytest.approx(360)  # never above 60% of the video
    assert compute_budget(5400, cfg, target_minutes=20) == 1200
    assert compute_budget(5400, cfg, fraction=0.45) == pytest.approx(2100)


def test_selection_respects_budget_and_priority():
    secs = [scored(0, 90), scored(1, 30), scored(2, 75), scored(3, 60), scored(4, 45)]
    chosen = select_sections(secs, budget_s=360, config=CFG)
    assert chosen == {"S00", "S02", "S03"}
    total = sum(s.section.duration_s for s in secs if s.section.id in chosen)
    assert total <= 360 * CFG.budget_tolerance


def test_skip_sections_never_selected():
    secs = [scored(0, 95, Importance.SKIP, kind=SectionKind.SPONSOR), scored(1, 50)]
    assert select_sections(secs, budget_s=10000, config=CFG) == {"S01"}


def test_prerequisites_pulled_in():
    secs = [scored(0, 50), scored(1, 30), scored(2, 90, builds_on=["S00"])]
    chosen = select_sections(secs, budget_s=240, config=CFG)
    assert chosen == {"S00", "S02"}


def test_prerequisite_chain_never_crowds_out_target():
    # Regression from the first real run: S03 builds on S02, which builds on S01.
    # Pulling the chain in recursively used the budget and dropped S03 itself.
    secs = [scored(0, 99), scored(1, 72), scored(2, 72, builds_on=["S01"]), scored(3, 95, builds_on=["S02"])]
    chosen = select_sections(secs, budget_s=400, config=CFG)
    assert {"S00", "S03"} <= chosen
    assert "S01" not in chosen  # prerequisites of prerequisites are not pulled in


def test_adjacent_sections_merge_and_playlist_is_chronological():
    secs = [scored(0, 80), scored(1, 80), scored(2, 20), scored(3, 80, gap=0)]
    playlist, others = build_playlist(secs, duration_s=480, budget_s=400, config=CFG)
    assert [seg.section_ids for seg in playlist.segments] == [["S00", "S01"], ["S03"]]
    assert playlist.segments[0].start_s == 0  # lead-in clamped at 0
    starts = [seg.start_s for seg in playlist.segments]
    assert starts == sorted(starts)
    assert playlist.total_s == pytest.approx(sum(s.duration_s for s in playlist.segments))
    assert playlist.time_saved_s == pytest.approx(480 - playlist.total_s)
    assert 0 < playlist.coverage_estimate <= 1
    assert [s.section_id for s in playlist.skipped] == ["S02"]
    assert not others


def test_unselected_watchable_sections_offered():
    secs = [scored(0, 90), scored(1, 45), scored(2, 44)]
    playlist, others = build_playlist(secs, duration_s=360, budget_s=120, config=CFG)
    assert [s.section_ids for s in playlist.segments] == [["S00"]]
    assert [s.section_ids for s in others] == [["S01"], ["S02"]]
    assert all(not s.in_playlist for s in others)


def test_validation_flags_problems():
    secs = [scored(0, 80), scored(1, 80, gap=30)]
    blocks = [
        Block(id=f"B{i}", index=i, start_s=s.section.start_s, end_s=s.section.end_s, text="x")
        for i, s in enumerate(secs)
    ]
    playlist, others = build_playlist(secs, duration_s=280, budget_s=300, config=CFG)
    report = validate(
        duration_s=280, blocks=blocks, sections=[s.section for s in secs], playlist=playlist, others=others
    )
    assert report.ok, report.warnings

    playlist.segments[0].end_s = playlist.segments[1].start_s + 5
    playlist.segments[1].what_you_learn = ""
    report = validate(
        duration_s=280, blocks=blocks, sections=[s.section for s in secs], playlist=playlist, others=others
    )
    assert not report.ok
    assert any("overlap" in w for w in report.warnings)
    assert playlist.segments[1].what_you_learn  # repaired
    assert report.repairs


def test_validation_flags_short_transcript():
    secs = [scored(0, 80)]
    blocks = [Block(id="B0", index=0, start_s=0, end_s=120, text="x")]
    playlist, others = build_playlist(secs, duration_s=600, budget_s=200, config=CFG)
    report = validate(duration_s=600, blocks=blocks, sections=[secs[0].section], playlist=playlist, others=others)
    assert any("transcript ends" in w for w in report.warnings)


def test_validation_flags_speech_missing_between_sections():
    secs = [scored(0, 80), scored(1, 80, gap=30)]
    blocks = [
        Block(id="B0", index=0, start_s=0, end_s=120, text="x"),
        Block(id="B1", index=1, start_s=125, end_s=145, text="dropped speech"),
        Block(id="B2", index=2, start_s=150, end_s=270, text="x"),
    ]
    playlist, others = build_playlist(secs, duration_s=280, budget_s=300, config=CFG)
    report = validate(
        duration_s=280, blocks=blocks, sections=[s.section for s in secs], playlist=playlist, others=others
    )
    assert any("in no section" in w for w in report.warnings)
