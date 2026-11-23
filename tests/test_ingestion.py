import pytest

from curio_learn.errors import InvalidURL, TranscriptUnavailable
from curio_learn.ingestion.transcripts import YouTubeCaptionsProvider
from curio_learn.ingestion.url import extract_video_id

VID = "e_m99aR7_3Q"


@pytest.mark.parametrize(
    "url",
    [
        VID,
        f"https://www.youtube.com/watch?v={VID}",
        f"https://www.youtube.com/watch?v={VID}&t=120s&list=PL123",
        f"youtube.com/watch?v={VID}",
        f"https://m.youtube.com/watch?v={VID}",
        f"https://youtu.be/{VID}?si=abc",
        f"https://www.youtube.com/shorts/{VID}",
        f"https://www.youtube.com/embed/{VID}",
        f"https://www.youtube.com/live/{VID}?feature=share",
        f"  https://www.youtube.com/watch?v={VID}  ",
    ],
)
def test_extract_video_id(url):
    assert extract_video_id(url) == VID


@pytest.mark.parametrize(
    "url",
    [
        "",
        "hello",
        "https://vimeo.com/123456",
        "https://www.youtube.com/watch?v=short",
        "https://www.youtube.com/channel/UC123",
    ],
)
def test_extract_video_id_rejects(url):
    with pytest.raises(InvalidURL):
        extract_video_id(url)


class _Track:
    def __init__(self, code, generated, translatable=True):
        self.language_code, self.is_generated, self.is_translatable = code, generated, translatable


class _Listing:
    def __init__(self, tracks):
        self.tracks = tracks

    def __iter__(self):
        return iter(self.tracks)

    def _find(self, langs, generated):
        import youtube_transcript_api as yta

        for code in langs:
            for t in self.tracks:
                if t.language_code == code and t.is_generated == generated:
                    return t
        raise yta.NoTranscriptFound("vid", langs, None)

    def find_manually_created_transcript(self, langs):
        return self._find(langs, False)

    def find_generated_transcript(self, langs):
        return self._find(langs, True)


def test_track_choice_prefers_manual_then_generated():
    choose = YouTubeCaptionsProvider._choose_track
    both = _Listing([_Track("en", True), _Track("en", False)])
    assert choose(both, ["en"]).is_generated is False
    auto_only = _Listing([_Track("en", True)])
    assert choose(auto_only, ["en"]).is_generated is True


def test_track_choice_reports_available_languages():
    listing = _Listing([_Track("de", False, translatable=False)])
    with pytest.raises(TranscriptUnavailable, match="available: de"):
        YouTubeCaptionsProvider._choose_track(listing, ["en"])
