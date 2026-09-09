"""Per-state statistics for commercial ground-mounted solar parks.

Computed entirely from the local Marktstammdatenregister extract, so it needs no
network and is independent of any route search.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass

from . import config
from .mastr import MastrStore

# Official land areas in km2 (Statistisches Bundesamt, Fläche und Bevölkerung).
# Densities are meaningless without these, and a wrong value silently skews one
# row, so they are listed explicitly rather than derived.
STATE_AREA_KM2 = {
    "Baden-Württemberg": 35_748,
    "Bayern": 70_542,
    "Berlin": 891,
    "Brandenburg": 29_654,
    "Bremen": 420,
    "Hamburg": 755,
    "Hessen": 21_116,
    "Mecklenburg-Vorpommern": 23_295,
    "Niedersachsen": 47_710,
    "Nordrhein-Westfalen": 34_113,
    "Rheinland-Pfalz": 19_858,
    "Saarland": 2_571,
    "Sachsen": 18_450,
    "Sachsen-Anhalt": 20_459,
    "Schleswig-Holstein": 15_804,
    "Thüringen": 16_202,
}

# A ground-mounted array below this is farm self-supply rather than a commercial
# park. It removes about 1,200 units but only ~0.3% of installed capacity.
MIN_COMMERCIAL_KW = 100.0

# Same rule the route counter uses, so "parks" means the same thing in both
# places: one site registered as several units is one park.
SITE_LINK_M = 300.0


@dataclass
class StateStats:
    state: str
    area_km2: int
    sites: int
    units: int
    capacity_mw: float
    sites_per_1000km2: float
    mw_per_1000km2: float
    median_site_mw: float | None
    largest_site_mw: float | None
    share_sites_pct: float
    share_mw_pct: float


def _cluster(points: list[tuple[float, float]], link_m: float) -> list[int]:
    """Single-link clustering; returns a cluster id per input point."""
    if not points:
        return []
    lat0 = sum(p[0] for p in points) / len(points)
    mx = 111_320 * math.cos(math.radians(lat0))
    my = 110_540
    xy = [(lon * mx, lat * my) for lat, lon in points]

    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, (x, y) in enumerate(xy):
        grid[(int(x // link_m), int(y // link_m))].append(i)

    parent = list(range(len(xy)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i, (x, y) in enumerate(xy):
        gx, gy = int(x // link_m), int(y // link_m)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((gx + dx, gy + dy), ()):
                    if j <= i:
                        continue
                    if math.hypot(x - xy[j][0], y - xy[j][1]) <= link_m:
                        ra, rb = find(i), find(j)
                        if ra != rb:
                            parent[rb] = ra
    return [find(i) for i in range(len(xy))]


def state_statistics(
    store: MastrStore | None = None,
    min_kw: float = MIN_COMMERCIAL_KW,
    link_m: float = SITE_LINK_M,
) -> dict:
    """Per-state figures for commercial ground-mount parks."""
    store = store or MastrStore()
    if not store.available():
        return {"available": False, "states": [], "totals": {}}

    # Resolved before taking the lock: MastrStore's lock is not reentrant, so
    # calling this inside the `with` block deadlocks.
    has_commercial = _has_commercial(store)
    query = (
        "SELECT lat, lon, capacity_kw, state, commercial, usage FROM units"
        if has_commercial
        else "SELECT lat, lon, capacity_kw, state, NULL, NULL FROM units"
    )
    with store._lock:  # noqa: SLF001 - same module family, read-only query
        rows = store._connect().execute(query).fetchall()  # noqa: SLF001

    # The registry's Nutzungsbereich turns out to be almost never set for
    # ground-mount units (0.19% of them): it describes the consumption side, and
    # a park feeding the grid has none. Claiming the register classified these
    # as commercial would be false, so report the coverage and let the caller
    # describe the filter honestly.
    usage_known = sum(1 for r in rows if r[5] is not None)
    usage_coverage = usage_known / len(rows) if rows else 0.0
    classification_usable = has_commercial and usage_coverage >= 0.5

    units = [
        (r[0], r[1], r[2] or 0.0, r[3])
        for r in rows
        # A state is required: without it the row cannot be attributed, and
        # inventing one would be worse than leaving it out of the table.
        if r[3] in STATE_AREA_KM2
        and (r[2] or 0.0) >= min_kw
        # `commercial` is absent until the registry has been re-extracted with
        # the usage field; treat unknown as included rather than silently
        # dropping everything.
        and (r[4] is None or r[4] == 1)
    ]

    labels = _cluster([(u[0], u[1]) for u in units], link_m)

    site_mw: dict[int, float] = defaultdict(float)
    site_state: dict[int, str] = {}
    units_per_state: dict[str, int] = defaultdict(int)
    for (lat, lon, kw, state), label in zip(units, labels):
        site_mw[label] += kw / 1000.0
        site_state.setdefault(label, state)
        units_per_state[state] += 1

    by_state: dict[str, list[float]] = defaultdict(list)
    for label, mw in site_mw.items():
        by_state[site_state[label]].append(mw)

    total_sites = len(site_mw)
    total_mw = sum(site_mw.values())

    stats: list[StateStats] = []
    for state, area in STATE_AREA_KM2.items():
        sizes = by_state.get(state, [])
        mw = sum(sizes)
        per = 1000.0 / area
        stats.append(
            StateStats(
                state=state,
                area_km2=area,
                sites=len(sizes),
                units=units_per_state.get(state, 0),
                capacity_mw=round(mw, 1),
                sites_per_1000km2=round(len(sizes) * per, 2),
                mw_per_1000km2=round(mw * per, 1),
                median_site_mw=round(statistics.median(sizes), 2) if sizes else None,
                largest_site_mw=round(max(sizes), 1) if sizes else None,
                share_sites_pct=round(100 * len(sizes) / total_sites, 1) if total_sites else 0.0,
                share_mw_pct=round(100 * mw / total_mw, 1) if total_mw else 0.0,
            )
        )

    stats.sort(key=lambda s: s.mw_per_1000km2, reverse=True)
    germany_area = sum(STATE_AREA_KM2.values())
    return {
        "available": True,
        "states": [s.__dict__ for s in stats],
        "totals": {
            "sites": total_sites,
            "units": len(units),
            "capacity_mw": round(total_mw, 1),
            "area_km2": germany_area,
            "sites_per_1000km2": round(total_sites * 1000 / germany_area, 2),
            "mw_per_1000km2": round(total_mw * 1000 / germany_area, 1),
        },
        "params": {"min_kw": min_kw, "link_m": link_m},
        "source": store.meta().get("source", "unknown"),
        "commercial_filter": classification_usable,
        "usage_coverage_pct": round(100 * usage_coverage, 2),
    }


def _has_commercial(store: MastrStore) -> bool:
    """True once the extract carries the registry's usage classification."""
    with store._lock:  # noqa: SLF001
        cols = {r[1] for r in store._connect().execute("PRAGMA table_info(units)")}  # noqa: SLF001
    return "commercial" in cols
