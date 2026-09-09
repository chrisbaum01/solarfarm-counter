"""Tunable defaults and service endpoints."""

from __future__ import annotations

import os
from pathlib import Path

# --- Counting parameters (all overridable per request) ---------------------
# Chosen during planning against live Munich -> Nuremberg data. The thresholds
# visibly move the answer (35 / 24 / 17 parks at min-area 0 / 10k / 20k m2),
# so they are request parameters rather than constants.
DEFAULT_CORRIDOR_M = 500.0  # max distance from the Autobahn centerline
DEFAULT_LINK_M = 300.0  # features closer than this merge into one park
DEFAULT_MIN_AREA_M2 = 10_000.0  # clusters smaller than this are not "parks"

# Guard rails so a hand-edited query cannot ask for something absurd.
MAX_CORRIDOR_M = 5_000.0
MAX_LINK_M = 2_000.0

# --- Tiling ----------------------------------------------------------------
# Fixed grid, deliberately not route-shaped: tiles are shared between routes,
# so Munich->Nuremberg and Munich->Berlin reuse the same A9 tiles and the cache
# actually hits. Route-shaped bboxes took 215s and hit 429/504 repeatedly.
TILE_DEG = 0.1

# --- External services -----------------------------------------------------
# Nominatim and OSRM demo servers ask for an identifying User-Agent.
USER_AGENT = os.environ.get(
    "SOLARFARM_USER_AGENT",
    "solarfarm-counter/0.1 (https://github.com/chrisbaum01/solarfarm-counter)",
)

NOMINATIM_URL = os.environ.get(
    "SOLARFARM_NOMINATIM_URL", "https://nominatim.openstreetmap.org/search"
)
OSRM_URL = os.environ.get(
    "SOLARFARM_OSRM_URL", "https://router.project-osrm.org/route/v1/driving"
)
OVERPASS_ENDPOINTS = [
    e.strip()
    for e in os.environ.get(
        "SOLARFARM_OVERPASS_ENDPOINTS",
        "https://overpass-api.de/api/interpreter,"
        "https://overpass.kumi.systems/api/interpreter,"
        "https://overpass.osm.ch/api/interpreter",
    ).split(",")
    if e.strip()
]

# --- Cache -----------------------------------------------------------------
CACHE_PATH = Path(
    os.environ.get("SOLARFARM_CACHE", Path(__file__).resolve().parent.parent / "cache.sqlite3")
)

# Pre-built Marktstammdatenregister extract; absent until scripts/build_mastr.py
# has been run, in which case the app falls back to OpenStreetMap only.
MASTR_DB_PATH = Path(
    os.environ.get(
        "SOLARFARM_MASTR_DB",
        Path(__file__).resolve().parent.parent / "data" / "mastr_solar.sqlite3",
    )
)
# Bump when the Overpass query text changes, so stale rows are ignored rather
# than silently served under a different query definition.
QUERY_VERSION = 1
TILE_TTL_SECONDS = 30 * 24 * 3600
GEOCODE_TTL_SECONDS = 90 * 24 * 3600

# --- Networking ------------------------------------------------------------
HTTP_TIMEOUT = 180.0
# Overpass mirrors routinely answer 429/504 under load. Short retries just burn
# the budget without ever clearing the rate limit, so back off properly.
OVERPASS_BACKOFF_SECONDS = [5.0, 15.0, 30.0, 60.0, 90.0]
OVERPASS_MAX_RETRIES = len(OVERPASS_BACKOFF_SECONDS)
# Overpass fair use allows very few concurrent slots per client.
OVERPASS_CONCURRENCY = 2
