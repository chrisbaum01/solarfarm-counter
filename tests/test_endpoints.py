"""Guards on which Overpass mirrors may be used, and on cache provenance."""

import json

import pytest

from app import cache, config, overpass


# A mirror that only holds one region answers an out-of-area query with HTTP 200
# and zero elements. That is indistinguishable from "nothing here" and gets
# cached as fact. overpass.osm.ch did exactly this: Zurich returned 18,577
# buildings, Munich returned 0.
REGIONAL_MIRRORS = ("overpass.osm.ch",)


def test_no_regional_mirrors_in_the_default_list():
    for endpoint in config.OVERPASS_ENDPOINTS:
        for regional in REGIONAL_MIRRORS:
            assert regional not in endpoint, (
                f"{endpoint} serves only one region; an out-of-area query returns "
                "an empty result that would be cached as though it were real"
            )


def test_cache_version_is_past_the_poisoned_generation():
    """v1 tiles may hold empty results served by the regional mirror."""
    assert config.QUERY_VERSION >= 2


class TestTileProvenance:
    @pytest.fixture(autouse=True)
    def isolate_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CACHE_PATH", tmp_path / "cache.sqlite3")
        monkeypatch.setattr(cache, "_conn", None)
        yield
        monkeypatch.setattr(cache, "_conn", None)

    @pytest.mark.asyncio
    async def test_cached_tile_records_which_endpoint_served_it(self, monkeypatch):
        """An empty tile must be attributable to a mirror after the fact."""
        async def fake(client, tile, worker=0):
            return [], "https://example.test/api/interpreter"

        monkeypatch.setattr(overpass, "_fetch_tile", fake)
        tile = (48.0, 11.0, 48.1, 11.1)
        await overpass.fetch_solar_features([tile])

        stored = cache.get(overpass._tile_key(tile), config.TILE_TTL_SECONDS)
        assert stored["endpoint"] == "https://example.test/api/interpreter"
        assert stored["elements"] == []

    @pytest.mark.asyncio
    async def test_cached_tiles_are_read_back(self, monkeypatch):
        async def fake(client, tile, worker=0):
            return [{"type": "way", "id": 7, "tags": {}, "geometry": []}], "ep"

        monkeypatch.setattr(overpass, "_fetch_tile", fake)
        tile = (48.0, 11.0, 48.1, 11.1)
        first, s1 = await overpass.fetch_solar_features([tile])
        second, s2 = await overpass.fetch_solar_features([tile])
        assert s1["tiles_fetched"] == 1 and s2["tiles_cached"] == 1
        assert [e["id"] for e in second] == [7]
