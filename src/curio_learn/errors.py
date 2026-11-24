"""Typed errors that frontends can map to user-facing messages."""


class CurioError(Exception):
    code = "curio_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class InvalidURL(CurioError):
    code = "invalid_url"


class VideoUnavailable(CurioError):
    code = "video_unavailable"


class TranscriptUnavailable(CurioError):
    code = "transcript_unavailable"


class RateLimited(CurioError):
    code = "rate_limited"


class LLMError(CurioError):
    code = "llm_error"


class AnalysisError(CurioError):
    code = "analysis_error"


class Paused(CurioError):
    """A queued analysis stopped because something else started using the GPU."""

    code = "paused"
