import logging
from collections.abc import Callable
from contextlib import contextmanager
from time import perf_counter
from typing import Protocol

import numpy as np

from . import change, demo, indices, landcover, masks, render, stac, store, urban_model
from .config import THRESHOLDS, data_path
from .geo import aoi_mask, build_grid, validate_aoi
from .models import AnswerPacket, ChangeStats, UplinkCommand
from .packet import raw_equivalent
from .trust import assess

Emit = Callable[[str, str, dict], None]
logger = logging.getLogger(__name__)


class VerdictWriter(Protocol):
    def write(self, packet: AnswerPacket) -> str: ...


# Bands each analysis type needs in both dates for a pixel to count as clear.
REQUIRED_BANDS = {
    "water_gain": ("green", "nir", "swir16"),
    "water_loss": ("green", "nir", "swir16"),
    "veg_gain": ("nir", "red"),
    "veg_loss": ("nir", "red"),
    "urban_growth": ("red", "nir", "swir16"),
    "visible_change": ("red", "green", "blue"),
    "burn": ("nir", "swir22"),
}


def cloud_buffer_px() -> int:
    return THRESHOLDS["cleanup"].get("cloud_buffer_px", 0)


def clear_masks(a: dict, b: dict, inside: np.ndarray, intent: str):
    """Clear pixels in both dates, the subset used for detection, and overlap %.

    `valid` is clear coverage (counts toward overlap and the changed share);
    `detectable` also skips a fringe around clouds and their shadows.
    """
    water_intent = intent.startswith("water")
    valid = (
        inside
        & masks.valid_mask(a["scl"], exclude_dark=water_intent)
        & masks.valid_mask(b["scl"], exclude_dark=water_intent)
    )
    for layers in (a, b):
        if water_intent:
            valid &= np.isfinite(layers["green"]) & (
                np.isfinite(layers["nir"]) | np.isfinite(layers["swir16"])
            )
        else:
            for band in REQUIRED_BANDS[intent]:
                valid &= np.isfinite(layers[band])
    overlap = round(100 * valid.sum() / max(1, inside.sum()), 1)
    detectable = valid & ~masks.near_clouds(a["scl"], b["scl"], cloud_buffer_px())
    return valid, detectable, overlap


def uses_model(intent: str, demo_id: str | None) -> bool:
    return intent in {"urban_growth", "visible_change"} and not demo_id


def model_scores(a: dict, b: dict, valid: np.ndarray, intent: str):
    """FresUNet change probability, or None when the model is unavailable."""
    try:
        return urban_model.predict(a, b, valid)
    except Exception:
        if intent == "visible_change":
            raise
        logger.exception("FresUNet inference failed; using the urban rules.")
        return None


def _evidence(a, b, model_score):
    """Keyword arguments shared by change.detect and change.candidates."""
    return {
        "reflectance": (a, b),
        "model_score": model_score,
        "scl": (a["scl"], b["scl"]),
    }


def detect_patches(ia, ib, a, b, detectable, grid, intent, model_score=None):
    """Labelled patches, the cloud-edge zone and the patch polygons."""
    edge = masks.cloud_edge(a["scl"], b["scl"], cloud_buffer_px() + 2)
    pixel_ha = grid.resolution**2 / 10_000
    labels = change.detect(
        ia, ib, detectable, intent, pixel_ha, **_evidence(a, b, model_score)
    )
    return labels, edge, change.polygons(labels, edge, grid, intent)


def change_stats(labels, valid, edge, geojson, overlap, aoi, grid, cover=None):
    """Packet statistics for the detected patches."""
    mask = labels > 0
    pixel_ha = grid.resolution**2 / 10_000
    areas = [feature["properties"]["ha"] for feature in geojson["features"]]
    return ChangeStats(
        changed_ha=round(mask.sum() * pixel_ha, 2),
        changed_pct=round(100 * mask.sum() / valid.sum(), 1),
        valid_overlap_pct=overlap,
        aoi_ha=round(validate_aoi(aoi) * 100, 2),
        n_polygons=len(areas),
        largest_polygon_ha=max(areas, default=0),
        from_to=landcover.breakdown(mask, cover, pixel_ha),
        artefact_ha=round((mask & edge).sum() * pixel_ha, 2),
    )


def seed_mask(ia, ib, a, b, detectable, intent, model_score=None):
    """The strict seed pixels behind detect_patches (for evaluation and display)."""
    seed, _, _ = change.candidates(
        ia, ib, detectable, intent, **_evidence(a, b, model_score)
    )
    return seed


