"""Accuracy evaluation harness for change patches on real Sentinel-2 imagery.

    python -m scripts.eval_patches fetch [--case ID]   # network: select scenes, cache bands
    python -m scripts.eval_patches run [--case ID]     # offline: detect, score, draw overlays
    python -m scripts.eval_patches report              # offline: var/eval/report.html + docs/eval_baseline.md
    python -m scripts.eval_patches snapshot            # freeze current metrics as the baseline
    python -m scripts.eval_patches compare             # offline: current metrics vs baseline; exit 1 on regression

Cases live in config/eval_cases.yaml, AOIs in config/eval_aois/, optional hand-drawn
true-change polygons in config/eval_refs/. Detection calls the same satcore functions
as pipeline.run_analysis (clear_masks, detect_patches, seed_mask, change_stats), so
harness numbers and app numbers cannot drift apart.
"""

import argparse
import html
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np
import yaml
from PIL import Image, ImageDraw
from rasterio.features import rasterize
from scipy.ndimage import binary_erosion, label
from shapely.geometry import mapping, shape

from satcore import change, indices, masks, pipeline, stac, store
from satcore.config import ROOT, THRESHOLDS, data_path
from satcore.geo import affine as grid_affine
from satcore.geo import aoi_mask, build_grid, project, transform
from satcore.landcover import CLASSES
from satcore.models import AOI, SceneRef

CASES_FILE = ROOT / "config/eval_cases.yaml"
BASELINE_DOC = ROOT / "docs/eval_baseline.md"
OBJECT_COVER = 0.5  # a reference object counts as found when half its area is detected
NEAR_CLOUD_M = 30
# Who made a case's reference polygons: an official product, crowd-mapped data, or by hand.
REFERENCE_SOURCES = ("official", "crowd-mapped", "hand-drawn")


def out_dir() -> Path:
    return data_path("EVAL_DIR", "var/eval")


# ---------------------------------------------------------------- cases


def load_cases(only: str | None = None) -> list[dict]:
    cases = yaml.safe_load(CASES_FILE.read_text(encoding="utf-8"))["cases"]
    if only:
        cases = [case for case in cases if case["id"] == only]
        if not cases:
            raise SystemExit(f"Unknown case: {only}")
    for case in cases:
        case["aoi_model"] = read_aoi(ROOT / case["aoi"])
        for key in ("date_a", "date_b"):
            if not isinstance(case[key], date):
                case[key] = date.fromisoformat(str(case[key]))
        kind = case.get("reference_source")
        if case.get("reference") and kind not in REFERENCE_SOURCES:
            raise SystemExit(
                f"{case['id']}: reference_source must be one of {', '.join(REFERENCE_SOURCES)}."
            )
    return cases


def reference_label(case: dict, has_ref: bool) -> str | None:
    """'official · INPE PRODES ...': the kind plus the source named in the file's metadata."""
    if not has_ref:
        return None
    data = json.loads((ROOT / case["reference"]).read_text("utf-8"))
    detail = (data.get("metadata") or {}).get("source")
    return " · ".join(part for part in (case.get("reference_source"), detail) if part)


def read_aoi(path: Path) -> AOI:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("type") == "FeatureCollection":
        data = data["features"][0]
    geometry = data.get("geometry", data)
    return AOI(geometry=geometry, name=data.get("properties", {}).get("name"))


def case_dir(case: dict) -> Path:
    return out_dir() / case["id"]


# ---------------------------------------------------------------- fetch (network)


