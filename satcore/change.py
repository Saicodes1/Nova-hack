import numpy as np
from rasterio.features import shapes
from scipy.ndimage import (
    binary_closing,
    binary_fill_holes,
    binary_opening,
    label,
    uniform_filter,
)
from shapely.geometry import mapping, shape

from .config import THRESHOLDS as T
from .geo import affine, project, transform
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
    scl: tuple[np.ndarray, np.ndarray] | None = None,
):
    """Label change patches with seeded region growing (hysteresis).

    Every analysis type defines a strict *seed* rule and a looser *grow* rule.
    Seeds are pixels we are confident about. A patch then extends over the
    connected pixels that pass the grow rule, so a flood, clearing or new
    district is outlined in full instead of only its most obvious pixels,
    while isolated weak signals elsewhere are ignored.
    """
    seed, grow, max_hole_ha = candidates(
        a,
        b,
        valid,
        intent,
        reflectance=reflectance,
        model_score=model_score,
        scl=scl,
    )
    change = grow_from_seeds(seed, grow)
    return clean(change, valid, pixel_ha, max_hole_ha)


def candidates(
    a: dict,
    b: dict,
    valid: np.ndarray,
    intent: Intent,
    *,
    reflectance=None,
    model_score: np.ndarray | None = None,
    scl: tuple[np.ndarray, np.ndarray] | None = None,
):
    """Seed and grow masks (restricted to `valid`) and the hole-fill limit."""
    max_hole_ha = None
    if intent == "visible_change":
        if model_score is None:
            raise ValueError("Visible change requires FresUNet.")
        settings = T[intent]
        seed = model_score >= settings["model_score_min"]
        grow = model_score >= settings.get(
            "grow_score_min", settings["model_score_min"]
        )
    elif intent.startswith("water"):
        seed, grow = water_change(a, b, intent, scl)
    elif intent in ("veg_loss", "veg_gain"):
        seed, grow = vegetation_change(a, b, intent)
    elif intent == "urban_growth":
        if reflectance is None:
            seed = grow = urban_rules(a, b, valid, None, model_score)
        else:
            seed, grow = urban_evidence(*reflectance, a, b, valid, model_score)
            max_hole_ha = T[intent]["evidence"]["max_hole_ha"]
    else:
        seed, grow = burn_change(a, b)
    return seed & valid, grow & valid, max_hole_ha


def finite(values: np.ndarray, fill: float = 0.0) -> np.ndarray:
    return np.nan_to_num(values, nan=fill, posinf=fill, neginf=fill)


def grow_from_seeds(seed: np.ndarray, grow: np.ndarray) -> np.ndarray:
    """Keep the connected regions of `grow | seed` that contain a seed."""
    regions, count = label(grow | seed)
    if not count:
        return np.zeros_like(seed)
    keep = np.zeros(count + 1, bool)
    keep[np.unique(regions[seed])] = True
    keep[0] = False
    return keep[regions]


def water_change(a, b, intent, scl):
    settings = T["water"]
    before = water(a, scl[0] if scl else None)
    after = water(b, scl[1] if scl else None)
    shift = finite(water_index(b) - water_index(a))
    if intent == "water_loss":
        before, after, shift = after, before, -shift
        wet = water_index(a)
    else:
        wet = water_index(b)
    # Seed: clear water now (signals agree), not before, and the index moved.
    seed = after & ~before & (shift >= settings["min_delta"])
    # Grow: shallow or mixed water at the margins of the same flood/lake.
    grow = (
        ~before
        & (finite(wet, -1) > settings["grow_index"])
        & (shift >= settings["grow_delta"])
    )
    return seed, grow


