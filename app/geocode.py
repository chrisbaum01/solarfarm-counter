"""City name -> coordinates via Nominatim."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from . import cache, config

# Nominatim's usage policy allows at most 1 request/second.
_rate_limit = asyncio.Lock()


class GeocodeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Place:
    query: str
    name: str
    lat: float
    lon: float


async def geocode(name: str, country: str = "de") -> Place:
    """Resolve a place name, preferring German results.

    Returns Nominatim's top match along with its full display name, so the UI
    can show what was actually resolved rather than silently counting along a
    route between the wrong two towns.
    """
    key = f"geo:{country}:{name.strip().casefold()}"
    cached = cache.get(key, config.GEOCODE_TTL_SECONDS)
    if cached is not None:
        return Place(**cached)

    params = {
        "q": name,
        "format": "jsonv2",
        "limit": 1,
        "countrycodes": country,
        "addressdetails": 0,
    }
    headers = {"User-Agent": config.USER_AGENT}
    async with _rate_limit:
        async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
            resp = await client.get(config.NOMINATIM_URL, params=params)
        await asyncio.sleep(1.0)

    if resp.status_code != 200:
        raise GeocodeError(f"geocoding service returned HTTP {resp.status_code}")
    results = resp.json()
    if not results:
        raise GeocodeError(f"could not find a place in Germany named {name!r}")

    top = results[0]
    place = Place(
        query=name,
        name=top.get("display_name", name),
        lat=float(top["lat"]),
        lon=float(top["lon"]),
    )
    cache.put(key, place.__dict__)
    return place
