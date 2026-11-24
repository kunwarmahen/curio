"""Quizzes and flashcards, grounded in the transcript like learning notes.

Sections are sent in batches as numbered sentences, with how many questions and
cards to write for each (more for longer sections). Every question and card names
the sentence that answers it; code turns that into the sentence's cue time, so
"watch the answer" always plays the real moment.

The model writes the wrong options and the right one in separate fields (wrong
first, so it can keep the right one from being the longest, a giveaway). Code keeps
2-4 distinct wrong options and shuffles them with the right one deterministically.
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

from pydantic import BaseModel

from curio_learn.analysis import prompts
from curio_learn.analysis.notes import _batches, scope_sections, section_units
from curio_learn.analysis.profiles import learner_brief
from curio_learn.domain import (
    AnalysisStatus,
    CardGrade,
    CardState,
    Flashcard,
    LLMInfo,
    NotesScope,
    QuizQuestion,
    Section,
    StudySet,
    VideoAnalysis,
    fmt_ts,
)
from curio_learn.errors import LLMError
from curio_learn.llm.base import LLMProvider, generate_structured
from curio_learn.processing.normalize import Utterance

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


class QuestionOut(BaseModel):
    question: str
    # Wrong options before the right one, so the model writes (and sizes) them first.
    # No index: a separate field can't point at the wrong option.
    wrong_options: list[str]
    right_option: str
    explanation: str
    sentence: int


class CardOut(BaseModel):
    front: str
    back: str
    sentence: int


class ReviewedQuestionOut(BaseModel):
    id: str
    wrong_options: list[str]
    right_option: str


class QuizReviewOut(BaseModel):
    questions: list[ReviewedQuestionOut]


class SectionStudyOut(BaseModel):
    section_id: str
    questions: list[QuestionOut]
    cards: list[CardOut]


class StudyOut(BaseModel):
    sections: list[SectionStudyOut]


def counts(section: Section) -> tuple[int, int]:
    """Questions and cards to ask for: more for longer sections."""
    minutes = (section.end_s - section.start_s) / 60
    if minutes < 3:
        return 1, 2
    if minutes < 8:
        return 2, 3
    return 3, 4


def _render(section: Section, units: Sequence[Utterance]) -> str:
    q, c = counts(section)
    lines = "\n".join(f"{k}. {u.text}" for k, u in enumerate(units, 1))
    span = f"{fmt_ts(section.start_s)}–{fmt_ts(section.end_s)}"
    return (
        f"### {section.id} [{span}] {section.kind.value}: {section.title} "
        f"(up to {q} question{'s' if q > 1 else ''}, {c} cards)\n{lines}"
    )


def clean_question(q: QuestionOut) -> tuple[str, list[str], int] | None:
    """Question text, shuffled options and the index of the right one; None if unusable."""
    text = " ".join(q.question.split())
    right = " ".join(q.right_option.split())
    wrong: list[str] = []
    for o in q.wrong_options:
        o = " ".join(o.split())
        if o and o.lower() != right.lower() and o.lower() not in {x.lower() for x in wrong}:
            wrong.append(o)
    if not text or not right or len(wrong) < 2:
        return None
    options = [right, *wrong[:4]]
    rng = random.Random(hashlib.sha256(text.encode()).digest())
    rng.shuffle(options)
    return text, options, options.index(right)


def word_range(q: QuizQuestion) -> tuple[int, int]:
    """Words every option should have: around the longest wrong option, 6-18 words."""
    longest_wrong = max(len(o.split()) for i, o in enumerate(q.options) if i != q.answer)
    target = min(max(longest_wrong, 6), 16)
    return target - 2, target + 2


def _too_long(q: QuizQuestion) -> bool:
    right = len(q.options[q.answer])
    return right > max(len(o) for i, o in enumerate(q.options) if i != q.answer)


def review_questions(
    provider: LLMProvider, *, model: str, questions: list[QuizQuestion], retries: int = 2
) -> tuple[int, int]:
    """Second look at the questions, in one call: replace "wrong" options that are also
    right, top up to three wrong options, and shorten right answers that are longer than
    every wrong one (a giveaway). Updates `questions` in place; returns how many changed
    and how many still have the longest right answer."""
    blocks = []
    for q in questions:
        wrong = [o for i, o in enumerate(q.options) if i != q.answer]
        lo, hi = word_range(q)
        blocks.append(
            f"id: {q.id}\nquestion: {q.question}\nword range for every option: {lo}-{hi} words\n"
            f"right answer: {q.options[q.answer]}\nwrong options: "
            + " | ".join(wrong)
            + f"\nexplanation: {q.explanation}"
        )
    out = generate_structured(
        provider,
        QuizReviewOut,
        model=model,
        system=prompts.QUIZ_REVIEW_SYSTEM,
        user=prompts.quiz_review_user(questions="\n\n".join(blocks)),
        max_tokens=160 * len(questions) + 400,
        retries=retries,
        task="study[review]",
    )
    by_id = {q.id: q for q in questions}
    changed = 0
    for r in out.questions:
        q = by_id.get(r.id.strip().lower())
        if q is None:
            continue
        cleaned = clean_question(
            QuestionOut(
                question=q.question,
                wrong_options=r.wrong_options,
                right_option=r.right_option,
                explanation="",
                sentence=0,
            )
        )
        if cleaned is None:
            continue
        _, options, answer = cleaned
        if (options, answer) != (q.options, q.answer):
            q.options, q.answer = options, answer
            changed += 1
    return changed, sum(_too_long(q) for q in questions)


def generate_study(
    provider: LLMProvider,
    *,
    model: str,
    analysis: VideoAnalysis,
    units: Sequence[Utterance],
    scope: NotesScope,
    segment_ids: Sequence[str] = (),
    study_id: str,
    batch_tokens: int,
    retries: int = 2,
    progress: Progress = lambda _: None,
) -> StudySet:
    started = time.monotonic()
    sections = scope_sections(analysis, scope, segment_ids)
    shared = f"Video: {analysis.video.title}\n\n{learner_brief(analysis.preferences)}"
    units_by = {s.id: section_units(s, units) for s in sections}
    by_id = {s.id: s for s in sections}
    results: dict[str, SectionStudyOut] = {}
    warnings: list[str] = []

    def run(batch: list[Section], label: str) -> None:
        ids = [s.id for s in batch]
        q_max = sum(counts(s)[0] for s in batch)
        c_max = sum(counts(s)[1] for s in batch)
        out = generate_structured(
            provider,
            StudyOut,
            model=model,
            system=prompts.STUDY_SYSTEM,
            user=prompts.study_user(sections_text="\n\n".join(_render(s, units_by[s.id]) for s in batch), ids=ids),
            shared_prefix=shared,
            max_tokens=260 * q_max + 100 * c_max + 600,
            retries=retries,
            task=label,
        )
        for entry in out.sections:
            sid = entry.section_id.strip().upper()
            if sid in by_id and sid not in results:
                results[sid] = entry

    batches = _batches(sections, units_by, batch_tokens)
    for n, batch in enumerate(batches, 1):
        progress(f"Writing questions and cards for {batch[0].id}–{batch[-1].id} (batch {n} of {len(batches)})")
        run(batch, f"study[{n}]")
    missing = [s for s in sections if s.id not in results]
    if missing:
        progress(f"Writing questions and cards for {len(missing)} section(s) the model skipped")
        try:
            run(missing, "study[missing]")
        except LLMError as exc:
            warnings.append(f"Retrying skipped sections failed: {exc}")

    questions: list[QuizQuestion] = []
    cards: list[Flashcard] = []
    bad_refs = bad_questions = 0
    for s in sections:
        entry = results.get(s.id)
        if entry is None:
            warnings.append(f"No questions or cards for {s.id} ('{s.title}').")
            continue
        su = units_by[s.id]

        def at(n: int, s: Section = s, su: list[Utterance] = su) -> float:
            nonlocal bad_refs
            if 1 <= n <= len(su):
                return su[n - 1].start_s
            bad_refs += 1
            return s.start_s

        q_max, c_max = counts(s)
        for q in entry.questions[:q_max]:
            cleaned = clean_question(q)
            if cleaned is None:
                bad_questions += 1
                continue
            text, options, answer = cleaned
            questions.append(
                QuizQuestion(
                    id="",
                    question=text,
                    options=options,
                    answer=answer,
                    explanation=q.explanation.strip(),
                    section_id=s.id,
                    start_s=at(q.sentence),
                )
            )
        for c in entry.cards[:c_max]:
            front, back = " ".join(c.front.split()), " ".join(c.back.split())
            if front and back:
                cards.append(Flashcard(id="", front=front, back=back, section_id=s.id, start_s=at(c.sentence)))

    questions.sort(key=lambda q: q.start_s)
    cards.sort(key=lambda c: c.start_s)
    for i, q in enumerate(questions, 1):
        q.id = f"q{i}"
    for i, c in enumerate(cards, 1):
        c.id = f"c{i}"
    if questions:
        progress("Checking the questions")
        try:
            changed, long_left = review_questions(provider, model=model, questions=questions, retries=retries)
            log.info("Quiz review changed %d of %d questions; %d still longest", changed, len(questions), long_left)
        except LLMError as exc:
            warnings.append(f"Couldn't double-check the questions ({exc}).")
    if bad_questions:
        warnings.append(f"Dropped {bad_questions} question(s) without a valid set of options.")
    if bad_refs:
        warnings.append(
            f"{bad_refs} item(s) cited a sentence that doesn't exist; they link to their section's start instead."
        )
    if not questions and not cards:
        warnings.append("The model wrote no questions or cards for these sections.")

    return StudySet(
        id=study_id,
        analysis_id=analysis.id,
        video_id=analysis.video.video_id,
        scope=scope,
        segment_ids=sorted(set(segment_ids)) if scope is NotesScope.SELECTED else [],
        status=AnalysisStatus.DONE,
        progress="Done",
        questions=questions,
        cards=cards,
        warnings=warnings,
        llm=LLMInfo(provider=provider.name, models={"study": model}),
        elapsed_s=round(time.monotonic() - started, 1),
    )


# ------------------------------------------------------- spaced repetition

# Days until a card in each Leitner box is due again. Box 0 is a new card.
BOX_DAYS = {1: 1, 2: 3, 3: 7, 4: 16, 5: 35, 6: 90}
AGAIN_AFTER = timedelta(minutes=10)


def schedule(state: CardState, grade: CardGrade, now: datetime) -> CardState:
    """The card's state after a review: forgotten cards go back to box 1 and come
    back in ten minutes; remembered ones move up one box (two when easy)."""
    if grade is CardGrade.AGAIN:
        box, due, lapses = 1, now + AGAIN_AFTER, state.lapses + (1 if state.box else 0)
    else:
        box = min(state.box + (2 if grade is CardGrade.EASY else 1), max(BOX_DAYS))
        due, lapses = now + timedelta(days=BOX_DAYS[box]), state.lapses
    return state.model_copy(update={"box": box, "due_at": due, "reviews": state.reviews + 1, "lapses": lapses})


def is_due(state: CardState, now: datetime) -> bool:
    return state.due_at is None or state.due_at <= now
