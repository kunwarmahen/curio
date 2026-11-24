"""CurioService: the single entry point frontends use (the FastAPI app behind the web UI, and the CLI)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable

from curio_learn.analysis.ask import answer_question
from curio_learn.analysis.notes import generate_notes, scope_key, scope_sections, to_markdown
from curio_learn.analysis.pipeline import AnalysisPipeline
from curio_learn.config import Settings, get_settings
from curio_learn.domain import (
    AnalysisStatus,
    Answer,
    Block,
    LearningNotes,
    LearningPreferences,
    NotesScope,
    VideoAnalysis,
    VideoMetadata,
)
from curio_learn.errors import AnalysisError, CurioError
from curio_learn.ingestion.metadata import MetadataProvider, YtDlpMetadataProvider
from curio_learn.ingestion.transcripts import TranscriptProvider, YouTubeCaptionsProvider
from curio_learn.ingestion.url import extract_video_id
from curio_learn.llm.base import LLMProvider
from curio_learn.llm.factory import build_provider
from curio_learn.processing.normalize import approx_tokens, boundary_units, build_blocks
from curio_learn.processing.retrieval import Embedder, OllamaEmbedder, blocks_key, rank
from curio_learn.storage.repository import RecentVideo, Repository

log = logging.getLogger(__name__)

# Bump when pipeline changes should invalidate cached analyses.
PIPELINE_VERSION = 5
# Bump when notes generation changes should invalidate saved notes.
NOTES_VERSION = 1

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
        embedder: Embedder | None = None,
    ):
        self.settings = settings or get_settings()
        self.repo = repository or Repository(self.settings.database_url)
        self._llm = llm
        self.metadata = metadata or YtDlpMetadataProvider()
        self._transcripts = transcripts
        self._embedder = embedder

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

    # ------------------------------------------------------------------- notes

    def notes_llm_key(self) -> str:
        return f"{self.llm.name}:{self.settings.model_for('notes')}:n{NOTES_VERSION}"

    def submit_notes(
        self,
        analysis_id: str,
        scope: NotesScope = NotesScope.PLAYLIST,
        segment_ids: list[str] | None = None,
        *,
        force: bool = False,
    ) -> tuple[str, bool]:
        """Register notes for an analysis. Returns (notes_id, needs_run).

        Checks the scope up front so an empty or unknown selection fails fast. Reuses
        finished or in-progress notes with identical inputs unless `force`.
        """
        analysis = self.repo.get_analysis(analysis_id)
        if analysis is None:
            raise KeyError(analysis_id)
        if analysis.status != AnalysisStatus.DONE:
            raise AnalysisError("Notes can be written once the analysis has finished.")
        ids = sorted(set(segment_ids or [])) if scope is NotesScope.SELECTED else []
        scope_sections(analysis, scope, ids)  # validates the selection
        key, llm_key = scope_key(scope, ids), self.notes_llm_key()
        if not force:
            existing = self.repo.find_notes(analysis_id, key, llm_key)
            if existing:
                return existing, False
        notes_id = uuid.uuid4().hex[:12]
        self.repo.create_notes(notes_id, analysis_id, scope, ids, key, llm_key)
        return notes_id, True

    def run_notes(self, notes_id: str, progress: Progress = lambda _: None) -> LearningNotes:
        pending = self.repo.get_notes(notes_id)
        if pending is None:
            raise KeyError(notes_id)

        def report(msg: str) -> None:
            log.info("[notes %s] %s", notes_id, msg)
            self.repo.update_notes_status(notes_id, AnalysisStatus.RUNNING, msg)
            progress(msg)

        try:
            report("Loading transcript")
            analysis = self.repo.get_analysis(pending.analysis_id)
            transcript = self.repo.get_transcript(analysis.video.video_id) if analysis else None
            if analysis is None or transcript is None:
                raise AnalysisError("The analysis or its transcript is no longer stored.")
            anthropic = self.llm.name == "anthropic"
            notes = generate_notes(
                self.llm,
                model=self.settings.model_for("notes"),
                analysis=analysis,
                units=boundary_units(transcript.cues),
                scope=pending.scope,
                segment_ids=pending.segment_ids,
                notes_id=notes_id,
                batch_tokens=60000 if anthropic else self.settings.evaluate_batch_tokens,
                retries=self.settings.llm_max_retries,
                progress=report,
            )
            notes.created_at = pending.created_at
            self.repo.save_notes(notes)
            return notes
        except CurioError as exc:
            self.repo.update_notes_status(notes_id, AnalysisStatus.FAILED, error=exc.message)
            raise
        except Exception as exc:
            log.exception("Notes %s failed", notes_id)
            self.repo.update_notes_status(notes_id, AnalysisStatus.FAILED, error=f"Unexpected error: {exc}")
            raise

    def notes(
        self,
        analysis_id: str,
        scope: NotesScope = NotesScope.PLAYLIST,
        segment_ids: list[str] | None = None,
        *,
        force: bool = False,
        progress: Progress = lambda _: None,
    ) -> LearningNotes:
        """Synchronous convenience: submit and run (or return saved notes)."""
        notes_id, needs_run = self.submit_notes(analysis_id, scope, segment_ids, force=force)
        if not needs_run:
            saved = self.repo.get_notes(notes_id)
            if saved is not None and saved.status == AnalysisStatus.DONE:
                return saved
            notes_id, _ = self.submit_notes(analysis_id, scope, segment_ids, force=True)
        return self.run_notes(notes_id, progress)

    def get_notes(self, notes_id: str) -> LearningNotes | None:
        return self.repo.get_notes(notes_id)

    def list_notes(self, analysis_id: str) -> list[LearningNotes]:
        return self.repo.list_notes(analysis_id)

    def notes_markdown(self, notes_id: str) -> str | None:
        notes = self.repo.get_notes(notes_id)
        if notes is None or notes.status != AnalysisStatus.DONE:
            return None
        analysis = self.repo.get_analysis(notes.analysis_id)
        return to_markdown(notes, analysis) if analysis else None

    # --------------------------------------------------------- ask the video

    @property
    def embedder(self) -> Embedder | None:
        if self._embedder is None and self.settings.embed_model:
            self._embedder = OllamaEmbedder(self.settings.ollama_host, self.settings.embed_model)
        return self._embedder

    def _ask_budget(self, analysis: VideoAnalysis, history: list[Answer]) -> int:
        s = self.settings
        if s.ask_context_tokens:
            return s.ask_context_tokens
        if self.llm.name == "anthropic":
            return 150000
        # Room for instructions (~700), the outline, the conversation, the question and the answer.
        outline = sum(approx_tokens(f"{x.section.title} {x.section.summary}") + 12 for x in analysis.sections)
        convo = sum(approx_tokens(a.question + " ".join(p.text for p in a.parts)) + 20 for a in history[-4:])
        return max(2000, s.ollama_num_ctx - 2000 - 700 - outline - convo - 600)

    def _ranker(self, video_id: str, blocks: list[Block]) -> tuple[Callable[[str], list[int]], list[str]]:
        """A search function over blocks, and what it uses ("keyword", "semantic")."""
        emb = self.embedder
        vectors = None
        if emb is not None:
            key = blocks_key(blocks)
            try:
                vectors = self.repo.get_embeddings(video_id, emb.name, key)
                if vectors is None:
                    vectors = emb.embed([b.text for b in blocks], kind="document")
                    self.repo.save_embeddings(video_id, emb.name, key, vectors)
            except Exception as exc:  # embeddings are an optional improvement
                log.warning("Embedding search unavailable (%s); using keyword search", exc)
                vectors = None
        kinds = ["keyword"] + (["semantic"] if vectors else [])

        def ranked(query: str) -> list[int]:
            qv = None
            if vectors:
                try:
                    qv = emb.embed([query], kind="query")[0]
                except Exception as exc:
                    log.warning("Query embedding failed (%s); using keyword search", exc)
            return rank(query, blocks, query_vector=qv, block_vectors=vectors)

        return ranked, kinds

    def ask(self, analysis_id: str, question: str) -> Answer:
        """Answer a question about an analyzed video and save it to the conversation."""
        if not question.strip():
            raise AnalysisError("Ask a question about the video.")
        analysis = self.repo.get_analysis(analysis_id)
        if analysis is None:
            raise KeyError(analysis_id)
        if analysis.status != AnalysisStatus.DONE:
            raise AnalysisError("Questions can be asked once the analysis has finished.")
        transcript = self.repo.get_transcript(analysis.video.video_id)
        if transcript is None:
            raise AnalysisError("The transcript of this video is no longer stored.")
        blocks = build_blocks(transcript.cues)
        history = self.repo.list_answers(analysis_id)
        budget = self._ask_budget(analysis, history)
        searched = sum(approx_tokens(b.text) + 8 for b in blocks) > budget
        ranked, kinds = self._ranker(analysis.video.video_id, blocks) if searched else (lambda _: [], [])
        answer = answer_question(
            self.llm,
            model=self.settings.model_for("ask"),
            analysis=analysis,
            blocks=blocks,
            units=boundary_units(transcript.cues),
            question=question,
            history=history,
            budget_tokens=budget,
            ranked=ranked,
            search="+".join(kinds),
            retries=self.settings.llm_max_retries,
        )
        self.repo.save_answer(answer)
        return answer

    def conversation(self, analysis_id: str) -> list[Answer]:
        return self.repo.list_answers(analysis_id)

    def clear_conversation(self, analysis_id: str) -> int:
        return self.repo.clear_answers(analysis_id)
