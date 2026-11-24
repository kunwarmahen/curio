import json

from curio_learn.analysis.boundaries import refine_boundaries
from curio_learn.analysis.outline import OutlineSectionOut, build_sections
from curio_learn.domain import TranscriptCue
from curio_learn.processing.normalize import build_blocks, build_utterances, clean_cues

from .conftest import make_cues


def cue(start, dur, text):
    return TranscriptCue(start_s=start, duration_s=dur, text=text)


# ------------------------------------------------------------------ utterances


def test_utterances_split_at_sentence_ends_and_keep_cue_times():
    cues = [cue(0, 2, "So this is"), cue(2, 2, "the first idea."), cue(4, 3, "Now the second."), cue(7, 2, "Done.")]
    units = build_utterances(cues)
    assert [u.text for u in units] == ["So this is the first idea.", "Now the second.", "Done."]
    assert [(u.start_s, u.end_s) for u in units] == [(0, 4), (4, 7), (7, 9)]


def test_new_sentence_inside_a_cue_starts_a_unit_at_that_cue():
    # The topic starts mid-cue; starting at that cue beats cutting off its first words.
    cues = [cue(0, 3, "and that is the answer"), cue(3, 3, "to your question. Let's talk about RL"), cue(6, 3, "now.")]
    units = build_utterances(cues)
    assert units[1].start_s == 3 and units[1].text.startswith("to your question. Let's talk")


def test_unpunctuated_captions_split_by_length_and_pauses():
    cues = [cue(i * 3.0, 3.0, f"word{i} more words") for i in range(10)]
    cues.append(cue(32.0, 3.0, "after a pause"))
    units = build_utterances(cues, max_s=12)
    assert all(u.end_s - u.start_s <= 12 for u in units)
    assert units[-1].start_s == 32.0


def test_every_unit_edge_is_a_real_cue_time():
    cues = make_cues(200)
    clean = clean_cues(cues)
    starts, ends = {c.start_s for c in clean}, {c.end_s for c in clean}
    for u in build_utterances(cues):
        assert u.start_s in starts and u.end_s in ends


# ------------------------------------------------------------------ refinement


class ChooseProvider:
    """Answers the boundary call with fixed choices per boundary id."""

    name = "choose"

    def __init__(self, choices=None, fail=False):
        self.choices = choices or {}
        self.fail = fail
        self.user = ""

    def schema_for(self, model_cls):
        return {}

    def complete_json(self, *, model, system, user, schema, max_tokens, shared_prefix=""):
        self.user = user
        if self.fail:
            return "not json"
        return json.dumps({"boundaries": [{"boundary_id": k, "first_sentence": v} for k, v in self.choices.items()]})


def lecture_cues():
    """Three topics. Topic B's first sentence sits in the middle of a block."""
    cues, t = [], 0.0
    for topic, n in (("alpha", 30), ("beta", 30), ("gamma", 30)):
        for i in range(n):
            text = f"Let's talk about {topic}." if i == 0 else f"More on {topic} point {i}."
            cues.append(cue(t, 3.0, text))
            t += 3.0
    return cues


def outline(blocks, starts):
    raw = [OutlineSectionOut(start_block=b, title=f"T{b}", summary="s", kind="core", key_concepts=[]) for b in starts]
    return build_sections(raw, blocks)[0]


def test_boundary_moves_to_chosen_sentence_and_neighbour_end_follows():
    cues = lecture_cues()
    blocks, units = build_blocks(cues), build_utterances(cues)
    beta_start = next(u.start_s for u in units if u.text == "Let's talk about beta.")  # 90 s
    beta_block = max(b.index for b in blocks if b.start_s <= beta_start)
    sections = outline(blocks, [0, beta_block])  # block-level start, before the real topic change
    assert sections[1].start_s < beta_start

    first_call = ChooseProvider()
    refine_boundaries(first_call, model="m", sections=sections, blocks=blocks, units=units)
    # Find the number the model would see for the real start of "beta".
    line = next(ln for ln in first_call.user.splitlines() if "Let's talk about beta." in ln)
    k = int(line.strip().split(".")[0])

    refined, repairs, warnings = refine_boundaries(
        ChooseProvider({"S01/S02": k}), model="m", sections=sections, blocks=blocks, units=units
    )
    assert refined[1].start_s == beta_start
    assert refined[0].end_s <= refined[1].start_s
    assert refined[0].end_s == max(u.end_s for u in units if u.start_s < beta_start)
    assert refined[1].start_block == sections[1].start_block  # block refs unchanged
    assert not repairs and not warnings


def test_out_of_range_and_too_short_choices_are_rejected():
    cues = lecture_cues()
    blocks, units = build_blocks(cues), build_utterances(cues)
    sections = outline(blocks, [0, 3, 6])
    refined, repairs, _ = refine_boundaries(
        ChooseProvider({"S01/S02": 999, "S02/S03": 1}),
        model="m",
        sections=sections,
        blocks=blocks,
        units=units,
        min_section_s=200,
    )
    assert refined[1].start_s == sections[1].start_s and refined[2].start_s == sections[2].start_s
    assert any("only" in r for r in repairs)
    assert any("too short" in r for r in repairs)


def test_failed_refinement_keeps_block_boundaries_with_warning(monkeypatch):
    monkeypatch.setattr("curio_learn.llm.base.time.sleep", lambda _: None)
    cues = lecture_cues()
    blocks, units = build_blocks(cues), build_utterances(cues)
    sections = outline(blocks, [0, 3])
    refined, _, warnings = refine_boundaries(
        ChooseProvider(fail=True), model="m", sections=sections, blocks=blocks, units=units
    )
    assert [s.start_s for s in refined] == [s.start_s for s in sections]
    assert warnings and "block-level" in warnings[0]


def test_candidates_never_cross_into_other_sections():
    cues = lecture_cues()
    blocks, units = build_blocks(cues), build_utterances(cues)
    sections = outline(blocks, [0, 2, 4])
    provider = ChooseProvider()
    refine_boundaries(provider, model="m", sections=sections, blocks=blocks, units=units)
    # Boundary S02/S03 may only offer sentences after S02's first block.
    part = provider.user.split("### Boundary S02/S03")[1]
    times = [ln.split("[")[1].split("]")[0] for ln in part.splitlines() if ln.strip()[:1].isdigit()]
    first = times[0].split(":")
    assert int(first[0]) * 60 + int(first[1]) >= blocks[sections[1].start_block + 1].start_s - 1


class BatchProvider(ChooseProvider):
    """Records each boundary call and answers sentence 1 for every boundary in it."""

    def __init__(self):
        super().__init__()
        self.calls: list[list[str]] = []

    def complete_json(self, *, model, system, user, schema, max_tokens, shared_prefix=""):
        import re

        ids = re.findall(r"### Boundary (\S+)", user)
        self.calls.append(ids)
        return json.dumps({"boundaries": [{"boundary_id": b, "first_sentence": 1} for b in ids]})


def test_many_boundaries_are_sent_in_batches_that_fit_the_context():
    cues = lecture_cues()
    blocks, units = build_blocks(cues), build_utterances(cues)
    sections = outline(blocks, [0, 3, 6, 9])
    assert len(sections) == 4
    provider = BatchProvider()
    _, _, warnings = refine_boundaries(
        provider, model="m", sections=sections, blocks=blocks, units=units, min_section_s=0, batch_tokens=50
    )
    assert len(provider.calls) > 1 and not warnings
    sent = [b for call in provider.calls for b in call]
    assert sorted(sent) == sorted(set(sent))  # each boundary once
    assert len(sent) == len(sections) - 1
