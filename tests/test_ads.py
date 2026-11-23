import json

from curio_learn.analysis.ads import AD_SIGNAL_RE, candidate_windows, carve, remove_ads
from curio_learn.domain import Block, Section, SectionKind, TranscriptCue
from curio_learn.processing.normalize import Utterance, build_utterances


def section(i, start, end, kind=SectionKind.CORE, title=None):
    return Section(
        id=f"S{i:02d}",
        start_block=0,
        end_block=0,
        start_s=start,
        end_s=end,
        title=title or f"Topic {i}",
        summary=f"About topic {i}.",
        kind=kind,
        key_concepts=["c"],
    )


BLOCKS = [Block(id=f"B{i}", index=i, start_s=i * 30.0, end_s=(i + 1) * 30.0, text="x") for i in range(40)]


def assert_contiguous(sections):
    for a, b in zip(sections, sections[1:], strict=False):
        assert a.end_s == b.start_s
    assert [s.id for s in sections] == [f"S{n:02d}" for n in range(1, len(sections) + 1)]


def test_ad_signals():
    for text in [
        "go to applovin.com",
        "That is deel.com/senra.",
        "this video is sponsored by Kasten",
        "use code CURIO for 20% off",
        "check the link in the video description",
        "ramp dot com",
    ]:
        assert AD_SIGNAL_RE.search(text), text
    for text in ["the gradient of the loss", "we commit to the main branch", "postgres statistics"]:
        assert not AD_SIGNAL_RE.search(text), text


def test_windows_reach_back_before_the_sponsor_name():
    units = [Utterance(i, i * 5.0, i * 5.0 + 5, "content") for i in range(60)]
    units[30] = Utterance(30, 150.0, 155.0, "go to applovin.com today")
    ((lo, hi),) = candidate_windows(units, lead_s=60, tail_s=20)
    assert units[lo].start_s <= 90 and units[hi].end_s >= 170


def test_carve_mid_section_ad_splits_into_three():
    sections = [section(1, 0, 600), section(2, 600, 900)]
    carved, repairs = carve(sections, [(300.0, 370.0, "AppLovin")], BLOCKS)
    assert_contiguous(carved)
    assert [(s.start_s, s.end_s, s.kind) for s in carved[:3]] == [
        (0, 300, SectionKind.CORE),
        (300, 370, SectionKind.SPONSOR),
        (370, 600, SectionKind.CORE),
    ]
    assert carved[1].title == "Sponsor message: AppLovin"
    assert carved[2].title == "Topic 1 (continued)"
    assert carved[3].title == "Topic 2"
    assert repairs and "AppLovin" in repairs[0]


def test_tiny_content_fragment_joins_content_not_the_ad():
    sections = [section(1, 0, 588), section(2, 588, 900)]
    carved, _ = carve(sections, [(600.0, 620.0, "Acme")], BLOCKS)
    assert_contiguous(carved)
    assert [(s.start_s, s.end_s, s.kind) for s in carved] == [
        (0, 600, SectionKind.CORE),
        (600, 620, SectionKind.SPONSOR),
        (620, 900, SectionKind.CORE),
    ]


def test_carve_ad_spanning_two_sections_and_absorbing_tiny_pieces():
    sections = [section(1, 0, 300), section(2, 300, 310), section(3, 310, 900)]
    carved, _ = carve(sections, [(250.0, 305.0, "Ramp"), (305.0, 330.0, "Ramp")], BLOCKS)
    assert_contiguous(carved)
    kinds = [s.kind for s in carved]
    assert kinds == [SectionKind.CORE, SectionKind.SPONSOR, SectionKind.CORE]
    ad = carved[1]
    assert (ad.start_s, ad.end_s) == (250.0, 330.0)  # S02's 5 s leftover absorbed
    assert carved[0].end_s == 250.0 and carved[2].start_s == 330.0


def test_carve_ad_at_section_start_moves_the_start():
    sections = [section(1, 0, 300), section(2, 300, 900)]
    carved, _ = carve(sections, [(300.0, 360.0, "Deel")], BLOCKS)
    assert [(s.start_s, s.kind) for s in carved] == [
        (0, SectionKind.CORE),
        (300, SectionKind.SPONSOR),
        (360, SectionKind.CORE),
    ]
    assert carved[2].title == "Topic 2"  # nothing came before it, so not "(continued)"


