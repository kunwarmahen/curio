"""FastAPI app exposing CurioService over HTTP (for a future React frontend)."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from curio_learn.config import PROJECT_ROOT, get_settings
from curio_learn.domain import (
    AnalysisStatus,
    Answer,
    Block,
    CardGrade,
    CardState,
    Course,
    CourseAnswer,
    CoursePractice,
    CourseSectionChoice,
    CurriculumCandidate,
    CurriculumSource,
    FlashcardDeck,
    LearningNotes,
    LearningPreferences,
    NotesScope,
    QuizAttempt,
    StudySet,
    VideoAnalysis,
)
from curio_learn.errors import CurioError, InvalidURL, Paused
from curio_learn.logging_setup import configure_logging
from curio_learn.overnight import LocalGpuProbe, QueueStatus, QueueWorker, Window
from curio_learn.service import CurioService
from curio_learn.storage.repository import RecentVideo

log = logging.getLogger(__name__)


class AnalyzeRequest(BaseModel):
    url: str
    preferences: LearningPreferences = LearningPreferences()
    force: bool = False
    # Wait for the overnight queue instead of starting now.
    queue: bool = False


class AnalyzeAccepted(BaseModel):
    analysis_id: str
    status: AnalysisStatus


class CompletedRequest(BaseModel):
    completed: bool


class NotesRequest(BaseModel):
    scope: NotesScope = NotesScope.PLAYLIST
    segment_ids: list[str] = []
    force: bool = False


class AskRequest(BaseModel):
    question: str


class StudyAccepted(BaseModel):
    study_id: str
    status: AnalysisStatus


class AttemptRequest(BaseModel):
    answers: dict[str, int]  # question id -> chosen option index


class ReviewRequest(BaseModel):
    study_id: str
    card_id: str
    grade: CardGrade


class PreferencesBody(BaseModel):
    preferences: LearningPreferences


class RetargetRequest(BaseModel):
    preferences: LearningPreferences
    # Empty = every video waiting in the queue.
    analysis_ids: list[str] = []
    # Also save these preferences as the default for new videos.
    save_default: bool = True


class CustomPlaylistBody(BaseModel):
    segment_ids: list[str] | None = None  # None: back to Curio's picks


class CoursePlaylistBody(BaseModel):
    sections: list[str] | None = None  # "analysis id:section id"; None: back to Curio's picks


class CoursePinsBody(BaseModel):
    sections: list[str]  # "analysis id:section id"; empty: no pins


class LectureOrderBody(BaseModel):
    video_ids: list[str]


class CourseRequest(BaseModel):
    url: str  # a YouTube playlist link
    preferences: LearningPreferences = LearningPreferences()


class GroupRequest(BaseModel):
    title: str = ""
    analysis_ids: list[str]


class SuggestCurriculumRequest(BaseModel):
    goal: str


class CurriculumRequest(BaseModel):
    goal: str
    title: str = ""
    sources: list[CurriculumSource]
    order: str = "lectures"  # or "sections"


class CurriculumSettingsBody(BaseModel):
    order: str | None = None
    auto_order: bool | None = None


class CombineRequest(BaseModel):
    title: str = ""
    course_ids: list[str]


class CourseAccepted(BaseModel):
    course_id: str


class PlanRequest(BaseModel):
    minutes: int | None = None  # empty: about 15 minutes a lecture
    force: bool = False


class PlanAccepted(BaseModel):
    plan_id: str
    status: AnalysisStatus


class WatchedRequest(BaseModel):
    part_key: str
    watched: bool


class NotesAccepted(BaseModel):
    notes_id: str
    status: AnalysisStatus


_STATUS = {
    "invalid_url": 422,
    "video_unavailable": 404,
    "transcript_unavailable": 422,
    "rate_limited": 429,
    "llm_error": 502,
}


class Jobs:
    """One job at a time (local LLMs can't usefully run several in parallel), with a
    count of jobs submitted and not yet finished, so the queue knows when Curio is idle."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="curio-analysis")
        self._lock = threading.Lock()
        self._inflight = 0
        self.on_done: Callable[[], None] = lambda: None

    def submit(self, fn: Callable, *args) -> None:
        with self._lock:
            self._inflight += 1

        def run() -> None:
            try:
                fn(*args)
            finally:
                with self._lock:
                    self._inflight -= 1
                self.on_done()

        self._executor.submit(run)

    def active(self) -> bool:
        with self._lock:
            return self._inflight > 0

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


def build_queue(service: CurioService, jobs: Jobs) -> QueueWorker:
    s = service.settings
    try:
        window = Window.parse(s.queue_hours)
    except ValueError as exc:
        log.error("%s; using 1-7", exc)
        window = Window.parse("1-7")
    probe = None
    if s.resolved_provider == "ollama":
        own = {s.model_for(t) for t in ("outline", "evaluate", "notes", "study", "course", "ask")} | {s.embed_model} - {
            ""
        }
        probe = LocalGpuProbe(busy_percent=s.queue_gpu_busy_percent, ollama_host=s.ollama_host, own_models=own)
    worker = QueueWorker(
        service.repo,
        window=window,
        probe=probe,
        active=jobs.active,
        start=lambda analysis_id: jobs.submit(_run_queued, service, analysis_id, worker),
    )
    jobs.on_done = worker.touch
    return worker


def create_app(service: CurioService | None = None) -> FastAPI:
    jobs = Jobs()
    # Reading a course's lectures from YouTube is network work, not GPU work: it runs
    # beside the job executor so it never holds up an analysis (or the queue).
    imports = ThreadPoolExecutor(max_workers=1, thread_name_prefix="curio-import")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if service is None:
            configure_logging(get_settings().log_level)
            app.state.service = CurioService()
        else:
            app.state.service = service
        stale = app.state.service.repo.mark_stale_running_failed()
        if stale:
            log.warning("Marked %d interrupted analyses as failed", stale)
        app.state.queue = build_queue(app.state.service, jobs)
        if app.state.service.settings.queue_check_s > 0:
            app.state.queue.run_forever(app.state.service.settings.queue_check_s)
        yield
        app.state.queue.stop()
        jobs.shutdown()
        imports.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="Curio API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    def svc(request: Request) -> CurioService:
        return request.app.state.service

    @app.exception_handler(CurioError)
    async def curio_error(_: Request, exc: CurioError):
        return JSONResponse(
            status_code=_STATUS.get(exc.code, 400), content={"detail": {"code": exc.code, "message": exc.message}}
        )

    @app.get("/api/v1/health")
    def health(request: Request):
        s = svc(request)
        return {
            "status": "ok",
            "llm_provider": s.settings.resolved_provider,
            "models": {"outline": s.settings.model_for("outline"), "evaluate": s.settings.model_for("evaluate")},
            "queue_hours": s.settings.queue_hours,
        }

    @app.post("/api/v1/analyses", status_code=202, response_model=AnalyzeAccepted)
    def analyze(body: AnalyzeRequest, request: Request):
        s = svc(request)
        if not body.url.strip():
            raise InvalidURL("A YouTube URL is required.")
        analysis_id, needs_run = s.submit(body.url, body.preferences, force=body.force, queue=body.queue)
        if needs_run:
            jobs.submit(_run_safely, s, analysis_id)
        current = s.get(analysis_id)
        return AnalyzeAccepted(analysis_id=analysis_id, status=current.status if current else AnalysisStatus.PENDING)

    @app.get("/api/v1/analyses/{analysis_id}", response_model=VideoAnalysis)
    def get_analysis(analysis_id: str, request: Request):
        result = svc(request).get(analysis_id)
        if result is None:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown analysis id"})
        return result

    @app.put("/api/v1/analyses/{analysis_id}/segments/{segment_id}/completed")
    def set_completed(analysis_id: str, segment_id: str, body: CompletedRequest, request: Request):
        svc(request).set_completed(analysis_id, segment_id, body.completed)
        return {"analysis_id": analysis_id, "segment_id": segment_id, "completed": body.completed}

    @app.get("/api/v1/analyses/{analysis_id}/custom-playlist", response_model=CustomPlaylistBody)
    def get_custom_playlist(analysis_id: str, request: Request):
        return CustomPlaylistBody(segment_ids=svc(request).custom_playlist(analysis_id))

    @app.put("/api/v1/analyses/{analysis_id}/custom-playlist", response_model=CustomPlaylistBody)
    def put_custom_playlist(analysis_id: str, body: CustomPlaylistBody, request: Request):
        try:
            return CustomPlaylistBody(segment_ids=svc(request).set_custom_playlist(analysis_id, body.segment_ids))
        except KeyError:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown analysis id"}) from None

    @app.get("/api/v1/analyses/{analysis_id}/completed", response_model=list[str])
    def completed(analysis_id: str, request: Request):
        return sorted(svc(request).completed(analysis_id))

    @app.post("/api/v1/analyses/{analysis_id}/notes", status_code=202, response_model=NotesAccepted)
    def create_notes(analysis_id: str, body: NotesRequest, request: Request):
        s = svc(request)
        try:
            notes_id, needs_run = s.submit_notes(analysis_id, body.scope, body.segment_ids, force=body.force)
        except KeyError:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown analysis id"}) from None
        if needs_run:
            jobs.submit(_run_notes_safely, s, notes_id)
        current = s.get_notes(notes_id)
        return NotesAccepted(notes_id=notes_id, status=current.status if current else AnalysisStatus.PENDING)

    @app.get("/api/v1/analyses/{analysis_id}/notes", response_model=list[LearningNotes])
    def list_notes(analysis_id: str, request: Request):
        return svc(request).list_notes(analysis_id)

    @app.get("/api/v1/notes/{notes_id}", response_model=LearningNotes)
    def get_notes(notes_id: str, request: Request):
        notes = svc(request).get_notes(notes_id)
        if notes is None:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown notes id"})
        return notes

    @app.get("/api/v1/notes/{notes_id}/markdown", response_class=PlainTextResponse)
    def notes_markdown(notes_id: str, request: Request):
        text = svc(request).notes_markdown(notes_id)
        if text is None:
            raise HTTPException(404, {"code": "not_found", "message": "No finished notes with this id"})
        return PlainTextResponse(
            text,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="curio-notes-{notes_id}.md"'},
        )

    @app.post("/api/v1/analyses/{analysis_id}/study", status_code=202, response_model=StudyAccepted)
    def create_study(analysis_id: str, body: NotesRequest, request: Request):
        s = svc(request)
        try:
            study_id, needs_run = s.submit_study(analysis_id, body.scope, body.segment_ids, force=body.force)
        except KeyError:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown analysis id"}) from None
        if needs_run:
            jobs.submit(_run_study_safely, s, study_id)
        current = s.get_study(study_id)
        return StudyAccepted(study_id=study_id, status=current.status if current else AnalysisStatus.PENDING)

    @app.get("/api/v1/analyses/{analysis_id}/study", response_model=list[StudySet])
    def list_study(analysis_id: str, request: Request):
        return svc(request).list_study(analysis_id)

    @app.get("/api/v1/study/{study_id}", response_model=StudySet)
    def get_study(study_id: str, request: Request):
        study = svc(request).get_study(study_id)
        if study is None:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown quiz id"})
        return study

    @app.post("/api/v1/study/{study_id}/attempts", response_model=QuizAttempt)
    def grade_quiz(study_id: str, body: AttemptRequest, request: Request):
        try:
            return svc(request).grade_quiz(study_id, body.answers)
        except KeyError:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown quiz id"}) from None

    @app.get("/api/v1/study/{study_id}/attempts", response_model=list[QuizAttempt])
    def quiz_attempts(study_id: str, request: Request):
        return svc(request).quiz_attempts(study_id)

    # ------------------------------------------------------------- courses

    def course_or_404(s: CurioService, course_id: str) -> Course:
        course = s.course(course_id)
        if course is None:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown course id"})
        return course

    @app.post("/api/v1/courses", status_code=202, response_model=CourseAccepted)
    def create_course(body: CourseRequest, request: Request):
        s = svc(request)
        course_id = s.import_course(body.url, body.preferences)
        imports.submit(_queue_course_safely, s, course_id)
        return CourseAccepted(course_id=course_id)

    @app.post("/api/v1/courses/group", status_code=201, response_model=CourseAccepted)
    def group_course(body: GroupRequest, request: Request):
        return CourseAccepted(course_id=svc(request).group_course(body.title, body.analysis_ids))

    @app.post("/api/v1/courses/combine", status_code=201, response_model=CourseAccepted)
    def combine_courses(body: CombineRequest, request: Request):
        try:
            return CourseAccepted(course_id=svc(request).combine_courses(body.title, body.course_ids))
        except KeyError:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown course id"}) from None

    @app.get("/api/v1/curricula/candidates", response_model=list[CurriculumCandidate])
    def curriculum_candidates(request: Request):
        return svc(request).curriculum_candidates()

    @app.post("/api/v1/curricula/suggest", response_model=list[CurriculumCandidate])
    def suggest_curriculum(body: SuggestCurriculumRequest, request: Request):
        return svc(request).suggest_curriculum(body.goal)

    @app.post("/api/v1/curricula", status_code=201, response_model=CourseAccepted)
    def create_curriculum(body: CurriculumRequest, request: Request):
        course_id = svc(request).create_curriculum(body.goal, body.sources, title=body.title, order=body.order)
        return CourseAccepted(course_id=course_id)

    @app.put("/api/v1/courses/{course_id}/curriculum", response_model=Course)
    def set_curriculum(course_id: str, body: CurriculumSettingsBody, request: Request):
        try:
            return svc(request).set_curriculum(course_id, order=body.order, auto_order=body.auto_order)
        except KeyError:
            raise HTTPException(404, {"code": "not_found", "message": "Not a curriculum"}) from None

    @app.get("/api/v1/courses", response_model=list[Course])
    def list_courses(request: Request):
        return svc(request).courses()

    @app.get("/api/v1/courses/{course_id}", response_model=Course)
    def get_course(course_id: str, request: Request):
        return course_or_404(svc(request), course_id)

    @app.delete("/api/v1/courses/{course_id}")
    def delete_course(course_id: str, request: Request):
        if not svc(request).delete_course(course_id):
            raise HTTPException(404, {"code": "not_found", "message": "Unknown course id"})
        return {"deleted": course_id}

    @app.post("/api/v1/courses/{course_id}/refresh", status_code=202, response_model=CourseAccepted)
    def refresh_course(course_id: str, request: Request):
        """Read the playlist again (new lectures) and queue what isn't analyzed yet."""
        s = svc(request)
        course = course_or_404(s, course_id)
        if course.playlist_id:
            s.import_course(course.url, course.preferences)
        else:
            s.repo.update_course(course_id, importing=True)
        imports.submit(_queue_course_safely, s, course_id)
        return CourseAccepted(course_id=course_id)

    @app.post("/api/v1/courses/{course_id}/plan", status_code=202, response_model=PlanAccepted)
    def create_plan(course_id: str, body: PlanRequest, request: Request):
        s = svc(request)
        try:
            plan_id, needs_run = s.submit_course_plan(course_id, body.minutes, force=body.force)
        except KeyError:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown course id"}) from None
        if needs_run:
            jobs.submit(_run_course_plan_safely, s, plan_id)
        current = s.repo.get_course_plan(plan_id)
        return PlanAccepted(plan_id=plan_id, status=current.status if current else AnalysisStatus.PENDING)

    @app.post("/api/v1/courses/{course_id}/questions", response_model=CourseAnswer)
    def ask_course(course_id: str, body: AskRequest, request: Request):
        try:
            return svc(request).ask_course(course_id, body.question)
        except KeyError:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown course id"}) from None
        finally:
            request.app.state.queue.touch()

    @app.get("/api/v1/courses/{course_id}/questions", response_model=list[CourseAnswer])
    def course_conversation(course_id: str, request: Request):
        return svc(request).course_conversation(course_id)

    @app.delete("/api/v1/courses/{course_id}/questions")
    def clear_course_conversation(course_id: str, request: Request):
        return {"deleted": svc(request).clear_course_conversation(course_id)}

    @app.post("/api/v1/courses/{course_id}/practice", status_code=202, response_model=CoursePractice)
    def write_course_practice(course_id: str, body: PlanRequest, request: Request):
        """A quiz and flashcards for every lecture of the course playlist (`force`: rewrite)."""
        s = svc(request)
        try:
            to_run = s.submit_course_practice(course_id, force=body.force)
        except KeyError:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown course id"}) from None
        if to_run:
            jobs.submit(_run_course_practice_safely, s, course_id, to_run)
        return s.course_practice(course_id)

    @app.get("/api/v1/courses/{course_id}/practice", response_model=CoursePractice)
    def course_practice(course_id: str, request: Request):
        try:
            return svc(request).course_practice(course_id)
        except KeyError:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown course id"}) from None

    @app.get("/api/v1/courses/{course_id}/playlist-sections", response_model=list[CourseSectionChoice])
    def course_playlist_sections(course_id: str, request: Request):
        try:
            return svc(request).course_playlist_sections(course_id)
        except KeyError:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown course id"}) from None

    @app.put("/api/v1/courses/{course_id}/custom-playlist", response_model=Course)
    def put_course_playlist(course_id: str, body: CoursePlaylistBody, request: Request):
        try:
            return svc(request).set_course_playlist(course_id, body.sections)
        except KeyError:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown course id"}) from None

    @app.put("/api/v1/courses/{course_id}/pins", response_model=list[str])
    def put_course_pins(course_id: str, body: CoursePinsBody, request: Request):
        try:
            return svc(request).set_course_pins(course_id, body.sections)
        except KeyError:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown course id"}) from None

    @app.put("/api/v1/courses/{course_id}/lectures/order", response_model=Course)
    def reorder_lectures(course_id: str, body: LectureOrderBody, request: Request):
        try:
            return svc(request).reorder_course(course_id, body.video_ids)
        except KeyError:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown course id"}) from None

    @app.get("/api/v1/courses/{course_id}/watched", response_model=list[str])
    def course_watched(course_id: str, request: Request):
        return sorted(svc(request).course_watched(course_id))

    @app.put("/api/v1/courses/{course_id}/watched")
    def set_course_watched(course_id: str, body: WatchedRequest, request: Request):
        s = svc(request)
        course_or_404(s, course_id)
        s.set_course_watched(course_id, body.part_key, body.watched)
        return {"part_key": body.part_key, "watched": body.watched}

    @app.get("/api/v1/flashcards", response_model=FlashcardDeck)
    def flashcards(
        request: Request,
        analysis_id: str | None = None,
        study_id: str | None = None,
        course_id: str | None = None,
        limit: int = 50,
    ):
        return svc(request).flashcards(
            analysis_id, study_id=study_id, course_id=course_id, limit=min(max(limit, 0), 500)
        )

    @app.post("/api/v1/flashcards/review", response_model=CardState)
    def review_card(body: ReviewRequest, request: Request):
        try:
            return svc(request).review_card(body.study_id, body.card_id, body.grade)
        except KeyError:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown flashcard"}) from None

    @app.post("/api/v1/analyses/{analysis_id}/questions", response_model=Answer)
    def ask(analysis_id: str, body: AskRequest, request: Request):
        # Synchronous: an answer takes seconds, not minutes, and runs beside analyses
        # (Ollama queues concurrent requests itself).
        try:
            return svc(request).ask(analysis_id, body.question)
        except KeyError:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown analysis id"}) from None
        finally:
            request.app.state.queue.touch()

    @app.get("/api/v1/analyses/{analysis_id}/questions", response_model=list[Answer])
    def conversation(analysis_id: str, request: Request):
        return svc(request).conversation(analysis_id)

    @app.delete("/api/v1/analyses/{analysis_id}/questions")
    def clear_conversation(analysis_id: str, request: Request):
        return {"deleted": svc(request).clear_conversation(analysis_id)}

    @app.get("/api/v1/preferences/default", response_model=LearningPreferences)
    def default_preferences(request: Request):
        return svc(request).default_preferences()

    @app.put("/api/v1/preferences/default", response_model=LearningPreferences)
    def set_default_preferences(body: PreferencesBody, request: Request):
        svc(request).set_default_preferences(body.preferences)
        return body.preferences

    @app.post("/api/v1/queue/preferences")
    def retarget_queue(body: RetargetRequest, request: Request):
        s = svc(request)
        if body.save_default:
            s.set_default_preferences(body.preferences)
        return s.retarget_queued(body.preferences, body.analysis_ids or None)

    @app.get("/api/v1/queue", response_model=QueueStatus)
    def queue_status(request: Request):
        return request.app.state.queue.status()

    @app.post("/api/v1/queue/{analysis_id}/start", status_code=202, response_model=AnalyzeAccepted)
    def queue_start(analysis_id: str, request: Request):
        s = svc(request)
        try:
            s.start_queued(analysis_id)
        except KeyError:
            raise HTTPException(404, {"code": "not_found", "message": "Unknown analysis id"}) from None
        jobs.submit(_run_safely, s, analysis_id)
        return AnalyzeAccepted(analysis_id=analysis_id, status=AnalysisStatus.PENDING)

    @app.delete("/api/v1/queue/{analysis_id}")
    def queue_remove(analysis_id: str, request: Request):
        if not svc(request).remove_queued(analysis_id):
            raise HTTPException(404, {"code": "not_found", "message": "No queued video with this id"})
        return {"removed": analysis_id}

    @app.get("/api/v1/videos/recent", response_model=list[RecentVideo])
    def recent(request: Request, limit: int = 10):
        return svc(request).recent(min(max(limit, 1), 50))

    @app.get("/api/v1/videos/{video_id}/transcript", response_model=list[Block])
    def transcript(video_id: str, request: Request):
        blocks = svc(request).blocks(video_id)
        if not blocks:
            raise HTTPException(404, {"code": "not_found", "message": "No stored transcript"})
        return blocks

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built React app (web/dist) at /, with client-side routes falling back
    to index.html. In development, Vite serves the UI and proxies /api here instead."""
    dist = PROJECT_ROOT / "web" / "dist"
    if not (dist / "index.html").exists():
        log.info("No built frontend at %s; run `npm run build` in web/ to serve the UI", dist)
        return
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, {"code": "not_found", "message": "Unknown API route"})
        file = (dist / path).resolve()
        if path and file.is_file() and file.is_relative_to(dist.resolve()):
            return FileResponse(file)
        return FileResponse(dist / "index.html")


def _run_safely(service: CurioService, analysis_id: str) -> None:
    try:
        service.run(analysis_id)
    except Exception:  # status is already recorded as failed by the service
        log.warning("Analysis %s failed", analysis_id, exc_info=True)


def _every(seconds: float, check: Callable[[], str]) -> Callable[[], str]:
    """`check`, but asked at most once every `seconds` (a GPU check takes 5 s)."""
    last = [time.monotonic()]

    def throttled() -> str:
        now = time.monotonic()
        if now - last[0] < seconds:
            return ""
        last[0] = now
        return check()

    return throttled


def _run_queued(service: CurioService, analysis_id: str, worker: QueueWorker) -> None:
    """A queued video: analyze it, then write its notes and quiz while the GPU stays free.
    If something else starts using the GPU during the analysis, it pauses and waits in
    the queue again."""
    try:
        service.run(analysis_id, should_pause=_every(60.0, worker.gpu_busy))
    except Paused:
        return
    except Exception:
        log.warning("Analysis %s failed", analysis_id, exc_info=True)
        return
    if service.settings.queue_prepare:
        written = service.prepare(analysis_id, gpu_busy=worker.gpu_busy)
        log.info("Queue prepared %s for analysis %s", ", ".join(written) or "nothing new", analysis_id)
        plans = service.rebuild_courses_after(analysis_id, gpu_busy=worker.gpu_busy)
        if plans:
            log.info("Queue rebuilt %d course playlist(s) after analysis %s", len(plans), analysis_id)


def _queue_course_safely(service: CurioService, course_id: str) -> None:
    try:
        service.queue_course(course_id)
    except Exception:
        log.warning("Queuing course %s failed", course_id, exc_info=True)


def _run_course_plan_safely(service: CurioService, plan_id: str) -> None:
    try:
        service.run_course_plan(plan_id)
    except Exception:
        log.warning("Course plan %s failed", plan_id, exc_info=True)


def _run_course_practice_safely(service: CurioService, course_id: str, study_ids: list[str]) -> None:
    try:
        service.run_course_practice(course_id, study_ids)
    except Exception:
        log.warning("Course practice for %s failed", course_id, exc_info=True)


def _run_study_safely(service: CurioService, study_id: str) -> None:
    try:
        service.run_study(study_id)
    except Exception:
        log.warning("Study set %s failed", study_id, exc_info=True)


def _run_notes_safely(service: CurioService, notes_id: str) -> None:
    try:
        service.run_notes(notes_id)
    except Exception:
        log.warning("Notes %s failed", notes_id, exc_info=True)


app = create_app()
