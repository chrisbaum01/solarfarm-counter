"""FastAPI application."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from . import cache, config, geocode, mastr, overpass, routing
from .models import SearchResponse
from .pipeline import SearchParams, search

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

app = FastAPI(
    title="Solarfarm Counter",
    description="Counts solar parks along the Autobahn between two German cities.",
    version="0.1.0",
)

_STATIC = Path(__file__).resolve().parent / "static"


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


@app.get("/api/health")
async def health() -> dict:
    store = mastr.MastrStore()
    return {
        "status": "ok",
        "cache": cache.stats(),
        "mastr": {"available": store.available(), **store.meta()},
    }


@app.get("/api/search", response_model=SearchResponse)
async def api_search(
    origin: str = Query(alias="from", min_length=1, description="Start city"),
    destination: str = Query(alias="to", min_length=1, description="Destination city"),
    corridor_m: float = Query(
        config.DEFAULT_CORRIDOR_M, gt=0, le=config.MAX_CORRIDOR_M,
        description="Max distance from the motorway centerline",
    ),
    link_m: float = Query(
        config.DEFAULT_LINK_M, gt=0, le=config.MAX_LINK_M,
        description="Features closer than this merge into one park",
    ),
    min_area_m2: float = Query(
        config.DEFAULT_MIN_AREA_M2, ge=0, description="Minimum park footprint"
    ),
    include_bundesstrasse: bool = Query(
        False, description="Also scan Bundesstraßen, not just Autobahnen"
    ),
    use_mastr: bool = Query(
        True, description="Also use the Marktstammdatenregister, not just OpenStreetMap"
    ),
    exclude_on_buildings: bool = Query(
        False,
        description="Also cross-check candidates against building outlines. More "
        "thorough but costs extra Overpass requests and is much slower",
    ),
    require_corroboration: bool = Query(
        True,
        description="Drop OpenStreetMap points that have no area and no registry "
        "entry backing them — usually untagged rooftop arrays",
    ),
) -> SearchResponse:
    params = SearchParams(
        corridor_m=corridor_m,
        link_m=link_m,
        min_area_m2=min_area_m2,
        include_bundesstrasse=include_bundesstrasse,
        use_mastr=use_mastr,
        exclude_on_buildings=exclude_on_buildings,
        require_corroboration=require_corroboration,
    )
    try:
        result = await search(origin, destination, params)
    except geocode.GeocodeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except routing.RoutingError as exc:
        raise HTTPException(status_code=502, detail=f"Routing failed: {exc}") from exc
    except overpass.OverpassTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except overpass.OverpassError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"OpenStreetMap data service is unavailable: {exc}",
        ) from exc
    return SearchResponse(**result)
