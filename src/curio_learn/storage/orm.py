"""SQLAlchemy tables. SQLite by default; PostgreSQL via CURIO_DATABASE_URL."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class VideoRow(Base):
    __tablename__ = "videos"

    video_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text, default="")
    duration_s: Mapped[float] = mapped_column(Float)
    metadata_json: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class TranscriptRow(Base):
    __tablename__ = "transcripts"

    video_id: Mapped[str] = mapped_column(ForeignKey("videos.video_id"), primary_key=True)
    language: Mapped[str] = mapped_column(String(16))
    is_generated: Mapped[bool] = mapped_column(Boolean)
    source: Mapped[str] = mapped_column(String(64))
    transcript_json: Mapped[dict] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AnalysisRow(Base):
    __tablename__ = "analyses"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.video_id"), index=True)
    preferences_key: Mapped[str] = mapped_column(Text, index=True)
    preferences_json: Mapped[dict] = mapped_column(JSON)
    llm_key: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(16), index=True)
    progress: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class AnalysisCheckpointRow(Base):
    """The finished steps of an analysis that was paused, so it can pick up there."""

    __tablename__ = "analysis_checkpoints"

    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id", ondelete="CASCADE"), primary_key=True)
    step: Mapped[str] = mapped_column(String(16))
    data: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class SegmentRow(Base):
    """One row per learning segment, so progress tracking can attach to it later."""

    __tablename__ = "segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id", ondelete="CASCADE"), index=True)
    segment_id: Mapped[str] = mapped_column(String(8))
    title: Mapped[str] = mapped_column(Text)
    start_s: Mapped[float] = mapped_column(Float)
    end_s: Mapped[float] = mapped_column(Float)
    importance: Mapped[str] = mapped_column(String(16))
    score: Mapped[float] = mapped_column(Float)
    in_playlist: Mapped[bool] = mapped_column(Boolean)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)


class NotesRow(Base):
    """Learning notes for one analysis and scope (playlist, whole video, or picked segments)."""

    __tablename__ = "notes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(String(16))
    scope_key: Mapped[str] = mapped_column(Text, index=True)
    segment_ids: Mapped[list] = mapped_column(JSON, default=list)
    llm_key: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(16), index=True)
    progress: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    notes_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class QuestionRow(Base):
    """One question and its answer, in the conversation about an analysis."""

    __tablename__ = "questions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text)
    answer_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EmbeddingRow(Base):
    """Embedding vectors of a video's transcript blocks, for one embedding model."""

    __tablename__ = "embeddings"

    video_id: Mapped[str] = mapped_column(ForeignKey("videos.video_id"), primary_key=True)
    model: Mapped[str] = mapped_column(String(100), primary_key=True)
    # Hash of the block texts, so a changed transcript or blocking invalidates the vectors.
    blocks_key: Mapped[str] = mapped_column(String(64))
    vectors: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SettingRow(Base):
    """Small app-wide values chosen in the UI, such as the default learning goal."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class StudySetRow(Base):
    """A quiz and flashcards for one analysis and scope."""

    __tablename__ = "study_sets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(String(16))
    scope_key: Mapped[str] = mapped_column(Text, index=True)
    segment_ids: Mapped[list] = mapped_column(JSON, default=list)
    llm_key: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(16), index=True)
    progress: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    study_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class QuizAttemptRow(Base):
    __tablename__ = "quiz_attempts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    study_id: Mapped[str] = mapped_column(ForeignKey("study_sets.id", ondelete="CASCADE"), index=True)
    attempt_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CardStateRow(Base):
    """Spaced-repetition state of one flashcard. No row = a new card."""

    __tablename__ = "card_states"

    study_id: Mapped[str] = mapped_column(ForeignKey("study_sets.id", ondelete="CASCADE"), primary_key=True)
    card_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    box: Mapped[int] = mapped_column(Integer, default=0)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    reviews: Mapped[int] = mapped_column(Integer, default=0)
    lapses: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class CourseRow(Base):
    """A course: lectures in order (from a YouTube playlist or grouped by hand) and a goal."""

    __tablename__ = "courses"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    playlist_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    title: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text, default="")
    preferences_key: Mapped[str] = mapped_column(Text)
    preferences_json: Mapped[dict] = mapped_column(JSON)
    # [{video_id, title, duration_s}] in course order
    lectures_json: Mapped[list] = mapped_column(JSON)
    # {video_id: why it couldn't be queued}
    errors_json: Mapped[dict] = mapped_column(JSON, default=dict)
    unavailable: Mapped[int] = mapped_column(Integer, default=0)
    importing: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class CoursePlanRow(Base):
    """A course playlist across lectures, with repeats and the checklist."""

    __tablename__ = "course_plans"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), index=True)
    inputs_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), index=True)
    progress: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    plan_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class CourseWatchedRow(Base):
    """A course playlist part the learner has watched (keyed by video and start)."""

    __tablename__ = "course_watched"

    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True)
    part_key: Mapped[str] = mapped_column(String(48), primary_key=True)


class CourseQuestionRow(Base):
    """One question and its answer, in the conversation about a course."""

    __tablename__ = "course_questions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text)
    answer_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CustomPlaylistRow(Base):
    """The learner's own playlist for an analysis: segments they chose, in time order."""

    __tablename__ = "custom_playlists"

    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id", ondelete="CASCADE"), primary_key=True)
    segment_ids: Mapped[list] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class CurriculumRow(Base):
    """A course that is a curriculum: one goal across courses and videos, ordered by what
    builds on what."""

    __tablename__ = "curricula"

    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True)
    goal: Mapped[str] = mapped_column(Text)
    order_mode: Mapped[str] = mapped_column(String(16), default="lectures")  # or "sections"
    groups: Mapped[dict] = mapped_column(JSON, default=dict)  # video id -> its course id ("" alone)
    auto_order: Mapped[bool] = mapped_column(Boolean, default=True)  # off once the learner reorders
    notes: Mapped[list] = mapped_column(JSON, default=list)  # why sources moved
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class CoursePinsRow(Base):
    """Sections the learner pinned: kept in Curio's course playlist whenever it's built,
    as "analysis id:section id"."""

    __tablename__ = "course_pins"

    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True)
    sections: Mapped[list] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class CourseCustomPlaylistRow(Base):
    """The learner's own course playlist: sections they chose, as "analysis id:section id"."""

    __tablename__ = "course_custom_playlists"

    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True)
    sections: Mapped[list] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
