"""Transcript providers.

The default provider reads public caption tracks with `youtube-transcript-api`.
Other compliant sources (uploaded .srt/.vtt, the YouTube Data API for channel
owners, local speech-to-text) can implement the same protocol.
"""

from __future__ import annotations

import logging
from typing import Protocol

from curio_learn.domain import Transcript, TranscriptCue
from curio_learn.errors import RateLimited, TranscriptUnavailable, VideoUnavailable

log = logging.getLogger(__name__)


class TranscriptProvider(Protocol):
    def get_transcript(self, video_id: str, languages: list[str]) -> Transcript: ...


class YouTubeCaptionsProvider:
    """Manual captions first, then auto-generated, then a translated manual track."""

    source = "youtube-transcript-api"

    def __init__(self, api=None):
        from youtube_transcript_api import YouTubeTranscriptApi

        self._api = api or YouTubeTranscriptApi()

    def get_transcript(self, video_id: str, languages: list[str]) -> Transcript:
        import youtube_transcript_api as yta

        try:
            listing = self._api.list(video_id)
            track = self._choose_track(listing, languages)
            fetched = track.fetch()
        except (yta.IpBlocked, yta.RequestBlocked) as exc:
            raise RateLimited(
                "YouTube is blocking transcript requests from this network. Wait and retry, or configure a proxy."
            ) from exc
        except (yta.TranscriptsDisabled, yta.NoTranscriptFound) as exc:
            raise TranscriptUnavailable("This video has no accessible captions, so it can't be analyzed.") from exc
        except (yta.VideoUnavailable, yta.VideoUnplayable, yta.AgeRestricted, yta.InvalidVideoId) as exc:
            raise VideoUnavailable(f"YouTube won't serve captions for this video: {exc.__class__.__name__}") from exc
        except yta.CouldNotRetrieveTranscript as exc:
            raise TranscriptUnavailable(f"Could not retrieve captions: {exc.__class__.__name__}") from exc

        cues = [
            TranscriptCue(start_s=float(s.start), duration_s=float(s.duration), text=s.text)
            for s in fetched.snippets
            if s.text and s.text.strip()
        ]
        if not cues:
            raise TranscriptUnavailable("The caption track is empty.")
        log.info(
            "Fetched %d cues (%s, generated=%s) for %s", len(cues), track.language_code, track.is_generated, video_id
        )
        return Transcript(
            video_id=video_id,
            language=track.language_code,
            is_generated=track.is_generated,
            source=self.source,
            cues=cues,
        )

    @staticmethod
    def _choose_track(listing, languages: list[str]):
        import youtube_transcript_api as yta

        for finder in (listing.find_manually_created_transcript, listing.find_generated_transcript):
            try:
                return finder(languages)
            except yta.NoTranscriptFound:
                continue
        # Fall back to YouTube's own translation of any translatable track.
        tracks = list(listing)
        tracks.sort(key=lambda t: t.is_generated)  # prefer manual
        for track in tracks:
            if track.is_translatable:
                try:
                    return track.translate(languages[0].split("-")[0])
                except yta.YouTubeTranscriptApiException:
                    continue
        available = ", ".join(f"{t.language_code}{'*' if t.is_generated else ''}" for t in tracks)
        raise TranscriptUnavailable(
            f"No captions in {', '.join(languages)}" + (f" (available: {available})" if available else "")
        )
