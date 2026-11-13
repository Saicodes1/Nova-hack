import json
from datetime import date
from uuid import uuid4

import numpy as np
import pytest
from rasterio.features import shapes
from shapely.geometry import mapping, shape

from satcore import demo, pipeline, stac, store
from satcore.geo import affine, build_grid, place_box, project, transform
from satcore.models import UplinkCommand
from scripts import eval_patches as harness


def square(size=40, top=10, left=10, side=10):
    mask = np.zeros((size, size), bool)
    mask[top : top + side, left : left + side] = True
    return mask


def test_identical_masks_score_one():
    truth = square()
    scores = harness.score_reference(truth, [truth], np.ones_like(truth))
    assert scores["iou"] == scores["precision"] == scores["recall"] == 1
    assert scores["object_recall"] == 1 and scores["reference_objects"] == 1


def test_disjoint_masks_score_zero():
    scores = harness.score_reference(
        square(left=0), [square(left=25)], np.ones((40, 40), bool)
    )
    assert scores["iou"] == scores["precision"] == scores["recall"] == 0
    assert scores["object_recall"] == 0


def test_partial_overlap_and_object_recall():
    first, second = square(left=0), square(left=20)  # two 100-px reference objects
    found = np.zeros_like(first)
    found[10:20, 0:6] = True  # 60 % of the first object
    found[10:20, 20:24] = True  # 40 % of the second object
    scores = harness.score_reference(found, [first, second], np.ones_like(first))
    assert scores["precision"] == 1
    assert scores["recall"] == 0.5 and scores["iou"] == 0.5
    assert scores["object_recall"] == 0.5  # only the first passes the 50 % cut-off


def test_scoring_ignores_unclear_pixels():
    truth = square()
    found = truth.copy()
    found[0:5, 0:5] = True  # false positive, but under cloud
    valid = np.ones_like(truth)
    valid[0:5, 0:5] = False
    assert harness.score_reference(found, [truth], valid)["precision"] == 1


def test_seed_share():
    patch = square()
    seed = np.zeros_like(patch)
    seed[10:15, 10:20] = True
    seed[0, 0] = True  # outside the patch: not counted
    assert harness.seed_share(seed, patch) == 0.5
    assert harness.seed_share(seed, np.zeros_like(patch)) is None


def demo_inputs(case_id):
    entry = {c["id"]: c for c in demo.cases()}[case_id]
    aoi = place_box(entry["lon"], entry["lat"], entry["name"])
    grid = build_grid(aoi)
    return entry, aoi, grid, [demo.bands(grid, case_id, role) for role in "AB"]


@pytest.mark.parametrize("case_id", ["dubai_flood", "rondonia", "dubai_south"])
def test_harness_matches_run_analysis(case_id, monkeypatch, tmp_path):
    """Same bands in, same numbers out: the harness cannot drift from the app."""
    monkeypatch.setenv("ANALYSES_DIR", str(tmp_path))
    entry, aoi, grid, (a, b) = demo_inputs(case_id)
    result = harness.evaluate(entry["intent"], aoi, grid, a, b, demo_id=case_id)
    cmd = UplinkCommand(
        analysis_id=uuid4(),
        aoi=aoi,
        date_a=date.fromisoformat(entry["date_a"]),
        date_b=date.fromisoformat(entry["date_b"]),
        intent=entry["intent"],
        demo_id=case_id,
    )
    stats = pipeline.run_analysis(
        cmd, lambda *args: None, type("V", (), {"write": lambda s, p: ""})()
    ).stats
    metrics = result["metrics"]
    assert stats.changed_ha > 0
    assert metrics["changed_ha"] == stats.changed_ha
    assert metrics["n_patches"] == stats.n_polygons
    assert metrics["changed_pct"] == stats.changed_pct
    assert metrics["largest_patch_ha"] == stats.largest_polygon_ha
    assert metrics["artefact_ha"] == stats.artefact_ha


