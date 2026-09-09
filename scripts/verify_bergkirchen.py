"""Check that the registry finds the A8 parks OpenStreetMap has no record of.

The two ANUMAR fields at Bergkirchen are clearly visible on satellite imagery
and completely absent from OSM, which is what motivated adding MaStR as a second
source. This is the regression check for that.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import mastr  # noqa: E402
from app.geometry import PolylineIndex, projection_for  # noqa: E402
from app.pipeline import SearchParams, search  # noqa: E402


async def main() -> None:
    store = mastr.MastrStore()
    if not store.available():
        sys.exit("MaStR dataset not built — run scripts/build_mastr.py first")
    print("dataset:", store.meta())

    print("\n--- registry units near the Bergkirchen screenshot ---")
    units = store.query_bbox(48.245, 11.258, 48.280, 11.315)
    print(f"found {len(units)} ground-mount unit(s) where OSM has zero")
    for u in sorted(units, key=lambda x: -(x.capacity_kw or 0))[:12]:
        area = f"{u.area_m2/10000:.1f} ha" if u.area_m2 else "n/a"
        cap = f"{u.capacity_kw/1000:.2f} MW" if u.capacity_kw else "n/a"
        print(f"  {u.lat:.5f},{u.lon:.5f}  {cap:>9}  {area:>8}  {u.best_name or '(unnamed)'}")

    print("\n--- München -> Augsburg, both sources ---")
    for corridor in (500, 1000):
        result = await search("München", "Augsburg", SearchParams(corridor_m=corridor))
        s = result["stats"]
        print(
            f"  corridor {corridor:>5} m -> {result['count']:>3} parks   "
            f"(OSM only {s.get('osm_only')}, both {s.get('both')}, registry only {s.get('mastr_only')})"
        )

    result = await search("München", "Augsburg", SearchParams(corridor_m=1000))
    print("\n  parks found only in the registry:")
    for p in result["parks"]:
        if p["sources"] == ["mastr"]:
            area = f"{p['area_m2']/10000:.1f} ha" if p["area_m2"] else "unknown"
            cap = f"{p['capacity_kw']/1000:.2f} MW" if p["capacity_kw"] else "n/a"
            print(
                f"    km {p['km_along_route']:>5.1f}  {int(p['distance_m']):>4} m  "
                f"{area:>8}  {cap:>9}  {p['name'] or '(unnamed)'}"
            )

    # Match by position, not by name: Google labels this site with the operator's
    # brand ("ANUMAR Solarpark Bergkirchen"), while the registry files it under
    # its project designation. A name check would wrongly report a miss.
    proj = projection_for([(11.2957, 48.2616)])
    index = PolylineIndex([[(11.2957, 48.2616), (11.2958, 48.2617)]], proj)
    hits = [
        p for p in result["parks"]
        if index.distance(p["lat"], p["lon"]) < 800
    ]
    print(f"\n  parks found at the Bergkirchen site: {len(hits)}")
    for p in hits:
        cap = f"{p['capacity_kw']/1000:.2f} MW" if p["capacity_kw"] else "n/a"
        print(
            f"    {p['name']}  —  {int(p['distance_m'])} m from the A8, {cap}, "
            f"{p['feature_count']} registered unit(s), sources={p['sources']}"
        )


asyncio.run(main())
