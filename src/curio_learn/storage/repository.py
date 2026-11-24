"""Persistence API. The rest of the app never touches SQLAlchemy directly."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from curio_learn.domain import (
    AnalysisStatus,
    Answer,
    CardState,
    CourseAnswer,
    CoursePlan,
    LearningNotes,
    LearningPreferences,
    NotesScope,
    QuizAttempt,
    StudySet,
    Transcript,
    VideoAnalysis,
    VideoMetadata,
)
from curio_learn.storage.orm import (
    AnalysisCheckpointRow,
    AnalysisRow,
    Base,
    CardStateRow,
    CourseCustomPlaylistRow,
    CoursePlanRow,
    CourseQuestionRow,
    CourseRow,
    CourseWatchedRow,
    CustomPlaylistRow,
    EmbeddingRow,
    NotesRow,
    QuestionRow,
    QuizAttemptRow,
    SegmentRow,
    SettingRow,
    StudySetRow,
    TranscriptRow,
    VideoRow,
)

# A running analysis updates its row at every pipeline step; one that hasn't for this
# long belongs to a process that died. The slowest step (a local-LLM call) is minutes.
STALE_AFTER = timedelta(minutes=20)


def _aware(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; they are stored in UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class MapStretch(BaseModel):
    """One section of a video as drawn on the watch map."""

    start_s: float
    end_s: float
    importance: str
    in_playlist: bool


class RecentVideo(BaseModel):
    video: VideoMetadata
    analysis_id: str
    profile: str
    analyzed_at: datetime
    playlist_s: float = 0.0
    watch_map: list[MapStretch] = []


def watch_map(result: dict) -> list[MapStretch]:
    """Sections with their tier and playlist membership, from a stored analysis."""
    in_playlist = {
        sid for seg in (result.get("playlist") or {}).get("segments", []) for sid in seg.get("section_ids", [])
    }
    return [
        MapStretch(
            start_s=s["section"]["start_s"],
            end_s=s["section"]["end_s"],
            importance=s["importance"],
            in_playlist=s["section"]["id"] in in_playlist,
        )
        for s in result.get("sections", [])
    ]


class Repository:
    def __init__(self, database_url: str):
        if database_url.startswith("sqlite:///"):
            Path(database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {},
        )
        Base.metadata.create_all(self.engine)
        self._session = sessionmaker(self.engine, expire_on_commit=False)

    def session(self) -> Session:
        return self._session()

    # ------------------------------------------------------------------ videos

    def save_video(self, meta: VideoMetadata) -> None:
        with self.session() as db, db.begin():
            db.merge(
                VideoRow(
                    video_id=meta.video_id,
                    title=meta.title,
                    channel=meta.channel,
                    duration_s=meta.duration_s,
                    metadata_json=meta.model_dump(mode="json"),
                )
            )

    def get_video(self, video_id: str) -> VideoMetadata | None:
        with self.session() as db:
            row = db.get(VideoRow, video_id)
            return VideoMetadata.model_validate(row.metadata_json) if row else None

    def save_transcript(self, t: Transcript) -> None:
        with self.session() as db, db.begin():
            db.merge(
                TranscriptRow(
                    video_id=t.video_id,
                    language=t.language,
                    is_generated=t.is_generated,
                    source=t.source,
                    transcript_json=t.model_dump(mode="json"),
                )
            )

    def get_transcript(self, video_id: str) -> Transcript | None:
        with self.session() as db:
            row = db.get(TranscriptRow, video_id)
            return Transcript.model_validate(row.transcript_json) if row else None

    # ---------------------------------------------------------------- analyses

    def create_analysis(
        self,
        analysis_id: str,
        video_id: str,
        prefs: LearningPreferences,
        llm_key: str,
        status: AnalysisStatus = AnalysisStatus.PENDING,
    ) -> None:
        with self.session() as db, db.begin():
            db.add(
                AnalysisRow(
                    id=analysis_id,
                    video_id=video_id,
                    preferences_key=prefs.cache_key(),
                    preferences_json=prefs.model_dump(mode="json"),
                    llm_key=llm_key,
                    status=status.value,
                )
            )

    def update_status(self, analysis_id: str, status: AnalysisStatus, progress: str = "", error: str = "") -> None:
        with self.session() as db, db.begin():
            row = db.get(AnalysisRow, analysis_id)
            if row:
                row.status, row.progress, row.error = status.value, progress, error

    def save_result(self, analysis: VideoAnalysis) -> None:
        with self.session() as db, db.begin():
            row = db.get(AnalysisRow, analysis.id)
            if row is None:
                raise KeyError(analysis.id)
            row.status = analysis.status.value
            row.progress = analysis.progress
            row.error = analysis.error
            row.result_json = analysis.model_dump(mode="json")
            db.execute(delete(SegmentRow).where(SegmentRow.analysis_id == analysis.id))
            for seg in analysis.segments:
                db.add(
                    SegmentRow(
                        analysis_id=analysis.id,
                        segment_id=seg.id,
                        title=seg.title,
                        start_s=seg.start_s,
                        end_s=seg.end_s,
                        importance=seg.importance.value,
                        score=seg.score,
                        in_playlist=seg.in_playlist,
                    )
                )

    def get_analysis(self, analysis_id: str) -> VideoAnalysis | None:
        with self.session() as db:
            row = db.get(AnalysisRow, analysis_id)
            if row is None:
                return None
            if row.result_json:
                return VideoAnalysis.model_validate(row.result_json)
            video = db.get(VideoRow, row.video_id)
            return VideoAnalysis(
                id=row.id,
                video=VideoMetadata.model_validate(video.metadata_json),
                preferences=LearningPreferences.model_validate(row.preferences_json),
                status=AnalysisStatus(row.status),
                progress=row.progress,
                error=row.error,
                created_at=row.created_at.replace(tzinfo=row.created_at.tzinfo or UTC),
            )

    def find_cached(self, video_id: str, prefs: LearningPreferences, llm_key: str) -> str | None:
        """Id of a finished analysis with identical inputs, if any."""
        with self.session() as db:
            stmt = (
                select(AnalysisRow.id)
                .where(
                    AnalysisRow.video_id == video_id,
                    AnalysisRow.preferences_key == prefs.cache_key(),
                    AnalysisRow.llm_key == llm_key,
                    AnalysisRow.status == AnalysisStatus.DONE.value,
                )
                .order_by(AnalysisRow.created_at.desc())
                .limit(1)
            )
            return db.scalar(stmt)

    def find_in_progress(
        self, video_id: str, prefs: LearningPreferences, llm_key: str, *, stale_after: timedelta = timedelta(minutes=20)
    ) -> str | None:
        """Id of a queued or live analysis with identical inputs. Running rows whose
        progress hasn't been updated within `stale_after` belong to a dead process and
        are ignored; queued rows wait without updates."""
        cutoff = datetime.now(UTC) - stale_after
        waiting = [AnalysisStatus.QUEUED.value, AnalysisStatus.PENDING.value, AnalysisStatus.RUNNING.value]
        with self.session() as db:
            stmt = (
                select(AnalysisRow)
                .where(
                    AnalysisRow.video_id == video_id,
                    AnalysisRow.preferences_key == prefs.cache_key(),
                    AnalysisRow.llm_key == llm_key,
                    AnalysisRow.status.in_(waiting),
                )
                .order_by(AnalysisRow.updated_at.desc())
            )
            for row in db.scalars(stmt):
                if row.status == AnalysisStatus.QUEUED.value or _aware(row.updated_at) >= cutoff:
                    return row.id
            return None

    # ------------------------------------------------------------------- queue

    def done_analysis_ids(self) -> list[str]:
        with self.session() as db:
            return list(db.scalars(select(AnalysisRow.id).where(AnalysisRow.status == AnalysisStatus.DONE.value)).all())

    def queued_ids(self) -> list[str]:
        """Queued analyses, oldest first."""
        with self.session() as db:
            stmt = (
                select(AnalysisRow.id)
                .where(AnalysisRow.status == AnalysisStatus.QUEUED.value)
                .order_by(AnalysisRow.created_at)
            )
            return list(db.scalars(stmt))

    def queue_items(self, *, stale_after: timedelta = STALE_AFTER) -> list[dict]:
        """Analyses in progress (most recently active first), then the queue in order."""
        cutoff = datetime.now(UTC) - stale_after
        live = [AnalysisStatus.PENDING.value, AnalysisStatus.RUNNING.value]
        with self.session() as db:
            stmt = (
                select(AnalysisRow, VideoRow)
                .join(VideoRow, VideoRow.video_id == AnalysisRow.video_id)
                .where(AnalysisRow.status.in_([AnalysisStatus.QUEUED.value, *live]))
            )
            rows = [
                (a, v)
                for a, v in db.execute(stmt)
                if a.status == AnalysisStatus.QUEUED.value or _aware(a.updated_at) >= cutoff
            ]
        rows.sort(key=lambda r: (r[0].status == AnalysisStatus.QUEUED.value, _aware(r[0].created_at)))
        return [
            {
                "analysis_id": a.id,
                "video": VideoMetadata.model_validate(v.metadata_json),
                "preferences": LearningPreferences.model_validate(a.preferences_json),
                "status": AnalysisStatus(a.status),
                "progress": a.progress,
                "added_at": _aware(a.created_at),
            }
            for a, v in rows
        ]

    def claim_queued(self, analysis_id: str, progress: str = "Starting") -> bool:
        """Move a queued analysis to pending. False if it isn't queued (any more), so
        the queue worker and a "start now" request can't both start it."""
        with self.session() as db, db.begin():
            result = db.execute(
                update(AnalysisRow)
                .where(AnalysisRow.id == analysis_id, AnalysisRow.status == AnalysisStatus.QUEUED.value)
                .values(status=AnalysisStatus.PENDING.value, progress=progress, updated_at=datetime.now(UTC))
            )
            return result.rowcount == 1

    def get_checkpoint(self, analysis_id: str) -> dict | None:
        with self.session() as db:
            row = db.get(AnalysisCheckpointRow, analysis_id)
            return dict(row.data) if row else None

    def save_checkpoint(self, analysis_id: str, data: dict) -> None:
        with self.session() as db, db.begin():
            row = db.get(AnalysisCheckpointRow, analysis_id)
            if row is None:
                db.add(AnalysisCheckpointRow(analysis_id=analysis_id, step=data.get("step", ""), data=data))
            else:
                row.step, row.data = data.get("step", ""), data

    def delete_checkpoint(self, analysis_id: str) -> None:
        with self.session() as db, db.begin():
            db.execute(delete(AnalysisCheckpointRow).where(AnalysisCheckpointRow.analysis_id == analysis_id))

    def requeue(self, analysis_id: str, progress: str) -> None:
        """Put a paused analysis back in the queue (it resumes from its checkpoint)."""
        with self.session() as db, db.begin():
            row = db.get(AnalysisRow, analysis_id)
            if row is not None:
                row.status, row.progress, row.error = AnalysisStatus.QUEUED.value, progress, ""

    def set_queued_preferences(self, analysis_id: str, prefs: LearningPreferences) -> bool:
        """Change the goal of an analysis that is still waiting in the queue."""
        with self.session() as db, db.begin():
            row = db.get(AnalysisRow, analysis_id)
            if row is None or row.status != AnalysisStatus.QUEUED.value:
                return False
            row.preferences_key = prefs.cache_key()
            row.preferences_json = prefs.model_dump(mode="json")
            return True

    def video_of(self, analysis_id: str) -> str | None:
        with self.session() as db:
            row = db.get(AnalysisRow, analysis_id)
            return row.video_id if row else None

    def remove_queued(self, analysis_id: str) -> bool:
        """Delete an analysis that is still waiting in the queue."""
        with self.session() as db, db.begin():
            row = db.get(AnalysisRow, analysis_id)
            if row is None or row.status != AnalysisStatus.QUEUED.value:
                return False
            db.execute(delete(AnalysisCheckpointRow).where(AnalysisCheckpointRow.analysis_id == analysis_id))
            db.delete(row)
            return True

    def mark_stale_running_failed(self, *, stale_after: timedelta = STALE_AFTER) -> int:
        """Mark analyses abandoned by a dead process as failed.

        Several processes (CLI, API, UI) may share the database, so only rows whose
        progress hasn't been updated within `stale_after` are touched: a live run in
        another process updates its row at every pipeline step.
        """
        cutoff = datetime.now(UTC) - stale_after
        with self.session() as db, db.begin():
            rows = db.scalars(
                select(AnalysisRow).where(
                    AnalysisRow.status.in_([AnalysisStatus.PENDING.value, AnalysisStatus.RUNNING.value])
                )
            ).all()
            notes = db.scalars(
                select(NotesRow).where(
                    NotesRow.status.in_([AnalysisStatus.PENDING.value, AnalysisStatus.RUNNING.value])
                )
            ).all()
            study = db.scalars(
                select(StudySetRow).where(
                    StudySetRow.status.in_([AnalysisStatus.PENDING.value, AnalysisStatus.RUNNING.value])
                )
            ).all()
            plans = db.scalars(
                select(CoursePlanRow).where(
                    CoursePlanRow.status.in_([AnalysisStatus.PENDING.value, AnalysisStatus.RUNNING.value])
                )
            ).all()
            stale = [r for r in [*rows, *notes, *study, *plans] if _aware(r.updated_at) < cutoff]
            # An import thread doesn't survive its process either.
            db.execute(update(CourseRow).where(CourseRow.importing.is_(True)).values(importing=False))
            for row in stale:
                row.status, row.error = AnalysisStatus.FAILED.value, "Interrupted: the process running it stopped."
            if gone := [r.id for r in rows if r in stale]:
                db.execute(delete(AnalysisCheckpointRow).where(AnalysisCheckpointRow.analysis_id.in_(gone)))
            return len(stale)

    def recent(self, limit: int = 10) -> list[RecentVideo]:
        with self.session() as db:
            stmt = (
                select(AnalysisRow, VideoRow)
                .join(VideoRow, VideoRow.video_id == AnalysisRow.video_id)
                .where(AnalysisRow.status == AnalysisStatus.DONE.value)
                .order_by(AnalysisRow.updated_at.desc())
            )
            out: list[RecentVideo] = []
            seen: set[str] = set()
            for a, v in db.execute(stmt):
                if v.video_id in seen:
                    continue
                seen.add(v.video_id)
                out.append(
                    RecentVideo(
                        video=VideoMetadata.model_validate(v.metadata_json),
                        analysis_id=a.id,
                        profile=a.preferences_json.get("profile", ""),
                        analyzed_at=a.updated_at,
                        playlist_s=((a.result_json or {}).get("playlist") or {}).get("total_s", 0.0),
                        watch_map=watch_map(a.result_json or {}),
                    )
                )
                if len(out) >= limit:
                    break
            return out

    def set_segment_completed(self, analysis_id: str, segment_id: str, completed: bool) -> None:
        with self.session() as db, db.begin():
            row = db.scalar(
                select(SegmentRow).where(SegmentRow.analysis_id == analysis_id, SegmentRow.segment_id == segment_id)
            )
            if row:
                row.completed = completed

    def completed_segments(self, analysis_id: str) -> set[str]:
        with self.session() as db:
            rows = db.scalars(
                select(SegmentRow.segment_id).where(
                    SegmentRow.analysis_id == analysis_id, SegmentRow.completed.is_(True)
                )
            )
            return set(rows)

    # ------------------------------------------------------------------- notes

    def create_notes(
        self, notes_id: str, analysis_id: str, scope: NotesScope, segment_ids: list[str], key: str, llm_key: str
    ) -> None:
        with self.session() as db, db.begin():
            db.add(
                NotesRow(
                    id=notes_id,
                    analysis_id=analysis_id,
                    scope=scope.value,
                    scope_key=key,
                    segment_ids=segment_ids,
                    llm_key=llm_key,
                    status=AnalysisStatus.PENDING.value,
                )
            )

    def update_notes_status(self, notes_id: str, status: AnalysisStatus, progress: str = "", error: str = "") -> None:
        with self.session() as db, db.begin():
            row = db.get(NotesRow, notes_id)
            if row:
                row.status, row.progress, row.error = status.value, progress, error

    def save_notes(self, notes: LearningNotes) -> None:
        with self.session() as db, db.begin():
            row = db.get(NotesRow, notes.id)
            if row is None:
                raise KeyError(notes.id)
            row.status, row.progress, row.error = notes.status.value, notes.progress, notes.error
            row.notes_json = notes.model_dump(mode="json", exclude={"glossary", "watch_s"})

    @staticmethod
    def _notes_from_row(row: NotesRow) -> LearningNotes:
        if row.notes_json:
            return LearningNotes.model_validate(row.notes_json)
        return LearningNotes(
            id=row.id,
            analysis_id=row.analysis_id,
            video_id="",
            scope=NotesScope(row.scope),
            segment_ids=row.segment_ids or [],
            status=AnalysisStatus(row.status),
            progress=row.progress,
            error=row.error,
            created_at=_aware(row.created_at),
        )

    def get_notes(self, notes_id: str) -> LearningNotes | None:
        with self.session() as db:
            row = db.get(NotesRow, notes_id)
            return self._notes_from_row(row) if row else None

    def list_notes(self, analysis_id: str) -> list[LearningNotes]:
        """Notes for an analysis, newest first."""
        with self.session() as db:
            rows = db.scalars(
                select(NotesRow).where(NotesRow.analysis_id == analysis_id).order_by(NotesRow.created_at.desc())
            )
            return [self._notes_from_row(r) for r in rows]

    def find_notes(
        self, analysis_id: str, key: str, llm_key: str, *, stale_after: timedelta = STALE_AFTER
    ) -> str | None:
        """Id of finished or live notes with identical inputs."""
        cutoff = datetime.now(UTC) - stale_after
        with self.session() as db:
            rows = db.scalars(
                select(NotesRow)
                .where(
                    NotesRow.analysis_id == analysis_id,
                    NotesRow.scope_key == key,
                    NotesRow.llm_key == llm_key,
                    NotesRow.status != AnalysisStatus.FAILED.value,
                )
                .order_by(NotesRow.created_at.desc())
            )
            for row in rows:
                if row.status == AnalysisStatus.DONE.value or _aware(row.updated_at) >= cutoff:
                    return row.id
            return None

    # ------------------------------------------------------- quiz and flashcards

    def create_study(
        self, study_id: str, analysis_id: str, scope: NotesScope, segment_ids: list[str], key: str, llm_key: str
    ) -> None:
        with self.session() as db, db.begin():
            db.add(
                StudySetRow(
                    id=study_id,
                    analysis_id=analysis_id,
                    scope=scope.value,
                    scope_key=key,
                    segment_ids=segment_ids,
                    llm_key=llm_key,
                    status=AnalysisStatus.PENDING.value,
                )
            )

    def update_study_status(self, study_id: str, status: AnalysisStatus, progress: str = "", error: str = "") -> None:
        with self.session() as db, db.begin():
            row = db.get(StudySetRow, study_id)
            if row:
                row.status, row.progress, row.error = status.value, progress, error

    def save_study(self, study: StudySet) -> None:
        with self.session() as db, db.begin():
            row = db.get(StudySetRow, study.id)
            if row is None:
                raise KeyError(study.id)
            row.status, row.progress, row.error = study.status.value, study.progress, study.error
            row.study_json = study.model_dump(mode="json")

    @staticmethod
    def _study_from_row(row: StudySetRow) -> StudySet:
        # A course's sets have scope keys "course|<course id>|<sections hash>".
        course_id = row.scope_key.split("|")[1] if row.scope_key.startswith("course|") else ""
        if row.study_json:
            study = StudySet.model_validate(row.study_json)
            study.course_id = course_id
            return study
        return StudySet(
            id=row.id,
            analysis_id=row.analysis_id,
            video_id="",
            scope=NotesScope(row.scope),
            segment_ids=row.segment_ids or [],
            course_id=course_id,
            status=AnalysisStatus(row.status),
            progress=row.progress,
            error=row.error,
            created_at=_aware(row.created_at),
        )

    def course_study_sets(self, course_id: str) -> list[StudySet]:
        """A course's quiz and flashcard sets (one per lecture and build), newest first."""
        with self.session() as db:
            rows = db.scalars(
                select(StudySetRow)
                .where(StudySetRow.scope_key.startswith(f"course|{course_id}|"))
                .order_by(StudySetRow.created_at.desc())
            )
            return [self._study_from_row(r) for r in rows]

    def get_study(self, study_id: str) -> StudySet | None:
        with self.session() as db:
            row = db.get(StudySetRow, study_id)
            return self._study_from_row(row) if row else None

    def list_study(self, analysis_id: str) -> list[StudySet]:
        """Study sets for an analysis, newest first."""
        with self.session() as db:
            rows = db.scalars(
                select(StudySetRow)
                .where(StudySetRow.analysis_id == analysis_id)
                .order_by(StudySetRow.created_at.desc())
            )
            return [self._study_from_row(r) for r in rows]

    def find_study(
        self, analysis_id: str, key: str, llm_key: str, *, stale_after: timedelta = STALE_AFTER
    ) -> str | None:
        """Id of a finished or live study set with identical inputs."""
        cutoff = datetime.now(UTC) - stale_after
        with self.session() as db:
            rows = db.scalars(
                select(StudySetRow)
                .where(
                    StudySetRow.analysis_id == analysis_id,
                    StudySetRow.scope_key == key,
                    StudySetRow.llm_key == llm_key,
                    StudySetRow.status != AnalysisStatus.FAILED.value,
                )
                .order_by(StudySetRow.created_at.desc())
            )
            for row in rows:
                if row.status == AnalysisStatus.DONE.value or _aware(row.updated_at) >= cutoff:
                    return row.id
            return None

    def decks(self) -> list[tuple[StudySet, VideoMetadata]]:
        """The newest finished study set of each analysis (its cards are the ones
        reviewed; writing a new set replaces the older one's cards), and of each
        analysis within each course (a course's cards are their own deck), newest first."""
        with self.session() as db:
            stmt = (
                select(StudySetRow, VideoRow)
                .join(AnalysisRow, AnalysisRow.id == StudySetRow.analysis_id)
                .join(VideoRow, VideoRow.video_id == AnalysisRow.video_id)
                .where(StudySetRow.status == AnalysisStatus.DONE.value)
                .order_by(StudySetRow.created_at.desc())
            )
            out, seen = [], set()
            for row, video in db.execute(stmt):
                course = row.scope_key.split("|")[1] if row.scope_key.startswith("course|") else ""
                if (row.analysis_id, course) in seen:
                    continue
                seen.add((row.analysis_id, course))
                out.append((self._study_from_row(row), VideoMetadata.model_validate(video.metadata_json)))
            return out

    def save_attempt(self, attempt: QuizAttempt) -> None:
        with self.session() as db, db.begin():
            db.add(
                QuizAttemptRow(
                    id=attempt.id,
                    study_id=attempt.study_id,
                    attempt_json=attempt.model_dump(mode="json"),
                    created_at=attempt.created_at,
                )
            )

    def list_attempts(self, study_id: str) -> list[QuizAttempt]:
        """Quiz attempts, newest first."""
        with self.session() as db:
            rows = db.scalars(
                select(QuizAttemptRow)
                .where(QuizAttemptRow.study_id == study_id)
                .order_by(QuizAttemptRow.created_at.desc())
            )
            return [QuizAttempt.model_validate(r.attempt_json) for r in rows]

    def card_states(self, study_ids: list[str]) -> dict[tuple[str, str], CardState]:
        with self.session() as db:
            rows = db.scalars(select(CardStateRow).where(CardStateRow.study_id.in_(study_ids)))
            return {
                (r.study_id, r.card_id): CardState(
                    study_id=r.study_id,
                    card_id=r.card_id,
                    box=r.box,
                    due_at=_aware(r.due_at) if r.due_at else None,
                    reviews=r.reviews,
                    lapses=r.lapses,
                )
                for r in rows
            }

    def save_card_state(self, state: CardState) -> None:
        with self.session() as db, db.begin():
            db.merge(
                CardStateRow(
                    study_id=state.study_id,
                    card_id=state.card_id,
                    box=state.box,
                    due_at=state.due_at,
                    reviews=state.reviews,
                    lapses=state.lapses,
                )
            )

    # --------------------------------------------------------------- questions

    def save_answer(self, answer: Answer) -> None:
        with self.session() as db, db.begin():
            db.add(
                QuestionRow(
                    id=answer.id,
                    analysis_id=answer.analysis_id,
                    question=answer.question,
                    answer_json=answer.model_dump(mode="json"),
                    created_at=answer.created_at,
                )
            )

    def list_answers(self, analysis_id: str) -> list[Answer]:
        """The conversation about an analysis, oldest first."""
        with self.session() as db:
            rows = db.scalars(
                select(QuestionRow).where(QuestionRow.analysis_id == analysis_id).order_by(QuestionRow.created_at)
            )
            return [Answer.model_validate(r.answer_json) for r in rows]

    def clear_answers(self, analysis_id: str) -> int:
        with self.session() as db, db.begin():
            return db.execute(delete(QuestionRow).where(QuestionRow.analysis_id == analysis_id)).rowcount

    # ---------------------------------------------------------------- settings

    def get_setting(self, key: str) -> dict | None:
        with self.session() as db:
            row = db.get(SettingRow, key)
            return row.value if row else None

    def set_setting(self, key: str, value: dict) -> None:
        with self.session() as db, db.begin():
            db.merge(SettingRow(key=key, value=value))

    # -------------------------------------------------------------- embeddings

    def get_embeddings(self, video_id: str, model: str, blocks_key: str) -> list[list[float]] | None:
        with self.session() as db:
            row = db.get(EmbeddingRow, (video_id, model))
            return row.vectors if row and row.blocks_key == blocks_key else None

    def save_embeddings(self, video_id: str, model: str, blocks_key: str, vectors: list[list[float]]) -> None:
        with self.session() as db, db.begin():
            db.merge(EmbeddingRow(video_id=video_id, model=model, blocks_key=blocks_key, vectors=vectors))

    # ----------------------------------------------------------------- courses

    @staticmethod
    def _course_dict(row: CourseRow) -> dict:
        return {
            "id": row.id,
            "playlist_id": row.playlist_id,
            "title": row.title,
            "channel": row.channel,
            "preferences": LearningPreferences.model_validate(row.preferences_json),
            "lectures": list(row.lectures_json or []),
            "errors": dict(row.errors_json or {}),
            "unavailable": row.unavailable,
            "importing": row.importing,
            "created_at": _aware(row.created_at),
        }

    def create_course(
        self,
        course_id: str,
        *,
        title: str,
        channel: str,
        playlist_id: str,
        prefs: LearningPreferences,
        lectures: list[dict],
        unavailable: int = 0,
        importing: bool = False,
    ) -> None:
        with self.session() as db, db.begin():
            db.add(
                CourseRow(
                    id=course_id,
                    playlist_id=playlist_id,
                    title=title,
                    channel=channel,
                    preferences_key=prefs.cache_key(),
                    preferences_json=prefs.model_dump(mode="json"),
                    lectures_json=lectures,
                    errors_json={},
                    unavailable=unavailable,
                    importing=importing,
                )
            )

    def find_course(self, playlist_id: str, prefs: LearningPreferences) -> str | None:
        with self.session() as db:
            return db.scalar(
                select(CourseRow.id)
                .where(CourseRow.playlist_id == playlist_id, CourseRow.preferences_key == prefs.cache_key())
                .order_by(CourseRow.created_at.desc())
                .limit(1)
            )

    def update_course(self, course_id: str, **fields) -> None:
        """Set `lectures`, `errors`, `importing`, `title`, `channel` or `unavailable`."""
        names = {"lectures": "lectures_json", "errors": "errors_json"}
        with self.session() as db, db.begin():
            row = db.get(CourseRow, course_id)
            if row is None:
                raise KeyError(course_id)
            for k, v in fields.items():
                setattr(row, names.get(k, k), v)

    def set_course_error(self, course_id: str, video_id: str, error: str) -> None:
        with self.session() as db, db.begin():
            row = db.get(CourseRow, course_id)
            if row is not None:
                errors = dict(row.errors_json or {})
                if error:
                    errors[video_id] = error
                else:
                    errors.pop(video_id, None)
                row.errors_json = errors

    def get_course(self, course_id: str) -> dict | None:
        with self.session() as db:
            row = db.get(CourseRow, course_id)
            return self._course_dict(row) if row else None

    def list_courses(self) -> list[dict]:
        with self.session() as db:
            rows = db.scalars(select(CourseRow).order_by(CourseRow.updated_at.desc()))
            return [self._course_dict(r) for r in rows]

    def delete_course(self, course_id: str) -> bool:
        with self.session() as db, db.begin():
            db.execute(delete(CourseWatchedRow).where(CourseWatchedRow.course_id == course_id))
            db.execute(delete(CourseQuestionRow).where(CourseQuestionRow.course_id == course_id))
            db.execute(delete(CoursePlanRow).where(CoursePlanRow.course_id == course_id))
            db.execute(delete(CourseCustomPlaylistRow).where(CourseCustomPlaylistRow.course_id == course_id))
            return db.execute(delete(CourseRow).where(CourseRow.id == course_id)).rowcount > 0

    def analyses_for_videos(self, video_ids: list[str]) -> list[dict]:
        """Every analysis of these videos (without results), newest first."""
        if not video_ids:
            return []
        with self.session() as db:
            stmt = (
                select(
                    AnalysisRow.id,
                    AnalysisRow.video_id,
                    AnalysisRow.preferences_key,
                    AnalysisRow.status,
                    AnalysisRow.error,
                    AnalysisRow.updated_at,
                )
                .where(AnalysisRow.video_id.in_(video_ids))
                .order_by(AnalysisRow.updated_at.desc())
            )
            return [
                {
                    "id": r.id,
                    "video_id": r.video_id,
                    "preferences_key": r.preferences_key,
                    "status": AnalysisStatus(r.status),
                    "error": r.error,
                    "updated_at": _aware(r.updated_at),
                }
                for r in db.execute(stmt)
            ]

    def create_course_plan(self, plan: CoursePlan, inputs_key: str) -> None:
        """A pending plan; its budget and analysis ids are the inputs for the run."""
        with self.session() as db, db.begin():
            db.add(
                CoursePlanRow(
                    id=plan.id,
                    course_id=plan.course_id,
                    inputs_key=inputs_key,
                    status=AnalysisStatus.PENDING.value,
                    plan_json=plan.model_dump(mode="json"),
                )
            )

    def update_course_plan_status(
        self, plan_id: str, status: AnalysisStatus, progress: str = "", error: str = ""
    ) -> None:
        with self.session() as db, db.begin():
            row = db.get(CoursePlanRow, plan_id)
            if row:
                row.status, row.progress, row.error = status.value, progress, error

    def save_course_plan(self, plan: CoursePlan) -> None:
        with self.session() as db, db.begin():
            row = db.get(CoursePlanRow, plan.id)
            if row is None:
                raise KeyError(plan.id)
            row.status, row.progress, row.error = plan.status.value, plan.progress, plan.error
            row.plan_json = plan.model_dump(mode="json")

    @staticmethod
    def _plan_from_row(row: CoursePlanRow) -> CoursePlan:
        """The saved plan (a pending one holds its inputs), with the row's live status."""
        base = (
            CoursePlan.model_validate(row.plan_json)
            if row.plan_json
            else CoursePlan(id=row.id, course_id=row.course_id)
        )
        return base.model_copy(
            update={
                "status": AnalysisStatus(row.status),
                "progress": row.progress,
                "error": row.error,
                "created_at": _aware(row.created_at),
            }
        )

    def get_course_plan(self, plan_id: str) -> CoursePlan | None:
        with self.session() as db:
            row = db.get(CoursePlanRow, plan_id)
            return self._plan_from_row(row) if row else None

    def latest_course_plan(self, course_id: str) -> CoursePlan | None:
        """The newest plan that isn't failed, else the newest one."""
        with self.session() as db:
            rows = list(
                db.scalars(
                    select(CoursePlanRow)
                    .where(CoursePlanRow.course_id == course_id)
                    .order_by(CoursePlanRow.created_at.desc())
                )
            )
        if not rows:
            return None
        live = next((r for r in rows if r.status != AnalysisStatus.FAILED.value), rows[0])
        # A failed rebuild is shown (with its error) only when it's the newest.
        return self._plan_from_row(rows[0] if rows[0].status == AnalysisStatus.FAILED.value else live)

    def find_course_plan(self, course_id: str, inputs_key: str, *, stale_after: timedelta = STALE_AFTER) -> str | None:
        cutoff = datetime.now(UTC) - stale_after
        with self.session() as db:
            rows = db.scalars(
                select(CoursePlanRow)
                .where(
                    CoursePlanRow.course_id == course_id,
                    CoursePlanRow.inputs_key == inputs_key,
                    CoursePlanRow.status != AnalysisStatus.FAILED.value,
                )
                .order_by(CoursePlanRow.created_at.desc())
            )
            for row in rows:
                if row.status == AnalysisStatus.DONE.value or _aware(row.updated_at) >= cutoff:
                    return row.id
            return None

    def course_watched(self, course_id: str) -> set[str]:
        with self.session() as db:
            return set(db.scalars(select(CourseWatchedRow.part_key).where(CourseWatchedRow.course_id == course_id)))

    def set_course_watched(self, course_id: str, part_key: str, watched: bool) -> None:
        with self.session() as db, db.begin():
            if watched:
                db.merge(CourseWatchedRow(course_id=course_id, part_key=part_key))
            else:
                db.execute(
                    delete(CourseWatchedRow).where(
                        CourseWatchedRow.course_id == course_id, CourseWatchedRow.part_key == part_key
                    )
                )

    def save_course_answer(self, answer: CourseAnswer) -> None:
        with self.session() as db, db.begin():
            db.add(
                CourseQuestionRow(
                    id=answer.id,
                    course_id=answer.course_id,
                    question=answer.question,
                    answer_json=answer.model_dump(mode="json"),
                    created_at=answer.created_at,
                )
            )

    def list_course_answers(self, course_id: str) -> list[CourseAnswer]:
        """The conversation about a course, oldest first."""
        with self.session() as db:
            rows = db.scalars(
                select(CourseQuestionRow)
                .where(CourseQuestionRow.course_id == course_id)
                .order_by(CourseQuestionRow.created_at)
            )
            return [CourseAnswer.model_validate(r.answer_json) for r in rows]

    def clear_course_answers(self, course_id: str) -> int:
        with self.session() as db, db.begin():
            return db.execute(delete(CourseQuestionRow).where(CourseQuestionRow.course_id == course_id)).rowcount

    def custom_playlist(self, analysis_id: str) -> list[str] | None:
        with self.session() as db:
            row = db.get(CustomPlaylistRow, analysis_id)
            return list(row.segment_ids) if row else None

    def set_custom_playlist(self, analysis_id: str, segment_ids: list[str] | None) -> None:
        with self.session() as db, db.begin():
            row = db.get(CustomPlaylistRow, analysis_id)
            if segment_ids is None:
                if row is not None:
                    db.delete(row)
            elif row is None:
                db.add(CustomPlaylistRow(analysis_id=analysis_id, segment_ids=segment_ids))
            else:
                row.segment_ids = segment_ids

    def course_custom_playlist(self, course_id: str) -> list[str] | None:
        with self.session() as db:
            row = db.get(CourseCustomPlaylistRow, course_id)
            return list(row.sections) if row else None

    def set_course_custom_playlist(self, course_id: str, sections: list[str] | None) -> None:
        with self.session() as db, db.begin():
            row = db.get(CourseCustomPlaylistRow, course_id)
            if sections is None:
                if row is not None:
                    db.delete(row)
            elif row is None:
                db.add(CourseCustomPlaylistRow(course_id=course_id, sections=sections))
            else:
                row.sections = sections
