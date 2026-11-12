import math
import os

import numpy as np
import shapely
from pyproj import Geod, Transformer
from rasterio.features import geometry_mask
from rasterio.transform import from_origin
from shapely.geometry import Polygon, box, mapping, shape

from .models import AOI, Grid

GEOD = Geod(ellps="WGS84")


def validate_aoi(aoi: AOI) -> float:
    try:
        polygon = shape(aoi.geometry)
    except (ValueError, TypeError, AttributeError, KeyError, IndexError) as exc:
        raise ValueError("Provide a valid GeoJSON polygon.") from exc
    if not isinstance(polygon, Polygon) or polygon.is_empty or not polygon.is_valid:
        raise ValueError("Draw a closed polygon without crossing its edges.")
    west, south, east, north = polygon.bounds
    if (
        not (-180 <= west < east <= 180 and -80 <= south < north <= 84)
        or east - west >= 180
    ):
        raise ValueError(
            "Draw a local area within the Sentinel-2 latitude range without crossing the date line."
        )
    area = abs(GEOD.geometry_area_perimeter(polygon.normalize())[0]) / 1e6
    maximum = float(os.getenv("MAX_AOI_KM2", "50"))
    if not 0 < area <= maximum:
        raise ValueError(f"The selected area must be between 0 and {maximum:g} km².")
    return area


def utm_epsg(lon: float, lat: float) -> int:
    return (32600 if lat >= 0 else 32700) + min(60, int((lon + 180) / 6) + 1)


def project(epsg_from: int, epsg_to: int):
    return Transformer.from_crs(epsg_from, epsg_to, always_xy=True).transform


def transform(convert, geometry):
    """Apply a pyproj-style (x, y) -> (x, y) function to every coordinate."""

    def apply(xy: np.ndarray) -> np.ndarray:
        x, y = convert(xy[:, 0], xy[:, 1])
        return np.column_stack([x, y])

    return shapely.transform(geometry, apply)


def build_grid(aoi: AOI) -> Grid:
    validate_aoi(aoi)
    polygon = shape(aoi.geometry)
    epsg = utm_epsg(polygon.centroid.x, polygon.centroid.y)
    x0, y0, x1, y1 = transform(project(4326, epsg), polygon).bounds
    bounds = (
        math.floor(x0 / 10) * 10,
        math.floor(y0 / 10) * 10,
        math.ceil(x1 / 10) * 10,
        math.ceil(y1 / 10) * 10,
    )
    width, height = (
        round((bounds[2] - bounds[0]) / 10),
        round((bounds[3] - bounds[1]) / 10),
    )
    if max(width, height) > 3500 or width * height > 1_500_000:
        raise ValueError("Draw a more compact area.")
    return Grid(crs_epsg=epsg, bounds=bounds, width=width, height=height)


def affine(grid: Grid):
    return from_origin(grid.bounds[0], grid.bounds[3], grid.resolution, grid.resolution)


def aoi_mask(aoi: AOI, grid: Grid):
    polygon = transform(project(4326, grid.crs_epsg), shape(aoi.geometry))
    return geometry_mask(
        [mapping(polygon)], (grid.height, grid.width), affine(grid), invert=True
    )


def place_box(lon: float, lat: float, name: str) -> AOI:
    epsg = utm_epsg(lon, lat)
    x, y = project(4326, epsg)(lon, lat)
    polygon = transform(
        project(epsg, 4326), box(x - 2500, y - 2500, x + 2500, y + 2500)
    )
    return AOI(geometry=mapping(polygon), name=name)
