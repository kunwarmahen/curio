import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from curio_learn.analysis.study import BOX_DAYS, QuestionOut, clean_question, schedule
from curio_learn.api.app import create_app
from curio_learn.domain import AnalysisStatus, CardGrade, CardState, LearningPreferences, NotesScope
from curio_learn.errors import AnalysisError
from curio_learn.processing.normalize import boundary_units
from curio_learn.service import CurioService

from .conftest import FakeLLM, FakeMetadata, FakeTranscripts

URL = "https://www.youtube.com/watch?v=e_m99aR7_3Q"
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def service(settings):
    return CurioService(
        settings, llm=FakeLLM(kinds={0: "intro", 3: "sponsor"}), metadata=FakeMetadata(), transcripts=FakeTranscripts()
    )


@pytest.fixture
def analysis(service):
    return service.analyze(URL)


def q(right, wrong, text="Which is right?"):
    return QuestionOut(question=text, wrong_options=wrong, right_option=right, explanation="", sentence=1)


def test_clean_question_shuffles_and_tracks_the_answer():
    text, options, answer = clean_question(q("Right", ["Wrong a", "wrong A", "Wrong b", " ", "right"]))
    assert text == "Which is right?" and options[answer] == "Right"
    assert sorted(options) == ["Right", "Wrong a", "Wrong b"]  # blank, duplicates and the answer dropped
    assert clean_question(q("a", ["b"])) is None  # too few options
    assert clean_question(q(" ", ["b", "c", "d"])) is None  # no right answer
    assert len(clean_question(q("R", list("abcdefg")))[1]) == 5  # at most 5 options
    # Deterministic, and the right answer doesn't always land first.
    positions = {clean_question(q("R", ["x", "y", "z"], text=f"Question {i}?"))[2] for i in range(20)}
    assert len(positions) > 1
    assert clean_question(q("R", ["x", "y", "z"])) == clean_question(q("R", ["x", "y", "z"]))


def test_schedule():
    new = CardState(study_id="s", card_id="c1")
    good = schedule(new, CardGrade.GOOD, NOW)
    assert good.box == 1 and good.due_at == NOW + timedelta(days=BOX_DAYS[1]) and good.reviews == 1
    easy = schedule(good, CardGrade.EASY, NOW)
    assert easy.box == 3 and easy.due_at == NOW + timedelta(days=7)
    again = schedule(easy, CardGrade.AGAIN, NOW)
    assert again.box == 1 and again.due_at == NOW + timedelta(minutes=10) and again.lapses == 1
    assert schedule(new, CardGrade.AGAIN, NOW).lapses == 0  # a new card can't be forgotten
    top = CardState(study_id="s", card_id="c1", box=6)
    assert schedule(top, CardGrade.EASY, NOW).box == 6


def test_study_end_to_end(service, analysis):
    study = service.study(analysis.id)
    assert study.status is AnalysisStatus.DONE and study.questions and study.cards
    sections = {sid for seg in analysis.playlist.segments for sid in seg.section_ids}
    assert {x.section_id for x in study.questions + study.cards} <= sections
    assert [x.id for x in study.questions] == [f"q{i}" for i in range(1, len(study.questions) + 1)]
    assert [c.start_s for c in study.cards] == sorted(c.start_s for c in study.cards)
    # Every time is a real sentence start.
    starts = {u.start_s for u in boundary_units(service.transcripts.cues)} | {
        s.section.start_s for s in analysis.sections
    }
    for item in study.questions + study.cards:
        assert item.start_s in starts
    for x in study.questions:
        assert x.options[x.answer].startswith("The idea of")
    # The review pass replaced q1's wrong options (3 now) and left the others alone.
    q1 = study.questions[0]
    assert sorted(o for i, o in enumerate(q1.options) if i != q1.answer) == [
        "New wrong 1",
        "New wrong 2",
        "New wrong 3",
    ]
    assert all(len(x.options) == 3 for x in study.questions[1:])
    assert any("cited a sentence" in w for w in study.warnings)
    # Saved: asking again returns the same set without calling the model.
    calls = len(service.llm.calls)
    assert service.study(analysis.id).id == study.id and len(service.llm.calls) == calls
    assert service.study(analysis.id, NotesScope.VIDEO).id != study.id


