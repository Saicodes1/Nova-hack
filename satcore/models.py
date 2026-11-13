from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

Intent = Literal[
    "water_gain",
    "water_loss",
    "veg_loss",
    "veg_gain",
    "urban_growth",
    "burn",
    "visible_change",
]
Language = Literal["en", "ar", "hi"]


class AOI(BaseModel):
    geometry: dict
    name: str | None = None


class Grid(BaseModel):
    crs_epsg: int
    bounds: tuple[float, float, float, float]
    resolution: float = 10
    width: int
    height: int


class SceneRef(BaseModel):
    role: Literal["A", "B"]
    item_ids: list[str]
    datetime: datetime
    requested_date: date
    days_from_requested: int
    aoi_cloud_pct: float
    source: Literal["earth-search", "planetary-computer", "local-store", "synthetic"]
    asset_hrefs: dict[str, list[str]]


class UplinkCommand(BaseModel):
    analysis_id: UUID
    aoi: AOI
    date_a: date
    date_b: date
    intent: Intent
    window_days: int = Field(15, ge=1, le=30)
    season_baseline: bool = False
    demo_id: str | None = None
    # Pinned scenes (real demo cases): use these catalog items instead of searching.
    item_a: list[str] | None = None
    item_b: list[str] | None = None
    catalog: Literal["earth-search", "planetary-computer"] = "earth-search"


class FromTo(BaseModel):
    from_class: str
    ha: float


class ChangeStats(BaseModel):
    changed_ha: float
    changed_pct: float
    valid_overlap_pct: float
    aoi_ha: float
    n_polygons: int
    largest_polygon_ha: float
    from_to: list[FromTo]
    artefact_ha: float


class TrustReport(BaseModel):
    score: int
    level: Literal["High", "Medium", "Low"]
    flags: list[str]
    caveats: list[str]
    thresholds: dict


class LinkReport(BaseModel):
    mode: str
    packet_bytes: int
    raw_equivalent_bytes: int | None
    saved_pct: float | None
    simulated_seconds: float
    raw_simulated_seconds: float | None


class AnswerPacket(BaseModel):
    analysis_id: UUID
    intent: Intent
    aoi: AOI
    scenes: list[SceneRef]
    stats: ChangeStats | None
    trust: TrustReport
    status: Literal["ok", "no_change", "refused_cloudy", "no_scenes"]
    change_geojson: dict | None
    thumbnails: dict[str, str]
    image_bounds: list[list[float]]
    onboard_verdict: str
    timings_ms: dict[str, int]
    attribution: list[str]
    raw_equivalent_bytes: int | None = None
    link: LinkReport | None = None
