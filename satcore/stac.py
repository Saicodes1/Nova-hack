import logging
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta

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

logger = logging.getLogger(__name__)


@dataclass
class Selection:
    scene: SceneRef
    assets: dict[str, list[Asset]]


@dataclass(frozen=True)
class Catalog:
    source: str
    url: str
    collection: str
    band_ids: bool = False  # assets are named B02… (Planetary Computer)


def catalogs() -> list[Catalog]:
    primary = os.getenv("STAC_PRIMARY", "https://earth-search.aws.element84.com/v1")
    result = [
        Catalog(
            "earth-search",
            primary,
            os.getenv("STAC_PRIMARY_COLLECTION", "sentinel-2-c1-l2a"),
        ),
        # Collection 1 does not reach back before 2019 everywhere. The older
        # Earth Search collection does.
        Catalog(
            "earth-search",
            primary,
            os.getenv("STAC_ARCHIVE_COLLECTION", "sentinel-2-l2a"),
        ),
        Catalog(
            "planetary-computer",
            os.getenv(
                "STAC_BACKUP", "https://planetarycomputer.microsoft.com/api/stac/v1"
            ),
            os.getenv("STAC_BACKUP_COLLECTION", "sentinel-2-l2a"),
            band_ids=True,
        ),
    ]
    unique = []
    for catalog in result:
        if catalog.collection and (catalog.url, catalog.collection) not in [
            (c.url, c.collection) for c in unique
        ]:
            unique.append(catalog)
    return unique


def _offset(item, raster: dict, catalog: Catalog) -> float:
    """Reflectance offset for one item.

    Since processing baseline 04.00 (January 2022) ESA adds 1000 to every
    digital number. Each catalog handles that differently, so indices from
    before and after 2022 only compare when the offset is undone exactly once.
    """
    properties = item.properties
    if catalog.band_ids:  # Planetary Computer serves ESA values with no metadata
        try:
            baseline = float(properties.get("s2:processing_baseline", 0))
        except (TypeError, ValueError):
            baseline = 0
        return -0.1 if baseline >= 4 else 0
    if properties.get("earthsearch:boa_offset_applied"):
        return 0  # pixels already shifted back; the metadata offset would double it
    return raster.get("offset", 0)


def _assets(items, catalog: Catalog) -> dict[str, list[Asset]]:
    result = defaultdict(list)
    for item in items:
        for key, alternate in BANDS.items():
            asset = item.assets.get(alternate if catalog.band_ids else key)
            if asset:
                meta = asset.extra_fields
                raster = (meta.get("raster:bands") or meta.get("bands") or [{}])[0]
                href = (
                    planetary_computer.sign(asset.href)
                    if catalog.source == "planetary-computer"
                    else asset.href
                )
                result[key].append(
                    Asset(
                        href,
                        raster.get("scale", 0.0001),
                        0 if key == "scl" else _offset(item, raster, catalog),
                        meta.get("file:size"),
                    )
                )
    return dict(result)


def _groups(aoi: AOI, catalog: Catalog, start: date, end: date, cloudy: bool):
    """Acquisitions that cover the whole area, one list of items per overpass."""
    client = Client.open(catalog.url, stac_io=StacApiIO(timeout=15, max_retries=1))
    items = client.search(
        collections=[catalog.collection],
        intersects=aoi.geometry,
        max_items=120 if cloudy else 40,
        datetime=[f"{start}T00:00:00Z", f"{end}T23:59:59Z"],
        query=None if cloudy else {"eo:cloud_cover": {"lt": 80}},
    ).items()
    groups = defaultdict(list)
    area = shape(aoi.geometry)
    for item in items:
        if item.datetime and item.geometry and start <= item.datetime.date() <= end:
            groups[(item.datetime.date(), item.properties.get("platform"))].append(item)
    return [
        sorted(items, key=lambda item: item.id)
        for items in groups.values()
        if unary_union([shape(item.geometry) for item in items]).covers(area)
    ]


def _clouds(aoi: AOI, grid: Grid, groups, catalog: Catalog):
    """Measure cloud over the area itself for each overpass.

    A tile can be cloudy while the area is clear, so the scene classification
    layer is read at 60 m for the area only. Returns (items, assets, percent
    obscured) and the read failures.
    """
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
    candidates = [
        (items, assets)
        for items in groups
        if all(band in (assets := _assets(items, catalog)) for band in BANDS)
    ]

    def cloud_layer(candidate):
        try:
            return read_band(candidate[1]["scl"], small, categorical=True)
        except RasterioError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=8) as pool:
        layers = list(pool.map(cloud_layer, candidates))
    measured, failures = [], []
    for (items, assets), scl in zip(candidates, layers, strict=True):
        if isinstance(scl, Exception):
            failures.append(scl)
            continue
        obscured = round(
            100 * (inside & ~valid_mask(scl)).sum() / max(1, inside.sum()), 1
        )
        measured.append((items, assets, obscured))
    return measured, failures


