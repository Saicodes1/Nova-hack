import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import numpy as np

from .config import data_path
from .load import read_band
from .models import Grid
from .stac import Selection


def load_scene(selection: Selection, grid: Grid):
    scene = selection.scene
    identity = "|".join([*scene.item_ids, grid.model_dump_json()])
    key = hashlib.sha256(identity.encode()).hexdigest()
    file = data_path("SCENE_STORE", "var/scenes") / f"{key}.npz"
    if file.exists():
        with np.load(file, allow_pickle=False) as stored:
            return {name: stored[name] for name in stored.files}
    names = list(selection.assets)
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        bands = dict(
            zip(
                names,
                pool.map(
                    lambda name: read_band(selection.assets[name], grid, name == "scl"),
                    names,
                ),
                strict=True,
            )
        )
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name(f".{file.stem}-{uuid4().hex}.npz")
    try:
        np.savez_compressed(temporary, **bands)
        os.replace(temporary, file)
    finally:
        temporary.unlink(missing_ok=True)
    return bands


def _key(*parts) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


def select_scene(aoi, grid: Grid, target, role, window, **limits):
    """Scene selection with an on-disk memo.

    A repeated question about a past date gets the same answer from the
    catalog, so the choice is stored and the node can replay it without a
    network connection. Dates within the last 30 days are always re-checked
    because new acquisitions may still arrive.
    """
    import json
    from dataclasses import asdict
    from datetime import date, timedelta

    from . import stac
    from .load import Asset
    from .models import SceneRef

    file = data_path("SCENE_STORE", "var/scenes") / (
        "pick-"
        + _key(
            grid.model_dump_json(), aoi.model_dump_json(), target, role, window, limits
        )
        + ".json"
    )
    settled = target + timedelta(days=window) < date.today() - timedelta(days=30)
    if settled and file.exists():
        saved = json.loads(file.read_text(encoding="utf-8"))
        return Selection(
            SceneRef.model_validate(saved["scene"]),
            {
                band: [Asset(**asset) for asset in assets]
                for band, assets in saved["assets"].items()
            },
        )
    found = stac.select_scene(aoi, grid, target, role, window, **limits)
    if found and settled:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(
            json.dumps(
                {
                    "scene": found.scene.model_dump(mode="json"),
                    "assets": {
                        band: [asdict(asset) for asset in assets]
                        for band, assets in found.assets.items()
                    },
                }
            ),
            encoding="utf-8",
        )
    return found


def worldcover(aoi, grid: Grid):
    """WorldCover for the grid, stored after the first download."""
    from . import stac

    file = data_path("SCENE_STORE", "var/scenes") / (
        "cover-" + _key(grid.model_dump_json()) + ".npy"
    )
    if file.exists():
        return np.load(file, allow_pickle=False)
    cover = stac.worldcover(aoi, grid)
    if cover is not None:
        file.parent.mkdir(parents=True, exist_ok=True)
        np.save(file, cover)
    return cover


def passes(aoi, grid: Grid, start, end):
    """Overpasses of the area with cloud cover, stored once the period is past."""
    import json
    from datetime import date, timedelta

    from . import stac

    file = data_path("SCENE_STORE", "var/scenes") / (
        "passes-"
        + _key(grid.model_dump_json(), aoi.model_dump_json(), start, end)
        + ".json"
    )
    settled = end < date.today() - timedelta(days=30)
    if settled and file.exists():
        return json.loads(file.read_text(encoding="utf-8"))
    found = stac.passes(aoi, grid, start, end)
    if found and settled:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(found), encoding="utf-8")
    return found
