"""Patch accuracy: patches should follow real change and ignore noise."""

import numpy as np
from rasterio.features import shapes
from scipy.ndimage import gaussian_filter
from shapely.geometry import shape
from shapely.ops import unary_union

from satcore import masks
from satcore.change import detect, polygons
from satcore.geo import affine, project, transform
from satcore.models import Grid

SHAPE = (120, 120)


def water_indices(mndwi, ndwi):
    return {"mndwi": mndwi.astype(np.float32), "ndwi": ndwi.astype(np.float32)}


def iou(a, b):
    return np.count_nonzero(a & b) / max(1, np.count_nonzero(a | b))


def test_near_threshold_noise_does_not_become_water_patches():
    rng = np.random.default_rng(3)

    def blotches():  # smooth moisture/haze variation, not pixel noise
        field = gaussian_filter(rng.normal(size=SHAPE), 6)
        return 0.04 * field / field.std()

    # Damp ground sitting just under the water threshold on both dates.
    base = np.full(SHAPE, -0.01)
    before = water_indices(base + blotches(), base + blotches())
    after = water_indices(base + blotches(), base + blotches())
    labels = detect(before, after, np.ones(SHAPE, bool), "water_gain", 0.01)
    assert not labels.any()


def test_new_water_patch_matches_the_flooded_area():
    rng = np.random.default_rng(4)
    flooded = np.zeros(SHAPE, bool)
    flooded[30:80, 40:95] = True
    before = water_indices(
        -0.3 + rng.normal(0, 0.03, SHAPE), -0.3 + rng.normal(0, 0.03, SHAPE)
    )
    after = water_indices(
        np.where(flooded, 0.45, -0.3) + rng.normal(0, 0.03, SHAPE),
        np.where(flooded, 0.3, -0.3) + rng.normal(0, 0.03, SHAPE),
    )
    labels = detect(before, after, np.ones(SHAPE, bool), "water_gain", 0.01)
    assert iou(labels > 0, flooded) > 0.97


def test_shadows_with_one_water_like_index_are_not_water():
    before = water_indices(np.full(SHAPE, -0.3), np.full(SHAPE, -0.3))
    shadow = np.zeros(SHAPE, bool)
    shadow[20:60, 20:60] = True
    # Dark shadow: MNDWI turns positive but NDWI stays negative, SCL says "dark".
    after = water_indices(np.where(shadow, 0.15, -0.3), np.where(shadow, -0.1, -0.3))
    scl_a = np.full(SHAPE, 5, np.uint8)
    scl_b = np.where(shadow, 2, 5).astype(np.uint8)
    labels = detect(
        before,
        after,
        np.ones(SHAPE, bool),
        "water_gain",
        0.01,
        scl=(scl_a, scl_b),
    )
    assert not labels.any()
    assert not masks.valid_mask(scl_b, exclude_dark=True)[shadow].any()


def test_thin_gaps_and_small_holes_are_closed_inside_a_patch():
    rng = np.random.default_rng(5)
    area = np.zeros(SHAPE, bool)
    area[20:90, 20:90] = True
    holes = area.copy()
    holes[55, 20:90] = False  # one-pixel road across the patch
    holes[30:33, 30:33] = False  # small hole
    before = water_indices(np.full(SHAPE, -0.3), np.full(SHAPE, -0.3))
    after = water_indices(
        np.where(holes, 0.4, -0.3) + rng.normal(0, 0.01, SHAPE),
        np.where(holes, 0.3, -0.3),
    )
    labels = detect(before, after, np.ones(SHAPE, bool), "water_gain", 0.01)
    assert len(np.unique(labels[labels > 0])) == 1
    assert labels[55, 50] and labels[31, 31]


def test_cloud_fringe_is_excluded_from_detection():
    scl_a = np.full(SHAPE, 5, np.uint8)
    scl_b = scl_a.copy()
    scl_b[50:70, 50:70] = 9
    near = masks.near_clouds(scl_a, scl_b, 3)
    assert near[47, 60] and near[72, 60]
    assert not near[46, 60] and not near[73, 60]


