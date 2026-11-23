"""CurioService: the single entry point frontends use (the FastAPI app behind the web UI, and the CLI)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable

from curio_learn.analysis.pipeline import AnalysisPipeline
from curio_learn.config import Settings, get_settings
from curio_learn.domain import AnalysisStatus, Block, LearningPreferences, VideoAnalysis, VideoMetadata
from curio_learn.errors import CurioError
from curio_learn.ingestion.metadata import MetadataProvider, YtDlpMetadataProvider
from curio_learn.ingestion.transcripts import TranscriptProvider, YouTubeCaptionsProvider
from curio_learn.ingestion.url import extract_video_id
from curio_learn.llm.base import LLMProvider
from curio_learn.llm.factory import build_provider
from curio_learn.processing.normalize import build_blocks
from curio_learn.storage.repository import RecentVideo, Repository

log = logging.getLogger(__name__)

# Bump when pipeline changes should invalidate cached analyses.
PIPELINE_VERSION = 5

Progress = Callable[[str], None]


class CurioService:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        repository: Repository | None = None,
        llm: LLMProvider | None = None,
        metadata: MetadataProvider | None = None,
        transcripts: TranscriptProvider | None = None,
    ):
        self.settings = settings or get_settings()
        self.repo = repository or Repository(self.settings.database_url)
        self._llm = llm
        self.metadata = metadata or YtDlpMetadataProvider()
        self._transcripts = transcripts

    @property
    def llm(self) -> LLMProvider:
        if self._llm is None:
            self._llm = build_provider(self.settings)
        return self._llm

    @property
    def transcripts(self) -> TranscriptProvider:
        if self._transcripts is None:
            self._transcripts = YouTubeCaptionsProvider()
        return self._transcripts

    def llm_key(self) -> str:
        s = self.settings
        return f"{self.llm.name}:{s.model_for('outline')}:{s.model_for('evaluate')}:v{PIPELINE_VERSION}"

    # --------------------------------------------------------------- ingestion

    def ingest(self, video_id: str, *, refresh: bool = False):
        meta = None if refresh else self.repo.get_video(video_id)
        if meta is None:
            meta = self.metadata.get_metadata(video_id)
            self.repo.save_video(meta)
        transcript = None if refresh else self.repo.get_transcript(video_id)
        if transcript is None:
            transcript = self.transcripts.get_transcript(video_id, self.settings.languages)
            self.repo.save_transcript(transcript)
        return meta, transcript

    def blocks(self, video_id: str) -> list[Block]:
        t = self.repo.get_transcript(video_id)
        return build_blocks(t.cues) if t else []

    # ---------------------------------------------------------------- analysis

    def submit(
        self,
        url: str,
        prefs: LearningPreferences,
        *,
        force: bool = False,
    ) -> tuple[str, bool]:
        """Register an analysis. Returns (analysis_id, needs_run).

        Fetches metadata and transcript up front so invalid videos fail fast.
        Reuses a finished or in-progress analysis with identical inputs unless `force`.
        """
        video_id = extract_video_id(url)
        meta, _ = self.ingest(video_id)
        key = self.llm_key()
        if not force:
            existing = self.repo.find_cached(video_id, prefs, key) or self.repo.find_in_progress(video_id, prefs, key)
            if existing:
                log.info("Reusing analysis %s for %s", existing, video_id)
                return existing, False
        analysis_id = uuid.uuid4().hex[:12]
        self.repo.create_analysis(analysis_id, meta.video_id, prefs, key)
        return analysis_id, True

    def run(self, analysis_id: str, progress: Progress = lambda _: None) -> VideoAnalysis:
        pending = self.repo.get_analysis(analysis_id)
        if pending is None:
            raise KeyError(analysis_id)

        def report(msg: str) -> None:
            log.info("[%s] %s", analysis_id, msg)
            self.repo.update_status(analysis_id, AnalysisStatus.RUNNING, msg)
            progress(msg)

        try:
            report("Loading transcript")
            meta, transcript = self.ingest(pending.video.video_id)
            pipeline = AnalysisPipeline(self.settings, self.llm)
            result = pipeline.run(meta, transcript, pending.preferences, analysis_id=analysis_id, progress=report)
            result.created_at = pending.created_at
            self.repo.save_result(result)
            return result
        except CurioError as exc:
            self.repo.update_status(analysis_id, AnalysisStatus.FAILED, error=exc.message)
            raise
        except Exception as exc:
            log.exception("Analysis %s failed", analysis_id)
            self.repo.update_status(analysis_id, AnalysisStatus.FAILED, error=f"Unexpected error: {exc}")
            raise

    def analyze(
        self,
        url: str,
        prefs: LearningPreferences | None = None,
        *,
        force: bool = False,
        progress: Progress = lambda _: None,
    ) -> VideoAnalysis:
        """Synchronous convenience: submit and run (or return the cached result)."""
        prefs = prefs or LearningPreferences()
        analysis_id, needs_run = self.submit(url, prefs, force=force)
        if not needs_run:
            cached = self.repo.get_analysis(analysis_id)
            if cached is not None and cached.status == AnalysisStatus.DONE:
                return cached
            # Another process is still working on it; a synchronous caller can't wait on
            # that process, so run independently.
            analysis_id, _ = self.submit(url, prefs, force=True)
        return self.run(analysis_id, progress)

    # ------------------------------------------------------------------ queries

    def get(self, analysis_id: str) -> VideoAnalysis | None:
        return self.repo.get_analysis(analysis_id)

    def recent(self, limit: int = 10) -> list[RecentVideo]:
        return self.repo.recent(limit)

    def video(self, video_id: str) -> VideoMetadata | None:
        return self.repo.get_video(video_id)

    def set_completed(self, analysis_id: str, segment_id: str, completed: bool) -> None:
        self.repo.set_segment_completed(analysis_id, segment_id, completed)

    def completed(self, analysis_id: str) -> set[str]:
        return self.repo.completed_segments(analysis_id)
