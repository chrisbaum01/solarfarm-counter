"""The search deadline: partial results beat an unbounded wait."""

import asyncio

import pytest

from app import cache, config, overpass

TILES = [(48.0, 11.0, 48.1, 11.1), (48.1, 11.1, 48.2, 11.2), (48.2, 11.2, 48.3, 11.3)]


@pytest.fixture(autouse=True)
def isolate_cache(tmp_path, monkeypatch):
    """Point the tile cache at a throwaway file so tests never hit the real one."""
    monkeypatch.setattr(config, "CACHE_PATH", tmp_path / "cache.sqlite3")
    monkeypatch.setattr(cache, "_conn", None)
    yield
    monkeypatch.setattr(cache, "_conn", None)


def _slow_fetch(delay: float):
    async def fake(client, tile, worker=0):
        await asyncio.sleep(delay)
        return [{"type": "way", "id": int(tile[0] * 1000), "tags": {}, "geometry": []}], "test"
    return fake


@pytest.mark.asyncio
async def test_deadline_stops_the_wait_rather_than_hanging(monkeypatch):
    """Overrunning tiles are abandoned promptly instead of running to completion."""
    monkeypatch.setattr(overpass, "_fetch_tile", _slow_fetch(30.0))
    loop = asyncio.get_running_loop()
    started = loop.time()

    # Nothing arrived at all, which is reported as a timeout rather than as an
    # empty result -- "we could not look" is not "there are none".
    with pytest.raises(overpass.OverpassTimeout):
        await asyncio.wait_for(
            overpass.fetch_solar_features(TILES, deadline=loop.time() + 0.2),
            timeout=5.0,
        )
    assert loop.time() - started < 2.0, "should abandon near the deadline, not run on"


@pytest.mark.asyncio
async def test_total_timeout_is_not_reported_as_mirrors_being_down(monkeypatch):
    """The error must name the real cause, so the message is actionable."""
    monkeypatch.setattr(overpass, "_fetch_tile", _slow_fetch(30.0))
    loop = asyncio.get_running_loop()
    with pytest.raises(overpass.OverpassTimeout, match="time limit"):
        await overpass.fetch_solar_features(TILES, deadline=loop.time() + 0.1)


@pytest.mark.asyncio
async def test_work_finished_before_the_deadline_is_kept(monkeypatch):
    monkeypatch.setattr(overpass, "_fetch_tile", _slow_fetch(0.0))
    loop = asyncio.get_running_loop()

    elements, stats = await overpass.fetch_solar_features(
        TILES, deadline=loop.time() + 30.0
    )

    assert stats["tiles_timed_out"] == 0
    assert stats["tiles_failed"] == 0
    assert len(elements) == 3


@pytest.mark.asyncio
async def test_no_deadline_means_no_limit(monkeypatch):
    monkeypatch.setattr(overpass, "_fetch_tile", _slow_fetch(0.0))
    _, stats = await overpass.fetch_solar_features(TILES)
    assert stats["tiles_timed_out"] == 0


@pytest.mark.asyncio
async def test_partial_completion_keeps_what_finished(monkeypatch):
    """A slow tile must not discard the fast ones that already returned."""
    calls = {"n": 0}

    async def mixed(client, tile, worker=0):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"type": "way", "id": 1, "tags": {}, "geometry": []}], "test"
        await asyncio.sleep(10.0)
        return [], "test"

    monkeypatch.setattr(overpass, "_fetch_tile", mixed)
    monkeypatch.setattr(config, "OVERPASS_CONCURRENCY", 3)
    loop = asyncio.get_running_loop()

    elements, stats = await overpass.fetch_solar_features(
        TILES, deadline=loop.time() + 0.3
    )

    assert len(elements) == 1  # the fast tile survived
    assert stats["tiles_timed_out"] == 2


@pytest.mark.asyncio
async def test_already_expired_deadline_does_not_hang(monkeypatch):
    monkeypatch.setattr(overpass, "_fetch_tile", _slow_fetch(10.0))
    loop = asyncio.get_running_loop()
    with pytest.raises(overpass.OverpassTimeout):
        await asyncio.wait_for(
            overpass.fetch_solar_features(TILES, deadline=loop.time() - 5.0),
            timeout=5.0,
        )


def test_configured_default_is_five_minutes():
    assert config.SEARCH_TIMEOUT_SECONDS == 300.0
