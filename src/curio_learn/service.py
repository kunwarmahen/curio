"""CurioService: the single entry point frontends use (the FastAPI app behind the web UI, and the CLI)."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from curio_learn.analysis.ask import answer_question
from curio_learn.analysis.course import Lecture, build_course_plan
from curio_learn.analysis.course_ask import LectureText, answer_course_question, course_passages
from curio_learn.analysis.notes import generate_notes, scope_key, scope_sections, to_markdown
from curio_learn.analysis.pipeline import AnalysisPipeline
from curio_learn.analysis.playlist import PlaylistConfig
from curio_learn.analysis.study import generate_study, is_due, schedule
from curio_learn.config import Settings, get_settings
from curio_learn.domain import (
    AnalysisStatus,
    Answer,
    Block,
    CardGrade,
    CardState,
    Course,
    CourseAnswer,
    CourseLecture,
    CoursePlan,
    DueCard,
    FlashcardDeck,
    LearningNotes,
    LearningPreferences,
    LectureState,
    NotesScope,
    QuizAttempt,
    StudySet,
    VideoAnalysis,
    VideoMetadata,
)
from curio_learn.errors import AnalysisError, CurioError, InvalidURL
from curio_learn.ingestion.metadata import MetadataProvider, YtDlpMetadataProvider
from curio_learn.ingestion.playlist import (
    PlaylistProvider,
    YtDlpPlaylistProvider,
    extract_playlist_id,
    lecture_number,
)
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
# Bump when quiz and flashcard generation changes should invalidate saved sets.
STUDY_VERSION = 2
# Bump when course playlist building changes should invalidate saved plans.
COURSE_VERSION = 1
# Course playlist lengths offered, in minutes; the default is about 15 minutes a lecture.
COURSE_MINUTES = (60, 90, 120, 180, 240, 360)

Progress = Callable[[str], None]

_GUEST_RE = re.compile(r"\bguest\b", re.I)


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
        playlists: PlaylistProvider | None = None,
    ):
        self.settings = settings or get_settings()
        self.repo = repository or Repository(self.settings.database_url)
        self._llm = llm
        self.metadata = metadata or YtDlpMetadataProvider()
        self._transcripts = transcripts
        self._embedder = embedder
        self.playlists = playlists or YtDlpPlaylistProvider()

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
        queue: bool = False,
    ) -> tuple[str, bool]:
        """Register an analysis. Returns (analysis_id, needs_run).

        Fetches metadata and transcript up front so invalid videos fail fast.
        Reuses a finished, queued or in-progress analysis with identical inputs unless
        `force`. With `queue` the analysis waits for the overnight queue (needs_run is
        False); without it, a matching queued analysis is taken out of the queue to
        run now.
        """
        video_id = extract_video_id(url)
        meta, _ = self.ingest(video_id)
        key = self.llm_key()
        if not force:
            existing = self.repo.find_cached(video_id, prefs, key) or self.repo.find_in_progress(video_id, prefs, key)
            if existing:
                log.info("Reusing analysis %s for %s", existing, video_id)
                current = self.repo.get_analysis(existing)
                if not queue and current is not None and current.status == AnalysisStatus.QUEUED:
                    return self.start_queued(existing), True
                return existing, False
        analysis_id = uuid.uuid4().hex[:12]
        status = AnalysisStatus.QUEUED if queue else AnalysisStatus.PENDING
        self.repo.create_analysis(analysis_id, meta.video_id, prefs, key, status)
        return analysis_id, not queue

    def start_queued(self, analysis_id: str) -> str:
        """Take a queued analysis out of the queue so it can run now."""
        if self.repo.get_analysis(analysis_id) is None:
            raise KeyError(analysis_id)
        if not self.repo.claim_queued(analysis_id):
            raise AnalysisError("This video isn't waiting in the queue any more.")
        return analysis_id

    def remove_queued(self, analysis_id: str) -> bool:
        return self.repo.remove_queued(analysis_id)

    # ------------------------------------------------------------- default goal

    def default_preferences(self) -> LearningPreferences:
        """The goal the learner last chose, used for new videos."""
        saved = self.repo.get_setting("default_preferences")
        return LearningPreferences.model_validate(saved) if saved else LearningPreferences()

    def set_default_preferences(self, prefs: LearningPreferences) -> None:
        self.repo.set_setting("default_preferences", prefs.model_dump(mode="json"))

    def retarget_queued(
        self, prefs: LearningPreferences, analysis_ids: list[str] | None = None
    ) -> dict[str, list[str]]:
        """Give queued videos a new goal. Returns the ids updated, and the ids removed
        because that video was already analyzed (or queued) with this goal."""
        ids = analysis_ids or self.repo.queued_ids()
        key = self.llm_key()
        updated, merged = [], []
        for analysis_id in ids:
            video_id = self.repo.video_of(analysis_id)
            if video_id is None:
                continue
            existing = self.repo.find_cached(video_id, prefs, key) or self.repo.find_in_progress(video_id, prefs, key)
            if existing and existing != analysis_id:
                if self.repo.remove_queued(analysis_id):
                    merged.append(analysis_id)
            elif self.repo.set_queued_preferences(analysis_id, prefs):
                updated.append(analysis_id)
        return {"updated": updated, "already_analyzed": merged}

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

    def prepare(self, analysis_id: str, *, gpu_busy: Callable[[], str] = lambda: "") -> list[str]:
        """Write playlist notes and a quiz with flashcards for a finished analysis, as the
        overnight queue does after each video. Skips what already exists. Before each one
        asks `gpu_busy` and stops if someone else started using the GPU. Returns what was
        written ("notes", "quiz")."""
        steps = (("notes", self.submit_notes, self.run_notes), ("quiz", self.submit_study, self.run_study))
        written: list[str] = []
        for name, submit, run in steps:
            reason = gpu_busy()
            if reason:
                log.info("[%s] Not writing the %s overnight: %s", analysis_id, name, reason)
                break
            try:
                item_id, needs_run = submit(analysis_id)
                if needs_run:
                    run(item_id)
                    written.append(name)
            except Exception:  # the failure is recorded on the notes or quiz row
                log.warning("[%s] Writing the %s failed", analysis_id, name, exc_info=True)
        return written

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

    # ---------------------------------------------------- quiz and flashcards

    def study_llm_key(self) -> str:
        return f"{self.llm.name}:{self.settings.model_for('study')}:s{STUDY_VERSION}"

    def submit_study(
        self,
        analysis_id: str,
        scope: NotesScope = NotesScope.PLAYLIST,
        segment_ids: list[str] | None = None,
        *,
        force: bool = False,
    ) -> tuple[str, bool]:
        """Register a quiz and flashcards for an analysis. Returns (study_id, needs_run)."""
        analysis = self.repo.get_analysis(analysis_id)
        if analysis is None:
            raise KeyError(analysis_id)
        if analysis.status != AnalysisStatus.DONE:
            raise AnalysisError("A quiz can be written once the analysis has finished.")
        ids = sorted(set(segment_ids or [])) if scope is NotesScope.SELECTED else []
        scope_sections(analysis, scope, ids)  # validates the selection
        key, llm_key = scope_key(scope, ids), self.study_llm_key()
        if not force:
            existing = self.repo.find_study(analysis_id, key, llm_key)
            if existing:
                return existing, False
        study_id = uuid.uuid4().hex[:12]
        self.repo.create_study(study_id, analysis_id, scope, ids, key, llm_key)
        return study_id, True

    def run_study(self, study_id: str, progress: Progress = lambda _: None) -> StudySet:
        pending = self.repo.get_study(study_id)
        if pending is None:
            raise KeyError(study_id)

        def report(msg: str) -> None:
            log.info("[study %s] %s", study_id, msg)
            self.repo.update_study_status(study_id, AnalysisStatus.RUNNING, msg)
            progress(msg)

        try:
            report("Loading transcript")
            analysis = self.repo.get_analysis(pending.analysis_id)
            transcript = self.repo.get_transcript(analysis.video.video_id) if analysis else None
            if analysis is None or transcript is None:
                raise AnalysisError("The analysis or its transcript is no longer stored.")
            study = generate_study(
                self.llm,
                model=self.settings.model_for("study"),
                analysis=analysis,
                units=boundary_units(transcript.cues),
                scope=pending.scope,
                segment_ids=pending.segment_ids,
                study_id=study_id,
                batch_tokens=60000 if self.llm.name == "anthropic" else self.settings.evaluate_batch_tokens,
                retries=self.settings.llm_max_retries,
                progress=report,
            )
            study.created_at = pending.created_at
            self.repo.save_study(study)
            return study
        except CurioError as exc:
            self.repo.update_study_status(study_id, AnalysisStatus.FAILED, error=exc.message)
            raise
        except Exception as exc:
            log.exception("Study set %s failed", study_id)
            self.repo.update_study_status(study_id, AnalysisStatus.FAILED, error=f"Unexpected error: {exc}")
            raise

    def study(
        self,
        analysis_id: str,
        scope: NotesScope = NotesScope.PLAYLIST,
        segment_ids: list[str] | None = None,
        *,
        force: bool = False,
        progress: Progress = lambda _: None,
    ) -> StudySet:
        """Synchronous convenience: submit and run (or return the saved set)."""
        study_id, needs_run = self.submit_study(analysis_id, scope, segment_ids, force=force)
        if not needs_run:
            saved = self.repo.get_study(study_id)
            if saved is not None and saved.status == AnalysisStatus.DONE:
                return saved
            study_id, _ = self.submit_study(analysis_id, scope, segment_ids, force=True)
        return self.run_study(study_id, progress)

    def get_study(self, study_id: str) -> StudySet | None:
        return self.repo.get_study(study_id)

    def list_study(self, analysis_id: str) -> list[StudySet]:
        return self.repo.list_study(analysis_id)

    def grade_quiz(self, study_id: str, answers: dict[str, int]) -> QuizAttempt:
        """Score a quiz attempt and save it. Unanswered questions count as wrong."""
        study = self.repo.get_study(study_id)
        if study is None:
            raise KeyError(study_id)
        if study.status != AnalysisStatus.DONE or not study.questions:
            raise AnalysisError("This quiz has no questions yet.")
        known = {q.id for q in study.questions}
        answers = {k: v for k, v in answers.items() if k in known}
        correct = [q.id for q in study.questions if answers.get(q.id) == q.answer]
        attempt = QuizAttempt(
            id=uuid.uuid4().hex[:12],
            study_id=study_id,
            answers=answers,
            correct=correct,
            score=len(correct),
            total=len(study.questions),
        )
        self.repo.save_attempt(attempt)
        return attempt

    def quiz_attempts(self, study_id: str) -> list[QuizAttempt]:
        return self.repo.list_attempts(study_id)

    def flashcards(
        self,
        analysis_id: str | None = None,
        *,
        study_id: str | None = None,
        limit: int = 50,
        now: datetime | None = None,
    ) -> FlashcardDeck:
        """Cards due now: overdue reviews first (most overdue first), then new cards in
        video order. From every video's newest set, one video's, or one given set."""
        now = now or datetime.now(UTC)
        if study_id:
            study = self.repo.get_study(study_id)
            analysis = self.repo.get_analysis(study.analysis_id) if study else None
            decks = [(study, analysis.video)] if study and analysis and study.status == AnalysisStatus.DONE else []
        else:
            decks = [(s, v) for s, v in self.repo.decks() if analysis_id is None or s.analysis_id == analysis_id]
        states = self.repo.card_states([s.id for s, _ in decks])
        due: list[DueCard] = []
        upcoming: list[datetime] = []
        total = 0
        for study, video in decks:
            for card in study.cards:
                total += 1
                state = states.get((study.id, card.id)) or CardState(study_id=study.id, card_id=card.id)
                if not is_due(state, now):
                    upcoming.append(state.due_at)
                    continue
                due.append(
                    DueCard(
                        card=card,
                        state=state,
                        study_id=study.id,
                        analysis_id=study.analysis_id,
                        video_title=video.title,
                        video_url=video.url,
                    )
                )
        due.sort(key=lambda d: (d.state.due_at is None, d.state.due_at or now))
        return FlashcardDeck(cards=due[:limit], due=len(due), total=total, next_due_at=min(upcoming, default=None))

    def review_card(self, study_id: str, card_id: str, grade: CardGrade, *, now: datetime | None = None) -> CardState:
        study = self.repo.get_study(study_id)
        if study is None or all(c.id != card_id for c in study.cards):
            raise KeyError(f"{study_id}/{card_id}")
        state = self.repo.card_states([study_id]).get((study_id, card_id)) or CardState(
            study_id=study_id, card_id=card_id
        )
        new = schedule(state, grade, now or datetime.now(UTC))
        self.repo.save_card_state(new)
        return new

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

    # ----------------------------------------------------------------- courses

    def import_course(self, url: str, prefs: LearningPreferences) -> str:
        """Create (or refresh) a course from a YouTube playlist. Lists the lectures only;
        `queue_course` then checks each one and queues those not analyzed for the goal."""
        playlist_id = extract_playlist_id(url)
        if playlist_id is None:
            raise InvalidURL("Paste a link to a YouTube playlist (it has list= in it).")
        info = self.playlists.get_playlist(playlist_id)
        lectures = [e.model_dump() for e in info.entries]
        course_id = self.repo.find_course(playlist_id, prefs)
        if course_id:
            self.repo.update_course(
                course_id,
                title=info.title,
                channel=info.channel,
                lectures=lectures,
                unavailable=info.unavailable,
                importing=True,
            )
            return course_id
        course_id = uuid.uuid4().hex[:12]
        self.repo.create_course(
            course_id,
            title=info.title,
            channel=info.channel,
            playlist_id=playlist_id,
            prefs=prefs,
            lectures=lectures,
            unavailable=info.unavailable,
            importing=True,
        )
        return course_id

    def queue_course(self, course_id: str) -> None:
        """Fetch each lecture's transcript and queue the lectures that aren't analyzed (or
        waiting) for the course goal, in course order. Slow: one YouTube lookup per new
        lecture. Records why a lecture couldn't be queued (no captions, for example)."""
        course = self.repo.get_course(course_id)
        if course is None:
            raise KeyError(course_id)
        try:
            for lec in course["lectures"]:
                vid = lec["video_id"]
                try:
                    self.submit(f"https://www.youtube.com/watch?v={vid}", course["preferences"], queue=True)
                    self.repo.set_course_error(course_id, vid, "")
                except CurioError as exc:
                    log.info("[course %s] Lecture %s not queued: %s", course_id, vid, exc.message)
                    self.repo.set_course_error(course_id, vid, exc.message)
        finally:
            self.repo.update_course(course_id, importing=False)

    def group_course(self, title: str, analysis_ids: list[str]) -> str:
        """A course from videos already analyzed, in upload order. Its goal is the one most
        of them were analyzed with."""
        analyses = []
        for aid in dict.fromkeys(analysis_ids):
            a = self.repo.get_analysis(aid)
            if a is None or a.status != AnalysisStatus.DONE:
                raise AnalysisError("Pick videos that have finished analyzing.")
            if all(x.video.video_id != a.video.video_id for x in analyses):
                analyses.append(a)
        if len(analyses) < 2:
            raise AnalysisError("A course needs at least two videos.")
        analyses.sort(key=lambda a: (a.video.upload_date or "9", a.created_at))
        keys = [a.preferences.cache_key() for a in analyses]
        prefs = next(a.preferences for a in analyses if a.preferences.cache_key() == max(keys, key=keys.count))
        course_id = uuid.uuid4().hex[:12]
        self.repo.create_course(
            course_id,
            title=title.strip() or analyses[0].video.channel or "My course",
            channel=analyses[0].video.channel,
            playlist_id="",
            prefs=prefs,
            lectures=[
                {"video_id": a.video.video_id, "title": a.video.title, "duration_s": a.video.duration_s}
                for a in analyses
            ],
        )
        return course_id

    def _lectures(self, course: dict) -> list[CourseLecture]:
        key = course["preferences"].cache_key()
        rows = self.repo.analyses_for_videos([x["video_id"] for x in course["lectures"]])
        waiting = (AnalysisStatus.QUEUED, AnalysisStatus.PENDING, AnalysisStatus.RUNNING)
        numbers = [lecture_number(x["title"]) for x in course["lectures"]]
        named = [n for n in numbers if n is not None]
        use_numbers = len(named) == len(set(named))
        guests = [i for i, x in enumerate(course["lectures"]) if numbers[i] is None and _GUEST_RE.search(x["title"])]

        def label(i: int) -> str:
            if use_numbers and numbers[i] is not None:
                return f"Lecture {numbers[i]}"
            if i in guests:
                return "Guest lecture" if len(guests) == 1 else f"Guest lecture {guests.index(i) + 1}"
            return f"#{i + 1}"

        out = []
        for pos, lec in enumerate(course["lectures"], 1):
            mine = [r for r in rows if r["video_id"] == lec["video_id"]]
            done_goal = next(
                (r for r in mine if r["status"] == AnalysisStatus.DONE and r["preferences_key"] == key), None
            )
            done_any = next((r for r in mine if r["status"] == AnalysisStatus.DONE), None)
            live = next((r for r in mine if r["status"] in waiting and r["preferences_key"] == key), None)
            failed = next(
                (r for r in mine if r["status"] == AnalysisStatus.FAILED and r["preferences_key"] == key), None
            )
            item = CourseLecture(
                position=pos,
                label=label(pos - 1),
                video_id=lec["video_id"],
                title=lec["title"],
                duration_s=lec.get("duration_s") or 0,
            )
            if done_goal:
                item.state, item.analysis_id = LectureState.READY, done_goal["id"]
            elif done_any:
                item.state, item.analysis_id = LectureState.OTHER_GOAL, done_any["id"]
                item.pending_id = live["id"] if live else None
            elif live:
                queued = live["status"] == AnalysisStatus.QUEUED
                item.state = LectureState.QUEUED if queued else LectureState.ANALYZING
                item.pending_id = live["id"]
            elif failed:
                item.state, item.pending_id, item.error = LectureState.FAILED, failed["id"], failed["error"]
            else:
                item.error = course["errors"].get(lec["video_id"], "")
            out.append(item)
        return out

    def course(self, course_id: str) -> Course | None:
        data = self.repo.get_course(course_id)
        if data is None:
            return None
        lectures = self._lectures(data)
        plan = self.repo.latest_course_plan(course_id)
        usable = [lec.analysis_id for lec in lectures if lec.analysis_id]
        stale = plan is not None and plan.status == AnalysisStatus.DONE and plan.analysis_ids != usable
        return Course(
            id=data["id"],
            title=data["title"],
            channel=data["channel"],
            playlist_id=data["playlist_id"],
            preferences=data["preferences"],
            lectures=lectures,
            unavailable=data["unavailable"],
            importing=data["importing"],
            plan=plan,
            plan_stale=stale,
            created_at=data["created_at"],
        )

    def courses(self) -> list[Course]:
        return [c for c in (self.course(d["id"]) for d in self.repo.list_courses()) if c is not None]

    def delete_course(self, course_id: str) -> bool:
        return self.repo.delete_course(course_id)

    def course_llm_key(self) -> str:
        return f"{self.llm.name}:{self.settings.model_for('course')}:c{COURSE_VERSION}"

    @staticmethod
    def default_course_minutes(lectures: int) -> int:
        return min(COURSE_MINUTES, key=lambda m: abs(m - 15 * lectures))

    def submit_course_plan(
        self, course_id: str, minutes: int | None = None, *, force: bool = False
    ) -> tuple[str, bool]:
        """Register a course playlist built from the lectures analyzed so far. Returns
        (plan_id, needs_run); reuses one with identical inputs unless `force`."""
        course = self.course(course_id)
        if course is None:
            raise KeyError(course_id)
        ids = [lec.analysis_id for lec in course.lectures if lec.analysis_id]
        if len(ids) < 2:
            raise AnalysisError("A course playlist needs at least two analyzed lectures.")
        minutes = minutes or self.default_course_minutes(len(ids))
        if not 10 <= minutes <= 1200:
            raise AnalysisError("Choose between 10 minutes and 20 hours.")
        key = f"{','.join(ids)}|{minutes}|{self.course_llm_key()}"
        if not force:
            existing = self.repo.find_course_plan(course_id, key)
            if existing:
                return existing, False
        plan = CoursePlan(id=uuid.uuid4().hex[:12], course_id=course_id, budget_s=minutes * 60.0, analysis_ids=ids)
        self.repo.create_course_plan(plan, key)
        return plan.id, True

    def run_course_plan(self, plan_id: str, progress: Progress = lambda _: None) -> CoursePlan:
        pending = self.repo.get_course_plan(plan_id)
        if pending is None:
            raise KeyError(plan_id)

        def report(msg: str) -> None:
            log.info("[course plan %s] %s", plan_id, msg)
            self.repo.update_course_plan_status(plan_id, AnalysisStatus.RUNNING, msg)
            progress(msg)

        try:
            report("Loading the lectures")
            course = self.repo.get_course(pending.course_id)
            if course is None:
                raise AnalysisError("The course is no longer stored.")
            order = {x["video_id"]: n for n, x in enumerate(course["lectures"], 1)}
            labels = {lec.position: lec.label for lec in self._lectures(course)}
            lectures = []
            for aid in pending.analysis_ids:
                analysis = self.repo.get_analysis(aid)
                transcript = self.repo.get_transcript(analysis.video.video_id) if analysis else None
                if analysis is None or transcript is None:
                    raise AnalysisError("A lecture's analysis or transcript is no longer stored.")
                pos = order.get(analysis.video.video_id, len(order) + len(lectures) + 1)
                lectures.append(
                    Lecture(
                        position=pos,
                        analysis=analysis,
                        units=boundary_units(transcript.cues),
                        label=labels.get(pos, ""),
                    )
                )
            anthropic = self.llm.name == "anthropic"
            plan = build_course_plan(
                self.llm,
                model=self.settings.model_for("course"),
                plan_id=plan_id,
                course_id=pending.course_id,
                lectures=lectures,
                prefs=course["preferences"],
                budget_s=pending.budget_s,
                config=PlaylistConfig(merge_gap_s=self.settings.merge_gap_s),
                context_tokens=150000 if anthropic else self.settings.ollama_num_ctx,
                embedder=self.embedder,
                retries=self.settings.llm_max_retries,
                progress=report,
            )
            plan.created_at = pending.created_at
            self.repo.save_course_plan(plan)
            return plan
        except CurioError as exc:
            self.repo.update_course_plan_status(plan_id, AnalysisStatus.FAILED, error=exc.message)
            raise
        except Exception as exc:
            log.exception("Course plan %s failed", plan_id)
            self.repo.update_course_plan_status(plan_id, AnalysisStatus.FAILED, error=f"Unexpected error: {exc}")
            raise

    def course_plan(
        self, course_id: str, minutes: int | None = None, *, force: bool = False, progress: Progress = lambda _: None
    ) -> CoursePlan:
        """Synchronous convenience: submit and run (or return the saved plan)."""
        plan_id, needs_run = self.submit_course_plan(course_id, minutes, force=force)
        if not needs_run:
            saved = self.repo.get_course_plan(plan_id)
            if saved is not None and saved.status == AnalysisStatus.DONE:
                return saved
            plan_id, _ = self.submit_course_plan(course_id, minutes, force=True)
        return self.run_course_plan(plan_id, progress)

    def rebuild_courses_after(self, analysis_id: str, *, gpu_busy: Callable[[], str] = lambda: "") -> list[str]:
        """After the queue analyzed a lecture: rebuild the course playlist of every course
        it's in once none of that course's lectures is still waiting, with the length
        chosen last (the default when none was built). Returns the plans built."""
        analysis = self.repo.get_analysis(analysis_id)
        if analysis is None or analysis.status != AnalysisStatus.DONE:
            return []
        built = []
        for data in self.repo.list_courses():
            if all(x["video_id"] != analysis.video.video_id for x in data["lectures"]):
                continue
            course = self.course(data["id"])
            if course is None or course.importing:
                continue
            if any(
                lec.state in (LectureState.QUEUED, LectureState.ANALYZING) or lec.pending_id for lec in course.lectures
            ):
                continue  # the queue's last lecture of this course rebuilds it
            last = course.plan if course.plan and course.plan.status == AnalysisStatus.DONE else None
            minutes = round(last.budget_s / 60) if last else None
            try:
                plan_id, needs_run = self.submit_course_plan(course.id, minutes)
            except AnalysisError:
                continue  # fewer than two analyzed lectures
            if not needs_run:
                continue
            reason = gpu_busy()
            if reason:
                log.info("[course %s] Not rebuilding the course playlist overnight: %s", course.id, reason)
                self.repo.update_course_plan_status(plan_id, AnalysisStatus.FAILED, error=f"Skipped: {reason}")
                continue
            try:
                self.run_course_plan(plan_id)
                built.append(plan_id)
            except Exception:  # recorded on the plan
                log.warning("[course %s] Rebuilding the course playlist failed", course.id, exc_info=True)
        return built

    # ------------------------------------------------------ ask the course

    def _course_texts(self, course: Course) -> list[LectureText]:
        out = []
        for lec in course.lectures:
            analysis = self.repo.get_analysis(lec.analysis_id) if lec.analysis_id else None
            transcript = self.repo.get_transcript(lec.video_id) if analysis else None
            if analysis is None or transcript is None:
                continue
            out.append(
                LectureText(
                    position=lec.position,
                    label=lec.label,
                    analysis=analysis,
                    blocks=build_blocks(transcript.cues),
                    units=boundary_units(transcript.cues),
                )
            )
        return out

    def _course_ranker(self, lectures: list[LectureText]) -> tuple[Callable[[str], list[int]], list[str]]:
        """Search over every lecture's blocks at once: keyword, plus embeddings when every
        lecture has (or gets) its vectors."""
        blocks = [p.block for p in course_passages(lectures)]
        emb = self.embedder
        vectors: list[list[float]] | None = None
        if emb is not None:
            try:
                vectors = []
                for lec in lectures:
                    key = blocks_key(lec.blocks)
                    vid = lec.analysis.video.video_id
                    vecs = self.repo.get_embeddings(vid, emb.name, key)
                    if vecs is None:
                        vecs = emb.embed([b.text for b in lec.blocks], kind="document")
                        self.repo.save_embeddings(vid, emb.name, key, vecs)
                    vectors.extend(vecs)
            except Exception as exc:  # embeddings are an optional improvement
                log.warning("Embedding search unavailable for the course (%s); using keyword search", exc)
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

    def ask_course(self, course_id: str, question: str) -> CourseAnswer:
        """Answer a question from every analyzed lecture of a course and save it."""
        if not question.strip():
            raise AnalysisError("Ask a question about the course.")
        course = self.course(course_id)
        if course is None:
            raise KeyError(course_id)
        lectures = self._course_texts(course)
        if not lectures:
            raise AnalysisError("Questions can be asked once a lecture has been analyzed.")
        history = self.repo.list_course_answers(course_id)
        s = self.settings
        if s.ask_context_tokens:
            budget = s.ask_context_tokens
        elif self.llm.name == "anthropic":
            budget = 150000
        else:
            outline = sum(len(lec.analysis.sections) * 12 + 20 for lec in lectures)
            convo = sum(approx_tokens(a.question + " ".join(p.text for p in a.parts)) + 20 for a in history[-4:])
            budget = max(2000, s.ollama_num_ctx - 2500 - 900 - outline - convo - 600)
        total = sum(approx_tokens(b.text) + 8 for lec in lectures for b in lec.blocks)
        ranked, kinds = self._course_ranker(lectures) if total > budget else (lambda _: [], [])
        answer = answer_course_question(
            self.llm,
            model=s.model_for("ask"),
            course_id=course_id,
            course_title=course.title,
            prefs=course.preferences,
            lectures=lectures,
            question=question,
            history=history,
            budget_tokens=budget,
            ranked=ranked,
            search="+".join(kinds),
            retries=s.llm_max_retries,
        )
        self.repo.save_course_answer(answer)
        return answer

    def course_conversation(self, course_id: str) -> list[CourseAnswer]:
        return self.repo.list_course_answers(course_id)

    def clear_course_conversation(self, course_id: str) -> int:
        return self.repo.clear_course_answers(course_id)

    def course_watched(self, course_id: str) -> set[str]:
        return self.repo.course_watched(course_id)

    def set_course_watched(self, course_id: str, part_key: str, watched: bool) -> None:
        self.repo.set_course_watched(course_id, part_key, watched)
