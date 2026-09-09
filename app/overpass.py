"""Overpass API client: fixed-grid tiles, SQLite cache, backoff, endpoint failover."""

from __future__ import annotations

import asyncio
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


class OverpassError(RuntimeError):
    pass


async def _fetch_tile(client: httpx.AsyncClient, tile: tuple[float, float, float, float]) -> list[dict]:
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
            return resp.json().get("elements", [])
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            log.warning("overpass request to %s failed: %s", endpoint, exc)
            await asyncio.sleep(wait)

    raise OverpassError(f"all Overpass attempts failed for tile {tile}") from last_error


async def fetch_solar_features(
    tiles: list[tuple[float, float, float, float]],
    progress: callable | None = None,
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
            for el in cached:
                elements[(el["type"], el["id"])] = el

    failed = 0
    if misses:
        semaphore = asyncio.Semaphore(config.OVERPASS_CONCURRENCY)
        done = 0
        headers = {"User-Agent": config.USER_AGENT}
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, headers=headers) as client:

            async def one(tile):
                nonlocal done
                async with semaphore:
                    result = await _fetch_tile(client, tile)
                    cache.put(_tile_key(tile), result)
                    done += 1
                    if progress:
                        progress(done, len(misses))
                    return result

            # A single unavailable mirror should not throw away the 22 tiles
            # that did come back. Failures are counted and surfaced to the
            # caller, which reports the count as a lower bound -- quietly
            # returning a short count would read as a complete answer.
            results = await asyncio.gather(
                *(one(t) for t in misses), return_exceptions=True
            )
            for result in results:
                if isinstance(result, BaseException):
                    failed += 1
                    log.error("tile fetch failed: %s", result)
                    continue
                for el in result:
                    elements[(el["type"], el["id"])] = el

        if failed == len(misses) and hits == 0:
            raise OverpassError(
                "every OpenStreetMap data request failed; the Overpass mirrors "
                "are likely rate-limiting or down. Try again in a few minutes."
            )

    return list(elements.values()), {
        "tiles_total": len(tiles),
        "tiles_cached": hits,
        "tiles_fetched": len(misses) - failed,
        "tiles_failed": failed,
    }
