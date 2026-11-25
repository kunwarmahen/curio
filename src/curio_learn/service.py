"""CurioService: the single entry point frontends use (the FastAPI app behind the web UI, and the CLI)."""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import BaseModel

from curio_learn.analysis.ask import answer_question
from curio_learn.analysis.course import Lecture, build_course_plan, custom_course_parts
from curio_learn.analysis.course_ask import LectureText, answer_course_question, course_passages
from curio_learn.analysis.curriculum import Source, order_sources, source_edges, suggest_sources
from curio_learn.analysis.notes import generate_notes, scope_key, scope_sections, to_markdown
from curio_learn.analysis.pipeline import AnalysisPipeline
from curio_learn.analysis.playlist import _RANK, PlaylistConfig, fit_labels
from curio_learn.analysis.study import checklist_fronts, generate_study, is_due, schedule
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
    CoursePractice,
    CourseSectionChoice,
    CourseStudy,
    CurriculumCandidate,
    CurriculumInfo,
    CurriculumSource,
    DueCard,
    Flashcard,
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
from curio_learn.errors import AnalysisError, CurioError, InvalidURL, Paused
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
STUDY_VERSION = 3
# Bump when course playlist building changes should invalidate saved plans.
COURSE_VERSION = 2
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

    def run(
        self,
        analysis_id: str,
        progress: Progress = lambda _: None,
        *,
        should_pause: Callable[[], str] = lambda: "",
    ) -> VideoAnalysis:
        """Analyze a registered video. `should_pause` is asked before each step (the
        queue passes its GPU check): a reason pauses the run, which goes back into the
        queue and raises `Paused`."""
        pending = self.repo.get_analysis(analysis_id)
        if pending is None:
            raise KeyError(analysis_id)

        def report(msg: str) -> None:
            reason = should_pause()
            if reason:
                raise Paused(reason)
            log.info("[%s] %s", analysis_id, msg)
            self.repo.update_status(analysis_id, AnalysisStatus.RUNNING, msg)
            progress(msg)

        try:
            report("Loading transcript")
            meta, transcript = self.ingest(pending.video.video_id)
            pipeline = AnalysisPipeline(self.settings, self.llm)
            result = pipeline.run(
                meta,
                transcript,
                pending.preferences,
                analysis_id=analysis_id,
                progress=report,
                checkpoint=self.repo.get_checkpoint(analysis_id),
                save_checkpoint=lambda data: self.repo.save_checkpoint(analysis_id, data),
                version=self.llm_key(),
            )
            result.created_at = pending.created_at
            self.repo.save_result(result)
            self.repo.delete_checkpoint(analysis_id)
            return result
        except Paused as exc:
            # The finished steps stay in the checkpoint; the next run picks up there.
            log.info("[%s] Paused, back in the queue: %s", analysis_id, exc.message)
            self.repo.requeue(analysis_id, f"Paused: {exc.message}")
            raise
        except CurioError as exc:
            self.repo.update_status(analysis_id, AnalysisStatus.FAILED, error=exc.message)
            self.repo.delete_checkpoint(analysis_id)
            raise
        except Exception as exc:
            log.exception("Analysis %s failed", analysis_id)
            self.repo.update_status(analysis_id, AnalysisStatus.FAILED, error=f"Unexpected error: {exc}")
            self.repo.delete_checkpoint(analysis_id)
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

    def relabel_saved(self) -> dict[str, int]:
        """Apply the current labeling rule (`fit_labels`: "essential" only for sections in
        the playlist) to finished analyses saved before it. Only labels change: the
        playlist, part ids, scores, custom playlists and watched parts stay. No model
        calls. Returns {analysis id: sections relabeled} for the analyses changed."""
        changed: dict[str, int] = {}
        for analysis_id in self.repo.done_analysis_ids():
            a = self.repo.get_analysis(analysis_id)
            if a is None or a.playlist is None:
                continue
            chosen = {sid for seg in a.playlist.segments for sid in seg.section_ids}
            labeled = fit_labels(a.sections, chosen, a.playlist.budget_s)
            n = sum(1 for old, new in zip(a.sections, labeled, strict=True) if old.importance != new.importance)
            if not n:
                continue
            by_id = {s.section.id: s.importance for s in labeled}
            others = [
                seg.model_copy(update={"importance": max((by_id[i] for i in seg.section_ids), key=_RANK.get)})
                if not seg.in_playlist
                else seg
                for seg in a.segments
            ]
            self.repo.save_result(a.model_copy(update={"sections": labeled, "segments": others}))
            changed[analysis_id] = n
        return changed

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

    # --------------------------------------------------------- custom playlists

    def custom_playlist(self, analysis_id: str) -> list[str] | None:
        """The learner's own playlist (segment ids in time order), or None for Curio's."""
        return self.repo.custom_playlist(analysis_id)

    def set_custom_playlist(self, analysis_id: str, segment_ids: list[str] | None) -> list[str] | None:
        """Save the learner's playlist; None (or Curio's exact picks) goes back to Curio's."""
        analysis = self.repo.get_analysis(analysis_id)
        if analysis is None:
            raise KeyError(analysis_id)
        if analysis.status != AnalysisStatus.DONE:
            raise AnalysisError("A playlist can be edited once the analysis has finished.")
        if segment_ids is None:
            self.repo.set_custom_playlist(analysis_id, None)
            return None
        by_id = {s.id: s for s in analysis.segments}
        unknown = set(segment_ids) - set(by_id)
        if unknown:
            raise AnalysisError(f"Unknown segment ids: {', '.join(sorted(unknown))}.")
        if not segment_ids:
            raise AnalysisError("Keep at least one part in your playlist.")
        ids = sorted(set(segment_ids), key=lambda i: by_id[i].start_s)
        curated = [s.id for s in analysis.segments if s.in_playlist]
        if ids == sorted(curated, key=lambda i: by_id[i].start_s):
            ids = None  # the same as Curio's picks
        self.repo.set_custom_playlist(analysis_id, ids)
        return ids

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
        course_id: str = "",
    ) -> tuple[str, bool]:
        """Register a quiz and flashcards for an analysis. Returns (study_id, needs_run).
        The course scope takes section ids (in order) and the course they're for."""
        analysis = self.repo.get_analysis(analysis_id)
        if analysis is None:
            raise KeyError(analysis_id)
        if analysis.status != AnalysisStatus.DONE:
            raise AnalysisError("A quiz can be written once the analysis has finished.")
        if scope is NotesScope.COURSE:
            if not course_id:
                raise AnalysisError("A course quiz needs its course.")
            ids = list(dict.fromkeys(segment_ids or []))
        else:
            ids = sorted(set(segment_ids or [])) if scope is NotesScope.SELECTED else []
        scope_sections(analysis, scope, ids)  # validates the selection
        if scope is NotesScope.COURSE:
            digest = hashlib.sha256(",".join(ids).encode()).hexdigest()[:12]
            key = f"course|{course_id}|{digest}"
        else:
            key = scope_key(scope, ids)
        llm_key = self.study_llm_key()
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
                # A course asks one question and one card per section, over many lectures.
                budget=(1, 1) if pending.scope is NotesScope.COURSE else None,
            )
            study.created_at = pending.created_at
            study.course_id = pending.course_id
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
        course_id: str | None = None,
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
            decks = [
                (s, v)
                for s, v in self.repo.decks()
                if (analysis_id is None or s.analysis_id == analysis_id)
                and (course_id is None or s.course_id == course_id)
            ]
            if course_id:  # a course's new cards come in lecture order
                course = self.course(course_id)
                order = {lec.analysis_id: lec.position for lec in course.lectures} if course else {}
                decks.sort(key=lambda d: order.get(d[0].analysis_id, 0))
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

    def combine_courses(self, title: str, course_ids: list[str]) -> str:
        """A learning path: one course with the lectures of several, in the order given
        (a video in two of them comes once, at its first place), and the first one's goal."""
        courses = [self.repo.get_course(cid) for cid in dict.fromkeys(course_ids)]
        if any(c is None for c in courses):
            raise KeyError("course")
        if len(courses) < 2:
            raise AnalysisError("Pick at least two courses to combine.")
        lectures, seen = [], set()
        for c in courses:
            for lec in c["lectures"]:
                if lec["video_id"] not in seen:
                    seen.add(lec["video_id"])
                    lectures.append(lec)
        course_id = uuid.uuid4().hex[:12]
        self.repo.create_course(
            course_id,
            title=title.strip() or " + ".join(c["title"] for c in courses),
            channel="",
            playlist_id="",
            prefs=courses[0]["preferences"],
            lectures=lectures,
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
        cur = self.repo.get_curriculum(course["id"]) if course.get("id") else None
        alone = {v for v, g in (cur or {}).get("groups", {}).items() if not g}  # a curriculum's single videos

        def label(i: int) -> str:
            if course["lectures"][i]["video_id"] in alone:
                return _short(course["lectures"][i]["title"], 40)
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

    def course(self, course_id: str, *, custom: bool = True) -> Course | None:
        """A course with its lectures' states and newest plan. With `custom`, also the
        learner's own course playlist, if they saved one."""
        data = self.repo.get_course(course_id)
        if data is None:
            return None
        lectures = self._lectures(data)
        plan = self.repo.latest_course_plan(course_id)
        usable = [lec.analysis_id for lec in lectures if lec.analysis_id]
        if plan is not None:
            # Lectures may have been reordered since the plan was built: number its
            # references by the current order and play its parts in that order.
            plan = _renumber(plan, {lec.video_id: lec.position for lec in lectures})
            if plan.order == "lectures":  # a curriculum section by section keeps its own order
                plan.parts.sort(key=lambda p: p.lecture)  # stable: keeps the order within a lecture
        stale = plan is not None and plan.status == AnalysisStatus.DONE and set(plan.analysis_ids) != set(usable)
        custom_parts = None
        if custom and plan is not None and plan.status == AnalysisStatus.DONE:
            picked = self.repo.course_custom_playlist(course_id)
            if picked is not None:
                custom_parts = self._custom_course_parts(lectures, plan, picked) or None
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
            custom_parts=custom_parts,
            curriculum=self._curriculum_info(course_id),
            created_at=data["created_at"],
        )

    def courses(self) -> list[Course]:
        listed = (self.course(d["id"], custom=False) for d in self.repo.list_courses())
        return [c for c in listed if c is not None]

    # --------------------------------------------------- editing a course

    def reorder_course(self, course_id: str, video_ids: list[str]) -> Course:
        """Put the lectures in this order (every lecture, each once). The course
        playlist follows at once; building it again also redoes the order-dependent
        choices (which of two repeats to keep)."""
        data = self.repo.get_course(course_id)
        if data is None:
            raise KeyError(course_id)
        by_id = {x["video_id"]: x for x in data["lectures"]}
        if sorted(video_ids) != sorted(by_id) or len(set(video_ids)) != len(video_ids):
            raise AnalysisError("List every lecture of the course once.")
        self.repo.update_course(course_id, lectures=[by_id[v] for v in video_ids])
        if self.repo.get_curriculum(course_id) is not None:
            self.repo.save_curriculum(course_id, auto_order=False)  # the learner's order from now on
        return self.course(course_id)

    # ------------------------------------------------------------ curricula

    def _curriculum_info(self, course_id: str) -> CurriculumInfo | None:
        cur = self.repo.get_curriculum(course_id)
        if cur is None:
            return None
        return CurriculumInfo(goal=cur["goal"], order=cur["order"], auto_order=cur["auto_order"], notes=cur["notes"])

    def curriculum_candidates(self) -> list[CurriculumCandidate]:
        """Every course (not curricula) and every analyzed video outside them."""
        out: list[CurriculumCandidate] = []
        in_courses: set[str] = set()
        for data in self.repo.list_courses():
            if self.repo.get_curriculum(data["id"]) is not None:
                continue
            in_courses.update(x["video_id"] for x in data["lectures"])
            n = len(data["lectures"])
            out.append(
                CurriculumCandidate(
                    kind="course", id=data["id"], title=data["title"], detail=f"{n} lecture{'s' if n != 1 else ''}"
                )
            )
        seen: set[str] = set()
        for r in self.repo.recent(limit=500):
            v = r.video
            if v.video_id in in_courses or v.video_id in seen or not r.analysis_id:
                continue
            seen.add(v.video_id)
            minutes = round((v.duration_s or 0) / 60)
            out.append(
                CurriculumCandidate(
                    kind="video",
                    id=r.analysis_id,
                    title=v.title,
                    detail=" · ".join(x for x in (v.channel, f"{minutes} min" if minutes else "") if x),
                )
            )
        return out

    def suggest_curriculum(self, goal: str) -> list[CurriculumCandidate]:
        """The courses and videos in the library, with the ones Curio suggests for `goal`
        marked and a reason; the learner confirms before anything is built."""
        goal = goal.strip()
        if not goal:
            raise AnalysisError("Say what you want to learn.")
        candidates = self.curriculum_candidates()
        if not candidates:
            raise AnalysisError("Analyze some videos or add a course first.")
        about = {}
        for c in candidates:
            if c.kind == "course":
                data = self.repo.get_course(c.id)
                about[c.id] = "Lectures: " + "; ".join(x["title"] for x in data["lectures"][:20])
            else:
                a = self.repo.get_analysis(c.id)
                concepts = ", ".join(getattr(a, "key_concepts", [])[:10]) if a else ""
                about[c.id] = f"{(a.overview or '')[:400] if a else ''} Key concepts: {concepts}"
        return suggest_sources(
            self.llm,
            goal=goal,
            candidates=candidates,
            about=about,
            model=self.settings.model_for("course"),
            retries=self.settings.llm_max_retries,
        )

    def create_curriculum(
        self, goal: str, sources: list[CurriculumSource], *, title: str = "", order: str = "lectures"
    ) -> str:
        """A curriculum: a course with the lectures of the chosen courses and videos (a
        video in two comes once, at its first place), ordered by what builds on what."""
        goal = goal.strip()
        if not goal:
            raise AnalysisError("Say what you want to learn.")
        if order not in ("lectures", "sections"):
            raise AnalysisError("Order by whole lectures or section by section.")
        lectures: list[dict] = []
        groups: dict[str, str] = {}
        analyses: list[VideoAnalysis] = []
        for src in sources:
            if src.kind == "course":
                data = self.repo.get_course(src.id)
                if data is None:
                    raise AnalysisError("A chosen course no longer exists.")
                new = [dict(x) for x in data["lectures"]]
                group = src.id
            else:
                a = self.repo.get_analysis(src.id)
                if a is None or a.status != AnalysisStatus.DONE:
                    raise AnalysisError("Pick videos that have finished analyzing.")
                analyses.append(a)
                new = [{"video_id": a.video.video_id, "title": a.video.title, "duration_s": a.video.duration_s}]
                group = ""
            for x in new:
                if x["video_id"] not in groups:
                    groups[x["video_id"]] = group
                    lectures.append(x)
        if len(lectures) < 2:
            raise AnalysisError("A curriculum needs at least two videos or lectures.")
        rows = self.repo.analyses_for_videos([x["video_id"] for x in lectures])
        keys = [r["preferences_key"] for r in rows if r["status"] == AnalysisStatus.DONE]
        prefs = self.default_preferences()
        if keys:
            common = max(keys, key=keys.count)
            row = next(r for r in rows if r["preferences_key"] == common and r["status"] == AnalysisStatus.DONE)
            done = self.repo.get_analysis(row["id"])
            if done is not None:
                prefs = done.preferences
        course_id = uuid.uuid4().hex[:12]
        self.repo.create_course(
            course_id, title=title.strip() or goal[:80], channel="", playlist_id="", prefs=prefs, lectures=lectures
        )
        self.repo.save_curriculum(course_id, goal=goal, order=order, groups=groups, auto_order=True, notes=[])
        self.order_curriculum(course_id)
        return course_id

    def set_curriculum(self, course_id: str, *, order: str | None = None, auto_order: bool | None = None) -> Course:
        """Change how a curriculum is ordered. Turning Curio's order back on reorders the
        lectures now; the playlist follows the next time it's built."""
        if self.repo.get_curriculum(course_id) is None:
            raise KeyError(course_id)
        fields: dict = {}
        if order is not None:
            if order not in ("lectures", "sections"):
                raise AnalysisError("Order by whole lectures or section by section.")
            fields["order"] = order
        if auto_order is not None:
            fields["auto_order"] = auto_order
        if fields:
            self.repo.save_curriculum(course_id, **fields)
        if auto_order:
            self.order_curriculum(course_id)
        return self.course(course_id)

    def order_curriculum(self, course_id: str) -> list[str]:
        """Put a curriculum's lectures in the order they build on each other (no model
        calls): each course keeps its own order; another course or a video goes before
        the first lecture that needs what it teaches. Lectures not analyzed yet stay after
        the one they followed. Saves the reasons; returns the video ids in order."""
        data = self.repo.get_course(course_id)
        cur = self.repo.get_curriculum(course_id)
        if data is None or cur is None:
            raise KeyError(course_id)
        lectures = self._lectures(data)
        sources = []
        for i, lec in enumerate(lectures):
            analysis = self.repo.get_analysis(lec.analysis_id) if lec.analysis_id else None
            if analysis is not None and analysis.status == AnalysisStatus.DONE:
                sources.append(
                    Source(key=lec.video_id, analysis=analysis, rank=i, group=cur["groups"].get(lec.video_id, ""))
                )
        keys, kept = order_sources(sources, source_edges(sources))
        order = list(keys)
        for i, lec in enumerate(lectures):  # lectures without an analysis: after their old neighbour
            if lec.video_id in order:
                continue
            prev = next((lectures[j].video_id for j in range(i - 1, -1, -1) if lectures[j].video_id in order), None)
            order.insert(order.index(prev) + 1 if prev else 0, lec.video_id)
        by_id = {x["video_id"]: x for x in data["lectures"]}
        if order != [x["video_id"] for x in data["lectures"]]:
            self.repo.update_course(course_id, lectures=[by_id[v] for v in order])
        title = {lec.video_id: lec.title for lec in lectures}
        notes, said = [], set()
        for e in sorted(kept, key=lambda e: order.index(e.after)):
            if e.before in said:
                continue  # once per moved source: before the first lecture that needs it
            said.add(e.before)
            needs = ", ".join(x[:1].lower() + x[1:] if x[1:2].islower() else x for x in e.examples)
            notes.append(f"“{_short(title[e.before])}” comes before “{_short(title[e.after])}”: it teaches {needs}.")
        self.repo.save_curriculum(course_id, notes=notes)
        return order

    def _course_lectures(self, lectures: list[CourseLecture]) -> list[Lecture]:
        out = []
        for lec in lectures:
            analysis = self.repo.get_analysis(lec.analysis_id) if lec.analysis_id else None
            if analysis is not None and analysis.status == AnalysisStatus.DONE:
                out.append(Lecture(position=lec.position, analysis=analysis, units=[], label=lec.label))
        return out

    def _custom_course_parts(self, lectures: list[CourseLecture], plan: CoursePlan, picked: list[str]) -> list:
        """The learner's parts from the lectures the course uses now; sections of an
        analysis the course no longer uses are left out."""
        refs = {tuple(x.split(":", 1)) for x in picked if ":" in x}
        return custom_course_parts(
            self._course_lectures(lectures), refs, plan.pairs, PlaylistConfig(merge_gap_s=self.settings.merge_gap_s)
        )

    def course_playlist_sections(self, course_id: str) -> list[CourseSectionChoice]:
        """Every section of the analyzed lectures, for editing the course playlist:
        which ones Curio picked and which are in the playlist shown now."""
        course = self.course(course_id)
        if course is None:
            raise KeyError(course_id)
        plan = course.plan
        if plan is None or plan.status != AnalysisStatus.DONE:
            raise AnalysisError("Build the course playlist first, then edit it.")
        curio = {(p.analysis_id, sid) for p in plan.parts for sid in p.section_ids}
        shown = {(p.analysis_id, sid) for p in (course.custom_parts or plan.parts) for sid in p.section_ids}
        pins = {tuple(x.split(":", 1)) for x in self.repo.course_pins(course_id)}
        watched = self._watched_sections(course)
        out = []
        for lec in self._course_lectures(course.lectures):
            for s in lec.analysis.sections:
                key = (lec.analysis.id, s.section.id)
                out.append(
                    CourseSectionChoice(
                        lecture=lec.position,
                        video_id=lec.analysis.video.video_id,
                        analysis_id=lec.analysis.id,
                        section_id=s.section.id,
                        title=s.section.title,
                        start_s=s.section.start_s,
                        end_s=s.section.end_s,
                        importance=s.importance,
                        curio_pick=key in curio,
                        picked=key in shown,
                        pinned=key in pins,
                        watched=key in watched,
                    )
                )
        return out

    def set_course_pins(self, course_id: str, sections: list[str]) -> list[str]:
        """Save the sections to keep in Curio's course playlist whenever it's built, as
        "analysis id:section id" strings, in course order. They count from the next build."""
        choices = self.course_playlist_sections(course_id)
        order = [f"{c.analysis_id}:{c.section_id}" for c in choices]
        unknown = set(sections) - set(order)
        if unknown:
            raise AnalysisError(f"Unknown sections: {', '.join(sorted(unknown))}.")
        pins = [k for k in order if k in set(sections)]
        self.repo.set_course_pins(course_id, pins)
        return pins

    def _course_watched_spans(self, course: Course) -> list[tuple[str, float, float]]:
        """(video id, start, end) of the parts the learner marked watched in one course. A
        part key is looked up in the playlists on screen (Curio's and the learner's); an
        older key gives the section playing just after its start."""
        keys = self.repo.course_watched(course.id)
        if not keys:
            return []
        out: list[tuple[str, float, float]] = []
        known: set[str] = set()
        for part in (course.plan.parts if course.plan else []) + (course.custom_parts or []):
            if part.key in keys and part.key not in known:
                known.add(part.key)
                out.append((part.video_id, part.start_s, part.end_s))
        by_video = {lec.video_id: lec.analysis_id for lec in course.lectures if lec.analysis_id}
        for key in keys - known:
            video_id, _, start = key.rpartition("@")
            aid = by_video.get(video_id)
            analysis = self.repo.get_analysis(aid) if aid else None
            if analysis is None or not start.isdigit():
                continue
            t = int(start) + 3  # a part starts 2 s before its first section
            out += [
                (video_id, s.section.start_s, s.section.end_s)
                for s in analysis.sections
                if s.section.start_s <= t < s.section.end_s
            ]
        return out

    def _watched_spans(self, course: Course) -> dict[str, list[tuple[float, float]]]:
        """Stretches of each video the learner marked watched anywhere: video playlists
        and every course (this one as passed in, others as saved)."""
        spans = list(self.repo.completed_spans()) + self._course_watched_spans(course)
        for cid in self.repo.watched_course_ids():
            if cid != course.id:
                other = self.course(cid)
                if other is not None:
                    spans += self._course_watched_spans(other)
        out: dict[str, list[tuple[float, float]]] = {}
        for video_id, a, b in spans:
            out.setdefault(video_id, []).append((a, b))
        return out

    def _watched_sections(self, course: Course) -> set[tuple[str, str]]:
        """(analysis id, section id) of the course's sections the learner has watched
        anywhere: at least 80% of the section lies in stretches marked watched in a video
        playlist or any course (sections are matched by time, so a video analyzed for
        another goal counts too)."""
        spans = self._watched_spans(course)
        out: set[tuple[str, str]] = set()
        for lec in course.lectures:
            mine = spans.get(lec.video_id)
            analysis = self.repo.get_analysis(lec.analysis_id) if mine and lec.analysis_id else None
            if analysis is None:
                continue
            for s in analysis.sections:
                a, b = s.section.start_s, s.section.end_s
                if b > a and _covered(a, b, mine) >= 0.8 * (b - a):
                    out.add((analysis.id, s.section.id))
        return out

    def set_course_playlist(self, course_id: str, sections: list[str] | None) -> Course:
        """Save the learner's course playlist, as "analysis id:section id" strings;
        None (or exactly Curio's picks) goes back to Curio's."""
        choices = self.course_playlist_sections(course_id)
        if sections is not None:
            known = {f"{c.analysis_id}:{c.section_id}" for c in choices}
            unknown = set(sections) - known
            if unknown:
                raise AnalysisError(f"Unknown sections: {', '.join(sorted(unknown))}.")
            if not sections:
                raise AnalysisError("Keep at least one part in your playlist.")
            ordered = [k for k in (f"{c.analysis_id}:{c.section_id}" for c in choices) if k in set(sections)]
            curio = [f"{c.analysis_id}:{c.section_id}" for c in choices if c.curio_pick]
            sections = None if ordered == curio else ordered
        self.repo.set_course_custom_playlist(course_id, sections)
        return self.course(course_id)

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
        (plan_id, needs_run); reuses one with identical inputs unless `force`. A
        curriculum is put in order first (unless the learner ordered it)."""
        cur = self.repo.get_curriculum(course_id)
        if cur is not None and cur["auto_order"]:
            self.order_curriculum(course_id)
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
        if cur is not None:
            key += f"|order:{cur['order']}"
        pins = self.repo.course_pins(course_id)
        if pins:  # pinning changes the playlist: build it again
            key += "|pins:" + hashlib.sha256(",".join(pins).encode()).hexdigest()[:12]
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
            pinned = {tuple(x.split(":", 1)) for x in self.repo.course_pins(pending.course_id)}
            cur = self.repo.get_curriculum(pending.course_id)
            shown = self.course(pending.course_id)
            watched = self._watched_sections(shown) if shown else set()
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
                pinned=pinned,
                watched=watched,
                groups=cur["groups"] if cur else None,
                by_section=bool(cur and cur["order"] == "sections"),
            )
            plan.created_at = pending.created_at
            self.repo.save_course_plan(plan)
            # Watched marks follow the sections: a new part made only of watched sections
            # is watched too (its key changes when its start moves).
            for part in plan.parts:
                if part.section_ids and all((part.analysis_id, sid) in watched for sid in part.section_ids):
                    self.repo.set_course_watched(pending.course_id, part.key, True)
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
        answers = self.repo.list_course_answers(course_id)
        data = self.repo.get_course(course_id)
        if data is None:
            return answers
        positions = {x["video_id"]: n for n, x in enumerate(data["lectures"], 1)}
        return [_renumber(a, positions) for a in answers]

    def clear_course_conversation(self, course_id: str) -> int:
        return self.repo.clear_course_answers(course_id)

    # ------------------------------------------------- course quiz and flashcards

    def _plan_sections(self, course: Course) -> dict[str, list[str]]:
        """The sections of each analysis in the course playlist (the learner's own when
        they saved one), in order."""
        plan = course.plan
        out: dict[str, list[str]] = {aid: [] for aid in plan.analysis_ids}
        for part in course.custom_parts or plan.parts:
            for sid in part.section_ids:
                if sid not in out.setdefault(part.analysis_id, []):
                    out[part.analysis_id].append(sid)
        return {aid: ids for aid, ids in out.items() if ids}

    def course_practice(self, course_id: str) -> CoursePractice:
        course = self.course(course_id)
        if course is None:
            raise KeyError(course_id)
        positions = {lec.analysis_id: lec.position for lec in course.lectures if lec.analysis_id}
        newest: dict[str, StudySet] = {}
        for st in self.repo.course_study_sets(course_id):
            newest.setdefault(st.analysis_id, st)
        if not newest:
            return CoursePractice(course_id=course_id)
        plan = course.plan if course.plan and course.plan.status == AnalysisStatus.DONE else None
        wanted = self._plan_sections(course) if plan else {}
        sets = [
            CourseStudy(lecture=positions.get(aid, 0), study=st)
            for aid, st in newest.items()
            if not wanted or aid in wanted
        ]
        sets.sort(key=lambda x: x.lecture)
        live = [x.study for x in sets if x.study.status in (AnalysisStatus.PENDING, AnalysisStatus.RUNNING)]
        done = [x.study for x in sets if x.study.status == AnalysisStatus.DONE]
        status = AnalysisStatus.RUNNING if live else AnalysisStatus.DONE if done else AnalysisStatus.FAILED
        stale = bool(wanted) and any(aid not in newest or newest[aid].segment_ids != ids for aid, ids in wanted.items())
        progress = ""
        if live:
            n = next(i for i, x in enumerate(sets, 1) if x.study in live)
            progress = f"Lecture {n} of {len(sets)}: {live[0].progress or 'waiting'}"
        return CoursePractice(
            course_id=course_id,
            sets=sets,
            status=status,
            progress=progress,
            stale=stale,
            questions=sum(len(s.questions) for s in done),
            cards=sum(len(s.cards) for s in done),
        )

    def submit_course_practice(self, course_id: str, *, force: bool = False) -> list[str]:
        """Register a quiz and flashcards for every lecture of the course playlist.
        Returns the sets that need writing (the rest are reused)."""
        course = self.course(course_id)
        if course is None:
            raise KeyError(course_id)
        plan = course.plan
        if plan is None or plan.status != AnalysisStatus.DONE:
            raise AnalysisError("Build the course playlist first; the quiz covers its parts.")
        to_run = []
        for aid, sections in self._plan_sections(course).items():
            study_id, needs_run = self.submit_study(aid, NotesScope.COURSE, sections, force=force, course_id=course_id)
            if needs_run:
                to_run.append(study_id)
        return to_run

    def run_course_practice(self, course_id: str, study_ids: list[str]) -> CoursePractice:
        """Write the sets in lecture order, then add a card for each "easy to miss"
        point to the set of the lecture it cites first."""
        written: dict[str, StudySet] = {}
        for sid in study_ids:
            try:
                st = self.run_study(sid)
                written[st.analysis_id] = st
            except Exception:  # recorded on the set
                log.warning("[course %s] A lecture's quiz failed", course_id, exc_info=True)
        course = self.course(course_id)
        plan = course.plan if course else None
        if written and plan and plan.checklist:
            items = [it for it in plan.checklist if it.refs and it.refs[0].analysis_id in written]
            try:
                fronts = (
                    checklist_fronts(
                        self.llm,
                        model=self.settings.model_for("study"),
                        points=[it.text for it in items],
                        retries=self.settings.llm_max_retries,
                    )
                    if items
                    else {}
                )
            except CurioError as exc:
                log.warning("[course %s] Checklist cards failed: %s", course_id, exc.message)
                fronts = {}
            for n, item in enumerate(items, 1):
                front = fronts.get(n)
                if not front:
                    continue
                ref = item.refs[0]
                st = written[ref.analysis_id]
                st.cards.append(
                    Flashcard(
                        id=f"m{sum(c.id.startswith('m') for c in st.cards) + 1}",
                        front=front,
                        back=item.text,
                        section_id=ref.section_id,
                        start_s=ref.start_s,
                    )
                )
            for st in written.values():
                self.repo.save_study(st)
        return self.course_practice(course_id)

    def course_watched(self, course_id: str) -> set[str]:
        return self.repo.course_watched(course_id)

    def set_course_watched(self, course_id: str, part_key: str, watched: bool) -> None:
        self.repo.set_course_watched(course_id, part_key, watched)


def _renumber[M: BaseModel](model: M, positions: dict[str, int]) -> M:
    """Renumber every lecture reference (`lecture` next to `video_id`) in a saved plan or
    answer by the course's current lecture order."""

    def walk(x) -> None:
        if isinstance(x, dict):
            if "lecture" in x and x.get("video_id") in positions:
                x["lecture"] = positions[x["video_id"]]
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    data = model.model_dump(mode="json")
    walk(data)
    return type(model).model_validate(data)


def _covered(a: float, b: float, spans: list[tuple[float, float]]) -> float:
    """Seconds of [a, b] inside the union of `spans`."""
    total, end = 0.0, a
    for x, y in sorted(spans):
        x, y = max(x, end), min(y, b)
        if y > x:
            total += y - x
            end = y
    return total


def _short(title: str, limit: int = 60) -> str:
    """A title without its channel or course suffix ("RAG Explained | Stanford …")."""
    head = re.split(r"\s[|:–-]\s", title)[0].strip() or title
    return head if len(head) <= limit else head[: limit - 1].rstrip() + "…"