def fetch(case: dict) -> dict:
    aoi, grid = case["aoi_model"], build_grid(case["aoi_model"])
    inside = aoi_mask(aoi, grid)
    window = case.get("window_days", 15)
    selections = []
    for role, target in (("A", case["date_a"]), ("B", case["date_b"])):
        pinned = case.get(f"item_{role.lower()}")
        if pinned:
            backup = case.get("catalog", "earth-search") == "planetary-computer"
            selection = stac.pinned_scene(aoi, grid, pinned, role, target, backup)
        else:
            after = selections[0].scene.datetime if selections else None
            selection = stac.select_scene(aoi, grid, target, role, window, after=after)
        if selection is None:
            raise SystemExit(
                f"{case['id']}: no scene found for date {role} ({target})."
            )
        selections.append(selection)
    bands = [store.load_scene(selection, grid) for selection in selections]
    for selection, layers in zip(selections, bands, strict=True):
        selection.scene.aoi_cloud_pct = masks.aoi_cloud_pct(layers["scl"], inside)
    directory = case_dir(case)
    directory.mkdir(parents=True, exist_ok=True)
    cover = stac.worldcover(aoi, grid)
    if cover is not None:
        np.save(directory / "worldcover.npy", cover)
    manifest = {
        "case": case["id"],
        "grid": grid.model_dump(mode="json"),
        "scenes": [s.scene.model_dump(mode="json") for s in selections],
        "worldcover": cover is not None,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }
    (directory / "scenes.json").write_text(json.dumps(manifest, indent=1), "utf-8")
    side_by_side([rgb(layers, inside) for layers in bands]).save(
        directory / "preview.png"
    )
    return manifest


# ---------------------------------------------------------------- run (offline)


def cached_bands(case: dict):
    manifest_file = case_dir(case) / "scenes.json"
    if not manifest_file.exists():
        raise SystemExit(
            f"{case['id']}: no scenes yet. Run `fetch --case {case['id']}`."
        )
    manifest = json.loads(manifest_file.read_text("utf-8"))
    grid = build_grid(case["aoi_model"])
    bands = []
    for scene in manifest["scenes"]:
        selection = stac.Selection(SceneRef.model_validate(scene), {})
        if not store.scene_file(selection, grid).exists():
            raise SystemExit(
                f"{case['id']}: cached bands missing. Run `fetch --case {case['id']}`."
            )
        # The file exists, so load_scene reads the cache and never the network.
        bands.append(store.load_scene(selection, grid))
    return manifest, grid, bands


def evaluate(intent, aoi, grid, a, b, *, demo_id=None, reference=None, cover=None):
    """Detect patches exactly as run_analysis does and measure them."""
    inside = aoi_mask(aoi, grid)
    valid, detectable, overlap = pipeline.clear_masks(a, b, inside, intent)
    result = {"valid_overlap_pct": overlap, "inside": inside, "valid": valid}
    if overlap < THRESHOLDS["trust"]["overlap_low_pct"]:
        return dict(result, status="refused_cloudy", labels=None, seed=None)
    ia, ib = indices.calculate(a), indices.calculate(b)
    model_score = None
    if pipeline.uses_model(intent, demo_id):
        model_score = pipeline.model_scores(a, b, valid, intent)
    labels, edge, geojson, seed = pipeline.detect_patches(
        ia, ib, a, b, detectable, grid, intent, model_score, cover
    )
    stats = pipeline.change_stats(labels, valid, edge, geojson, overlap, aoi, grid)
    mask = labels > 0
    pixel_ha = grid.resolution**2 / 10_000
    near = masks.near_clouds(a["scl"], b["scl"], round(NEAR_CLOUD_M / grid.resolution))
    metrics = {
        "changed_ha": stats.changed_ha,
        "n_patches": stats.n_polygons,
        "changed_pct": stats.changed_pct,
        "largest_patch_ha": stats.largest_polygon_ha,
        "valid_overlap_pct": overlap,
        "near_cloud_ha": round((mask & near).sum() * pixel_ha, 2),
        "artefact_ha": stats.artefact_ha,
        "seed_share": seed_share(seed, mask),
        "detector": "FresUNet + rules" if model_score is not None else "rules",
    }
    if cover is not None:
        metrics["ha_by_cover"] = ha_by_cover(mask, cover, pixel_ha)
    if intent.startswith("water"):
        metrics["clear_water_change_detected_pct"] = clear_water_change(
            ia, ib, a, b, detectable, mask, intent
        )
    if reference is not None:
        metrics.update(
            score_reference(
                mask,
                reference.objects,
                valid,
                reference.ignore,
                min_object_px=round(THRESHOLDS["cleanup"]["min_patch_ha"] / pixel_ha),
            )
        )
    return dict(
        result,
        status="ok",
        labels=labels,
        seed=seed,
        edge=edge,
        geojson=geojson,
        metrics=metrics,
    )


def ha_by_cover(mask, cover, pixel_ha) -> dict[str, float]:
    """Detected hectares per WorldCover class (a proxy where no reference exists)."""
    codes, counts = np.unique(cover[mask], return_counts=True)
    return {
        CLASSES.get(int(code), str(code)): round(float(count) * pixel_ha, 2)
        for code, count in zip(codes, counts, strict=True)
    }