def run_analysis(
    cmd: UplinkCommand, emit: Emit, verdict: VerdictWriter
) -> AnswerPacket:
    timings = {}

    @contextmanager
    def stage(name):
        detail = {}
        emit(name, "started", detail)
        start = perf_counter()
        yield detail
        timings[name] = round((perf_counter() - start) * 1000)
        emit(name, "done", dict(detail, ms=timings[name]))

    grid = build_grid(cmd.aoi)
    inside = aoi_mask(cmd.aoi, grid)
    selections, scenes = [], []
    for role, target in zip(("A", "B"), (cmd.date_a, cmd.date_b), strict=True):
        with stage(f"scene_select_{role.lower()}") as detail:
            if cmd.demo_id:
                scenes.append(demo.scene(cmd.demo_id, role, target))
            else:
                found = stac.select_scene(
                    cmd.aoi,
                    grid,
                    target,
                    role,
                    cmd.window_days,
                    after=scenes[0].datetime if scenes else None,
                )
                if found:
                    selections.append(found)
                    scenes.append(found.scene)
            detail["scene_ids"] = (
                scenes[-1].item_ids if scenes and scenes[-1].role == role else []
            )
    stats, geojson, thumbnails, bounds = None, None, {}, []
    overlap, haze, cover = 0, 0, None
    model_score = None
    directory = data_path("ANALYSES_DIR", "var/analyses") / str(cmd.analysis_id)
    if len(scenes) == 2:
        with stage("load"):
            a, b = (
                [demo.bands(grid, cmd.demo_id, role) for role in ("A", "B")]
                if cmd.demo_id
                else [store.load_scene(selection, grid) for selection in selections]
            )
        with stage("cloud_check") as detail:
            for scene, layers in zip(scenes, (a, b), strict=True):
                scene.aoi_cloud_pct = masks.aoi_cloud_pct(layers["scl"], inside)
            valid, detectable, overlap = clear_masks(a, b, inside, cmd.intent)
            detail["valid_overlap_pct"] = overlap
        labels = edge = None
        if overlap < THRESHOLDS["trust"]["overlap_low_pct"]:
            emit("cloud_check", "refused", {"valid_overlap_pct": overlap})
        else:
            with stage("indices"):
                ia, ib = indices.calculate(a), indices.calculate(b)
                directory.mkdir(parents=True, exist_ok=True)
                np.savez_compressed(
                    directory / "indices.npz",
                    grid=grid.model_dump_json(),
                    valid=valid,
                    **{
                        f"{role}_{name}": data
                        for role, values in (("a", ia), ("b", ib))
                        for name, data in values.items()
                    },
                )
            with stage("change") as detail:
                if uses_model(cmd.intent, cmd.demo_id):
                    model_score = model_scores(a, b, valid, cmd.intent)
                    detail["detector"] = (
                        urban_model.MODEL_NAME
                        if model_score is not None
                        else "Built-up evidence"
                    )
                labels, edge, geojson = detect_patches(
                    ia, ib, a, b, detectable, grid, cmd.intent, model_score
                )
            with stage("landcover"):
                cover = None if cmd.demo_id else stac.worldcover(cmd.aoi, grid)
                if cover is not None and cmd.intent != "visible_change":
                    stable = valid & np.isin(cover, [50, 60])
                    index = {
                        "water": "mndwi",
                        "veg": "ndvi",
                        "urban": "ndbi",
                        "burn": "nbr",
                    }[cmd.intent.split("_")[0]]
                    shifts = np.abs(ia[index][stable] - ib[index][stable])
                    if np.isfinite(shifts).any():
                        haze = float(np.nanmedian(shifts))
                stats = change_stats(
                    labels, valid, edge, geojson, overlap, cmd.aoi, grid, cover
                )
    with stage("trust"):
        status, trust = assess(
            scenes, overlap, stats, cover is not None, cmd.season_baseline, haze
        )
        if cmd.intent == "urban_growth" and stats is not None:
            trust.flags.append("construction_candidates")
            trust.caveats.append(
                "The urban overlay includes possible construction. Changes in exposed soil "
                "can produce a similar signal to new roads or roofs."
            )
            if model_score is not None:
                trust.flags.append("fresunet_assisted")
        if cmd.intent == "visible_change" and stats is not None:
            trust.flags.append("fresunet_visible_change")
            trust.caveats.insert(
                0,
                "FresUNet highlights visible differences. Lighting, shadows and seasonal changes can appear in the overlay.",
            )
    if len(scenes) == 2:
        with stage("render") as detail:
            thumbnails, bounds = render.render(
                directory,
                grid,
                inside,
                a,
                b,
                labels,
                edge,
                cmd.intent in {"water_loss", "veg_loss", "burn"},
            )
            detail["image_bounds"] = bounds
    if cmd.demo_id:
        attribution = [
            "Synthetic demonstration imagery. Values illustrate the workflow."
        ]
    else:
        years = ", ".join(
            str(y) for y in sorted({scene.datetime.year for scene in scenes})
        )
        sources = {
            "earth-search": "Element 84 Earth Search",
            "planetary-computer": "Microsoft Planetary Computer",
        }
        attribution = (
            [f"Contains modified Copernicus Sentinel data ({years})"] if years else []
        )
        attribution.extend(
            f"Imagery accessed via {sources[source]}"
            for source in sorted({scene.source for scene in scenes})
            if source in sources
        )
        if cover is not None:
            attribution.append(
                "© ESA WorldCover project 2021 / Copernicus Sentinel data"
            )
        if model_score is not None:
            attribution.append(
                "Change model: FresUNet, RGB checkpoint published by Elyes Ouerghi (IPOL, 2022)."
            )
    packet = AnswerPacket(
        analysis_id=cmd.analysis_id,
        intent=cmd.intent,
        aoi=cmd.aoi,
        scenes=scenes,
        stats=stats,
        trust=trust,
        status=status,
        change_geojson=geojson,
        thumbnails=thumbnails,
        image_bounds=bounds,
        onboard_verdict="",
        timings_ms={},
        attribution=attribution,
        raw_equivalent_bytes=raw_equivalent(selections),
    )
    with stage("verdict"):
        packet.onboard_verdict = verdict.write(packet)
    with stage("packet"):
        pass
    packet.timings_ms = timings
    return packet
