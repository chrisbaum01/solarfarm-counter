"""FastAPI application."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from . import cache, config, geocode, mastr, overpass, routing
from .models import SearchResponse
from .pipeline import SearchParams, search

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

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


@app.get("/api/search/stream", include_in_schema=False)
async def api_search_stream(
    origin: str = Query(alias="from", min_length=1),
    destination: str = Query(alias="to", min_length=1),
    corridor_m: float = Query(config.DEFAULT_CORRIDOR_M, gt=0, le=config.MAX_CORRIDOR_M),
    link_m: float = Query(config.DEFAULT_LINK_M, gt=0, le=config.MAX_LINK_M),
    min_area_m2: float = Query(config.DEFAULT_MIN_AREA_M2, ge=0),
    include_bundesstrasse: bool = Query(False),
    use_mastr: bool = Query(True),
    exclude_on_buildings: bool = Query(False),
    require_corroboration: bool = Query(True),
) -> StreamingResponse:
    """Same search as /api/search, streamed as server-sent events.

    A cold search can take minutes while Overpass tiles are fetched. Without
    this the page shows nothing but a spinner and no way to tell a slow search
    from a stuck one.
    """
    params = SearchParams(
        corridor_m=corridor_m,
        link_m=link_m,
        min_area_m2=min_area_m2,
        include_bundesstrasse=include_bundesstrasse,
        use_mastr=use_mastr,
        exclude_on_buildings=exclude_on_buildings,
        require_corroboration=require_corroboration,
    )

    async def events():
        queue: asyncio.Queue = asyncio.Queue()

        def on_progress(update: dict) -> None:
            queue.put_nowait(("progress", update))

        async def run():
            try:
                result = await search(origin, destination, params, progress=on_progress)
                queue.put_nowait(("result", result))
            except (geocode.GeocodeError, routing.RoutingError, overpass.OverpassError) as exc:
                queue.put_nowait(("error", {"detail": str(exc)}))
            except Exception as exc:  # noqa: BLE001 - surfaced to the client
                log.exception("streamed search failed")
                queue.put_nowait(("error", {"detail": f"Unexpected error: {exc}"}))
            finally:
                queue.put_nowait(("done", None))

        task = asyncio.create_task(run())
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "done":
                    break
                yield f"event: {kind}\ndata: {json.dumps(payload, default=str)}\n\n"
        finally:
            # The browser closing the tab must not leave the search running.
            if not task.done():
                task.cancel()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