def test_quiz_grading(service, analysis):
    study = service.study(analysis.id, NotesScope.VIDEO)
    first, second = study.questions[0], study.questions[1]
    wrong = (second.answer + 1) % len(second.options)
    attempt = service.grade_quiz(study.id, {first.id: first.answer, second.id: wrong, "q999": 0})
    assert attempt.correct == [first.id] and attempt.score == 1 and attempt.total == len(study.questions)
    assert "q999" not in attempt.answers
    assert [a.id for a in service.quiz_attempts(study.id)] == [attempt.id]
    with pytest.raises(KeyError):
        service.grade_quiz("nope", {})


def test_flashcard_review_across_videos(service, analysis):
    study = service.study(analysis.id, NotesScope.VIDEO)
    deck = service.flashcards(now=NOW)
    assert deck.due == deck.total == len(study.cards) and deck.next_due_at is None
    first = deck.cards[0]
    assert first.video_title == "Test Lecture" and first.state.box == 0

    service.review_card(study.id, first.card.id, CardGrade.GOOD, now=NOW)
    later = service.flashcards(now=NOW)
    assert later.due == deck.due - 1 and later.next_due_at == NOW + timedelta(days=1)
    assert first.card.id not in {c.card.id for c in later.cards}
    # A day later it's due again, before the new cards.
    tomorrow = service.flashcards(now=NOW + timedelta(days=1, minutes=1))
    assert tomorrow.cards[0].card.id == first.card.id and tomorrow.cards[0].state.box == 1
    assert service.flashcards(analysis.id, now=NOW).total == len(study.cards)
    assert service.flashcards("other", now=NOW).total == 0
    assert len(service.flashcards(now=NOW, limit=2).cards) == 2

    # A new set for the video replaces the old one's cards in review, but the old set
    # can still be drilled on its own.
    newer = service.study(analysis.id, NotesScope.PLAYLIST)
    assert {c.study_id for c in service.flashcards(now=NOW).cards} == {newer.id}
    assert {c.study_id for c in service.flashcards(study_id=study.id, now=NOW).cards} == {study.id}
    assert service.flashcards(study_id="nope", now=NOW).total == 0
    with pytest.raises(KeyError):
        service.review_card(study.id, "c999", CardGrade.GOOD)


def test_study_needs_a_finished_analysis(service):
    analysis_id, _ = service.submit(URL, service.default_preferences(), queue=True)
    with pytest.raises(AnalysisError):
        service.submit_study(analysis_id)
    with pytest.raises(KeyError):
        service.submit_study("nope")


