"""Compare the Rondônia preset's detected vegetation loss with INPE PRODES.

    python -m scripts.validate_prodes            # reuse saved PRODES responses
    python -m scripts.validate_prodes --refresh  # fetch PRODES again

PRODES year N covers roughly August N-1 to July N, so years 2020-2024 match
the preset's 2019-08 to 2024-08 window. Both layers are rasterised onto the
engine's own 10 m UTM grid and compared pixel by pixel inside the AOI.
Results land in var/validation/prodes_rondonia.json.
"""

import json
import sys
from datetime import date, datetime, timezone
from uuid import NAMESPACE_URL, uuid5

import httpx
import numpy as np
from rasterio.features import rasterize
from shapely import transform
from shapely.geometry import mapping, shape

from node.verdict import TemplateVerdict
from satcore.config import ROOT
from satcore.demo import presets
from satcore.geo import affine, aoi_mask, build_grid, project
from satcore.models import AOI, UplinkCommand
from satcore.pipeline import run_analysis

PRESET = "real_rondonia"
YEARS = (2020, 2024)
WFS = "https://terrabrasilis.dpi.inpe.br/geoserver/prodes-legal-amz/ows"
YEARLY = "prodes-legal-amz:yearly_deforestation"  # one polygon set per year, 2008+
BASELINE = "prodes-legal-amz:accumulated_deforestation_2007"  # everything before
OUT = ROOT / "var/validation"
FILES = {
    YEARLY: OUT / "prodes_rondonia.geojson",
    BASELINE: OUT / "prodes_rondonia_pre2008.geojson",
}
MMU_NOTE = (
    "Secondary sources say PRODES counts clearings of 6.25 ha or more and also "
    "maps polygons down to 1 ha; INPE's own methodology text was not checked. "
    "Polygons are split at scene and state borders, so the fragment sizes "
    "below are a lower bound on the mapping unit."
)