def clear_water_change(ia, ib, a, b, detectable, mask, intent) -> float | None:
    """% of clear-cut water change that was detected (proxy for missed margins).

    Clear-cut: the water index crosses zero in the asked direction and the
    water votes (MNDWI, NDWI, SCL) agree on both dates, so the pixel really
    changed state. Turbid water that still votes water is not counted.
    """
    wet_a, wet_b = change.water_index(ia) > 0, change.water_index(ib) > 0
    vote_a, vote_b = change.water(ia, a["scl"]), change.water(ib, b["scl"])
    if intent == "water_gain":
        plain = ~wet_a & wet_b & ~vote_a & vote_b
    else:
        plain = wet_a & ~wet_b & vote_a & ~vote_b
    plain &= detectable
    total = int(plain.sum())
    return round(100 * float((mask & plain).sum()) / total, 1) if total else None


def seed_share(seed: np.ndarray, mask: np.ndarray) -> float | None:
    """Share of patch pixels that are seeds (how much of each patch is strong signal)."""
    total = int(mask.sum())
    return round(float((seed & mask).sum()) / total, 3) if total else None


@dataclass
class Reference:
    """True-change polygons (one raster each) and a zone excluded from scoring."""

    objects: list[np.ndarray]
    ignore: np.ndarray


def score_reference(
    mask: np.ndarray,
    objects: list[np.ndarray],
    valid: np.ndarray,
    ignore: np.ndarray | None = None,
    min_object_px: int = 1,
):
    """Pixel IoU / precision / recall and object recall, inside clear coverage.

    `objects` holds one boolean raster per reference polygon. Pixels in `ignore`
    (e.g. change that may fall just before or after the scene dates) count
    neither for nor against the detector. For object recall, the reference
    objects are the connected areas of the merged true change that are at least
    `min_object_px` (normally the detector's minimum patch): touching polygons
    form one object, and specks the detector cannot report are not counted.
    """
    scored = valid if ignore is None else valid & ~ignore
    truth = np.zeros_like(mask)
    for obj in objects:
        truth |= obj
    found, truth = mask & scored, truth & scored
    tp = int((found & truth).sum())
    fp, fn = int((found & ~truth).sum()), int((~found & truth).sum())
    regions, count = label(truth, structure=np.ones((3, 3), bool))
    sizes = np.bincount(regions.ravel(), minlength=count + 1)
    hits = np.bincount(regions[found], minlength=count + 1)
    covered = [
        hits[i] / sizes[i] >= OBJECT_COVER
        for i in range(1, count + 1)
        if sizes[i] >= min_object_px
    ]
    ratio = lambda num, den: round(num / den, 3) if den else None  # noqa: E731
    return {
        "iou": ratio(tp, tp + fp + fn),
        "precision": ratio(tp, tp + fp),
        "recall": ratio(tp, tp + fn),
        "object_recall": ratio(sum(covered), len(covered)),
        "reference_objects": len(covered),
    }


def reference_rasters(path: Path, grid) -> Reference:
    """Rasterise a reference file; features with role="ignore" form the ignore zone."""
    data = json.loads(path.read_text("utf-8"))
    features = data["features"] if data.get("type") == "FeatureCollection" else [data]
    to_grid = project(4326, grid.crs_epsg)
    objects, ignore = [], np.zeros((grid.height, grid.width), bool)
    for feature in features:
        props = feature.get("properties") or {}
        if "case" in props:
            continue  # the AOI box itself, if the reference was drawn on top of the AOI file
        geometry = transform(to_grid, shape(feature.get("geometry", feature)))
        if geometry.is_empty:
            continue
        raster = rasterize(
            [mapping(geometry)],
            out_shape=(grid.height, grid.width),
            transform=grid_affine(grid),
            dtype=np.uint8,
        ).astype(bool)
        if props.get("role") == "ignore":
            ignore |= raster
        else:
            objects.append(raster)
    return Reference(objects, ignore)