def vegetation_change(a, b, intent):
    settings = T[intent]
    ndvi_a, ndvi_b = finite(a["ndvi"]), finite(b["ndvi"])
    delta = ndvi_b - ndvi_a
    if intent == "veg_loss":
        seed = (ndvi_a > settings["ndvi_before_min"]) & (delta < settings["delta_max"])
        grow = (ndvi_a > settings["grow_ndvi_before_min"]) & (
            delta < settings["grow_delta_max"]
        )
    else:
        seed = (ndvi_b > settings["ndvi_after_min"]) & (delta > settings["delta_min"])
        grow = (ndvi_b > settings["grow_ndvi_after_min"]) & (
            delta > settings["grow_delta_min"]
        )
    return seed, grow


def burn_change(a, b):
    settings = T["burn"]
    dnbr = finite(a["nbr"]) - finite(b["nbr"])
    ndvi_drop = finite(a["ndvi"]) - finite(b["ndvi"])
    # A burn darkens SWIR *and* removes green vegetation; requiring both for
    # seeds stops moisture or shadow changes from looking like burn scars.
    seed = (dnbr > settings["dnbr_min"]) & (ndvi_drop > settings["ndvi_drop_min"])
    grow = dnbr > settings["grow_dnbr_min"]
    return seed, grow


def urban_rules(a, b, valid, reflectance, model_score):
    """Index-only urban rule, used when RGB reflectance is not available."""
    settings = T["urban_growth"]
    change = finite(b["ndbi"]) - finite(a["ndbi"]) > settings["ndbi_delta_min"]
    if reflectance is not None:
        change |= construction_candidates(*reflectance, valid, b)
    if model_score is not None:
        dry = settings["construction"]["mndwi_after_max"]
        change |= (
            (model_score >= settings["model"]["strong_change_min"])
            & (a["mndwi"] < dry)
            & (b["mndwi"] < dry)
            & (a["ndvi"] < settings["ndvi_after_max"])
        )
    return change & (b["ndvi"] < settings["ndvi_after_max"])


def robust_z(values: np.ndarray, valid: np.ndarray, floor: float) -> np.ndarray:
    """Positive robust z-score against the scene background (median / MAD).

    Subtracting the scene median removes shifts that affect the whole image
    between dates (season, sun angle, haze), so only local change scores high.
    """
    sample = values[valid & np.isfinite(values)]
    if sample.size < 50:
        return np.zeros_like(values, dtype=np.float32)
    median = np.median(sample)
    scale = max(1.4826 * np.median(np.abs(sample - median)), floor)
    return np.clip((finite(values, median) - median) / scale, 0, 6).astype(np.float32)


def surface(bands: dict, window: int):
    rgb = np.stack([finite(bands[key]) for key in ("red", "green", "blue")])
    brightness = rgb.mean(axis=0)
    mean = uniform_filter(brightness, window)
    texture = np.sqrt(np.maximum(uniform_filter(brightness**2, window) - mean**2, 0))
    chroma = (rgb.max(axis=0) - rgb.min(axis=0)) / np.maximum(brightness, 0.01)
    gray = rgb[2] / np.maximum(rgb[0], 0.001)  # blue / red: concrete vs sand
    return texture, uniform_filter(chroma, window), gray