class AdsProvider:
    name = "ads"

    def __init__(self, decide):
        self.decide = decide
        self.calls = 0

    def schema_for(self, model_cls):
        return {}

    def complete_json(self, *, model, system, user, schema, max_tokens, shared_prefix=""):
        self.calls += 1
        windows = []
        for part in user.split("### Window ")[1:]:
            wid = part.split("\n", 1)[0]
            lines = [ln.strip() for ln in part.splitlines() if ln.strip()[:1].isdigit()]
            windows.append(self.decide(wid, lines))
        return json.dumps({"windows": windows})


def podcast_cues():
    """Conversation with a mid-roll ad that opens with a content-like segue."""
    lines = [f"We talked about building teams, point {i}." for i in range(40)]
    ad = [
        "I found one of my all-time favorite quotes in Zero to One.",
        "That is exactly what AppLovin has done with advertising.",
        "AppLovin connects you with a billion customers.",
        "Go to applovin.com to get started.",
        "That's applovin.com.",
    ]
    lines[20:20] = ad
    return [TranscriptCue(start_s=i * 6.0, duration_s=6.0, text=t) for i, t in enumerate(lines)], ad


def _ad_span(wid, lines, first_text, last_text):
    first = next(int(ln.split(".")[0]) for ln in lines if first_text in ln)
    last = max(int(ln.split(".")[0]) for ln in lines if last_text in ln)
    return {"window_id": wid, "is_ad": True, "first_sentence": first, "last_sentence": last, "sponsor": "AppLovin"}


def test_remove_ads_cuts_out_segue_and_ad():
    cues, ad = podcast_cues()
    units = build_utterances(cues)
    sections = [section(1, 0, cues[-1].start_s + 6.0)]
    provider = AdsProvider(lambda wid, lines: _ad_span(wid, lines, "Zero to One", "That's applovin.com"))
    carved, repairs, warnings = remove_ads(provider, model="m", sections=sections, blocks=BLOCKS, units=units)
    sponsor = [s for s in carved if s.kind == SectionKind.SPONSOR]
    assert len(sponsor) == 1 and not warnings
    assert sponsor[0].start_s == 20 * 6.0  # starts at the segue, not at the brand name
    assert sponsor[0].end_s == (20 + len(ad)) * 6.0
    assert_contiguous(carved)
    assert any("AppLovin" in r for r in repairs)


def test_mentions_that_are_not_ads_are_kept():
    cues = [
        TranscriptCue(start_s=i * 6.0, duration_s=6.0, text=t)
        for i, t in enumerate(
            [f"Point {i} about the planner." for i in range(10)]
            + ["The paper is on arxiv.org, see section three."]
            + [f"Point {i} about evaluation." for i in range(10)]
        )
    ]
    sections = [section(1, 0, 21 * 6.0)]
    provider = AdsProvider(
        lambda wid, lines: {"window_id": wid, "is_ad": False, "first_sentence": 0, "last_sentence": 0, "sponsor": ""}
    )
    carved, repairs, _ = remove_ads(provider, model="m", sections=sections, blocks=BLOCKS, units=build_utterances(cues))
    assert provider.calls == 1 and carved == sections and not repairs


def test_no_ad_signals_means_no_llm_call():
    cues = [TranscriptCue(start_s=i * 6.0, duration_s=6.0, text=f"Plain point {i}.") for i in range(30)]
    provider = AdsProvider(lambda wid, lines: {})
    carved, _, _ = remove_ads(
        provider, model="m", sections=[section(1, 0, 180)], blocks=BLOCKS, units=build_utterances(cues)
    )
    assert provider.calls == 0 and len(carved) == 1


def test_out_of_range_ad_choice_is_ignored():
    cues, _ = podcast_cues()
    sections = [section(1, 0, cues[-1].start_s + 6.0)]
    provider = AdsProvider(
        lambda wid, lines: {"window_id": wid, "is_ad": True, "first_sentence": 3, "last_sentence": 999, "sponsor": "X"}
    )
    carved, repairs, _ = remove_ads(provider, model="m", sections=sections, blocks=BLOCKS, units=build_utterances(cues))
    assert carved == sections and any("outside" in r for r in repairs)
