import json
from datetime import datetime, time, timezone

import numpy as np
import yaml
from scipy.ndimage import gaussian_filter

from .config import ROOT
from .geo import place_box
from .models import AOI, Grid, SceneRef


def cases():
    """Demo cases. Real ones pin catalog scenes; synthetic ones use generated bands."""
    entries = yaml.safe_load(
        (ROOT / "config/demo_cases.yaml").read_text(encoding="utf-8")
    )["cases"]
    result = []
    for case in entries:
        synthetic = not (case.get("item_a") and case.get("item_b"))
        if case.get("aoi"):
            aoi = AOI(geometry=_geometry(ROOT / case["aoi"]), name=case["name"])
        else:
            aoi = place_box(case["lon"], case["lat"], case["name"])
        title = f"Synthetic · {case['title']}" if synthetic else case["title"]
        result.append(
            dict(case, title=title, aoi=aoi.model_dump(), synthetic=synthetic)
        )
    return result


def _geometry(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("type") == "FeatureCollection":
        data = data["features"][0]
    return data.get("geometry", data)


def scene(case_id, role, target):
    return SceneRef(
        role=role,
        item_ids=[f"DEMO-{case_id.upper()}-{role}"],
        datetime=datetime.combine(target, time(7, 18), tzinfo=timezone.utc),
        requested_date=target,
        days_from_requested=0,
        aoi_cloud_pct=0,
        source="synthetic",
        asset_hrefs={},
    )


def bands(grid: Grid, case_id: str, role: str):
    y, x = np.mgrid[0 : grid.height, 0 : grid.width]
    x, y = x / grid.width, y / grid.height
    rng = np.random.default_rng(14)
    texture = gaussian_filter(rng.normal(size=x.shape), 3) * 0.07 + rng.normal(
        0, 0.006, x.shape
    )
    channel = abs(y - (0.43 + 0.12 * np.sin(x * 8))) < 0.045
    patch = ((x - 0.58) / 0.23) ** 2 + ((y - 0.52) / 0.13) ** 2 < 1
    roads = (np.mod(x + 0.15 * y, 0.18) < 0.007) | (np.mod(y - 0.22 * x, 0.15) < 0.006)
    plots = ((np.floor(x * 31) + np.floor(y * 27) * 3) % 5) < 2
    values = {
        "sand": (0.18, 0.23, 0.27, 0.31, 0.36, 0.32),
        "vegetation": (0.045, 0.095, 0.065, 0.37, 0.17, 0.11),
        "water": (0.11, 0.14, 0.085, 0.042, 0.022, 0.016),
        "built": (0.19, 0.20, 0.22, 0.23, 0.42, 0.37),
    }
    keys = np.full(x.shape, "sand", dtype="U12")
    keys[plots & (x < 0.6)] = "vegetation"
    keys[roads] = "built"
    if case_id.startswith("dubai") and case_id != "dubai_south":
        keys[channel] = "water"
        if role == "B":
            keys[patch] = "water"
    elif case_id == "rondonia":
        keys[~roads] = "vegetation"
        if role == "B":
            keys[patch & ~plots] = "sand"
    elif role == "B":
        keys[patch & plots] = "built"
    layers = {}
    for i, name in enumerate(("blue", "green", "red", "nir", "swir16", "swir22")):
        layer = np.zeros(x.shape, np.float32)
        for key, signature in values.items():
            layer[keys == key] = signature[i]
        layers[name] = (layer + texture).clip(0.001).astype(np.float32)
    layers["scl"] = np.where(keys == "water", 6, 4).astype(np.uint8)
    if role == "B":
        cloud = (x - 0.87) ** 2 + (y - 0.17) ** 2 < 0.10**2
        if case_id == "dubai_cloudy":
            cloud |= x < 0.73 + 0.06 * np.sin(y * 8)
        layers["scl"][cloud] = 9
        for name in ("red", "green", "blue"):
            layers[name][cloud] = 0.55 + texture[cloud]
    return layers
