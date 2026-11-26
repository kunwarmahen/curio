import pytest

from curio_learn.analysis.playlist import (
    OutsideNeed,
    PlaylistConfig,
    build_playlist,
    compute_budget,
    cover_needs,
    select_sections,
)
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
    chosen = select_sections(secs, budget_s=400, config=CFG)  # room for three
    assert {"S00", "S03"} <= chosen
    assert "S02" in chosen and "S01" not in chosen  # the direct prerequisite wins the tie


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


def test_an_essential_section_that_does_not_fit_is_labeled_recommended():
    from curio_learn.analysis.playlist import BEYOND_BUDGET, fit_labels

    # Three essential sections of 2 minutes, a 5-minute budget: two fit.
    secs = [scored(0, 95), scored(1, 90), scored(2, 85), scored(3, 60)]
    chosen = select_sections(secs, 300, CFG)
    assert chosen == {"S00", "S01"}
    labeled = fit_labels(secs, chosen, 300)
    assert [s.importance for s in labeled] == [
        Importance.ESSENTIAL,
        Importance.ESSENTIAL,
        Importance.RECOMMENDED,
        Importance.RECOMMENDED,
    ]
    note = labeled[2].score.adjustments[-1]
    assert note.rule == BEYOND_BUDGET and note.delta == 0 and "5-minute" in note.reason
    assert labeled[2].score.final == 85  # the score itself is unchanged
    assert secs[2].importance is Importance.ESSENTIAL  # inputs aren't modified

    playlist, others = build_playlist(labeled, duration_s=480, budget_s=300, config=CFG, chosen=chosen)
    assert [s.section_ids for s in playlist.segments] == [["S00", "S01"]]
    assert others[0].importance is Importance.RECOMMENDED
    # Every essential is in the playlist.
    assert {s.section.id for s in labeled if s.importance is Importance.ESSENTIAL} <= chosen


def test_validation_accepts_boundaries_next_to_a_cut_out_ad():
    # Content ends where the ad's first cue starts and resumes where its last cue ends.
    secs = [scored(0, 80), scored(1, 0, kind=SectionKind.SPONSOR), scored(2, 80)]
    blocks = [Block(id="B0", index=0, start_s=0, end_s=360, text="x")]
    cue_starts = {0.0, 100.0, 120.0, 250.0}
    cue_ends = {99.0, 240.0, 245.0, 360.0}
    secs[0].section.end_s = 120.0  # an ad cue's start
    secs[2].section.start_s = 240.0  # an ad cue's end
    playlist, others = build_playlist(secs, duration_s=360, budget_s=600, config=CFG)
    report = validate(
        duration_s=360,
        blocks=blocks,
        sections=[s.section for s in secs],
        playlist=playlist,
        others=others,
        cue_starts=cue_starts,
        cue_ends=cue_ends,
    )
    assert not any("caption times" in w for w in report.warnings), report.warnings


def test_one_long_section_does_not_crowd_out_several_nearly_as_good():
    # MIT 6.006 lecture 1: a 10-minute proof scored 100 left out efficiency (87, 5 min)
    # and complexity classes (93, 6 min) from a 14-minute playlist.
    proof = scored(0, 100, dur=618)
    efficiency = scored(1, 87, dur=300)
    classes = scored(2, 93, dur=360)
    chosen = select_sections([proof, efficiency, classes], budget_s=14 * 60, config=CFG)
    assert chosen == {"S01", "S02"}
    # When nothing else is close, the long top section still goes in.
    weak = scored(1, 60, dur=300)
    chosen = select_sections([proof, weak, scored(2, 62, dur=360)], budget_s=14 * 60, config=CFG)
    assert "S00" in chosen


def test_course_selection_keeps_one_half_of_each_repeat_and_refills_the_time():
    # S00 and S01 teach the same thing (S01 slightly better); S02, S03 are unrelated.
    secs = [scored(0, 90), scored(1, 92), scored(2, 70), scored(3, 60)]
    excludes = {"S00": {"S01"}, "S01": {"S00"}}
    chosen = select_sections(secs, budget_s=400, config=CFG, excludes=excludes)  # room for three
    assert "S01" in chosen and "S00" not in chosen  # the better half
    assert chosen == {"S01", "S02", "S03"}  # the freed time went to the next best


def test_course_selection_handles_chains_of_repeats_and_prerequisites():
    # S00 repeats S01 and S02; S03 builds on S00.
    secs = [scored(0, 80), scored(1, 85), scored(2, 75), scored(3, 95, builds_on=["S00"])]
    excludes = {"S00": {"S01", "S02"}, "S01": {"S00"}, "S02": {"S00"}}
    chosen = select_sections(secs, budget_s=600, config=CFG, excludes=excludes)
    assert not any(b in chosen for a in chosen for b in excludes.get(a, ()))
    assert {"S01", "S03"} <= chosen


def test_a_top_section_is_not_crowded_out_by_shorter_ones():
    # CS329Z: DSPy optimizers (100, 9.3 min) lost to shorter sections in the knapsack.
    top = scored(0, 100, dur=560)
    shorter = [scored(i, 90, dur=100) for i in range(1, 30)]
    chosen = select_sections([top, *shorter], budget_s=30 * 60, config=CFG)
    assert "S00" in chosen
    # Without the reservation the knapsack prefers the short ones.
    plain = PlaylistConfig(merge_gap_s=CFG.merge_gap_s, top_score=101)
    assert "S00" not in select_sections([top, *shorter], budget_s=30 * 60, config=plain)


