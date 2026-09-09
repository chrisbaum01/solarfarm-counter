"""Verify Overpass mirrors before trusting them.

A mirror that only holds one region answers an out-of-area query with HTTP 200
and zero elements. That is indistinguishable from "there is nothing here" and
gets cached as fact. overpass.osm.ch was in the endpoint list and did exactly
this: Zurich returned 18,577 buildings, Munich returned 0.

So "does it respond?" is the wrong test. This asks "does it return known German
data?", and reports latency on a realistic tile query.

    python scripts/check_mirrors.py                       # configured mirrors
    python scripts/check_mirrors.py https://other/api/interpreter
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app import config  # noqa: E402
from app.overpass import _QUERY_TEMPLATE  # noqa: E402

# A real, named ground-mount park beside the A9. Any mirror holding German data
# returns it; a regional one returns nothing.
COVERAGE_QUERY = "[out:json][timeout:30];way(118414892);out tags;"
EXPECTED_NAME = "PV-Freiflächenanlage Garching"
SAMPLE_TILE = (48.2, 11.6, 48.3, 11.7)


async def check(client: httpx.AsyncClient, endpoint: str) -> dict:
    host = endpoint.split("/")[2]
    # None means "could not determine" — an unreachable mirror has not been
    # shown to lack German data, and saying so would repeat the very confusion
    # this script exists to prevent.
    result = {"host": host, "covers_germany": None, "latency_s": None, "error": None}

    try:
        started = time.monotonic()
        resp = await client.post(endpoint, data={"data": COVERAGE_QUERY})
        if resp.status_code != 200:
            result["error"] = f"HTTP {resp.status_code}"
            return result
        elements = resp.json().get("elements", [])
        name = elements[0].get("tags", {}).get("name") if elements else None
        result["covers_germany"] = name == EXPECTED_NAME
        if not result["covers_germany"]:
            result["error"] = "no German data" if not elements else f"unexpected: {name}"
            return result
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        result["error"] = f"{type(exc).__name__}"
        return result

    s, w, n, e = SAMPLE_TILE
    try:
        started = time.monotonic()
        resp = await client.post(
            endpoint, data={"data": _QUERY_TEMPLATE.format(s=s, w=w, n=n, e=e)}
        )
        elapsed = time.monotonic() - started
        if resp.status_code != 200:
            result["error"] = f"tile query HTTP {resp.status_code}"
            return result
        result["latency_s"] = elapsed
        result["elements"] = len(resp.json().get("elements", []))
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"tile query {type(exc).__name__}"
    return result


async def main() -> None:
    endpoints = sys.argv[1:] or list(config.OVERPASS_ENDPOINTS)
    print(f"checking {len(endpoints)} mirror(s)\n")
    print(f"  {'mirror':<30} {'German data':<13} {'tile query':>12}  note")
    print("  " + "-" * 74)

    async with httpx.AsyncClient(
        timeout=120, headers={"User-Agent": config.USER_AGENT}
    ) as client:
        for endpoint in endpoints:
            r = await check(client, endpoint)
            covers = {True: "yes", False: "NO", None: "unknown"}[r["covers_germany"]]
            latency = f"{r['latency_s']:.1f}s" if r["latency_s"] is not None else "-"
            note = r["error"] or f"{r.get('elements', 0)} elements"
            print(f"  {r['host']:<30} {covers:<13} {latency:>12}  {note}")
            await asyncio.sleep(2)

    print(
        "\n  A mirror without German data must NOT be used: it returns HTTP 200 with\n"
        "  zero elements, which this app would cache as 'no solar farms here'."
    )


asyncio.run(main())