def fetch(layer: str, bbox: tuple, refresh: bool) -> tuple[dict, str]:
    """Return the layer's features in the bbox and the day they were fetched."""
    path = FILES[layer]
    if refresh or not path.exists():
        response = httpx.get(
            WFS,
            params={
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeNames": layer,
                "outputFormat": "application/json",
                "srsName": "EPSG:4326",
                "bbox": ",".join(f"{v:.5f}" for v in bbox) + ",EPSG:4326",
                "count": 5000,
            },
            headers={"User-Agent": "Mozilla/5.0 (compatible; wfs-client)"},
            timeout=180,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("numberReturned") != data.get("totalFeatures"):
            raise RuntimeError(f"{layer}: response was truncated")
        OUT.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
    data = json.loads(path.read_text(encoding="utf-8"))
    fetched = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return data, fetched.date().isoformat()


def burn(features: list, grid) -> np.ndarray:
    """Rasterise lon/lat features onto the grid (pixel-centre rule)."""
    if not features:
        return np.zeros((grid.height, grid.width), bool)
    to_utm = project(4326, grid.crs_epsg)

    def move(xy):
        return np.column_stack(to_utm(xy[:, 0], xy[:, 1]))

    shapes = [mapping(transform(shape(f["geometry"]), move)) for f in features]
    return rasterize(
        shapes, (grid.height, grid.width), transform=affine(grid), dtype="uint8"
    ).astype(bool)


def detect(case: dict):
    return run_analysis(
        UplinkCommand(
            analysis_id=uuid5(NAMESPACE_URL, f"validate-prodes/{case['id']}"),
            aoi=AOI.model_validate(case["aoi"]),
            date_a=date.fromisoformat(case["date_a"]),
            date_b=date.fromisoformat(case["date_b"]),
            intent=case["intent"],
        ),
        lambda *_: None,
        TemplateVerdict(),
    )


def main(refresh: bool = False) -> dict:
    case = next(c for c in presets() if c["id"] == PRESET)
    packet = detect(case)
    if packet.status != "ok" or not packet.change_geojson:
        raise SystemExit(f"Engine returned {packet.status}; nothing to validate.")
    grid = build_grid(packet.aoi)
    inside = aoi_mask(packet.aoi, grid)
    bbox = shape(packet.aoi.geometry).bounds

    yearly, fetched = fetch(YEARLY, bbox, refresh)
    baseline, _ = fetch(BASELINE, bbox, refresh)
    by_year: dict[int, list] = {}
    for feature in yearly["features"]:
        by_year.setdefault(feature["properties"]["year"], []).append(feature)

    def years(low, high):
        return [f for y, fs in by_year.items() if low <= y <= high for f in fs]

    ours = burn(packet.change_geojson["features"], grid) & inside
    target = burn(years(*YEARS), grid) & inside
    earlier = burn(years(0, YEARS[0] - 1) + baseline["features"], grid) & inside
    later = burn(years(YEARS[1] + 1, 9999), grid) & inside

    def ha(mask) -> float:
        return round(float(mask.sum()) * grid.resolution**2 / 10_000, 2)

    def ratio(part, whole):
        return round(float(part.sum() / whole.sum()), 4) if whole.sum() else None

    hit = ours & target
    false_positive = ours & ~target
    recent = years(*YEARS)
    sizes = sorted(f["properties"]["area_km"] * 100 for f in recent)
    result = {
        "preset": PRESET,
        "aoi_ha": ha(inside),
        "grid": {"epsg": grid.crs_epsg, "resolution_m": grid.resolution},
        "scenes": [str(scene.datetime.date()) for scene in packet.scenes],
        "valid_overlap_pct": packet.stats.valid_overlap_pct,
        "engine_changed_ha": packet.stats.changed_ha,
        "engine_artefact_ha": packet.stats.artefact_ha,
        "detected_ha": ha(ours),
        "prodes_ha": ha(target),
        "intersection_ha": ha(hit),
        "precision": ratio(hit, ours),
        "recall": ratio(hit, target),
        "iou": ratio(hit, ours | target),
        "false_positive_ha": ha(false_positive),
        "false_positive_on_earlier_prodes_ha": ha(false_positive & earlier),
        "false_positive_on_earlier_prodes_share": ratio(
            false_positive & earlier, false_positive
        ),
        "false_positive_on_later_prodes_ha": ha(false_positive & later & ~earlier),
        "false_positive_elsewhere_ha": ha(false_positive & ~earlier & ~later),
        "missed_prodes_ha": ha(target & ~ours),
        "prodes_earlier_ha": ha(earlier),
        "prodes_ha_by_year": {
            str(year): ha(burn(by_year[year], grid) & inside)
            for year in sorted(by_year)
        },
        "prodes_image_dates": {
            str(year): sorted({f["properties"]["image_date"] for f in by_year[year]})
            for year in sorted(by_year)
            if year >= YEARS[0] - 1
        },
        "prodes_fragments": {
            "count": len(recent),
            "smallest_ha": round(sizes[0], 3) if sizes else None,
            "under_1_ha": sum(s < 1 for s in sizes),
            "under_6_25_ha": sum(s < 6.25 for s in sizes),
        },
        "minimum_mapped_area_note": MMU_NOTE,
        "prodes_years": list(YEARS),
        "source": {
            "url": WFS,
            "layer": YEARLY,
            "earlier_layers": [f"{YEARLY} (2008-{YEARS[0] - 1})", BASELINE],
            "bbox_lonlat": [round(v, 5) for v in bbox],
            "fetched": fetched,
            "prodes_pub_date": sorted(
                {f["properties"]["pub_date"] for f in yearly["features"]}
            ),
            "attribution": "PRODES, INPE / TerraBrasilis",
        },
    }
    (OUT / "prodes_rondonia.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return result


if __name__ == "__main__":
    summary = main(refresh="--refresh" in sys.argv[1:])
    for key, value in summary.items():
        if not isinstance(value, dict | list) and key != "minimum_mapped_area_note":
            print(f"{key:<42}{value}")