def test_run_end_to_end_from_cached_scenes(monkeypatch, tmp_path):
    """`run` reads cached bands, writes outputs, and scores a pixel-exact reference as IoU 1."""
    monkeypatch.setenv("EVAL_DIR", str(tmp_path / "eval"))
    monkeypatch.setenv("SCENE_STORE", str(tmp_path / "scenes"))
    entry, aoi, grid, bands = demo_inputs("dubai_flood")
    scenes = []
    for role, layers in zip("AB", bands, strict=True):
        ref = demo.scene(
            "dubai_flood", role, date.fromisoformat(entry[f"date_{role.lower()}"])
        )
        ref.source = "earth-search"
        file = store.scene_file(stac.Selection(ref, {}), grid)
        file.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(file, **layers)
        scenes.append(ref.model_dump(mode="json"))
    case = {"id": "synthetic", "intent": entry["intent"], "aoi_model": aoi}
    directory = harness.case_dir(case)
    directory.mkdir(parents=True)
    (directory / "scenes.json").write_text(json.dumps({"scenes": scenes}))

    # Reference = the detected pixels themselves, as unsimplified polygons.
    labels = harness.evaluate(entry["intent"], aoi, grid, *bands)["labels"]
    to_lonlat = project(grid.crs_epsg, 4326)
    features = [
        {
            "type": "Feature",
            "properties": {},
            "geometry": mapping(transform(to_lonlat, shape(geom))),
        }
        for geom, _ in shapes(labels, mask=labels > 0, transform=affine(grid))
    ]
    reference = tmp_path / "reference.geojson"
    reference.write_text(
        json.dumps({"type": "FeatureCollection", "features": features})
    )
    case["reference"] = str(reference)

    metrics = harness.run(case)
    assert metrics["status"] == "ok" and metrics["changed_ha"] > 0
    assert metrics["iou"] == 1 and metrics["object_recall"] == 1
    for name in ("overlay.png", "patches.geojson", "metrics.json"):
        assert (directory / name).is_file()


def test_run_without_fetch_explains_what_to_do(monkeypatch, tmp_path):
    monkeypatch.setenv("EVAL_DIR", str(tmp_path))
    _, aoi, _, _ = demo_inputs("dubai_flood")
    with pytest.raises(SystemExit, match="fetch"):
        harness.run({"id": "missing", "intent": "water_gain", "aoi_model": aoi})


def test_reference_drawn_on_the_aoi_file_ignores_the_aoi_box(tmp_path):
    _, aoi, grid, _ = demo_inputs("dubai_flood")
    (west, south, east, north) = shape(aoi.geometry).bounds
    drawn = {
        "type": "Polygon",
        "coordinates": [
            [
                [west, south],
                [west + 0.005, south],
                [west + 0.005, south + 0.005],
                [west, south + 0.005],
                [west, south],
            ]
        ],
    }
    path = tmp_path / "reference.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"case": "x", "name": "AOI"},
                        "geometry": aoi.geometry,
                    },
                    {"type": "Feature", "properties": {}, "geometry": drawn},
                ],
            }
        )
    )
    reference = harness.reference_rasters(path, grid)
    assert len(reference.objects) == 1 and 0 < reference.objects[0].mean() < 0.1
    assert not reference.ignore.any()


def test_reference_source_is_required_and_labelled(monkeypatch, tmp_path):
    aoi_file = tmp_path / "aoi.geojson"
    _, aoi, _, _ = demo_inputs("dubai_flood")
    aoi_file.write_text(
        json.dumps({"type": "Feature", "properties": {}, "geometry": aoi.geometry})
    )
    ref = tmp_path / "ref.geojson"
    ref.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "metadata": {"source": "Example survey"},
                "features": [],
            }
        )
    )
    case = {
        "id": "x",
        "intent": "water_gain",
        "aoi": str(aoi_file),
        "date_a": "2024-01-01",
        "date_b": "2024-02-01",
        "reference": str(ref),
    }
    cases_file = tmp_path / "cases.yaml"
    monkeypatch.setattr(harness, "CASES_FILE", cases_file)
    cases_file.write_text(json.dumps({"cases": [case]}))
    with pytest.raises(SystemExit, match="reference_source"):
        harness.load_cases()
    cases_file.write_text(
        json.dumps({"cases": [dict(case, reference_source="official")]})
    )
    (loaded,) = harness.load_cases()
    assert harness.reference_label(loaded, True) == "official · Example survey"
    assert harness.reference_label(loaded, False) is None