def test_polygons_follow_the_pixel_edges_within_half_a_pixel():
    grid = Grid(
        crs_epsg=32640,
        bounds=(300000, 2750000, 301200, 2751200),
        width=120,
        height=120,
    )
    labels = np.zeros(SHAPE, np.int32)
    yy, xx = np.indices(SHAPE)
    labels[((xx - 60) / 35) ** 2 + ((yy - 55) / 22) ** 2 < 1] = 1
    exact = unary_union(
        [
            shape(geometry)
            for geometry, value in shapes(
                labels, mask=labels > 0, transform=affine(grid)
            )
        ]
    )
    collection = polygons(labels, np.zeros(SHAPE, bool), grid, "water_gain")
    to_utm = project(4326, grid.crs_epsg)
    drawn = transform(to_utm, shape(collection["features"][0]["geometry"]))
    assert drawn.hausdorff_distance(exact) <= 5.01
    assert abs(drawn.area - exact.area) / exact.area < 0.03


def test_new_district_is_covered_and_plain_colour_change_is_not():
    from satcore.indices import calculate

    rng = np.random.default_rng(7)
    size = (140, 140)
    sand = {
        "blue": 0.18,
        "green": 0.24,
        "red": 0.30,
        "nir": 0.33,
        "swir16": 0.40,
        "swir22": 0.36,
    }

    def scene(shift):
        return {
            k: (np.full(size, v) + shift + rng.normal(0, 0.006, size)).astype(
                np.float32
            )
            for k, v in sand.items()
        }

    before, after = scene(0.0), scene(-0.04)  # the after date is darker overall
    district = np.zeros(size, bool)
    district[10:60, 10:120] = True
    yy, xx = np.indices(size)
    roofs = district & ((yy // 3 + xx // 3) % 2 == 0)
    streets = district & ~roofs
    for key, roof, street in [
        ("blue", 0.30, 0.15),
        ("green", 0.30, 0.15),
        ("red", 0.29, 0.15),
        ("nir", 0.31, 0.16),
        ("swir16", 0.33, 0.18),
        ("swir22", 0.31, 0.17),
    ]:
        after[key][roofs], after[key][streets] = roof, street
    plain = np.zeros(size, bool)
    plain[85:125, 30:90] = True  # uniform greyish-brown, e.g. damp ground
    for key, value in [("blue", 0.19), ("green", 0.20), ("red", 0.21)]:
        after[key][plain] = value
    labels = detect(
        calculate(before),
        calculate(after),
        np.ones(size, bool),
        "urban_growth",
        0.01,
        reflectance=(before, after),
    )
    found = labels > 0
    assert np.count_nonzero(found & district) / district.sum() > 0.95
    assert iou(found, district) > 0.8
    assert not found[plain].any()


def test_patches_report_seed_share_mean_score_and_weak():
    from satcore.change import Candidates, patches, polygons
    from satcore.models import Grid

    grid = Grid(
        crs_epsg=32640, bounds=(300000, 2780000, 300400, 2780400), width=40, height=40
    )
    valid = np.ones((40, 40), bool)
    grow = np.zeros_like(valid)
    grow[2:12, 2:12] = True  # strong patch: mostly seeds
    grow[20:35, 20:35] = True  # weak patch: one seed row, grown over the rest
    seed = np.zeros_like(valid)
    seed[2:12, 2:10] = True
    seed[20, 20:35] = True
    signal = np.where(seed, 0.5, np.where(grow, 0.1, 0)).astype(np.float32)
    found = Candidates(seed, grow, signal)
    labels = patches(found, valid, pixel_ha=0.01)
    features = polygons(labels, np.zeros_like(valid), grid, "water_gain", found)[
        "features"
    ]
    props = sorted((f["properties"] for f in features), key=lambda p: p["ha"])
    strong, weak = props
    assert strong["seed_share"] == 0.8 and not strong["weak"]
    assert weak["seed_share"] < 0.2 and weak["weak"]
    assert strong["mean_score"] > weak["mean_score"] > 0
