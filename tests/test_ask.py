import pytest
from fastapi.testclient import TestClient

from curio_learn.analysis.ask import precise_start, select_context
from curio_learn.api.app import create_app
from curio_learn.domain import Block, LearningPreferences
from curio_learn.errors import AnalysisError
from curio_learn.processing.normalize import Utterance, build_blocks
from curio_learn.processing.retrieval import bm25_scores, rank, time_hints
from curio_learn.service import CurioService

from .conftest import FakeLLM, FakeMetadata, FakeTranscripts

URL = "https://www.youtube.com/watch?v=e_m99aR7_3Q"


class FakeEmbedder:
    name = "fake/embed"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.document_calls = 0
        self.query_calls = 0

    def embed(self, texts, *, kind):
        if self.fail:
            raise ConnectionError("no ollama")
        if kind == "document":
            self.document_calls += 1
        else:
            self.query_calls += 1
        return [[float("topic3" in t), float("topic5" in t), 1.0] for t in texts]


def make_service(settings, **kw):
    return CurioService(
        settings, llm=FakeLLM(kinds={0: "intro"}), metadata=FakeMetadata(), transcripts=FakeTranscripts(), **kw
    )


@pytest.fixture
def service(settings):
    return make_service(settings)


def test_time_hints():
    assert time_hints("Explain the architecture discussed at 23 minutes") == [1380.0]
    assert time_hints("what about 23:10 and 1:05:00?") == [1390.0, 3900.0]
    assert time_hints("minute 7, then 1 hour 5 minutes in") == [420.0, 3900.0]
    assert time_hints("what are the three key arguments?") == []


def test_bm25_and_fusion():
    blocks = [
        Block(id=f"B{i}", index=i, start_s=i * 30, end_s=i * 30 + 30, text=t)
        for i, t in enumerate(
            ["we talk about gradients", "the reward model scores outputs", "lunch break", "reward hacking is a risk"]
        )
    ]
    scores = bm25_scores("how does the reward model work", [b.text for b in blocks])
    assert scores.index(max(scores)) == 1 and scores[2] == 0
    # Embeddings alone would favour block 2; fusion keeps the lexical winner on top.
    vecs = [[0, 1], [0.1, 1], [1, 0], [0, 1]]
    assert rank("reward model", blocks, query_vector=[1, 0], block_vectors=vecs)[0] == 1
    assert rank("", blocks) == [0, 1, 2, 3]


def test_select_context_full_or_excerpts():
    blocks = [Block(id=f"B{i}", index=i, start_s=i * 30.0, end_s=i * 30.0 + 30, text="word " * 20) for i in range(40)]
    idx, full = select_context(blocks, budget_tokens=100000)
    assert full and idx == list(range(40))
    # Each block costs 36 tokens. The stretch around 10:00 (8:30-12:30) comes first.
    idx, full = select_context(blocks, budget_tokens=6 * 36, hints=[600.0], ranked=lambda: [30, 2])
    assert not full and idx == [17, 18, 19, 20, 21, 22]
    # Without a hint: each search hit with its neighbours, in video order.
    idx, _ = select_context(blocks, budget_tokens=6 * 36, ranked=lambda: [30, 2])
    assert idx == [1, 2, 3, 29, 30, 31]


def test_precise_start_picks_the_matching_sentence():
    block = Block(id="B0", index=0, start_s=10.0, end_s=40.0, text="")
    units = [
        Utterance(0, 10.0, 15.0, "Welcome back everyone."),
        Utterance(1, 15.0, 25.0, "GRPO removes the value model entirely."),
        Utterance(2, 25.0, 40.0, "Questions later."),
        Utterance(3, 41.0, 45.0, "GRPO value model again, but outside the block."),
    ]
    assert precise_start("GRPO doesn't need a value model", block, units) == 15.0
    assert precise_start("nothing in common", block, units) == 10.0


def test_ask_end_to_end(service):
    analysis = service.analyze(URL)
    a = service.ask(analysis.id, "  What does   the first part explain? ")
    assert a.question == "What does the first part explain?"
    assert a.in_video and a.context == "full" and a.search == ""
    cue_starts = {c.start_s for c in service.transcripts.cues}
    first = a.parts[0]
    assert len(first.citations) == 1 and first.citations[0].start_s in cue_starts
    assert any("wasn't shown" in w for w in a.warnings)  # passage 9999 was dropped
    # The citation lands on the sentence mentioning word4 (12 s), not the block start (0 s).
    assert first.citations[0].start_s == 12.0

    b = service.ask(analysis.id, "and then?")
    assert "Conversation so far" in service.llm.prompts[-1] and a.question in service.llm.prompts[-1]
    assert [x.id for x in service.conversation(analysis.id)] == [a.id, b.id]
    assert service.clear_conversation(analysis.id) == 2 and service.conversation(analysis.id) == []


def test_question_the_video_doesnt_answer(service):
    analysis = service.analyze(URL)
    a = service.ask(analysis.id, "What's the weather like?")
    assert not a.in_video and a.outside_knowledge and not a.parts[0].citations


def test_long_transcript_is_searched_with_cached_embeddings(settings):
    settings.ask_context_tokens = 400
    emb = FakeEmbedder()
    svc = make_service(settings, embedder=emb)
    analysis = svc.analyze(URL)
    a = svc.ask(analysis.id, "what is topic5 about?")
    assert a.context == "excerpts" and a.search == "keyword+semantic"
    assert any("searched it" in w for w in a.warnings)
    prompt = svc.llm.prompts[-1]
    assert "excerpts" in prompt and "topic5" in prompt and "…" in prompt
    blocks = build_blocks(svc.transcripts.cues)
    assert all(blocks[0].start_s <= c.start_s for p in a.parts for c in p.citations)
    svc.ask(analysis.id, "and topic3?")
    assert emb.document_calls == 1 and emb.query_calls == 2  # block vectors stored, reused


def test_embedding_failure_falls_back_to_keywords(settings):
    settings.ask_context_tokens = 400
    svc = make_service(settings, embedder=FakeEmbedder(fail=True))
    analysis = svc.analyze(URL)
    a = svc.ask(analysis.id, "what is topic5 about?")
    assert a.context == "excerpts" and a.search == "keyword" and a.parts


def test_ask_errors(service):
    with pytest.raises(KeyError):
        service.ask("nope", "hi?")
    analysis_id, _ = service.submit(URL, LearningPreferences(profile="beginner"))
    with pytest.raises(AnalysisError, match="once the analysis has finished"):
        service.ask(analysis_id, "hi?")
    with pytest.raises(AnalysisError, match="Ask a question"):
        service.ask(analysis_id, "   ")


def test_api_ask_flow(service):
    analysis = service.analyze(URL)
    with TestClient(create_app(service)) as client:
        r = client.post(f"/api/v1/analyses/{analysis.id}/questions", json={"question": "What is explained?"})
        assert r.status_code == 200 and r.json()["parts"][0]["citations"]
        assert len(client.get(f"/api/v1/analyses/{analysis.id}/questions").json()) == 1
        assert client.delete(f"/api/v1/analyses/{analysis.id}/questions").json() == {"deleted": 1}
        assert client.post("/api/v1/analyses/nope/questions", json={"question": "x"}).status_code == 404
        bad = client.post(f"/api/v1/analyses/{analysis.id}/questions", json={"question": " "})
        assert bad.status_code == 400
