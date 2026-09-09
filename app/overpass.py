"""Overpass API client: fixed-grid tiles, SQLite cache, backoff, endpoint failover."""

from __future__ import annotations

import asyncio
import hashlib
import logging

import httpx

from . import cache, config

log = logging.getLogger(__name__)

# `out geom` rather than `out tags center`: geometry comes back in the same
# round-trip, so cluster areas need no second pass over the same features.
_QUERY_TEMPLATE = """[out:json][timeout:90];
(
  way["power"="plant"]["plant:source"="solar"]({s},{w},{n},{e});
  relation["power"="plant"]["plant:source"="solar"]({s},{w},{n},{e});
  way["power"="generator"]["generator:source"="solar"]({s},{w},{n},{e});
  relation["power"="generator"]["generator:source"="solar"]({s},{w},{n},{e});
  node["power"="generator"]["generator:source"="solar"]({s},{w},{n},{e});
);
out geom;"""


def _tile_key(tile: tuple[float, float, float, float]) -> str:
    s, w, _, _ = tile
    return f"tile:v{config.QUERY_VERSION}:{s:.4f}:{w:.4f}"


def _buildings_key(points: list[tuple[float, float]], radius_m: float) -> str:
    joined = ";".join(f"{lat:.5f},{lon:.5f}" for lat, lon in sorted(points))
    digest = hashlib.sha1(f"{radius_m}|{joined}".encode()).hexdigest()[:20]
    return f"bld:v{config.QUERY_VERSION}:{digest}"


async def fetch_buildings_near(
    points: list[tuple[float, float]], radius_m: float
) -> list[list[tuple[float, float]]] | None:
    """Building outlines near the given points, as lists of (lat, lon) rings.

    Used to catch rooftop arrays that carry no rooftop tag at all -- OSM is full
    of solar nodes whose only clue that they sit on a barn roof is that they are
    inside the barn. Returns None if the lookup could not be done, so the caller
    can carry on without silently pretending there are no buildings.
    """
    if not points:
        return []

    # Overpass reads an `around` coordinate list as a single linestring, and an
    # oversized one quietly returns nothing rather than failing: 401 points gave
    # 0 buildings while the same point alone gave 1. Query in small batches.
    batches = [
        points[i : i + config.BUILDING_BATCH_SIZE]
        for i in range(0, len(points), config.BUILDING_BATCH_SIZE)
    ]

    rings: list[list[tuple[float, float]]] = []
    any_failed = False
    semaphore = asyncio.Semaphore(config.OVERPASS_CONCURRENCY)
    headers = {"User-Agent": config.USER_AGENT}

    async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, headers=headers) as client:

        async def one(batch: list[tuple[float, float]]):
            key = _buildings_key(batch, radius_m)
            cached = cache.get(key, config.TILE_TTL_SECONDS)
            if cached is not None:
                return [[tuple(p) for p in ring] for ring in cached]

            coords = ",".join(f"{lat:.5f},{lon:.5f}" for lat, lon in batch)
            query = (
                f"[out:json][timeout:90];"
                f'way["building"](around:{radius_m:.0f},{coords});'
                f"out geom;"
            )
            async with semaphore:
                for attempt in range(config.OVERPASS_MAX_RETRIES):
                    endpoint = config.OVERPASS_ENDPOINTS[
                        attempt % len(config.OVERPASS_ENDPOINTS)
                    ]
                    try:
                        resp = await client.post(endpoint, data={"data": query})
                        if resp.status_code in (429, 502, 503, 504):
                            await asyncio.sleep(config.OVERPASS_BACKOFF_SECONDS[attempt])
                            continue
                        resp.raise_for_status()
                        found = [
                            [(p["lat"], p["lon"]) for p in el["geometry"]]
                            for el in resp.json().get("elements", [])
                            if el.get("geometry")
                        ]
                        cache.put(key, found)
                        return found
                    except (httpx.HTTPError, ValueError) as exc:
                        log.warning("building lookup failed on %s: %s", endpoint, exc)
                        await asyncio.sleep(config.OVERPASS_BACKOFF_SECONDS[attempt])
            raise OverpassError("building batch failed")

        results = await asyncio.gather(
            *(one(b) for b in batches), return_exceptions=True
        )

    for result in results:
        if isinstance(result, BaseException):
            any_failed = True
            continue
        rings.extend(result)

    # A partial answer would silently under-detect rooftops, so report the whole
    # check as unavailable rather than pretending the gaps contained no buildings.
    if any_failed and not rings:
        log.error("building lookup failed; rooftop geometry check skipped")
        return None
    return rings


