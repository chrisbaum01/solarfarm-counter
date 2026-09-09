"""Routing via OSRM, and extraction of the Autobahn portions of a route."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx

from . import config
from .geometry import LonLat

# OSRM reports road references like "A 9", "A9", or "A 9; B 2" for concurrencies.
_AUTOBAHN_RE = re.compile(r"(?:^|;)\s*A\s?\d+", re.IGNORECASE)
_BUNDESSTRASSE_RE = re.compile(r"(?:^|;)\s*B\s?\d+", re.IGNORECASE)


class RoutingError(RuntimeError):
    pass


@dataclass
class Route:
    total_km: float
    duration_min: float
    matched_km: float
    refs: list[str]
    # One polyline per contiguous run of matching road. Kept separate on
    # purpose: joining them would create a phantom straight segment across the
    # gap between runs (e.g. A9 -> A73) and drag unrelated features into the
    # corridor.
    polylines: list[list[LonLat]] = field(default_factory=list)
    full_geometry: list[LonLat] = field(default_factory=list)


def _normalise_ref(ref: str) -> str:
    match = _AUTOBAHN_RE.search(ref)
    if not match:
        return ref.strip()
    token = match.group(0).lstrip(";").strip()
    digits = re.sub(r"[^\d]", "", token)
    return f"A {digits}"


def extract_runs(steps: list[dict], include_bundesstrasse: bool = False) -> Route:
    """Pull the motorway portions out of an OSRM leg's steps.

    OSRM tags each step with the road's `ref`, which is what makes Autobahn-only
    scanning exact rather than heuristic -- no map matching required.
    """
    polylines: list[list[LonLat]] = []
    current: list[LonLat] = []
    refs: list[str] = []
    matched_m = 0.0

    for step in steps:
        ref = step.get("ref") or ""
        is_match = bool(_AUTOBAHN_RE.search(ref)) or (
            include_bundesstrasse and bool(_BUNDESSTRASSE_RE.search(ref))
        )
        if not is_match:
            if current:
                polylines.append(current)
                current = []
            continue

        coords = step.get("geometry", {}).get("coordinates", [])
        if not coords:
            continue
        matched_m += step.get("distance", 0.0)
        norm = _normalise_ref(ref)
        if norm not in refs:
            refs.append(norm)

        # Continue the current run if this step starts where the last ended.
        if current and current[-1] == coords[0]:
            current.extend(coords[1:])
        elif current and _close(current[-1], coords[0]):
            current.extend(coords)
        else:
            if current:
                polylines.append(current)
            current = list(coords)

    if current:
        polylines.append(current)

    return Route(
        total_km=0.0,
        duration_min=0.0,
        matched_km=matched_m / 1000.0,
        refs=refs,
        polylines=polylines,
    )


def _close(a: LonLat, b: LonLat, tol: float = 1e-4) -> bool:
    """Within roughly 10 m -- tolerates rounding between adjacent steps."""
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


async def route(
    start: tuple[float, float],
    end: tuple[float, float],
    include_bundesstrasse: bool = False,
) -> Route:
    """Fetch a driving route and return its motorway portions."""
    coords = f"{start[1]:.6f},{start[0]:.6f};{end[1]:.6f},{end[0]:.6f}"
    params = {
        "overview": "full",
        "geometries": "geojson",
        "steps": "true",
        "annotations": "false",
    }
    headers = {"User-Agent": config.USER_AGENT}
    async with httpx.AsyncClient(timeout=90.0, headers=headers) as client:
        resp = await client.get(f"{config.OSRM_URL}/{coords}", params=params)

    if resp.status_code != 200:
        raise RoutingError(f"routing service returned HTTP {resp.status_code}")
    data = resp.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RoutingError(f"no route found ({data.get('code', 'unknown error')})")

    osrm_route = data["routes"][0]
    steps: list[dict] = []
    for leg in osrm_route.get("legs", []):
        steps.extend(leg.get("steps", []))

    result = extract_runs(steps, include_bundesstrasse=include_bundesstrasse)
    result.total_km = osrm_route.get("distance", 0.0) / 1000.0
    result.duration_min = osrm_route.get("duration", 0.0) / 60.0
    result.full_geometry = osrm_route.get("geometry", {}).get("coordinates", [])
    return result
