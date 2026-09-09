"""Planar geometry helpers.

Everything is projected to metres with a local equirectangular projection
anchored at a reference latitude. Over a few hundred kilometres of Germany the
distortion is well under the precision we need (we are thresholding at 500 m
against OSM data whose own positional accuracy is metres), and it keeps the
distance and area maths to plain arithmetic.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

# metres per degree
_M_PER_DEG_LAT = 110_574.0
_M_PER_DEG_LON_EQ = 111_320.0

LonLat = tuple[float, float]
Point = tuple[float, float]  # projected (x, y) in metres


class Projection:
    """Equirectangular projection about a reference latitude."""

    __slots__ = ("lat0", "mx", "my")

    def __init__(self, lat0: float) -> None:
        self.lat0 = lat0
        self.mx = _M_PER_DEG_LON_EQ * math.cos(math.radians(lat0))
        self.my = _M_PER_DEG_LAT

    def xy(self, lat: float, lon: float) -> Point:
        return (lon * self.mx, lat * self.my)

    def latlon(self, x: float, y: float) -> tuple[float, float]:
        return (y / self.my, x / self.mx)


def projection_for(coords: Sequence[LonLat]) -> Projection:
    """Anchor a projection at the mean latitude of `coords` ([lon, lat] pairs)."""
    if not coords:
        return Projection(51.0)  # roughly mid-Germany
    return Projection(sum(c[1] for c in coords) / len(coords))


def _point_segment_distance(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> float:
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


class PolylineIndex:
    """Distance queries against a set of polylines, with a grid index.

    Holds several polylines rather than one, because the Autobahn portions of a
    route are not contiguous. Concatenating them would introduce a phantom
    straight segment across the gap (e.g. between the A9 and A73 runs) and pull
    unrelated features into the corridor.

    Also exposes `distance_and_offset`, which returns how far along the whole
    Autobahn stretch the nearest point lies -- used for the "km" column.
    """

    def __init__(self, polylines: Iterable[Sequence[LonLat]], proj: Projection) -> None:
        self.proj = proj
        self._segments: list[tuple[float, float, float, float, float]] = []
        # cumulative distance in metres at the START of each segment, measured
        # across all polylines in order
        travelled = 0.0
        for line in polylines:
            pts = [proj.xy(lat, lon) for lon, lat in line]
            for i in range(len(pts) - 1):
                (ax, ay), (bx, by) = pts[i], pts[i + 1]
                self._segments.append((ax, ay, bx, by, travelled))
                travelled += math.hypot(bx - ax, by - ay)
        self.length_m = travelled

        # Bucket segments into a coarse grid so a point only tests nearby ones.
        self._cell = 2_000.0
        self._grid: dict[tuple[int, int], list[int]] = {}
        for idx, (ax, ay, bx, by, _) in enumerate(self._segments):
            for key in self._cells_covering(ax, ay, bx, by):
                self._grid.setdefault(key, []).append(idx)

    def _cells_covering(
        self, ax: float, ay: float, bx: float, by: float
    ) -> Iterable[tuple[int, int]]:
        c = self._cell
        x0, x1 = sorted((ax, bx))
        y0, y1 = sorted((ay, by))
        for gx in range(int(math.floor(x0 / c)), int(math.floor(x1 / c)) + 1):
            for gy in range(int(math.floor(y0 / c)), int(math.floor(y1 / c)) + 1):
                yield (gx, gy)

    def distance_and_offset(self, lat: float, lon: float) -> tuple[float, float]:
        """Return (distance to nearest polyline, distance along route) in metres."""
        if not self._segments:
            return (math.inf, 0.0)
        px, py = self.proj.xy(lat, lon)
        c = self._cell
        gx0, gy0 = int(math.floor(px / c)), int(math.floor(py / c))

        # Widen the search ring until we have candidates and the ring itself is
        # farther away than the best hit so far, so we cannot miss a closer
        # segment sitting just outside the cells inspected.
        best = math.inf
        best_offset = 0.0
        seen: set[int] = set()
        ring = 0
        while True:
            candidates: list[int] = []
            for gx in range(gx0 - ring, gx0 + ring + 1):
                for gy in range(gy0 - ring, gy0 + ring + 1):
                    if ring > 0 and abs(gx - gx0) != ring and abs(gy - gy0) != ring:
                        continue  # interior already covered by a smaller ring
                    for idx in self._grid.get((gx, gy), ()):
                        if idx not in seen:
                            seen.add(idx)
                            candidates.append(idx)
            for idx in candidates:
                ax, ay, bx, by, travelled = self._segments[idx]
                d = _point_segment_distance(px, py, ax, ay, bx, by)
                if d < best:
                    best = d
                    dx, dy = bx - ax, by - ay
                    if dx == 0.0 and dy == 0.0:
                        t = 0.0
                    else:
                        t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
                        t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                    best_offset = travelled + t * math.hypot(dx, dy)
            if best <= ring * c:
                return (best, best_offset)
            ring += 1
            if ring > 64:  # pathological fallback: brute force
                for idx, (ax, ay, bx, by, travelled) in enumerate(self._segments):
                    d = _point_segment_distance(px, py, ax, ay, bx, by)
                    if d < best:
                        best, best_offset = d, travelled
                return (best, best_offset)

    def distance(self, lat: float, lon: float) -> float:
        return self.distance_and_offset(lat, lon)[0]


def point_in_ring(lat: float, lon: float, ring: Sequence[tuple[float, float]]) -> bool:
    """Ray-casting containment test for a ring of (lat, lon) pairs."""
    inside = False
    n = len(ring)
    for i in range(n):
        y1, x1 = ring[i]
        y2, x2 = ring[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            xin = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < xin:
                inside = not inside
    return inside


def distance_to_ring_m(
    lat: float, lon: float, ring: Sequence[tuple[float, float]], proj: Projection
) -> float:
    """Distance from a point to a ring's boundary, in metres (0 if inside)."""
    if point_in_ring(lat, lon, ring):
        return 0.0
    px, py = proj.xy(lat, lon)
    best = math.inf
    n = len(ring)
    for i in range(n):
        ax, ay = proj.xy(*ring[i])
        bx, by = proj.xy(*ring[(i + 1) % n])
        best = min(best, _point_segment_distance(px, py, ax, ay, bx, by))
    return best


