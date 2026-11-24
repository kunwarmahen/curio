import pytest
from fastapi.testclient import TestClient

from curio_learn.api.app import create_app
from curio_learn.domain import LearningPreferences
from curio_learn.errors import AnalysisError
from curio_learn.service import CurioService

from .conftest import FakeLLM, FakeMetadata, FakeTranscripts

URL = "https://www.youtube.com/watch?v=e_m99aR7_3Q"


@pytest.fixture
def service(settings):
    return CurioService(settings, llm=FakeLLM(), metadata=FakeMetadata(), transcripts=FakeTranscripts())


def test_custom_playlist(service):
    a = service.analyze(URL, LearningPreferences())
    curated = [s.id for s in a.segments if s.in_playlist]
    others = [s.id for s in a.segments if not s.in_playlist]
    assert service.custom_playlist(a.id) is None
    # Saved in time order, without duplicates.
    mine = service.set_custom_playlist(a.id, [others[0], curated[0], others[0]])
    by_start = {s.id: s.start_s for s in a.segments}
    assert mine == sorted({others[0], curated[0]}, key=by_start.get)
    assert service.custom_playlist(a.id) == mine
    # Choosing exactly Curio's picks, or None, goes back to Curio's.
    assert service.set_custom_playlist(a.id, list(reversed(curated))) is None
    assert service.custom_playlist(a.id) is None
    with pytest.raises(AnalysisError):
        service.set_custom_playlist(a.id, ["Z99"])
    with pytest.raises(AnalysisError):
        service.set_custom_playlist(a.id, [])

    with TestClient(create_app(service)) as client:
        r = client.put(f"/api/v1/analyses/{a.id}/custom-playlist", json={"segment_ids": [others[0]]})
        assert r.status_code == 200 and r.json() == {"segment_ids": [others[0]]}
        assert client.get(f"/api/v1/analyses/{a.id}/custom-playlist").json() == {"segment_ids": [others[0]]}
        assert client.put(f"/api/v1/analyses/{a.id}/custom-playlist", json={}).json() == {"segment_ids": None}
        assert client.put("/api/v1/analyses/nope/custom-playlist", json={}).status_code == 404
