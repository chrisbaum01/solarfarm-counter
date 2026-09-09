"""Geometric rooftop detection: catching arrays that carry no rooftop tag."""

import pytest

from app.cluster import Feature, drop_features_on_buildings, is_rooftop
from app.geometry import Projection, distance_to_ring_m, point_in_ring

PROJ = Projection(48.6)

# A square roughly 20 m x 20 m near the real farm shed on the A8.
SHED = [
    (48.61362, 9.61960), (48.61362, 9.61987),
    (48.61380, 9.61987), (48.61380, 9.61960),
]

# node/13155920665 — bare power=generator + generator:source=solar, no location
# tag, no area. It sits inside the shed above: PV on a farm roof.
REPORTED_NODE = {
    "type": "node", "id": 13155920665, "lat": 48.6137077, "lon": 9.6196938,
    "tags": {
        "power": "generator", "generator:source": "solar",
        "generator:method": "photovoltaic", "generator:type": "solar_photovoltaic_panel",
        "generator:output:electricity": "yes",
    },
}


def _feature(element, lat=None, lon=None):
    return Feature(
        element=element,
        lat=lat if lat is not None else element["lat"],
        lon=lon if lon is not None else element["lon"],
        area_m2=None, distance_m=80.0, offset_m=36_500.0,
    )


class TestRingGeometry:
    def test_point_inside_and_outside(self):
        assert point_in_ring(48.61371, 9.61973, SHED)
        assert not point_in_ring(48.61500, 9.62500, SHED)

    def test_distance_is_zero_inside(self):
        assert distance_to_ring_m(48.61371, 9.61973, SHED, PROJ) == 0.0

    def test_distance_grows_with_separation(self):
        near = distance_to_ring_m(48.61390, 9.61973, SHED, PROJ)
        far = distance_to_ring_m(48.61500, 9.61973, SHED, PROJ)
        assert 0 < near < far


class TestReportedCase:
    """Regression for the node a user correctly flagged as not a solar park."""

    def test_tag_filter_alone_does_not_catch_it(self):
        assert not is_rooftop(REPORTED_NODE["tags"]), (
            "the node genuinely has no rooftop tag — which is why the tag filter "
            "was not enough on its own"
        )

    def test_geometric_check_catches_it(self):
        kept, dropped = drop_features_on_buildings([_feature(REPORTED_NODE)], [SHED], PROJ)
        assert dropped == 1
        assert kept == []


class TestDropOnBuildings:
    def test_feature_in_open_field_is_kept(self):
        field = {"type": "node", "id": 1, "lat": 48.6200, "lon": 9.6300, "tags": {}}
        kept, dropped = drop_features_on_buildings([_feature(field)], [SHED], PROJ)
        assert dropped == 0
        assert len(kept) == 1

    def test_failed_lookup_drops_nothing(self):
        """None means "could not check" — it must not silently change the count."""
        kept, dropped = drop_features_on_buildings([_feature(REPORTED_NODE)], None, PROJ)
        assert dropped == 0
        assert len(kept) == 1

    def test_no_buildings_found_drops_nothing(self):
        kept, dropped = drop_features_on_buildings([_feature(REPORTED_NODE)], [], PROJ)
        assert dropped == 0
        assert len(kept) == 1

    def test_registry_units_are_never_dropped(self):
        """MaStR is already ground-mount-only; a nearby building is irrelevant."""
        unit = {
            "type": "mastr", "id": "SEE1", "lat": 48.6137077, "lon": 9.6196938,
            "_source": "mastr", "_declared_area_m2": 40_000.0, "_capacity_kw": 900.0,
            "tags": {"name": "Solarpark"},
        }
        kept, dropped = drop_features_on_buildings([_feature(unit)], [SHED], PROJ)
        assert dropped == 0
        assert len(kept) == 1

    def test_tolerance_catches_nodes_placed_at_the_roof_edge(self):
        """Mappers often drop the node just outside the outline."""
        just_outside = {"type": "node", "id": 2, "lat": 48.61386, "lon": 9.61973, "tags": {}}
        kept, dropped = drop_features_on_buildings(
            [_feature(just_outside)], [SHED], PROJ, tolerance_m=12.0
        )
        assert dropped == 1

    def test_large_ground_park_beside_a_barn_is_kept(self):
        """A real park near a building must survive; the test is proximity, not adjacency."""
        park = {"type": "node", "id": 3, "lat": 48.61600, "lon": 9.61973, "tags": {}}
        kept, dropped = drop_features_on_buildings(
            [_feature(park)], [SHED], PROJ, tolerance_m=12.0
        )
        assert dropped == 0
        assert len(kept) == 1
