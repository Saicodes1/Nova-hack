import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import planetary_computer
from pystac_client import Client
from pystac_client.stac_api_io import StacApiIO
from rasterio.errors import RasterioError
from shapely.geometry import shape
from shapely.ops import unary_union

from .geo import aoi_mask
from .load import BANDS, Asset, read_band
from .masks import valid_mask
from .models import AOI, Grid, SceneRef


@dataclass
class Selection:
    scene: SceneRef
    assets: dict[str, list[Asset]]


def default_offset(item, key: str) -> float:
    """Reflectance offset to use when the catalog does not state one.

    From processing baseline 04.00 (25 Jan 2022) ESA adds 1000 to every L2A
    reflectance value. Earth Search publishes the matching -0.1 offset in
    `raster:bands`; Planetary Computer does not, so it is applied here.
    Without it, newer scenes read ~0.1 too bright and comparisons across
    2022 show false change.
    """
    if key == "scl":
        return 0.0
    baseline = str(item.properties.get("s2:processing_baseline", "0"))
    try:
        return -0.1 if float(baseline) >= 4.0 else 0.0
    except ValueError:
        return 0.0


def _assets(items, backup: bool) -> dict[str, list[Asset]]:
    result = defaultdict(list)
    for item in items:
        for key, alternate in BANDS.items():
            asset = item.assets.get(alternate if backup else key)
            if asset:
                meta = asset.extra_fields
                raster = (meta.get("raster:bands") or meta.get("bands") or [{}])[0]
                href = planetary_computer.sign(asset.href) if backup else asset.href
                offset = raster.get("offset")
                result[key].append(
                    Asset(
                        href,
                        raster.get("scale", 0.0001),
                        default_offset(item, key) if offset is None else offset,
                        meta.get("file:size"),
                    )
                )
    return dict(result)


def _search(
    aoi: AOI,
    grid: Grid,
    target: date,
    role,
    window: int,
    backup: bool,
    after: datetime | None,
):
    url = (
        os.getenv("STAC_BACKUP", "https://planetarycomputer.microsoft.com/api/stac/v1")
        if backup
        else os.getenv("STAC_PRIMARY", "https://earth-search.aws.element84.com/v1")
    )
    collection = (
        os.getenv("STAC_BACKUP_COLLECTION", "sentinel-2-l2a")
        if backup
        else os.getenv("STAC_PRIMARY_COLLECTION", "sentinel-2-c1-l2a")
    )
    client = Client.open(url, stac_io=StacApiIO(timeout=10, max_retries=0))
    items = client.search(
        collections=[collection],
        intersects=aoi.geometry,
        max_items=40,
        datetime=[
            (target - timedelta(days=window)).isoformat(),
            (target + timedelta(days=window)).isoformat(),
        ],
        query={"eo:cloud_cover": {"lt": 80}},
    ).items()
    groups = defaultdict(list)
    area = shape(aoi.geometry)
    for item in items:
        if (
            item.datetime
            and item.geometry
            and (after is None or item.datetime.date() > after.date())
        ):
            groups[(item.datetime.date(), item.properties.get("platform"))].append(item)
    groups = [
        sorted(items, key=lambda item: item.id)
        for items in groups.values()
        if unary_union([shape(item.geometry) for item in items]).covers(area)
    ]
    groups.sort(
        key=lambda items: (
            sum(i.properties.get("eo:cloud_cover", 100) for i in items) / len(items),
            abs((items[0].datetime.date() - target).days),
        )
    )
    small = grid.model_copy(
        update={
            "resolution": 60,
            "width": (grid.width + 5) // 6,
            "height": (grid.height + 5) // 6,
        }
    )
    inside = aoi_mask(aoi, small)
    if not inside.any():
        small, inside = grid, aoi_mask(aoi, grid)
    ranked, failures = [], []
    for items in groups[:5]:
        assets = _assets(items, backup)
        if any(band not in assets for band in BANDS):
            continue
        try:
            scl = read_band(assets["scl"], small, categorical=True)
        except RasterioError as exc:
            failures.append(exc)
            continue
        obscured = round(
            100 * (inside & ~valid_mask(scl)).sum() / max(1, inside.sum()), 1
        )
        drift = abs((items[0].datetime.date() - target).days)
        scene = SceneRef(
            role=role,
            item_ids=[i.id for i in items],
            datetime=items[0].datetime,
            requested_date=target,
            days_from_requested=drift,
            aoi_cloud_pct=obscured,
            source="planetary-computer" if backup else "earth-search",
            asset_hrefs={
                k: [a.href.split("?")[0] for a in v] for k, v in assets.items()
            },
        )
        ranked.append((obscured + 0.5 * drift, Selection(scene, assets)))
    if not ranked and failures:
        raise failures[0]
    return min(ranked, key=lambda pair: pair[0])[1] if ranked else None


def select_scene(
    aoi: AOI, grid: Grid, target: date, role, window: int, after: datetime | None = None
):
    try:
        first = _search(aoi, grid, target, role, window, False, after)
        if window < 30 and (not first or first.scene.aoi_cloud_pct > 60):
            wider = _search(aoi, grid, target, role, 30, False, after)
            return wider or first
        return first
    except Exception as primary_error:
        try:
            first = _search(aoi, grid, target, role, window, True, after)
            if window < 30 and (not first or first.scene.aoi_cloud_pct > 60):
                return _search(aoi, grid, target, role, 30, True, after) or first
            return first
        except Exception as backup_error:
            raise RuntimeError(
                "Both Sentinel-2 catalogs are currently unavailable."
            ) from ExceptionGroup("Catalog errors", [primary_error, backup_error])


def worldcover(aoi: AOI, grid: Grid):
    # ESA distributes its COGs on a regular 3° grid.
    from math import floor

    west, south, east, north = shape(aoi.geometry).bounds
    assets = []
    for lat in range(floor(south / 3) * 3, floor(north / 3) * 3 + 1, 3):
        for lon in range(floor(west / 3) * 3, floor(east / 3) * 3 + 1, 3):
            tile = f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}"
            assets.append(
                Asset(
                    f"https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
                )
            )
    try:
        return read_band(assets, grid, categorical=True)
    except RasterioError:
        return None