def test_ignore_zone_counts_neither_for_nor_against():
    truth = square(left=0)  # 100 px true change
    unsure = square(left=25)  # 100 px of change dated just outside the scenes
    valid = np.ones_like(truth)
    found = truth | unsure  # detector also finds the uncertain change
    assert harness.score_reference(found, [truth], valid)["precision"] == 0.5
    scores = harness.score_reference(found, [truth], valid, ignore=unsure)
    assert scores["precision"] == scores["recall"] == scores["iou"] == 1
    missed = harness.score_reference(truth, [truth | unsure], valid, ignore=unsure)
    assert missed["recall"] == 1 and missed["object_recall"] == 1


def test_role_ignore_features_build_the_ignore_zone(tmp_path):
    _, aoi, grid, _ = demo_inputs("dubai_flood")
    (west, south, _, _) = shape(aoi.geometry).bounds

    def cell(dx, role=None):
        x = west + dx
        ring = [
            [x, south],
            [x + 0.004, south],
            [x + 0.004, south + 0.004],
            [x, south + 0.004],
            [x, south],
        ]
        props = {"role": role} if role else {}
        return {
            "type": "Feature",
            "properties": props,
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        }

    path = tmp_path / "ref.geojson"
    path.write_text(
        json.dumps(
            {"type": "FeatureCollection", "features": [cell(0), cell(0.01, "ignore")]}
        )
    )
    reference = harness.reference_rasters(path, grid)
    assert len(reference.objects) == 1 and reference.ignore.any()
    assert not (reference.objects[0] & reference.ignore).any()


def test_object_recall_merges_touching_polygons_and_skips_specks():
    left, right = square(left=0), square(left=10)  # touching: one 200-px object
    speck = np.zeros_like(left)
    speck[35, 35] = True
    found = left.copy()  # half of the merged object
    scores = harness.score_reference(
        found, [left, right, speck], np.ones_like(left), min_object_px=50
    )
    assert scores["reference_objects"] == 1 and scores["object_recall"] == 1


def test_compare_flags_iou_drops_beyond_two_points():
    case = {"id": "x"}
    assert harness.check_case(case, {"iou": 0.60}, {"iou": 0.59}) == []
    assert harness.check_case(case, {"iou": 0.60}, {"iou": 0.57})


def test_compare_guards():
    case = {
        "id": "x",
        "guards": {
            "min_ha": 150,
            "max_patches": 12,
            "ha_within_pct": 2,
            "no_drop": ["clear_water_change_detected_pct"],
            "keep_cover_ha": {"Bare or sparse vegetation": 2},
        },
    }
    before = {
        "changed_ha": 154.0,
        "n_patches": 10,
        "clear_water_change_detected_pct": 90.0,
        "ha_by_cover": {"Bare or sparse vegetation": 100.0, "Built-up": 20.0},
    }
    assert harness.check_case(case, before, dict(before)) == []
    fewer_roofs = dict(
        before, ha_by_cover={"Bare or sparse vegetation": 99.0, "Built-up": 5.0}
    )
    assert harness.check_case(case, before, fewer_roofs) == []
    broken = dict(
        before,
        changed_ha=140.0,
        n_patches=15,
        clear_water_change_detected_pct=80.0,
        ha_by_cover={"Bare or sparse vegetation": 90.0},
    )
    problems = " ".join(harness.check_case(case, before, broken))
    for word in ("min 150", "max 12", "moved", "clear_water", "Bare or sparse"):
        assert word in problems
