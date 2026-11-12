import numpy as np
from rasterio.features import shapes
from scipy.ndimage import binary_opening, label, uniform_filter
from shapely.geometry import mapping, shape
from shapely.ops import transform

from .config import THRESHOLDS as T
from .geo import affine, project
from .models import Grid, Intent


def construction_candidates(before, after, valid, after_indices):
    settings = T["urban_growth"]["construction"]
    window = settings["window_px"]
    rgb = [
        np.stack([bands[key] for key in ("red", "green", "blue")])
        for bands in (before, after)
    ]
    usable = valid & np.isfinite(rgb[0]).all(axis=0) & np.isfinite(rgb[1]).all(axis=0)
    coverage = uniform_filter(usable.astype(np.float32), window)

    def local_mean(values):
        summed = uniform_filter(np.where(usable, values, 0), window)
        return np.divide(
            summed, coverage, out=np.zeros_like(summed), where=coverage > 0
        )

    def surface_metrics(bands):
        brightness = bands.mean(axis=0)
        texture = np.sqrt(
            np.maximum(local_mean(brightness**2) - local_mean(brightness) ** 2, 0)
        )
        chroma = (bands.max(axis=0) - bands.min(axis=0)) / np.maximum(brightness, 0.01)
        return texture, local_mean(chroma)

    texture_a, chroma_a = surface_metrics(rgb[0])
    texture_b, chroma_b = surface_metrics(rgb[1])
    return (
        usable
        & (coverage >= settings["clear_fraction_min"])
        & (after_indices["mndwi"] < settings["mndwi_after_max"])
        & (texture_b - texture_a > settings["texture_delta_min"])
        & (texture_b > settings["texture_ratio_min"] * texture_a)
        & (chroma_a - chroma_b > settings["chroma_drop_min"])
    )


def detect(
    a: dict,
    b: dict,
    valid: np.ndarray,
    intent: Intent,
    pixel_ha: float,
    *,
    reflectance=None,
    model_score: np.ndarray | None = None,
):
    if intent == "visible_change":
        if model_score is None:
            raise ValueError("Visible change requires FresUNet.")
        change = model_score >= T[intent]["model_score_min"]
    elif intent.startswith("water"):

        def water(indices: dict):
            mndwi = indices["mndwi"]
            return np.where(
                np.isfinite(mndwi),
                mndwi > T["water"]["mndwi"],
                indices["ndwi"] > T["water"]["ndwi_fallback"],
            )

        wa, wb = water(a), water(b)
        change = (wb & ~wa) if intent == "water_gain" else (wa & ~wb)
    elif intent == "veg_loss":
        change = (a["ndvi"] > T[intent]["ndvi_before_min"]) & (
            b["ndvi"] - a["ndvi"] < T[intent]["delta_max"]
        )
    elif intent == "veg_gain":
        change = (b["ndvi"] > T[intent]["ndvi_after_min"]) & (
            b["ndvi"] - a["ndvi"] > T[intent]["delta_min"]
        )
    elif intent == "urban_growth":
        settings = T[intent]
        change = b["ndbi"] - a["ndbi"] > settings["ndbi_delta_min"]
        if reflectance is not None:
            textured = construction_candidates(*reflectance, valid, b)
            if model_score is not None:
                textured &= model_score >= settings["model"]["texture_support_min"]
            change |= textured
        if model_score is not None:
            dry = settings["construction"]["mndwi_after_max"]
            change |= (
                (model_score >= settings["model"]["strong_change_min"])
                & (a["mndwi"] < dry)
                & (b["mndwi"] < dry)
                & (a["ndvi"] < settings["ndvi_after_max"])
            )
        change &= b["ndvi"] < settings["ndvi_after_max"]
    else:
        change = a["nbr"] - b["nbr"] > T["burn"]["dnbr_min"]
    size = T["cleanup"]["opening_px"]
    labels, count = label(
        binary_opening(change & valid, structure=np.ones((size, size), bool))
    )
    counts = np.bincount(labels.ravel(), minlength=count + 1)
    keep = counts * pixel_ha >= T["cleanup"]["min_patch_ha"]
    keep[0] = False
    return np.where(keep[labels], labels, 0).astype(np.int32)


def polygons(labels: np.ndarray, edge: np.ndarray, grid: Grid, intent: Intent):
    counts = np.bincount(labels.ravel())
    edges = np.bincount(labels[edge], minlength=len(counts))
    result = []
    to_lonlat = project(grid.crs_epsg, 4326)
    pixel_ha = grid.resolution**2 / 10_000
    for geometry, value in shapes(labels, mask=labels > 0, transform=affine(grid)):
        idx = int(value)
        polygon = shape(geometry).simplify(
            T["cleanup"]["simplify_m"], preserve_topology=True
        )
        result.append(
            {
                "type": "Feature",
                "geometry": mapping(transform(to_lonlat, polygon)),
                "properties": {
                    "kind": intent,
                    "ha": round(counts[idx] * pixel_ha, 2),
                    "artefact": bool(edges[idx] > counts[idx] / 2),
                },
            }
        )
    return {"type": "FeatureCollection", "features": result}
