import pytest

from app.cluster import build_parks, element_area_m2, is_rooftop, select_features
from app.geometry import PolylineIndex, Projection, projection_for

PROJ = Projection(49.0)


def _way(way_id, lat, lon, size_deg=0.001, tags=None):
    """A small square way centred near (lat, lon)."""
    return {
        "type": "way",
        "id": way_id,
        "tags": tags or {"power": "generator", "generator:source": "solar"},
        "geometry": [
            {"lat": lat, "lon": lon},
            {"lat": lat, "lon": lon + size_deg},
            {"lat": lat + size_deg, "lon": lon + size_deg},
            {"lat": lat + size_deg, "lon": lon},
        ],
    }


class TestRooftopDetection:
    @pytest.mark.parametrize(
        "tags",
        [
            {"location": "roof"},
            {"location": "rooftop"},
            {"generator:place": "roof"},
            {"building": "yes"},
            {"building": "industrial"},
            {"roof:shape": "gabled"},
        ],
    )
    def test_rooftop_variants_excluded(self, tags):
        assert is_rooftop({"power": "generator", **tags})

    @pytest.mark.parametrize(
        "tags",
        [
            {"power": "plant", "plant:source": "solar"},
            {"power": "generator", "generator:source": "solar"},
            {"power": "generator", "location": "ground"},
        ],
    )
    def test_ground_mounted_kept(self, tags):
        assert not is_rooftop(tags)


class TestArea:
    def test_node_area_is_unknown_not_zero(self):
        """None and 0.0 mean different things; a node's size is unknown."""
        node = {"type": "node", "id": 1, "lat": 49.0, "lon": 11.0, "tags": {}}
        assert element_area_m2(node, PROJ) is None

    def test_multipolygon_subtracts_holes(self):
        outer = [{"lat": 49.0, "lon": 11.0}, {"lat": 49.0, "lon": 11.01},
                 {"lat": 49.01, "lon": 11.01}, {"lat": 49.01, "lon": 11.0}]
        inner = [{"lat": 49.002, "lon": 11.002}, {"lat": 49.002, "lon": 11.004},
                 {"lat": 49.004, "lon": 11.004}, {"lat": 49.004, "lon": 11.002}]
        solid = {"type": "relation", "id": 1, "tags": {},
                 "members": [{"role": "outer", "geometry": outer}]}
        holed = {"type": "relation", "id": 2, "tags": {},
                 "members": [{"role": "outer", "geometry": outer},
                             {"role": "inner", "geometry": inner}]}
        assert element_area_m2(holed, PROJ) < element_area_m2(solid, PROJ)

    def test_relation_without_geometry_is_unknown(self):
        rel = {"type": "relation", "id": 3, "tags": {}, "members": [{"role": "outer"}]}
        assert element_area_m2(rel, PROJ) is None


class TestSelection:
    def setup_method(self):
        self.line = [(11.0, 49.0), (11.2, 49.0)]
        self.proj = projection_for(self.line)
        self.index = PolylineIndex([self.line], self.proj)

    def test_corridor_filter(self):
        near = _way(1, 49.0 + 100 / 110_574, 11.05)
        far = _way(2, 49.0 + 2000 / 110_574, 11.05)
        kept, stats = select_features([near, far], self.index, self.proj, 500)
        assert [f.element["id"] for f in kept] == [1]
        assert stats["outside_corridor"] == 1

    def test_distance_uses_nearest_edge_not_centre(self):
        """A large park touching the road counts, even if its centre is far."""
        big = _way(3, 49.0 + 20 / 110_574, 11.05, size_deg=0.02)
        kept, _ = select_features([big], self.index, self.proj, 500)
        assert len(kept) == 1
        assert kept[0].distance_m < 100  # edge distance, not the ~1km centroid

    def test_rooftop_counted_separately(self):
        roof = _way(4, 49.0, 11.05, tags={"power": "generator", "location": "roof"})
        kept, stats = select_features([roof], self.index, self.proj, 500)
        assert kept == []
        assert stats["rooftop_excluded"] == 1


