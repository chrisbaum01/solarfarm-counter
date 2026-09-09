"""Marktstammdatenregister (MaStR) as a second source of solar parks.

OpenStreetMap only contains what a volunteer has traced, and commercial solar
parks are frequently never drawn -- the ANUMAR fields on the A8 near Bergkirchen
are visible on satellite imagery yet have no OSM presence whatsoever. MaStR is
the Bundesnetzagentur's register of every generating unit in Germany and
registration is legally mandatory, so it fills exactly that gap.

The trade-off is that MaStR gives a registered point location rather than a
traced outline. Many ground-mount units do declare their land area, but the
position is a single coordinate, so parks found only here carry no geometry.

Build the dataset with `python scripts/build_mastr.py <export.zip>`.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from . import config


@dataclass(frozen=True)
class MastrUnit:
    mastr_id: str
    lat: float
    lon: float
    name: str | None
    park_name: str | None
    capacity_kw: float | None
    area_m2: float | None
    commissioned: str | None
    municipality: str | None

    @property
    def best_name(self) -> str | None:
        """Park name if the operator gave one, else the unit name."""
        return self.park_name or self.name


class MastrStore:
    """Read-only access to the pre-built MaStR solar dataset."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or config.MASTR_DB_PATH)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        return self.path.exists()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def meta(self) -> dict[str, str]:
        if not self.available():
            return {}
        with self._lock:
            rows = self._connect().execute("SELECT key, value FROM meta").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def query_bbox(self, south: float, west: float, north: float, east: float) -> list[MastrUnit]:
        if not self.available():
            return []
        with self._lock:
            rows = self._connect().execute(
                "SELECT * FROM units WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                (south, north, west, east),
            ).fetchall()
        return [
            MastrUnit(
                mastr_id=r["mastr_id"],
                lat=r["lat"],
                lon=r["lon"],
                name=r["name"],
                park_name=r["park_name"],
                capacity_kw=r["capacity_kw"],
                area_m2=r["area_m2"],
                commissioned=r["commissioned"],
                municipality=r["municipality"],
            )
            for r in rows
        ]

    def query_tiles(self, tiles: list[tuple[float, float, float, float]]) -> list[MastrUnit]:
        """Units inside any of the given tiles, deduplicated by MaStR id."""
        found: dict[str, MastrUnit] = {}
        for south, west, north, east in tiles:
            for unit in self.query_bbox(south, west, north, east):
                found[unit.mastr_id] = unit
        return list(found.values())


def to_elements(units: list[MastrUnit]) -> list[dict]:
    """Adapt MaStR units to the element shape the OSM pipeline already handles.

    Feeding both sources through one clustering pass is what prevents double
    counting: a park that is both traced in OSM and registered in MaStR merges
    into a single park, and its provenance records both sources.
    """
    elements = []
    for unit in units:
        elements.append(
            {
                "type": "mastr",
                "id": unit.mastr_id,
                "lat": unit.lat,
                "lon": unit.lon,
                "_source": "mastr",
                "_declared_area_m2": unit.area_m2,
                "_capacity_kw": unit.capacity_kw,
                # Deliberately no power=plant / plant:source=solar here. Those
                # tags exempt a cluster from the minimum-area filter, which is
                # meant for features OSM has explicitly declared a plant. Every
                # registry unit carrying them made the filter inert: a 6,000 m²
                # park survived a 10,000 m² threshold. A registry unit that
                # declares its area is filtered on that area like anything else.
                "tags": {**({"name": unit.best_name} if unit.best_name else {})},
            }
        )
    return elements