def urban_evidence(before, after, a, b, valid, model_score=None):
    """Seeds and growth area for new built-up land.

    Two kinds of evidence must agree:
    * change: the surface lost colour (sand/soil -> grey) and gained fine
      texture (roofs, streets, shadows), measured against the scene background;
    * built-up look in the after image: textured and grey/blue rather than
      red-brown.
    NDBI is not used here: over desert it rises as much on bare sand as on new
    buildings. Vegetation and water changes are excluded.
    """
    settings = T["urban_growth"]["evidence"]
    window, smooth = settings["texture_px"], settings["window_px"]
    texture_a, chroma_a, _ = surface(before, window)
    texture_b, chroma_b, gray_b = surface(after, window)

    scores = [
        robust_z(chroma_a - chroma_b, valid, 0.02),
        robust_z(texture_b - texture_a, valid, 0.002),
        robust_z(texture_b, valid, 0.002),
        robust_z(gray_b, valid, 0.02),
    ]

    def evidence(size):
        chroma, texture, rough, gray = (uniform_filter(z, size) for z in scores)
        change, built = np.sqrt(chroma * texture), np.sqrt(rough * gray)
        if model_score is not None:
            model = uniform_filter(model_score.astype(np.float32), size)
            change = np.maximum(change, 3 * model)
        return change, built

    # Seeds use a wide window (a neighbourhood that looks like a new district);
    # growth uses a tight one so patch edges stay on the built-up pixels.
    change, built = evidence(smooth)
    edge_change, edge_built = evidence(settings["edge_px"])
    ndvi_a, ndvi_b = finite(a["ndvi"]), finite(b["ndvi"])
    allowed = (
        (ndvi_a < settings["ndvi_before_max"])
        & (ndvi_b < settings["ndvi_after_max"])
        & (uniform_filter(ndvi_b - ndvi_a, 7) < settings["greening_max"])
        & (finite(b["mndwi"], 1) < T["urban_growth"]["construction"]["mndwi_after_max"])
    )
    seed = (
        allowed & (change > settings["seed_change"]) & (built > settings["seed_built"])
    )
    grow = (
        allowed
        & (edge_change > settings["grow_change"])
        & (edge_built > settings["grow_built"])
    )
    return seed, grow


def water_index(indices: dict) -> np.ndarray:
    mndwi = indices["mndwi"]
    return np.where(np.isfinite(mndwi), mndwi, indices["ndwi"])


def water(indices: dict, scl: np.ndarray | None = None) -> np.ndarray:
    """Water where most available signals agree.

    Signals: MNDWI (green vs SWIR), NDWI (green vs NIR) and, when present, the
    SCL water class. A single index above zero is easily triggered by building
    shadows, dark asphalt or wet sand; requiring agreement removes most of those.
    """
    settings = T["water"]
    mndwi, ndwi = indices["mndwi"], indices["ndwi"]
    has_mndwi = np.isfinite(mndwi)
    votes = (has_mndwi & (mndwi > settings["mndwi"])).astype(np.int8)
    votes += np.isfinite(ndwi) & (ndwi > settings["ndwi"])
    signals = has_mndwi.astype(np.int8) + np.isfinite(ndwi)
    if scl is not None:
        votes += scl == 6
        signals += 1
    needed = np.minimum(2, np.maximum(signals, 1))
    single = ~has_mndwi & (ndwi > settings["ndwi_fallback"])
    return np.where(signals >= 2, votes >= needed, single)


def clean(
    change: np.ndarray,
    valid: np.ndarray,
    pixel_ha: float,
    max_hole_ha: float | None = None,
) -> np.ndarray:
    """Turn a raw change mask into labelled patches that follow the change.

    1. Closing bridges one-pixel gaps inside a patch (sensor noise, roads).
    2. Opening removes speckle and one-pixel slivers from misregistration.
    3. Holes smaller than the minimum patch (or `max_hole_ha`) are filled.
    4. Patches smaller than the minimum patch are dropped.
    """
    settings = T["cleanup"]
    change = change & valid
    cross = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], bool)
    if settings.get("closing_px", 1) > 0:
        change = (change | binary_closing(change, cross)) & valid
    size = settings["opening_px"]
    change = binary_opening(change, structure=np.ones((size, size), bool))
    min_px = settings["min_patch_ha"] / pixel_ha
    hole_px = (max_hole_ha or settings["min_patch_ha"]) / pixel_ha
    holes, count = label(binary_fill_holes(change) & ~change)
    if count:
        sizes = np.bincount(holes.ravel(), minlength=count + 1)
        small = sizes < hole_px
        small[0] = False
        change |= small[holes] & valid
    labels, count = label(change)
    counts = np.bincount(labels.ravel(), minlength=count + 1)
    keep = counts >= min_px
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