def test_api_study_flow(service, analysis):
    with TestClient(create_app(service)) as client:
        r = client.post(f"/api/v1/analyses/{analysis.id}/study", json={"scope": "playlist"})
        assert r.status_code == 202
        study_id = r.json()["study_id"]
        for _ in range(100):
            body = client.get(f"/api/v1/study/{study_id}").json()
            if body["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        assert body["status"] == "done", body.get("error")
        assert client.get(f"/api/v1/analyses/{analysis.id}/study").json()[0]["id"] == study_id

        q = body["questions"][0]
        r = client.post(f"/api/v1/study/{study_id}/attempts", json={"answers": {q["id"]: q["answer"]}})
        assert r.json()["score"] == 1
        assert len(client.get(f"/api/v1/study/{study_id}/attempts").json()) == 1

        deck = client.get("/api/v1/flashcards").json()
        assert deck["due"] == len(body["cards"])
        card = deck["cards"][0]
        r = client.post(
            "/api/v1/flashcards/review", json={"study_id": study_id, "card_id": card["card"]["id"], "grade": "easy"}
        )
        assert r.json()["box"] == 2
        assert client.get("/api/v1/flashcards").json()["due"] == deck["due"] - 1
        bad = {"study_id": study_id, "card_id": "c999", "grade": "good"}
        assert client.post("/api/v1/flashcards/review", json=bad).status_code == 404
        assert client.get("/api/v1/study/nope").status_code == 404


def test_even_out_fixes_only_real_giveaways():
    from curio_learn.analysis.study import even_out, giveaway
    from curio_learn.domain import QuizQuestion

    from .conftest import FakeLLM

    right = "The reader saturates after ten to twenty documents and then degrades"
    q1 = QuizQuestion(
        id="q1",
        question="Q1?",
        options=[right, "More is better", "No effect"],
        answer=0,
        explanation="",
        section_id="S01",
        start_s=0,
    )
    q2 = QuizQuestion(
        id="q2", question="Q2?", options=["No effect", right], answer=1, explanation="", section_id="S01", start_s=0
    )
    q3 = QuizQuestion(
        id="q3",
        question="Q3?",
        options=["Short", "Also short", "Shorter"],
        answer=0,
        explanation="",
        section_id="S01",
        start_s=0,
    )
    assert giveaway(q1) and giveaway(q2) and not giveaway(q3)
    llm = FakeLLM()
    assert even_out(llm, model="m", questions=[q1, q2, q3]) == 1
    # q1's padded options close the gap; the right answer is unchanged and still marked.
    assert not giveaway(q1) and q1.options[q1.answer] == right and len(q1.options) == 4
    # q2's rewrite didn't help, so it was not applied.
    assert q2.options == ["No effect", right]
    # A second round tried the one still flagged.
    assert llm.calls == ["QuizPadOut", "QuizPadOut"]


def test_unsupported_names():
    from curio_learn.analysis.study import unsupported_names

    said = "Then they come to ChatGPT and ask it to pretend to be a user, then do SFT on Llama 2."
    assert unsupported_names("It uses GPT-4 to generate queries", said) == ["GPT-4"]
    assert unsupported_names("It uses ChatGPT to write queries for SFT on Llama-2", said) == []
    assert unsupported_names("It keeps the call with the lowest loss", said) == []
    # A hyphen before a plain word ends the name: "API-only" is the API the video says.
    assert unsupported_names("Use it for API-only models", "when you only have an API") == []
    assert unsupported_names("It uses GPT-4o and A2A", "GPT-4o") == ["A2A"]


def test_plain_numbers_are_not_names():
    from curio_learn.analysis.study import unsupported_names

    assert unsupported_names("Accuracy saturates after 10-20 documents, at 16,000 tools", "about ten documents") == []


def test_quiz_drops_answers_naming_what_the_video_never_says(settings):
    from curio_learn.analysis.study import generate_study
    from curio_learn.processing.normalize import boundary_units

    from .conftest import FakeLLM, make_cues

    class MadeUp(FakeLLM):
        def complete_json(self, **kw):
            out = super().complete_json(**kw)
            return (
                out.replace("The idea of part 1", "GPT-4 does part 1") if kw["schema"]["_model"] == "StudyOut" else out
            )

    service = CurioService(settings, llm=FakeLLM(), metadata=FakeMetadata(), transcripts=FakeTranscripts())
    analysis = service.analyze("https://youtu.be/e_m99aR7_3Q", LearningPreferences())
    st = generate_study(
        MadeUp(),
        model="m",
        analysis=analysis,
        units=boundary_units(make_cues()),
        scope=NotesScope.VIDEO,
        study_id="x",
        batch_tokens=2000,
    )
    assert all("GPT-4" not in q.options[q.answer] for q in st.questions)
    assert any("named something the video doesn't say" in w for w in st.warnings)
    assert [q.id for q in st.questions] == [f"q{i}" for i in range(1, len(st.questions) + 1)]
