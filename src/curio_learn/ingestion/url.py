"""Extract a YouTube video id from the URL forms people paste."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from curio_learn.errors import InvalidURL

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YT_HOSTS = {
    "youtube.com",
    "m.youtube.com",
    "www.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}


def extract_video_id(url_or_id: str) -> str:
    text = (url_or_id or "").strip()
    if _ID_RE.match(text):
        return text
    if not re.match(r"^https?://", text):
        text = "https://" + text
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    candidate = ""
    if host in ("youtu.be", "www.youtu.be"):
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif host in _YT_HOSTS:
        if parsed.path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [""])[0]
        else:
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v", "e"):
                candidate = parts[1]
    if _ID_RE.match(candidate):
        return candidate
    raise InvalidURL(f"Not a recognizable YouTube video URL: {url_or_id!r}")