class OverpassError(RuntimeError):
    pass


class OverpassTimeout(OverpassError):
    """The search deadline expired before any map data arrived."""


async def _fetch_tile(
    client: httpx.AsyncClient, tile: tuple[float, float, float, float]
) -> tuple[list[dict], str]:
    s, w, n, e = tile
    query = _QUERY_TEMPLATE.format(s=s, w=w, n=n, e=e)

    last_error: Exception | None = None
    for attempt in range(config.OVERPASS_MAX_RETRIES):
        # Rotate endpoints across attempts so a single overloaded mirror does
        # not sink the whole request.
        endpoint = config.OVERPASS_ENDPOINTS[attempt % len(config.OVERPASS_ENDPOINTS)]
        wait = config.OVERPASS_BACKOFF_SECONDS[attempt]
        try:
            resp = await client.post(endpoint, data={"data": query})
            if resp.status_code in (429, 502, 503, 504):
                log.warning(
                    "overpass %s from %s, retrying in %.0fs", resp.status_code, endpoint, wait
                )
                last_error = OverpassError(f"HTTP {resp.status_code} from {endpoint}")
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json().get("elements", []), endpoint
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            log.warning("overpass request to %s failed: %s", endpoint, exc)
            await asyncio.sleep(wait)

    raise OverpassError(f"all Overpass attempts failed for tile {tile}") from last_error


async def fetch_solar_features(
    tiles: list[tuple[float, float, float, float]],
    progress: callable | None = None,
    deadline: float | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """Fetch solar features for the given tiles, deduplicated by (type, id).

    Cached tiles resolve instantly; only misses hit the network, and those are
    fetched with limited concurrency to stay within Overpass fair-use limits.
    """
    elements: dict[tuple[str, int], dict] = {}
    misses: list[tuple[float, float, float, float]] = []
    hits = 0

    for tile in tiles:
        cached = cache.get(_tile_key(tile), config.TILE_TTL_SECONDS)
        if cached is None:
            misses.append(tile)
        else:
            hits += 1
            for el in cached.get("elements", []):
                elements[(el["type"], el["id"])] = el

    failed = 0
    timed_out = 0
    if misses:
        semaphore = asyncio.Semaphore(config.OVERPASS_CONCURRENCY)
        completed = 0
        headers = {"User-Agent": config.USER_AGENT}
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, headers=headers) as client:

            async def one(tile):
                nonlocal completed
                async with semaphore:
                    result, endpoint = await _fetch_tile(client, tile)
                    # Record the source. An empty tile is otherwise
                    # indistinguishable from one a broken mirror emptied.
                    cache.put(_tile_key(tile), {"endpoint": endpoint, "elements": result})
                    completed += 1
                    if progress:
                        progress(completed, len(misses))
                    return result

            # A single unavailable mirror should not throw away the 22 tiles
            # that did come back. Failures are counted and surfaced to the
            # caller, which reports the count as a lower bound -- quietly
            # returning a short count would read as a complete answer.
            tasks = [asyncio.ensure_future(one(t)) for t in misses]
            remaining = None
            if deadline is not None:
                remaining = max(0.0, deadline - asyncio.get_running_loop().time())

            done, pending = await asyncio.wait(tasks, timeout=remaining)
            for task in pending:
                # Out of time. Abandon the stragglers; whatever they had already
                # written to the cache is kept, so a retry resumes rather than
                # starting over.
                task.cancel()
                failed += 1
                timed_out += 1
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                log.warning("search deadline reached with %d tile(s) outstanding", len(pending))

            for task in done:
                try:
                    result = task.result()
                except (Exception, asyncio.CancelledError) as exc:
                    failed += 1
                    log.error("tile fetch failed: %s", exc)
                    continue
                for el in result:
                    elements[(el["type"], el["id"])] = el

        # Returning zero parks here would read as "there are none along this
        # route", which is a very different claim from "we could not look".
        if failed == len(misses) and hits == 0:
            if timed_out == failed:
                raise OverpassTimeout(
                    "the search hit its time limit before any map data arrived. "
                    "Overpass is likely overloaded — try again shortly, or raise "
                    "SOLARFARM_SEARCH_TIMEOUT."
                )
            raise OverpassError(
                "every OpenStreetMap data request failed; the Overpass mirrors "
                "are likely rate-limiting or down. Try again in a few minutes."
            )

    return list(elements.values()), {
        "tiles_total": len(tiles),
        "tiles_cached": hits,
        "tiles_fetched": len(misses) - failed,
        "tiles_failed": failed,
        "tiles_timed_out": timed_out,
    }