def run(case: dict) -> dict:
    manifest, grid, (a, b) = cached_bands(case)
    ref_path = ROOT / case["reference"] if case.get("reference") else None
    has_ref = bool(ref_path and ref_path.exists())
    reference = reference_rasters(ref_path, grid) if has_ref else None
    cover_file = case_dir(case) / "worldcover.npy"
    cover = np.load(cover_file) if cover_file.exists() else None
    result = evaluate(
        case["intent"],
        case["aoi_model"],
        grid,
        a,
        b,
        reference=reference,
        cover=cover,
    )
    directory = case_dir(case)
    metrics = dict(
        result.get("metrics", {}),
        case=case["id"],
        intent=case["intent"],
        status=result["status"],
        valid_overlap_pct=result["valid_overlap_pct"],
        thresholds_version=THRESHOLDS["version"],
        reference=case.get("reference") if has_ref else None,
        reference_source=reference_label(case, has_ref),
        scenes=[
            {k: s[k] for k in ("item_ids", "datetime", "aoi_cloud_pct")}
            for s in manifest["scenes"]
        ],
    )
    (directory / "metrics.json").write_text(json.dumps(metrics, indent=1), "utf-8")
    geojson = result.get("geojson") or {"type": "FeatureCollection", "features": []}
    (directory / "patches.geojson").write_text(json.dumps(geojson), "utf-8")
    overlay(a, b, result, reference).save(directory / "overlay.png")
    return metrics


# ---------------------------------------------------------------- images


def stretch(layers_list, inside):
    values = np.concatenate(
        [
            np.stack([layers[k] for k in ("red", "green", "blue")], -1)[inside]
            for layers in layers_list
        ]
    )
    finite = values[np.isfinite(values)]
    return np.percentile(finite, [2, 98]) if finite.size else (0.0, 0.3)


def rgb(layers, inside, limits=None) -> np.ndarray:
    low, high = limits if limits is not None else stretch([layers], inside)
    image = np.stack([layers[k] for k in ("red", "green", "blue")], -1)
    pixels = np.nan_to_num(np.clip((image - low) / max(high - low, 1e-3), 0, 1)) ** (
        1 / 1.2
    )
    pixels[~inside] *= 0.35  # dim outside the AOI
    return (pixels * 255).astype(np.uint8)


def side_by_side(images: list[np.ndarray], gap: int = 6) -> Image.Image:
    height = max(image.shape[0] for image in images)
    width = sum(image.shape[1] for image in images) + gap * (len(images) - 1)
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    x = 0
    for image in images:
        canvas.paste(Image.fromarray(image), (x, 0))
        x += image.shape[1] + gap
    return canvas


PATCH = np.array([22, 200, 220])  # cyan fill: grown patch
SEED = np.array([255, 60, 160])  # magenta: seed pixels
REFERENCE = (255, 220, 0)  # yellow outline: reference true change
IGNORE = (255, 255, 255)  # white outline: reference ignore zone


def overlay(a, b, result, reference=None) -> Image.Image:
    inside = result["inside"]
    limits = stretch([a, b], inside)
    before, after = rgb(a, inside, limits), rgb(b, inside, limits)
    marked = after.astype(np.float32)
    if result["labels"] is not None:
        patch = result["labels"] > 0
        marked[patch] = 0.45 * marked[patch] + 0.55 * PATCH
        seeds = result["seed"] & patch
        marked[seeds] = 0.25 * marked[seeds] + 0.75 * SEED
    panels = [before, after, marked.astype(np.uint8)]
    image = side_by_side(panels)
    if reference is not None:
        draw = ImageDraw.Draw(image)
        offset = 2 * (inside.shape[1] + 6)  # third panel
        for zones, color in (
            (reference.objects, REFERENCE),
            ([reference.ignore], IGNORE),
        ):
            outline = np.zeros_like(inside)
            for zone in zones:
                outline |= zone & ~binary_erosion(zone)
            for y, x in zip(*np.nonzero(outline), strict=True):
                draw.point((x + offset, y), fill=color)
    return image


# ---------------------------------------------------------------- report


METRIC_COLUMNS = [
    ("changed_ha", "ha"),
    ("n_patches", "patches"),
    ("changed_pct", "% clear"),
    ("largest_patch_ha", "largest ha"),
    ("valid_overlap_pct", "clear overlap %"),
    ("near_cloud_ha", f"≤{NEAR_CLOUD_M} m cloud ha"),
    ("seed_share", "seed share"),
    ("iou", "IoU"),
    ("precision", "precision"),
    ("recall", "recall"),
    ("object_recall", "object recall"),
]


