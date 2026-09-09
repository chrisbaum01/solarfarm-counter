"""End-to-end checks against recorded Munich -> Nuremberg data. No network."""

import pytest

from app.pipeline import SearchParams, analyse_route
from app.routing import extract_runs


class TestAutobahnExtraction:
    def test_isolates_autobahn_from_full_route(self, route):
        """OSRM's per-step `ref` is what makes Autobahn-only scanning exact."""
        assert route.refs == ["A 9", "A 73"]
        assert route.matched_km == pytest.approx(156.1, abs=0.5)
        assert route.polylines

    def test_city_streets_excluded(self, osrm_response, route):
        total_km = osrm_response["routes"][0]["distance"] / 1000
        assert route.matched_km < total_km
        assert total_km - route.matched_km == pytest.approx(14.1, abs=1.0)

    def test_unreferenced_steps_ignored(self):
        steps = [
            {"ref": "", "distance": 500, "geometry": {"coordinates": [[11.0, 49.0], [11.01, 49.0]]}},
            {"ref": "A 9", "distance": 1000, "geometry": {"coordinates": [[11.01, 49.0], [11.02, 49.0]]}},
        ]
        result = extract_runs(steps)
        assert result.refs == ["A 9"]
        assert result.matched_km == pytest.approx(1.0)

    def test_ref_normalisation(self):
        steps = [
            {"ref": "A9", "distance": 100, "geometry": {"coordinates": [[11.0, 49.0], [11.01, 49.0]]}},
            {"ref": "A 9;B 2", "distance": 100, "geometry": {"coordinates": [[11.01, 49.0], [11.02, 49.0]]}},
        ]
        assert extract_runs(steps).refs == ["A 9"]

    def test_disjoint_runs_are_kept_apart(self):
        """Steps that do not join must become separate polylines."""
        steps = [
            {"ref": "A 9", "distance": 100, "geometry": {"coordinates": [[11.0, 49.0], [11.01, 49.0]]}},
            {"ref": "", "distance": 100, "geometry": {"coordinates": [[11.01, 49.0], [11.2, 49.0]]}},
            {"ref": "A 73", "distance": 100, "geometry": {"coordinates": [[11.2, 49.0], [11.21, 49.0]]}},
        ]
        assert len(extract_runs(steps).polylines) == 2

    def test_bundesstrasse_opt_in(self):
        steps = [
            {"ref": "B 2", "distance": 5000, "geometry": {"coordinates": [[11.0, 49.0], [11.01, 49.0]]}},
        ]
        assert extract_runs(steps).refs == []
        assert extract_runs(steps, include_bundesstrasse=True).matched_km == pytest.approx(5.0)


class TestCounting:
    """Regression checks on real data, so a change in the maths is visible."""

    def test_default_settings(self, route, overpass_elements):
        parks, stats = analyse_route(
            route, overpass_elements, SearchParams(corridor_m=500, min_area_m2=10_000)
        )
        assert len(parks) == 20
        assert stats["features_in_corridor"] == 49
        assert stats["clusters"] == 30

    def test_rooftop_is_the_dominant_exclusion(self, route, overpass_elements):
        _, stats = analyse_route(route, overpass_elements, SearchParams())
        assert stats["raw_elements"] == 791
        assert stats["rooftop_excluded"] > 400

    @pytest.mark.parametrize(
        "min_area_m2,expected",
        [(0, 28), (5_000, 24), (10_000, 20), (20_000, 17)],
    )
    def test_size_filter_is_monotonic(self, route, overpass_elements, min_area_m2, expected):
        parks, _ = analyse_route(
            route, overpass_elements, SearchParams(corridor_m=500, min_area_m2=min_area_m2)
        )
        assert len(parks) == expected

    def test_declared_plants_survive_any_size_filter(self, route, overpass_elements):
        """OSM calling something a solar plant outranks our size heuristic."""
        params = SearchParams(corridor_m=500, min_area_m2=10_000_000)
        parks, _ = analyse_route(route, overpass_elements, params)
        assert parks, "an absurd threshold still must not erase declared plants"
        assert all(p.area_m2 is None or p.area_m2 < 10_000_000 for p in parks)

    def test_wider_corridor_never_finds_fewer(self, route, overpass_elements):
        counts = [
            len(analyse_route(route, overpass_elements,
                              SearchParams(corridor_m=c, min_area_m2=0))[0])
            for c in (200, 500, 1000, 2000)
        ]
        assert counts == sorted(counts)

    def test_clustering_never_increases_the_count(self, route, overpass_elements):
        """Merging can only ever reduce the number of distinct parks."""
        params = SearchParams(corridor_m=500, min_area_m2=0)
        parks, stats = analyse_route(route, overpass_elements, params)
        assert len(parks) <= stats["features_in_corridor"]

    def test_every_park_is_inside_the_corridor(self, route, overpass_elements):
        parks, _ = analyse_route(route, overpass_elements, SearchParams(corridor_m=500))
        assert all(p.distance_m <= 500 for p in parks)

    def test_known_park_is_found(self, route, overpass_elements):
        """A real, named park on the A9 that must survive every filter."""
        parks, _ = analyse_route(route, overpass_elements, SearchParams())
        names = [p.name for p in parks if p.name]
        assert any("Garching" in n for n in names)

    def test_parks_carry_usable_metadata(self, route, overpass_elements):
        parks, _ = analyse_route(route, overpass_elements, SearchParams())
        for p in parks:
            assert p.osm_ids and all("/" in i for i in p.osm_ids)
            assert p.feature_count == len(p.osm_ids)
            assert 0 <= p.km_along_route <= route.matched_km + 1
            assert p.area_m2 is None or p.area_m2 > 0


class TestEmptyRoute:
    def test_no_motorway_yields_zero_not_an_error(self):
        steps = [
            {"ref": "", "distance": 800, "geometry": {"coordinates": [[11.0, 49.0], [11.01, 49.0]]}},
        ]
        result = extract_runs(steps)
        assert result.polylines == []
        assert result.matched_km == 0.0
