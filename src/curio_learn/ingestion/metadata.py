"""Video metadata providers. Never downloads media."""

from __future__ import annotations

import logging
from typing import Protocol

from curio_learn.domain import Chapter, VideoMetadata
from curio_learn.errors import RateLimited, VideoUnavailable

log = logging.getLogger(__name__)


class MetadataProvider(Protocol):
    def get_metadata(self, video_id: str) -> VideoMetadata: ...


class YtDlpMetadataProvider:
    """Reads metadata with yt-dlp in `skip_download` mode."""

    def get_metadata(self, video_id: str) -> VideoMetadata:
        import yt_dlp

        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
        url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            msg = str(exc)
            if "429" in msg or "Too Many Requests" in msg:
                raise RateLimited("YouTube is rate-limiting requests; try again later.") from exc
            raise VideoUnavailable(_clean_ytdlp_error(msg)) from exc

        if info.get("is_live"):
            raise VideoUnavailable("Live streams can't be analyzed until they have ended.")
        duration = info.get("duration")
        if not duration:
            raise VideoUnavailable("YouTube did not report a duration for this video.")

        chapters = [
            Chapter(title=c.get("title", ""), start_s=c.get("start_time", 0.0), end_s=c.get("end_time"))
            for c in (info.get("chapters") or [])
        ]
        return VideoMetadata(
            video_id=video_id,
            title=info.get("title") or video_id,
            channel=info.get("channel") or info.get("uploader") or "",
            duration_s=float(duration),
            description=info.get("description") or "",
            thumbnail_url=info.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            upload_date=info.get("upload_date") or "",
            chapters=chapters,
        )


def _clean_ytdlp_error(msg: str) -> str:
    msg = msg.replace("ERROR: ", "")
    if "] " in msg:
        msg = msg.split("] ", 1)[-1]
    return msg.strip() or "Video is unavailable."