def collected(cases):
    rows = []
    for case in cases:
        file = case_dir(case) / "metrics.json"
        metrics = json.loads(file.read_text("utf-8")) if file.exists() else None
        rows.append((case, metrics))
    return rows


def show(value):
    return (
        "–"
        if value is None
        else f"{value:g}"
        if isinstance(value, float)
        else str(value)
    )


def report(cases) -> None:
    rows = collected(cases)
    version = THRESHOLDS["version"]
    body = []
    for case, metrics in rows:
        cid = html.escape(case["id"])
        if metrics is None:
            body.append(f"<tr><td colspan=3><b>{cid}</b>: not run yet</td></tr>")
            continue
        scenes = "<br>".join(
            f"{s['datetime'][:10]} · {s['aoi_cloud_pct']:g}% cloud<br><code>{html.escape(', '.join(s['item_ids']))}</code>"
            for s in metrics["scenes"]
        )
        table = "".join(
            f"<tr><th>{label}</th><td>{show(metrics.get(key))}</td></tr>"
            for key, label in METRIC_COLUMNS
        )
        body.append(
            f"<tr><td><h2>{cid}</h2><p>{html.escape(case['intent'])} · {metrics['status']}</p>"
            f"<p>{html.escape(case.get('note', ''))}</p><p>{scenes}</p>"
            f"<p>Reference: {html.escape(metrics.get('reference_source') or 'none yet')}"
            f"{'<br><code>' + html.escape(metrics['reference']) + '</code>' if metrics.get('reference') else ''}</p></td>"
            f"<td><table class=m>{table}</table></td>"
            f"<td><img src='{cid}/overlay.png' alt='{cid} overlay'></td></tr>"
        )
    page = f"""<!doctype html><meta charset=utf-8><title>Patch evaluation</title>
<style>body{{font:14px system-ui;margin:16px;background:#fff;color:#111}}
td{{vertical-align:top;padding:8px;border-top:1px solid #ccc}} img{{max-width:900px;width:100%}}
table.m th{{text-align:left;font-weight:normal;color:#555;padding-right:12px}} h2{{margin:0}}</style>
<h1>Patch evaluation · thresholds v{version}</h1>
<p>Panels: before · after · after with patches (cyan), seed pixels (magenta), reference true change (yellow), ignore zone (white).</p>
<table>{"".join(body)}</table>"""
    (out_dir() / "report.html").write_text(page, "utf-8")
    baseline_doc(rows, version)


def baseline_doc(rows, version) -> None:
    header = (
        "| case | intent | dates | reference | "
        + " | ".join(label for _, label in METRIC_COLUMNS)
        + " |"
    )
    lines = [
        "# Patch evaluation baseline",
        "",
        f"Generated by `python -m scripts.eval_patches report` · thresholds v{version}.",
        "Metrics from `scripts/eval_patches.py`; IoU, precision and recall are scored inside",
        "clear coverage against hand-drawn polygons in `config/eval_refs/` (– = no reference yet).",
        f"Object recall: share of reference polygons with ≥ {OBJECT_COVER:.0%} of their area detected.",
        "",
        header,
        "|" + " --- |" * (header.count("|") - 1),
    ]
    for case, metrics in rows:
        if metrics is None:
            continue
        dates = " → ".join(s["datetime"][:10] for s in metrics["scenes"])
        values = " | ".join(show(metrics.get(key)) for key, _ in METRIC_COLUMNS)
        source = metrics.get("reference_source") or "–"
        lines.append(
            f"| {case['id']} | {case['intent']} | {dates} | {source} | {values} |"
        )
    BASELINE_DOC.write_text("\n".join(lines) + "\n", "utf-8")


# ---------------------------------------------------------------- compare

IOU_TOLERANCE = 0.02  # a referenced case may not lose more than 2 IoU points


def snapshot_file() -> Path:
    return out_dir() / "baseline.json"


def snapshot(cases) -> dict:
    """Freeze the current metrics of every case as the comparison baseline."""
    frozen = {case["id"]: metrics for case, metrics in collected(cases) if metrics}
    frozen["_thresholds_version"] = THRESHOLDS["version"]
    snapshot_file().write_text(json.dumps(frozen, indent=1), "utf-8")
    return frozen