def ring_area_m2(ring: Sequence[tuple[float, float]], proj: Projection) -> float:
    """Shoelace area of a closed ring of (lat, lon) pairs, in square metres."""
    if len(ring) < 3:
        return 0.0
    pts = [proj.xy(lat, lon) for lat, lon in ring]
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    total = 0.0
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def centroid(coords: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """Mean of (lat, lon) pairs."""
    n = len(coords)
    return (sum(c[0] for c in coords) / n, sum(c[1] for c in coords) / n)


def polygon_centroid(
    ring: Sequence[tuple[float, float]], proj: Projection
) -> tuple[float, float]:
    """Area-weighted centroid of a closed ring of (lat, lon) pairs.

    The plain vertex mean is pulled toward whichever edge happens to be mapped
    with more nodes, which for long thin panel rows can sit well off-centre.
    """
    if len(ring) < 3:
        return centroid(ring)
    pts = [proj.xy(lat, lon) for lat, lon in ring]
    if pts[0] != pts[-1]:
        pts.append(pts[0])

    area2 = 0.0
    cx = cy = 0.0
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        cross = x1 * y2 - x2 * y1
        area2 += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross

    if abs(area2) < 1e-9:  # degenerate ring
        return centroid(ring)
    cx /= 3.0 * area2
    cy /= 3.0 * area2
    return proj.latlon(cx, cy)


def bbox_tiles(
    polylines: Iterable[Sequence[LonLat]], pad_m: float, tile_deg: float
) -> list[tuple[float, float, float, float]]:
    """Fixed-grid tiles covering the polylines buffered by `pad_m`.

    Returns (south, west, north, east) in degrees. The grid is anchored at
    whole multiples of `tile_deg`, so tiles are identical across routes and can
    be cached and reused.
    """
    tiles: set[tuple[int, int]] = set()

    def add(lat: float, lon: float) -> None:
        pad_lat = pad_m / _M_PER_DEG_LAT
        denom = _M_PER_DEG_LON_EQ * math.cos(math.radians(lat))
        pad_lon = pad_m / denom if denom > 1.0 else pad_lat
        for la in (lat - pad_lat, lat + pad_lat):
            for lo in (lon - pad_lon, lon + pad_lon):
                tiles.add((int(math.floor(la / tile_deg)), int(math.floor(lo / tile_deg))))

    for line in polylines:
        pts = list(line)
        for i, (lon, lat) in enumerate(pts):
            add(lat, lon)
            if i + 1 >= len(pts):
                break
            # Walk the segment in sub-tile steps. Sampling only the vertices
            # would skip tiles that a long segment passes straight through --
            # which happens as soon as the caller hands us a simplified line.
            lon2, lat2 = pts[i + 1]
            span = max(abs(lat2 - lat), abs(lon2 - lon))
            steps = int(span / (tile_deg * 0.5))
            for s in range(1, steps + 1):
                f = s / (steps + 1)
                add(lat + (lat2 - lat) * f, lon + (lon2 - lon) * f)
    out = []
    for ty, tx in sorted(tiles):
        south, west = ty * tile_deg, tx * tile_deg
        out.append((south, west, south + tile_deg, west + tile_deg))
    return out