def _search(
    aoi: AOI,
    grid: Grid,
    target: date,
    role,
    window: int,
    catalog: Catalog,
    earliest: date | None,
    latest: date | None,
    exact: bool = False,
):
    if exact:
        window = 0
    groups = _groups(
        aoi,
        catalog,
        max(target - timedelta(days=window), earliest or date.min),
        min(target + timedelta(days=window), latest or date.max),
        cloudy=exact,
    )
    # Check the acquisitions nearest the requested date first.
    groups.sort(
        key=lambda items: (
            abs((items[0].datetime.date() - target).days),
            sum(i.properties.get("eo:cloud_cover", 100) for i in items) / len(items),
        )
    )
    measured, failures = _clouds(aoi, grid, groups[:8], catalog)
    ranked = []
    for items, assets, obscured in measured:
        drift = abs((items[0].datetime.date() - target).days)
        scene = SceneRef(
            role=role,
            item_ids=[i.id for i in items],
            datetime=items[0].datetime,
            requested_date=target,
            days_from_requested=drift,
            aoi_cloud_pct=obscured,
            source=catalog.source,
            asset_hrefs={
                k: [a.href.split("?")[0] for a in v] for k, v in assets.items()
            },
        )
        ranked.append((obscured + 0.5 * drift, Selection(scene, assets)))
    if not ranked and failures:
        raise failures[0]
    return min(ranked, key=lambda pair: pair[0])[1] if ranked else None


def select_scene(
    aoi: AOI,
    grid: Grid,
    target: date,
    role,
    window: int,
    earliest: date | None = None,
    latest: date | None = None,
    exact: bool = False,
):
    """Pick the clearest scene near the target date.

    Catalogs are tried in order. The next one is used when a catalog fails or
    simply holds no scene for that date, which is common before 2019.

    `earliest` and `latest` keep the scene on its own side of the comparison,
    so a "before" image is never taken from after the event.

    `exact` takes the acquisition of that very day, however cloudy it is. It is
    used when the user picked a specific overpass, and by standing watches.
    """
    errors, fallback = [], None
    for catalog in catalogs():
        try:
            found = _search(
                aoi, grid, target, role, window, catalog, earliest, latest, exact
            )
            if exact:
                if found:
                    return found
                continue
            if window < 30 and (not found or found.scene.aoi_cloud_pct > 60):
                found = (
                    _search(aoi, grid, target, role, 30, catalog, earliest, latest)
                    or found
                )
        except Exception as exc:
            logger.warning("%s/%s failed: %s", catalog.source, catalog.collection, exc)
            errors.append(exc)
            continue
        if found and found.scene.aoi_cloud_pct <= 60:
            return found
        if found and (
            fallback is None or found.scene.aoi_cloud_pct < fallback.scene.aoi_cloud_pct
        ):
            fallback = found
    if fallback is None and len(errors) == len(catalogs()):
        raise RuntimeError(
            "The Sentinel-2 catalogs are currently unavailable."
        ) from ExceptionGroup("Catalog errors", errors)
    return fallback


def passes(aoi: AOI, grid: Grid, start: date, end: date) -> list[dict]:
    """Every Sentinel-2 overpass of the area between two dates, with the share
    of the area hidden by cloud. The first catalog that holds data answers."""
    errors = []
    for catalog in catalogs():
        try:
            groups = _groups(aoi, catalog, start, end, cloudy=True)
            groups.sort(key=lambda items: items[0].datetime)
            measured, failures = _clouds(aoi, grid, groups[:40], catalog)
        except Exception as exc:
            logger.warning("%s/%s failed: %s", catalog.source, catalog.collection, exc)
            errors.append(exc)
            continue
        if not measured and failures:
            errors.append(failures[0])
            continue
        if measured:
            # Two satellites can pass on the same day; keep the clearer view.
            best = {}
            for items, _assets_, obscured in measured:
                day = items[0].datetime.date()
                if day not in best or obscured < best[day]["cloud_pct"]:
                    best[day] = {
                        "date": str(day),
                        "time_utc": items[0].datetime.isoformat(),
                        "platform": items[0].properties.get("platform"),
                        "cloud_pct": obscured,
                        "source": catalog.source,
                    }
            return [best[day] for day in sorted(best)]
    if errors and len(errors) == len(catalogs()):
        raise RuntimeError(
            "The Sentinel-2 catalogs are currently unavailable."
        ) from ExceptionGroup("Catalog errors", errors)
    return []


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