def check_case(case: dict, before: dict | None, after: dict | None) -> list[str]:
    """Problems with `after` vs `before` for one case (empty list = pass)."""
    if after is None:
        return ["not run"]
    if before is None:
        return []  # new case: nothing to regress against
    problems = []
    if before.get("iou") is not None and after.get("iou") is not None:
        if after["iou"] < before["iou"] - IOU_TOLERANCE:
            problems.append(f"IoU {before['iou']} -> {after['iou']}")
    guards = case.get("guards") or {}
    ha = after.get("changed_ha") or 0
    if "min_ha" in guards and ha < guards["min_ha"]:
        problems.append(f"{ha} ha < min {guards['min_ha']}")
    if (
        "max_patches" in guards
        and (after.get("n_patches") or 0) > guards["max_patches"]
    ):
        problems.append(f"{after['n_patches']} patches > max {guards['max_patches']}")
    if "ha_within_pct" in guards and before.get("changed_ha"):
        drift = 100 * abs(ha - before["changed_ha"]) / before["changed_ha"]
        if drift > guards["ha_within_pct"]:
            problems.append(f"ha moved {drift:.1f}% (> {guards['ha_within_pct']}%)")
    for name in guards.get("no_drop", []):
        old, new = before.get(name), after.get(name)
        if old is not None and (new is None or new < old - 0.5):
            problems.append(f"{name} {old} -> {new}")
    for cls, max_drop in (guards.get("keep_cover_ha") or {}).items():
        old = (before.get("ha_by_cover") or {}).get(cls, 0)
        new = (after.get("ha_by_cover") or {}).get(cls, 0)
        if old and new < old * (1 - max_drop / 100):
            problems.append(f"{cls} {old} -> {new} ha (> {max_drop}% drop)")
    return problems


PROXY_KEYS = ("iou", "clear_water_change_detected_pct", "changed_ha")


def proxy(metrics: dict | None) -> str:
    """The headline number for a case: IoU when referenced, else a proxy."""
    if not metrics:
        return "–"
    for key in PROXY_KEYS:
        if metrics.get(key) is not None:
            label = {"iou": "IoU", "changed_ha": "ha"}.get(key, "clear water %")
            return f"{label} {show(metrics[key])}"
    return "–"


def compare(cases) -> bool:
    if not snapshot_file().exists():
        raise SystemExit("No baseline yet. Run `snapshot` first.")
    frozen = json.loads(snapshot_file().read_text("utf-8"))
    ok = True
    print(
        f"baseline thresholds v{frozen.get('_thresholds_version')} -> now v{THRESHOLDS['version']}"
    )
    for case, metrics in collected(cases):
        before = frozen.get(case["id"])
        problems = check_case(case, before, metrics)
        ok &= not problems
        status = "FAIL " + "; ".join(problems) if problems else "ok"
        print(
            f"  {case['id']:18s} {proxy(before):>20s} -> {proxy(metrics):<20s} {status}"
        )
    print("PASS" if ok else "FAIL")
    return ok


# ---------------------------------------------------------------- CLI


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command", choices=["fetch", "run", "report", "snapshot", "compare"]
    )
    parser.add_argument("--case")
    args = parser.parse_args(argv)
    cases = load_cases(args.case)
    if args.command == "snapshot":
        frozen = snapshot(load_cases())
        print("froze", len(frozen) - 1, "cases to", snapshot_file())
        return
    if args.command == "compare":
        raise SystemExit(0 if compare(load_cases()) else 1)
    if args.command == "report":
        report(load_cases())
        print("wrote", out_dir() / "report.html", "and", BASELINE_DOC)
        return
    for case in cases:
        if args.command == "fetch":
            manifest = fetch(case)
            print(
                case["id"],
                *(
                    f"{s['role']}: {s['datetime'][:10]} {s['aoi_cloud_pct']:g}% cloud"
                    for s in manifest["scenes"]
                ),
                sep="\n  ",
            )
        else:
            metrics = run(case)
            keys = (
                "status",
                "changed_ha",
                "n_patches",
                "changed_pct",
                "largest_patch_ha",
                "seed_share",
                "iou",
            )
            print(case["id"], {k: metrics.get(k) for k in keys})


if __name__ == "__main__":
    main()
