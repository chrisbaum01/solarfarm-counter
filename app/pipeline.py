"""The search pipeline, kept free of HTTP concerns so it can be tested offline."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from . import cluster, config, geocode, mastr, overpass, routing
from .geometry import PolylineIndex, bbox_tiles, projection_for

_mastr_store = mastr.MastrStore()


@dataclass
class SearchParams:
    corridor_m: float = config.DEFAULT_CORRIDOR_M
    link_m: float = config.DEFAULT_LINK_M
    min_area_m2: float = config.DEFAULT_MIN_AREA_M2
    include_bundesstrasse: bool = False
    use_mastr: bool = True
    # Off by default: it costs one extra Overpass request per 20 candidates,
    # which is slow and unkind to a free service. The corroboration rule below
    # catches the same rooftop nodes for free.
    exclude_on_buildings: bool = False
    require_corroboration: bool = True


def analyse_route(
    route: routing.Route,
    elements: list[dict],
    params: SearchParams,
    buildings: list[list[tuple[float, float]]] | None = None,
) -> tuple[list[cluster.Park], dict]:
    """Filter and cluster raw elements against a route. Pure, no I/O.

    `buildings` are optional outlines used to drop untagged rooftop arrays; None
    means the lookup was unavailable, and nothing is dropped on that basis.
    """
    flat = [pt for line in route.polylines for pt in line]
    proj = projection_for(flat)
    index = PolylineIndex(route.polylines, proj)

    features, select_stats = cluster.select_features(
        elements, index, proj, params.corridor_m
    )
    features, on_buildings = cluster.drop_features_on_buildings(features, buildings, proj)
    parks, cluster_stats = cluster.build_parks(
        features, proj, params.link_m, params.min_area_m2, params.require_corroboration
    )
    return parks, {
        **select_stats,
        "on_building_excluded": on_buildings,
        "features_after_building_check": len(features),
        **cluster_stats,
    }


def candidate_points(
    route: routing.Route, elements: list[dict], params: SearchParams
) -> list[tuple[float, float]]:
    """Positions of in-corridor OSM features, for the building lookup.

    Only these need checking, so the query stays small however long the route:
    a few dozen points rather than every building along the motorway.
    """
    flat = [pt for line in route.polylines for pt in line]
    proj = projection_for(flat)
    index = PolylineIndex(route.polylines, proj)
    features, _ = cluster.select_features(elements, index, proj, params.corridor_m)
    return [
        (f.lat, f.lon)
        for f in features
        if f.element.get("_source") != "mastr"
        and (f.area_m2 is None or f.area_m2 <= config.BUILDING_CHECK_MAX_AREA_M2)
    ]


async def search(origin: str, destination: str, params: SearchParams) -> dict:
    """Full pipeline: geocode -> route -> Overpass tiles -> filter -> cluster.

    The whole search is bounded by `config.SEARCH_TIMEOUT_SECONDS`. Geocoding and
    routing are quick and have their own limits; the open-ended part is fetching
    Overpass tiles, so it gets whatever of the budget is left and abandons the
    rest. A search that runs out of time still returns its partial result with
    the count marked as a lower bound.
    """
    started = time.perf_counter()
    deadline = asyncio.get_running_loop().time() + config.SEARCH_TIMEOUT_SECONDS
    warnings: list[str] = []

    start = await geocode.geocode(origin)
    end = await geocode.geocode(destination)

    route = await routing.route(
        (start.lat, start.lon),
        (end.lat, end.lon),
        include_bundesstrasse=params.include_bundesstrasse,
    )

    road = "Autobahn or Bundesstraße" if params.include_bundesstrasse else "Autobahn"
    if not route.polylines:
        # Not an error: two adjacent towns can legitimately have no motorway
        # between them. Say so plainly instead of failing.
        return {
            "count": 0,
            "origin": start.__dict__,
            "destination": end.__dict__,
            "total_km": route.total_km,
            "matched_km": 0.0,
            "duration_min": route.duration_min,
            "refs": [],
            "parks": [],
            "route_geometry": route.full_geometry,
            "scanned_geometry": [],
            "params": params.__dict__,
            "stats": {"raw_elements": 0, "tiles_total": 0},
            "warnings": [
                f"This route uses no {road}, so there was nothing to scan."
            ],
            "elapsed_s": time.perf_counter() - started,
        }

    tiles = bbox_tiles(route.polylines, params.corridor_m, config.TILE_DEG)
    elements, tile_stats = await overpass.fetch_solar_features(tiles, deadline=deadline)

    # Both sources go through one clustering pass, so a park present in each is
    # merged rather than counted twice.
    mastr_stats: dict[str, int] = {}
    if params.use_mastr and _mastr_store.available():
        units = _mastr_store.query_tiles(tiles)
        mastr_elements = mastr.to_elements(units)
        mastr_stats["mastr_units_nearby"] = len(mastr_elements)
        elements = elements + mastr_elements
    elif params.use_mastr:
        warnings.append(
            "The Marktstammdatenregister dataset has not been built, so this "
            "count relies on OpenStreetMap alone and will miss parks nobody has "
            "mapped. Run scripts/build_mastr.py to add it."
        )

    # Catch rooftop arrays that carry no rooftop tag by checking whether they
    # sit on a building. Only in-corridor candidates are looked up, so this is
    # one small query regardless of route length.
    buildings = None
    if params.exclude_on_buildings:
        points = candidate_points(route, elements, params)
        buildings = await overpass.fetch_buildings_near(
            points, config.BUILDING_CHECK_RADIUS_M
        )
        if buildings is None:
            warnings.append(
                "The building cross-check could not be run, so rooftop arrays that "
                "carry no rooftop tag may still be counted."
            )

    parks, stats = analyse_route(route, elements, params, buildings)

    by_source = {"osm_only": 0, "mastr_only": 0, "both": 0}
    for park in parks:
        if park.sources == ["mastr"]:
            by_source["mastr_only"] += 1
        elif "mastr" in park.sources:
            by_source["both"] += 1
        else:
            by_source["osm_only"] += 1
    mastr_stats.update(by_source)

    if tile_stats.get("tiles_timed_out"):
        n = tile_stats["tiles_timed_out"]
        minutes = config.SEARCH_TIMEOUT_SECONDS / 60
        warnings.append(
            f"The {minutes:.0f}-minute time limit was reached with {n} of "
            f"{tile_stats['tiles_total']} map areas still outstanding, so this count is a "
            "lower bound. Areas already fetched were cached — re-run the search and it "
            "will pick up where this one stopped."
        )
    unreachable = tile_stats.get("tiles_failed", 0) - tile_stats.get("tiles_timed_out", 0)
    if unreachable > 0:
        warnings.append(
            f"{unreachable} of {tile_stats['tiles_total']} map areas could not be fetched "
            "from OpenStreetMap, so this count is a lower bound. Re-run the search to "
            "retry just the missing areas."
        )

    dropped_points = stats.get("uncorroborated_points", 0)
    if dropped_points:
        warnings.append(
            f"{dropped_points} OpenStreetMap point(s) were left out: they have no mapped "
            "area and no entry in the official registry, which usually means an untagged "
            "rooftop array. Set require_corroboration=false to include them."
        )

    unknown_area = sum(1 for p in parks if p.area_m2 is None)
    if unknown_area:
        warnings.append(
            f"{unknown_area} park(s) are recorded only as a point, so their size is unknown "
            "and the minimum-area filter could not be applied to them."
        )

    declared = sum(1 for p in parks if p.area_is_declared)
    if declared:
        warnings.append(
            f"{declared} park(s) have no traced outline, so their area is the operator's "
            "declared figure from the registry rather than a measured footprint."
        )

    registry_only = mastr_stats.get("mastr_only", 0)
    if registry_only:
        warnings.append(
            f"{registry_only} park(s) come from the official registry alone and are absent "
            "from OpenStreetMap. Their position is a registered point, so the distance from "
            "the road is approximate."
        )

    return {
        "count": len(parks),
        "origin": start.__dict__,
        "destination": end.__dict__,
        "total_km": route.total_km,
        "matched_km": route.matched_km,
        "duration_min": route.duration_min,
        "refs": route.refs,
        "parks": [p.__dict__ for p in parks],
        "route_geometry": route.full_geometry,
        "scanned_geometry": route.polylines,
        "params": params.__dict__,
        "stats": {**stats, **tile_stats, **mastr_stats},
        "warnings": warnings,
        "elapsed_s": time.perf_counter() - started,
    }
