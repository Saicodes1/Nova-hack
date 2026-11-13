from dataclasses import dataclass
from time import sleep

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.errors import RasterioError
from rasterio.vrt import WarpedVRT

from .geo import affine
from .models import Grid

BANDS = dict(
    zip(
        ("blue", "green", "red", "nir", "swir16", "swir22", "scl"),
        ("B02", "B03", "B04", "B08", "B11", "B12", "SCL"),
        strict=True,
    )
)


@dataclass(frozen=True)
class Asset:
    href: str
    scale: float = 0.0001
    offset: float = 0
    size: int | None = None


def read_band(assets: list[Asset], grid: Grid, categorical: bool = False) -> np.ndarray:
    """Read one band onto the analysis grid, retrying brief network failures.

    A download that is cut short can leave a truncated chunk in GDAL's
    in-process cache, and every later read of that tile then fails the same
    way for as long as the node runs. Retries therefore bypass the cache.
    """
    for attempt in range(3):
        try:
            return _read_band(assets, grid, categorical, fresh=attempt > 0)
        except RasterioError:
            if attempt == 2:
                raise
            sleep(1 + attempt)


def _read_band(
    assets: list[Asset], grid: Grid, categorical: bool, fresh: bool = False
) -> np.ndarray:
    result = np.full((grid.height, grid.width), np.nan, dtype=np.float32)
    uncached = (
        {
            "VSI_CACHE": "FALSE",
            "CPL_VSIL_CURL_NON_CACHED": ":".join(
                "/vsicurl/" + asset.href.split("?")[0] for asset in assets
            ),
        }
        if fresh
        else {"VSI_CACHE": "TRUE"}
    )
    with rasterio.Env(
        **uncached,
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        GDAL_HTTP_TIMEOUT="20",
        GDAL_HTTP_MAX_RETRY="3",
        GDAL_HTTP_RETRY_DELAY="1",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.TIF,.tiff",
        GDAL_HTTP_MULTIRANGE="YES",
        GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
    ):
        for asset in assets:
            with rasterio.open(asset.href) as source:
                with WarpedVRT(
                    source,
                    crs=f"EPSG:{grid.crs_epsg}",
                    transform=affine(grid),
                    width=grid.width,
                    height=grid.height,
                    nodata=0,
                    resampling=Resampling.nearest
                    if categorical
                    else Resampling.bilinear,
                ) as vrt:
                    pixels = vrt.read(1, masked=True).astype(np.float32).filled(np.nan)
            if not categorical:
                pixels = pixels * asset.scale + asset.offset
            missing = np.isnan(result)
            result[missing] = pixels[missing]
            if np.isfinite(result).all():
                break
    return np.nan_to_num(result).astype(np.uint8) if categorical else result
