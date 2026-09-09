"""Response schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PlaceOut(BaseModel):
    query: str
    name: str = Field(description="Full display name of what was actually resolved")
    lat: float
    lon: float


class ParkOut(BaseModel):
    osm_ids: list[str]
    name: str | None = None
    operator: str | None = None
    lat: float
    lon: float
    area_m2: float | None = Field(
        default=None,
        description="Total footprint. None when the park is mapped only as point(s), "
        "so its size is genuinely unknown rather than zero.",
    )
    area_is_declared: bool = Field(
        default=False,
        description="True when the area is the operator's declared figure from the "
        "registry rather than measured from traced geometry.",
    )
    distance_m: float = Field(description="Distance from the motorway centerline")
    km_along_route: float = Field(description="Distance travelled along the scanned road")
    feature_count: int = Field(description="Source records merged into this park")
    sources: list[str] = Field(
        default_factory=list, description='Which sources found it: "osm", "mastr", or both'
    )
    capacity_kw: float | None = Field(
        default=None, description="Registered gross capacity, when known from MaStR"
    )


class SearchResponse(BaseModel):
    count: int
    origin: PlaceOut
    destination: PlaceOut
    total_km: float
    matched_km: float = Field(description="Kilometres of motorway actually scanned")
    duration_min: float
    refs: list[str]
    parks: list[ParkOut]
    route_geometry: list[list[float]] = Field(
        default_factory=list, description="Full route as [lon, lat] pairs"
    )
    scanned_geometry: list[list[list[float]]] = Field(
        default_factory=list,
        description="Scanned motorway runs, one polyline each, as [lon, lat] pairs",
    )
    params: dict
    stats: dict
    warnings: list[str] = Field(default_factory=list)
    elapsed_s: float = 0.0