def test_top_sections_fill_at_most_half_the_playlist_and_skip_repeats():
    tops = [scored(i, 100 - i, dur=300) for i in range(4)]  # 5 min each, all 95+
    rest = [scored(i, 80, dur=60) for i in range(4, 30)]
    chosen = select_sections([*tops, *rest], budget_s=20 * 60, config=CFG)
    # Half of the 22-minute limit holds two top sections; the rest go to the knapsack.
    assert {"S00", "S01"} <= chosen
    # A top section that repeats a better one is not reserved, and never joins it.
    excludes = {"S00": {"S01"}, "S01": {"S00"}}
    chosen = select_sections([*tops, *rest], budget_s=20 * 60, config=CFG, excludes=excludes)
    assert "S00" in chosen and "S01" not in chosen


def _outcomes(out):
    return [(o.status, o.teacher, o.made_room) for o in out]


def test_a_needed_teacher_that_fits_goes_in():
    secs = [scored(0, 90, dur=600), scored(1, 60, dur=120)]
    need = OutsideNeed(section_id="S00", prerequisite="LLMs", teachers=["S01"])
    chosen, out = cover_needs(secs, {"S00"}, 800, PlaylistConfig(), [need])
    assert chosen == {"S00", "S01"} and _outcomes(out) == [("added", "S01", [])]


def test_a_needed_teacher_swaps_out_weaker_picks_within_its_share():
    # 30 minutes, full: the teacher (2 min) replaces the weakest pick.
    secs = [scored(0, 95, dur=900), scored(1, 70, dur=540), scored(2, 50, dur=540), scored(3, 60, dur=120)]
    need = OutsideNeed(section_id="S00", prerequisite="LLMs", teachers=["S03"])
    chosen, out = cover_needs(secs, {"S00", "S01", "S02"}, 1800, PlaylistConfig(), [need])
    assert chosen == {"S00", "S01", "S03"} and _outcomes(out) == [("added", "S03", ["S02"])]


def test_a_needed_teacher_never_displaces_pins_or_more_than_the_need_is_worth():
    secs = [scored(0, 60, dur=900), scored(1, 95, dur=900), scored(3, 60, dur=120)]
    need = OutsideNeed(section_id="S00", prerequisite="LLMs", teachers=["S03"])
    untaught = [("untaught", "S03", [])]
    # The only other pick is worth more than the teacher and the section that needs it.
    chosen, out = cover_needs(secs, {"S00", "S01"}, 1650, PlaylistConfig(), [need])
    assert chosen == {"S00", "S01"} and _outcomes(out) == untaught
    # ... or it is pinned.
    secs[1] = scored(1, 40, dur=900)
    chosen, out = cover_needs(secs, {"S00", "S01"}, 1650, PlaylistConfig(), [need], required={"S01"})
    assert chosen == {"S00", "S01"} and _outcomes(out) == untaught
    # A teacher longer than the need share (10% of the playlist) isn't forced in.
    secs = [scored(0, 90, dur=900), scored(1, 40, dur=900), scored(3, 60, dur=600)]
    chosen, out = cover_needs(secs, {"S00", "S01"}, 1650, PlaylistConfig(), [need])
    assert chosen == {"S00", "S01"} and _outcomes(out) == untaught
    # A section that isn't in the playlist has no outcome.
    assert cover_needs(secs, {"S01"}, 1650, PlaylistConfig(), [need])[1] == []


def test_a_need_already_taught_in_the_playlist_changes_nothing():
    secs = [scored(0, 90, dur=600), scored(1, 60, dur=120), scored(2, 60, dur=60)]
    need = OutsideNeed(section_id="S00", prerequisite="LLMs", teachers=["S02", "S01"])
    chosen, out = cover_needs(secs, {"S00", "S01"}, 720, PlaylistConfig(), [need])
    assert chosen == {"S00", "S01"} and _outcomes(out) == [("in_playlist", "S01", [])]


def test_an_untaught_need_says_why():
    need = OutsideNeed(section_id="S00", prerequisite="LLMs", teachers=["S01"])
    # The only teacher repeats a section in the playlist.
    secs = [scored(0, 90, dur=600), scored(1, 60, dur=120), scored(2, 60, dur=120)]
    _, out = cover_needs(secs, {"S00", "S02"}, 900, PlaylistConfig(), [need], excludes={"S01": {"S02"}, "S02": {"S01"}})
    assert [(o.status, o.why) for o in out] == [("untaught", "repeat")]
    # ... or is marked skip.
    secs[1] = scored(1, 20, dur=120)
    _, out = cover_needs(secs, {"S00"}, 900, PlaylistConfig(), [need])
    assert [(o.status, o.why) for o in out] == [("untaught", "skip")]


def test_a_foundation_of_a_top_section_wins_a_near_tie():
    # S01 (100) is reserved as a top section and builds on S00; one of S00 and S02 fits
    # next to it. S02 is worth a little more on its own; S00 is what S01 builds on.
    secs = [scored(0, 90, dur=180), scored(1, 100, dur=300, builds_on=["S00"]), scored(2, 91, dur=180)]
    assert select_sections(secs, 573, PlaylistConfig()) == {"S00", "S01"}
