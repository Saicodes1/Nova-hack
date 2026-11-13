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

from .config import THRESHOLDS
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
    client, collection = _catalog(backup)
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
        sorted(newest_per_tile(items), key=lambda item: item.id)
        for items in groups.values()
        if unary_union([shape(item.geometry) for item in items]).covers(area)
    ]
    groups.sort(
        key=lambda items: (
            sum(i.properties.get("eo:cloud_cover", 100) for i in items) / len(items),
            abs((items[0].datetime.date() - target).days),
        )
    )
    preview = _preview_grid(aoi, grid)
    ranked, failures = [], []
    for items in groups[:5]:
        try:
            selection = _selection(items, preview, role, target, backup)
        except RasterioError as exc:
            failures.append(exc)
            continue
        if selection is None:
            continue
        scene = selection.scene
        ranked.append(
            (scene.aoi_cloud_pct + 0.5 * scene.days_from_requested, selection)
        )
    if not ranked and failures:
        raise failures[0]
    return min(ranked, key=lambda pair: pair[0])[1] if ranked else None


def tile_id(item) -> str:
    """MGRS tile of an item, from either catalog's metadata (falls back to the item ID)."""
    props = item.properties
    if props.get("s2:mgrs_tile"):
        return str(props["s2:mgrs_tile"])
    parts = [
        props.get(f"mgrs:{key}") for key in ("utm_zone", "latitude_band", "grid_square")
    ]
    return "".join(str(part) for part in parts) if all(parts) else item.id


def newest_per_tile(items) -> list:
    """Keep one item per tile: the most recent processing.

    Planetary Computer lists some acquisitions twice, the original processing and a
    later reprocessing of the same tile. Mosaicking both would mix processing baselines.
    """
    newest = {}
    for item in items:
        key = tile_id(item)
        generated = str(item.properties.get("s2:generation_time", ""))
        if key not in newest or generated > newest[key][0]:
            newest[key] = (generated, item)
    return [item for _, item in newest.values()]


def _catalog(backup: bool):
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
    return Client.open(url, stac_io=StacApiIO(timeout=10, max_retries=0)), collection


def _preview_grid(aoi: AOI, grid: Grid):
    """A 60 m version of the grid for quick cloud checks, with its AOI mask."""
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
    return small, inside


def _selection(items, preview, role, target: date, backup: bool):
    """Selection for one acquisition (same-day tiles), with AOI cloud %.

    Returns None when a band is missing; raises RasterioError if SCL can't be read.
    """
    small, inside = preview
    assets = _assets(items, backup)
    if any(band not in assets for band in BANDS):
        return None
    scl = read_band(assets["scl"], small, categorical=True)
    obscured = round(100 * (inside & ~valid_mask(scl)).sum() / max(1, inside.sum()), 1)
    scene = SceneRef(
        role=role,
        item_ids=[i.id for i in items],
        datetime=items[0].datetime,
        requested_date=target,
        days_from_requested=abs((items[0].datetime.date() - target).days),
        aoi_cloud_pct=obscured,
        source="planetary-computer" if backup else "earth-search",
        asset_hrefs={k: [a.href.split("?")[0] for a in v] for k, v in assets.items()},
    )
    return Selection(scene, assets)


def pinned_scene(
    aoi: AOI, grid: Grid, item_ids: list[str], role, target: date, backup=False
):
    """Selection for specific catalog items (e.g. pinned evaluation or demo scenes)."""
    client, collection = _catalog(backup)
    found = {
        item.id: item
        for item in client.search(collections=[collection], ids=item_ids).items()
    }
    missing = [identifier for identifier in item_ids if identifier not in found]
    if missing:
        raise LookupError(f"Scenes not in the catalog: {', '.join(missing)}")
    selection = _selection(
        [found[i] for i in sorted(item_ids)],
        _preview_grid(aoi, grid),
        role,
        target,
        backup,
    )
    if selection is None:
        raise LookupError(f"Scenes lack required bands: {', '.join(item_ids)}")
    return selection


def select_scene(
    aoi: AOI, grid: Grid, target: date, role, window: int, after: datetime | None = None
):
    """Best scene near `target`, trying both catalogs before widening the window.

    Order: Earth Search, Planetary Computer, then both again with the wide window.
    The first scene with usable cloud cover wins. Catalogs can lack scenes that the
    other has, so an empty or cloudy primary result falls through to the backup
    rather than straight to a wider window. A catalog that errors is skipped for the
    rest of the search. If nothing is usable, the least cloudy scene found is
    returned; None means neither catalog had a scene; RuntimeError means both failed.
    """
    settings = THRESHOLDS["scene_selection"]
    windows = [window] + (
        [settings["wide_window_days"]] if window < settings["wide_window_days"] else []
    )
    errors: dict[bool, Exception] = {}
    fallback = None
    for days in windows:
        for backup in (False, True):
            if backup in errors:
                continue
            try:
                found = _search(aoi, grid, target, role, days, backup, after)
            except Exception as exc:
                errors[backup] = exc
                continue
            if found is None:
                continue
            if found.scene.aoi_cloud_pct <= settings["usable_cloud_pct"]:
                return found
            if (
                fallback is None
                or found.scene.aoi_cloud_pct < fallback.scene.aoi_cloud_pct
            ):
                fallback = found
    if fallback is None and len(errors) == 2:
        raise RuntimeError(
            "Both Sentinel-2 catalogs are currently unavailable."
        ) from ExceptionGroup("Catalog errors", list(errors.values()))
    return fallback


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
