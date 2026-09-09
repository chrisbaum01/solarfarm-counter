import math

import pytest

from app.geometry import (
    PolylineIndex,
    Projection,
    bbox_tiles,
    polygon_centroid,
    projection_for,
    ring_area_m2,
)


def test_projection_roundtrip():
    proj = Projection(49.0)
    lat, lon = proj.latlon(*proj.xy(49.1234, 11.5678))
    assert lat == pytest.approx(49.1234, abs=1e-9)
    assert lon == pytest.approx(11.5678, abs=1e-9)


def test_ring_area_of_known_square():
    """A 100 m x 100 m square should measure 10,000 m2."""
    proj = Projection(49.0)
    d_lat = 100 / 110_574.0
    d_lon = 100 / (111_320.0 * math.cos(math.radians(49.0)))
    ring = [(49.0, 11.0), (49.0, 11.0 + d_lon), (49.0 + d_lat, 11.0 + d_lon), (49.0 + d_lat, 11.0)]
    assert ring_area_m2(ring, proj) == pytest.approx(10_000, rel=0.01)


def test_ring_area_is_orientation_independent():
    proj = Projection(49.0)
    ring = [(49.0, 11.0), (49.0, 11.01), (49.01, 11.01), (49.01, 11.0)]
    assert ring_area_m2(ring, proj) == pytest.approx(ring_area_m2(list(reversed(ring)), proj))


def test_polygon_centroid_beats_vertex_mean_on_uneven_rings():
    """Extra vertices along one edge must not drag the centroid toward it."""
    proj = Projection(49.0)
    ring = [
        (49.0, 11.0), (49.0, 11.0025), (49.0, 11.005), (49.0, 11.0075),  # dense edge
        (49.0, 11.01), (49.01, 11.01), (49.01, 11.0),
    ]
    lat, _ = polygon_centroid(ring, proj)
    vertex_mean_lat = sum(p[0] for p in ring) / len(ring)
    assert lat == pytest.approx(49.005, abs=2e-4)
    assert vertex_mean_lat < 49.004  # the naive mean is visibly pulled south


def test_polyline_distance_and_offset():
    """A point beside a straight east-west line: distance and along-track offset."""
    line = [(11.0, 49.0), (11.1, 49.0)]  # [lon, lat]
    proj = projection_for(line)
    index = PolylineIndex([line], proj)

    on_line_offset = index.distance_and_offset(49.0, 11.05)[1]
    assert index.distance(49.0, 11.05) == pytest.approx(0.0, abs=1.0)
    assert on_line_offset == pytest.approx(index.length_m / 2, rel=0.02)

    north = 500 / 110_574.0
    assert index.distance(49.0 + north, 11.05) == pytest.approx(500, rel=0.02)


def test_separate_polylines_do_not_bridge_the_gap():
    """Two disjoint runs must not behave like one continuous line.

    Concatenating the Autobahn runs of a route would create a phantom segment
    across the gap, pulling features near that straight line into the corridor.
    """
    left = [(11.0, 49.0), (11.02, 49.0)]
    right = [(11.30, 49.0), (11.32, 49.0)]
    proj = projection_for(left + right)

    split = PolylineIndex([left, right], proj)
    joined = PolylineIndex([left + right], proj)

    midpoint_lat, midpoint_lon = 49.0, 11.16  # inside the gap
    assert joined.distance(midpoint_lat, midpoint_lon) == pytest.approx(0.0, abs=1.0)
    assert split.distance(midpoint_lat, midpoint_lon) > 8_000


def test_grid_search_matches_brute_force():
    """The ring-expanding grid search must agree with an exhaustive scan."""
    import random

    rng = random.Random(7)
    line = [(11.0 + i * 0.01, 49.0 + math.sin(i / 3) * 0.02) for i in range(60)]
    proj = projection_for(line)
    index = PolylineIndex([line], proj)

    for _ in range(200):
        lat = 49.0 + rng.uniform(-0.1, 0.1)
        lon = 11.0 + rng.uniform(-0.1, 0.7)
        got = index.distance(lat, lon)
        px, py = proj.xy(lat, lon)
        expected = min(
            _seg_dist(px, py, *proj.xy(line[i][1], line[i][0]), *proj.xy(line[i + 1][1], line[i + 1][0]))
            for i in range(len(line) - 1)
        )
        assert got == pytest.approx(expected, rel=1e-6)


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def test_bbox_tiles_are_grid_aligned_and_shared():
    """Tiles snap to the global grid, so different routes reuse the same tiles."""
    a = bbox_tiles([[(11.02, 49.03), (11.07, 49.06)]], 500, 0.1)
    b = bbox_tiles([[(11.04, 49.01), (11.09, 49.08)]], 500, 0.1)
    assert set(a) & set(b)
    for south, west, north, east in a:
        assert round(south / 0.1) == pytest.approx(south / 0.1, abs=1e-9)
        assert north - south == pytest.approx(0.1)
        assert east - west == pytest.approx(0.1)


def test_bbox_tiles_cover_long_segments():
    """A long segment must not skip the tiles it passes straight through."""
    tiles = bbox_tiles([[(11.0, 49.0), (11.0, 50.0)]], 500, 0.1)
    southern_edges = {round(t[0], 4) for t in tiles}
    for expected in (49.2, 49.5, 49.8):
        assert expected in southern_edges