class TestParkAssembly:
    def setup_method(self):
        self.line = [(11.0, 49.0), (11.2, 49.0)]
        self.proj = projection_for(self.line)
        self.index = PolylineIndex([self.line], self.proj)

    def _parks(self, elements, link_m=300, min_area_m2=0):
        kept, _ = select_features(elements, self.index, self.proj, 500)
        return build_parks(kept, self.proj, link_m, min_area_m2)

    def test_adjacent_panel_rows_merge_into_one_park(self):
        """The core problem: OSM maps one park as many separate rows."""
        rows = [_way(i, 49.0 + 50 / 110_574, 11.05 + i * 0.0012) for i in range(5)]
        parks, stats = self._parks(rows)
        assert len(parks) == 1
        assert parks[0].feature_count == 5
        assert stats["clusters"] == 1

    def test_distant_parks_stay_separate(self):
        a = _way(1, 49.0 + 50 / 110_574, 11.02)
        b = _way(2, 49.0 + 50 / 110_574, 11.15)
        parks, _ = self._parks([a, b])
        assert len(parks) == 2

    def test_area_sums_across_merged_features(self):
        rows = [_way(i, 49.0 + 50 / 110_574, 11.05 + i * 0.0012) for i in range(3)]
        parks, _ = self._parks(rows)
        singles, _ = self._parks([rows[0]])
        assert parks[0].area_m2 == pytest.approx(singles[0].area_m2 * 3, rel=1e-6)

    def test_min_area_filter_drops_small_clusters(self):
        small = _way(1, 49.0 + 50 / 110_574, 11.05, size_deg=0.0002)
        large = _way(2, 49.0 + 50 / 110_574, 11.15, size_deg=0.002)
        parks, stats = self._parks([small, large], min_area_m2=10_000)
        assert [p.osm_ids for p in parks] == [["way/2"]]
        assert stats["below_min_area"] == 1

    def test_uncorroborated_osm_point_is_dropped(self):
        """A bare OSM point with no area and nothing backing it is weak evidence.

        In practice these are usually untagged rooftop arrays: node/13155920665
        on the A8 is a panel node sitting inside a barn outline, with no rooftop
        tag to catch it and no area for the size filter to bite on.
        """
        node = {"type": "node", "id": 9, "lat": 49.0 + 50 / 110_574, "lon": 11.05,
                "tags": {"power": "generator", "generator:source": "solar"}}
        parks, stats = self._parks([node], min_area_m2=10_000)
        assert parks == []
        assert stats["uncorroborated_points"] == 1

    def test_registry_backing_rescues_an_area_less_point(self):
        """Corroborated by the register, the same point is a real park."""
        from app import mastr

        lat, lon = 49.0 + 50 / 110_574, 11.05
        node = {"type": "node", "id": 9, "lat": lat, "lon": lon,
                "tags": {"power": "generator", "generator:source": "solar"}}
        unit = mastr.to_elements(
            [mastr.MastrUnit("SEE1", lat, lon, "u", "Solarpark", 900.0, None, None, None)]
        )
        parks, stats = self._parks([node] + unit, min_area_m2=10_000)
        assert len(parks) == 1
        assert parks[0].area_m2 is None  # still unknown, but no longer unsupported
        assert stats["uncorroborated_points"] == 0

    def test_uncorroborated_rule_can_be_turned_off(self):
        node = {"type": "node", "id": 9, "lat": 49.0 + 50 / 110_574, "lon": 11.05,
                "tags": {"power": "generator", "generator:source": "solar"}}
        kept, _ = select_features([node], self.index, self.proj, 500)
        parks, _ = build_parks(kept, self.proj, 300, 10_000, require_corroboration=False)
        assert len(parks) == 1

    def test_parks_sorted_by_position_along_route(self):
        elements = [_way(1, 49.0, 11.15), _way(2, 49.0, 11.02), _way(3, 49.0, 11.09)]
        parks, _ = self._parks(elements)
        offsets = [p.km_along_route for p in parks]
        assert offsets == sorted(offsets)

    def test_name_and_operator_taken_from_any_member(self):
        tagged = _way(1, 49.0 + 50 / 110_574, 11.05,
                      tags={"power": "generator", "generator:source": "solar",
                            "name": "Solarpark Test", "operator": "Stadtwerke"})
        plain = _way(2, 49.0 + 50 / 110_574, 11.0512)
        parks, _ = self._parks([plain, tagged])
        assert len(parks) == 1
        assert parks[0].name == "Solarpark Test"
        assert parks[0].operator == "Stadtwerke"
