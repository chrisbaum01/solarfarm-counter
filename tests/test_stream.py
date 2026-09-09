"""The server-sent-events search stream."""

import json

import pytest
from fastapi.testclient import TestClient

from app import geocode, main


def _events(response):
    """Parse an SSE body into (event, data) pairs."""
    out, kind = [], None
    for line in response.text.splitlines():
        if line.startswith("event: "):
            kind = line[7:]
        elif line.startswith("data: "):
            out.append((kind, json.loads(line[6:])))
    return out


@pytest.fixture
def client():
    return TestClient(main.app)


def test_progress_then_result(client, monkeypatch):
    async def fake_search(origin, destination, params, progress=None):
        progress({"phase": "geocoding", "done": 0, "total": 0})
        progress({"phase": "tiles", "done": 3, "total": 7})
        return {"count": 4, "refs": ["A 8"], "parks": []}

    monkeypatch.setattr(main, "search", fake_search)
    r = client.get("/api/search/stream", params={"from": "A", "to": "B"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    events = _events(r)
    kinds = [k for k, _ in events]
    assert kinds == ["progress", "progress", "result"]
    assert events[1][1] == {"phase": "tiles", "done": 3, "total": 7}
    assert events[-1][1]["count"] == 4


def test_progress_arrives_before_the_result(client, monkeypatch):
    """The point of the stream: news before the answer, not with it."""
    async def fake_search(origin, destination, params, progress=None):
        for i in range(1, 4):
            progress({"phase": "tiles", "done": i, "total": 3})
        return {"count": 0, "parks": []}

    monkeypatch.setattr(main, "search", fake_search)
    events = _events(client.get("/api/search/stream", params={"from": "A", "to": "B"}))
    assert [k for k, _ in events] == ["progress"] * 3 + ["result"]
    assert [d["done"] for k, d in events if k == "progress"] == [1, 2, 3]


def test_known_failure_becomes_an_error_event(client, monkeypatch):
    async def fake_search(origin, destination, params, progress=None):
        raise geocode.GeocodeError("could not find a place named 'Xyzzy'")

    monkeypatch.setattr(main, "search", fake_search)
    events = _events(client.get("/api/search/stream", params={"from": "Xyzzy", "to": "B"}))
    assert [k for k, _ in events] == ["error"]
    assert "Xyzzy" in events[0][1]["detail"]


def test_unexpected_failure_still_closes_the_stream(client, monkeypatch):
    """A bug must not leave the browser waiting forever on an open stream."""
    async def fake_search(origin, destination, params, progress=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "search", fake_search)
    events = _events(client.get("/api/search/stream", params={"from": "A", "to": "B"}))
    assert [k for k, _ in events] == ["error"]
    assert "boom" in events[0][1]["detail"]


def test_stream_accepts_the_same_parameters_as_the_json_endpoint(client, monkeypatch):
    seen = {}

    async def fake_search(origin, destination, params, progress=None):
        seen.update(params.__dict__)
        return {"count": 0, "parks": []}

    monkeypatch.setattr(main, "search", fake_search)
    client.get("/api/search/stream", params={
        "from": "A", "to": "B", "corridor_m": 1200, "min_area_m2": 5000,
        "link_m": 250, "use_mastr": "false", "require_corroboration": "false",
    })
    assert seen["corridor_m"] == 1200
    assert seen["min_area_m2"] == 5000
    assert seen["link_m"] == 250
    assert seen["use_mastr"] is False
    assert seen["require_corroboration"] is False
