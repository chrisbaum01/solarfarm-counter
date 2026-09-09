"""Turn raw OSM solar features into distinct solar parks.

OSM maps one large park as anything from a single tagged polygon to dozens of
separate panel-row ways, so the raw element count is not an answer. Three steps
make it one: drop rooftop PV, merge features that are near each other, then
discard clusters too small to be a park.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .geometry import (
    PolylineIndex,
    Projection,
    centroid,
    distance_to_ring_m,
    polygon_centroid,
    ring_area_m2,
)

_ROOF_LOCATIONS = {"roof", "rooftop", "roof_top"}


def is_rooftop(tags: dict) -> bool:
    """Rooftop PV is not a solar park.

    This is the single biggest source of false positives: on the Munich ->
    Nuremberg probe, 195 of 610 raw elements were rooftop installations.
    """
    if tags.get("location") in _ROOF_LOCATIONS:
        return True
    if tags.get("generator:place") in _ROOF_LOCATIONS:
        return True
    # A generator mapped directly onto a building outline, or carrying building
    # geometry tags, is a roof array regardless of how it is tagged otherwise.
    if tags.get("building") is not None:
        return True
    if tags.get("roof:shape") is not None:
        return True
    return False


def element_geometry(element: dict) -> list[tuple[float, float]]:
    """All (lat, lon) vertices of an element, for centroid purposes."""
    if element["type"] in ("node", "mastr"):
        return [(element["lat"], element["lon"])]
    if "geometry" in element:
        return [(p["lat"], p["lon"]) for p in element["geometry"]]
    if element["type"] == "relation":
        pts: list[tuple[float, float]] = []
        for member in element.get("members", []):
            for p in member.get("geometry", []) or []:
                pts.append((p["lat"], p["lon"]))
        return pts
    if "center" in element:
        return [(element["center"]["lat"], element["center"]["lon"])]
    return []


def element_area_m2(element: dict, proj: Projection) -> float | None:
    """Area in m2, or None when the element carries no measurable footprint.

    Nodes have no geometry, so their area is genuinely unknown -- not zero.
    Returning None keeps that distinction, so a node-only cluster can be
    reported honestly rather than silently dropped by the size filter.
    """
    if element["type"] == "mastr":
        # MaStR has no outline, but operators of ground-mount units declare the
        # land area they occupy. That is a stated figure rather than a measured
        # one, so it is used only when no traced geometry is available.
        return element.get("_declared_area_m2")
    if element["type"] == "node":
        return None
    if element["type"] == "way":
        ring = element_geometry(element)
        return ring_area_m2(ring, proj) if len(ring) >= 3 else None
    if element["type"] == "relation":
        total = 0.0
        found = False
        for member in element.get("members", []):
            geom = member.get("geometry")
            if not geom:
                continue
            ring = [(p["lat"], p["lon"]) for p in geom]
            if len(ring) < 3:
                continue
            area = ring_area_m2(ring, proj)
            # Multipolygon: outer rings add, inner rings (holes) subtract.
            if member.get("role") == "inner":
                total -= area
            else:
                total += area
            found = True
        return max(total, 0.0) if found else None
    return None


@dataclass
class Park:
    osm_ids: list[str]
    name: str | None
    lat: float
    lon: float
    area_m2: float | None
    distance_m: float
    km_along_route: float
    feature_count: int
    operator: str | None = None
    sources: list[str] = field(default_factory=list)
    capacity_kw: float | None = None
    area_is_declared: bool = False

    @property
    def area_known(self) -> bool:
        return self.area_m2 is not None


@dataclass
class Feature:
    element: dict
    lat: float
    lon: float
    area_m2: float | None
    distance_m: float
    offset_m: float


def select_features(
    elements: list[dict],
    index: PolylineIndex,
    proj: Projection,
    corridor_m: float,
) -> tuple[list[Feature], dict[str, int]]:
    """Keep non-rooftop features whose centroid lies inside the corridor."""
    kept: list[Feature] = []
    rooftop = 0
    outside = 0

    for element in elements:
        tags = element.get("tags", {}) or {}
        if is_rooftop(tags):
            rooftop += 1
            continue
        coords = element_geometry(element)
        if not coords:
            continue

        # Proximity is measured from the feature's NEAREST point, not its
        # centre. A 12-hectare park whose edge runs 50 m from the carriageway is
        # plainly "next to the Autobahn" even though its centroid may sit 600 m
        # away; judging it by the centre would wrongly discard it.
        distance = math.inf
        offset = 0.0
        for lat_i, lon_i in coords:
            d, o = index.distance_and_offset(lat_i, lon_i)
            if d < distance:
                distance, offset = d, o
        if distance > corridor_m:
            outside += 1
            continue

        # Position for display/clustering is the area-weighted centroid.
        if element["type"] == "way" and len(coords) >= 3:
            lat, lon = polygon_centroid(coords, proj)
        else:
            lat, lon = centroid(coords)
        kept.append(
            Feature(
                element=element,
                lat=lat,
                lon=lon,
                area_m2=element_area_m2(element, proj),
                distance_m=distance,
                offset_m=offset,
            )
        )

    return kept, {
        "raw_elements": len(elements),
        "rooftop_excluded": rooftop,
        "outside_corridor": outside,
        "features_in_corridor": len(kept),
    }


def drop_features_on_buildings(
    features: list[Feature],
    buildings: list[list[tuple[float, float]]] | None,
    proj: Projection,
    tolerance_m: float = 12.0,
) -> tuple[list[Feature], int]:
    """Remove solar features that sit on a building outline.

    The tag-based rooftop test only catches arrays somebody remembered to tag.
    Plenty are not: node/13155920665 on the A8 is a bare `power=generator`
    +`generator:source=solar` node with no location tag, no area to filter on,
    and it sits one metre inside a `building=farm_auxiliary` — PV on a farm
    shed, counted as a solar park.

    `buildings` of None means the lookup failed; in that case nothing is
    dropped, so a failed request cannot silently change the count.
    """
    if not buildings:
        return features, 0

    kept: list[Feature] = []
    dropped = 0
    for feature in features:
        # Registry units are already restricted to ground-mounted installations
        # at extract time, so a nearby building says nothing about them.
        if feature.element.get("_source") == "mastr":
            kept.append(feature)
            continue
        on_building = any(
            distance_to_ring_m(feature.lat, feature.lon, ring, proj) <= tolerance_m
            for ring in buildings
        )
        if on_building:
            dropped += 1
        else:
            kept.append(feature)
    return kept, dropped


def _union_find(features: list[Feature], proj: Projection, link_m: float) -> list[list[int]]:
    """Single-link clustering of features within `link_m` of each other."""
    n = len(features)
    parent = list(range(n))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # Grid-bucket by link distance so only nearby pairs are compared; the naive
    # O(n^2) sweep gets slow on long routes with thousands of features.
    pts = [proj.xy(f.lat, f.lon) for f in features]
    grid: dict[tuple[int, int], list[int]] = {}
    for i, (x, y) in enumerate(pts):
        grid.setdefault((int(x // link_m), int(y // link_m)), []).append(i)

    for i, (x, y) in enumerate(pts):
        gx, gy = int(x // link_m), int(y // link_m)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((gx + dx, gy + dy), ()):
                    if j <= i:
                        continue
                    if math.hypot(x - pts[j][0], y - pts[j][1]) <= link_m:
                        union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def build_parks(
    features: list[Feature],
    proj: Projection,
    link_m: float,
    min_area_m2: float,
    require_corroboration: bool = True,
) -> tuple[list[Park], dict[str, int]]:
    """Cluster features into parks and apply the size and evidence filters."""
    clusters = _union_find(features, proj, link_m)
    parks: list[Park] = []
    below_min = 0
    uncorroborated = 0

    for indices in clusters:
        members = [features[i] for i in indices]
        traced = [m for m in members if m.element["type"] in ("way", "relation")]
        registry = [m for m in members if m.element["type"] == "mastr"]

        # Prefer measured geometry over the operator's declared figure. Adding
        # both would double count a park that is present in each source.
        if any(m.area_m2 is not None for m in traced):
            area = sum(m.area_m2 for m in traced if m.area_m2 is not None)
            area_is_declared = False
        elif any(m.area_m2 is not None for m in registry):
            # Max, not sum. A single park is often registered as several units,
            # and each one tends to declare the whole site's land area, so
            # adding them multiplies the park's size by the unit count.
            area = max(m.area_m2 for m in registry if m.area_m2 is not None)
            area_is_declared = True
        else:
            area = None
            area_is_declared = False

        sources = sorted({m.element.get("_source", "osm") for m in members})
        capacities = [
            m.element.get("_capacity_kw") for m in registry
            if m.element.get("_capacity_kw") is not None
        ]
        capacity_kw = sum(capacities) if capacities else None

        # An explicitly tagged solar power plant is a solar park by definition,
        # whatever its size. The area threshold exists to separate real parks
        # from stray untagged panel arrays, so applying it to a feature OSM has
        # already declared a plant just discards good data.
        declared_plant = any(
            m.element.get("tags", {}).get("power") == "plant"
            and m.element.get("tags", {}).get("plant:source") == "solar"
            for m in members
        )

        # A cluster with no measurable geometry keeps area=None and survives the
        # size filter; dropping it would silently discard a real tagged park,
        # counting it as zero would be a lie. It is flagged in the response.
        if area is not None and area < min_area_m2 and not declared_plant:
            below_min += 1
            continue

        # A ground-mount park in Germany is legally required to be registered,
        # and a real one is almost always traced as a polygon. A bare OSM point
        # with no area and no registry unit backing it is therefore weak
        # evidence, and in practice is usually rooftop PV on a farm building --
        # e.g. node/13155920665, a panel node sitting inside a barn outline with
        # no rooftop tag to give it away. Requiring corroboration costs nothing
        # and no extra requests.
        if area is None and not registry and require_corroboration:
            uncorroborated += 1
            continue

        name = next(
            (m.element.get("tags", {}).get("name") for m in members
             if m.element.get("tags", {}).get("name")),
            None,
        )
        operator = next(
            (m.element.get("tags", {}).get("operator") for m in members
             if m.element.get("tags", {}).get("operator")),
            None,
        )
        nearest = min(members, key=lambda m: m.distance_m)
        parks.append(
            Park(
                osm_ids=[f"{m.element['type']}/{m.element['id']}" for m in members],
                name=name,
                lat=sum(m.lat for m in members) / len(members),
                lon=sum(m.lon for m in members) / len(members),
                area_m2=area,
                distance_m=nearest.distance_m,
                km_along_route=min(m.offset_m for m in members) / 1000.0,
                feature_count=len(members),
                operator=operator,
                sources=sources,
                capacity_kw=capacity_kw,
                area_is_declared=area_is_declared,
            )
        )

    parks.sort(key=lambda p: p.km_along_route)
    return parks, {
        "clusters": len(clusters),
        "below_min_area": below_min,
        "uncorroborated_points": uncorroborated,
        "parks": len(parks),
    }
