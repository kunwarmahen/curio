"""YouTube playlists (a course's lectures), listed with yt-dlp. Never downloads media."""

from __future__ import annotations

import re
from typing import Protocol
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel

from curio_learn.errors import InvalidURL, RateLimited, VideoUnavailable

_LIST_RE = re.compile(r"^[A-Za-z0-9_-]{10,64}$")
_LECTURE_RE = re.compile(r"\b(?:lecture|lec\.?|session|week|part|class|episode|ep\.?)\s*#?\s*(\d{1,3})\b", re.I)


class PlaylistEntry(BaseModel):
    video_id: str
    title: str
    duration_s: float = 0.0


class PlaylistInfo(BaseModel):
    playlist_id: str
    title: str
    channel: str = ""
    entries: list[PlaylistEntry]  # in course order (see `course_order`)
    unavailable: int = 0  # private or deleted entries


class PlaylistProvider(Protocol):
    def get_playlist(self, playlist_id: str) -> PlaylistInfo: ...


def extract_playlist_id(url: str) -> str | None:
    """The `list=` id of a YouTube link, or None. Mixes and the "watch later" and
    "liked" lists aren't courses, so they don't count."""
    text = (url or "").strip()
    if not re.match(r"^https?://", text):
        text = "https://" + text
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if host not in ("youtube.com", "music.youtube.com", "youtu.be"):
        return None
    list_id = parse_qs(parsed.query).get("list", [""])[0]
    if not _LIST_RE.match(list_id) or list_id.startswith(("RD", "WL", "LL")):
        return None
    return list_id


def is_playlist_link(url: str) -> bool:
    """A link to a playlist rather than to one video in it."""
    parsed = urlparse(url.strip() if "://" in url else "https://" + url.strip())
    return extract_playlist_id(url) is not None and parsed.path.rstrip("/") == "/playlist"


def lecture_number(title: str) -> int | None:
    m = _LECTURE_RE.search(title)
    return int(m.group(1)) if m else None


def course_order(entries: list[PlaylistEntry]) -> list[PlaylistEntry]:
    """Playlists of lectures are often newest first. When the lecture numbers in the
    titles mostly go down along the playlist, reverse it; otherwise keep its order."""
    nums = [n for n in (lecture_number(e.title) for e in entries) if n is not None]
    if len(nums) < 2:
        return list(entries)
    down = sum(b < a for a, b in zip(nums, nums[1:], strict=False))
    up = sum(b > a for a, b in zip(nums, nums[1:], strict=False))
    return list(reversed(entries)) if down > up else list(entries)


class YtDlpPlaylistProvider:
    def get_playlist(self, playlist_id: str) -> PlaylistInfo:
        import yt_dlp

        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": "in_playlist"}
        url = f"https://www.youtube.com/playlist?list={playlist_id}"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            msg = str(exc)
            if "429" in msg or "Too Many Requests" in msg:
                raise RateLimited("YouTube is rate-limiting requests; try again later.") from exc
            raise VideoUnavailable(f"Couldn't read this playlist: {msg.replace('ERROR: ', '')}") from exc
        entries, unavailable, seen = [], 0, set()
        for e in info.get("entries") or []:
            vid, title = (e or {}).get("id"), (e or {}).get("title")
            # Private and deleted videos come back without a title (or as "[Private video]").
            if not vid or not title or title in ("[Private video]", "[Deleted video]"):
                unavailable += 1
                continue
            if vid in seen:
                continue
            seen.add(vid)
            entries.append(PlaylistEntry(video_id=vid, title=title, duration_s=float(e.get("duration") or 0)))
        if not entries:
            raise InvalidURL("This playlist has no videos Curio can open.")
        return PlaylistInfo(
            playlist_id=playlist_id,
            title=info.get("title") or playlist_id,
            channel=info.get("channel") or info.get("uploader") or "",
            entries=course_order(entries),
            unavailable=unavailable,
        )
