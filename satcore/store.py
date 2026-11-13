import hashlib
import os
from datetime import date
from uuid import uuid4

import numpy as np

from . import stac
from .config import data_path
from .load import read_band
from .models import AOI, Grid, SceneRef
from .stac import Selection


def _key(item_ids: list[str], grid: Grid) -> str:
    identity = "|".join([*item_ids, grid.model_dump_json()])
    return hashlib.sha256(identity.encode()).hexdigest()


def scene_file(selection: Selection, grid: Grid):
    """Cache file for a scene's bands cropped to `grid`."""
    return (
        data_path("SCENE_STORE", "var/scenes")
        / f"{_key(selection.scene.item_ids, grid)}.npz"
    )


def _ref_file(item_ids: list[str], grid: Grid):
    return data_path("SCENE_STORE", "var/scenes") / f"{_key(item_ids, grid)}.json"


def _write(file, write) -> None:
    """Write via a temporary file so a crash never leaves a half-written cache entry."""
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name(f".{file.stem}-{uuid4().hex}{file.suffix}")
    try:
        write(temporary)
        os.replace(temporary, file)
    finally:
        temporary.unlink(missing_ok=True)


def pinned_selection(
    aoi: AOI, grid: Grid, item_ids: list[str], role, target: date, backup: bool = False
) -> Selection:
    """Selection for pinned scenes, from the local cache when possible.

    A pinned scene whose bands and catalog record are both cached is rebuilt
    without any network access (offline demos). Otherwise the catalog is asked
    once and the record is cached next to the bands by `load_scene`.
    """
    ids = sorted(item_ids)
    ref_file = _ref_file(ids, grid)
    if ref_file.exists():
        scene = SceneRef.model_validate_json(ref_file.read_text("utf-8"))
        selection = Selection(
            scene.model_copy(update={"role": role, "requested_date": target}), {}
        )
        if scene_file(selection, grid).exists():
            return selection
    return stac.pinned_scene(aoi, grid, ids, role, target, backup)


def load_scene(selection: Selection, grid: Grid):
    file = scene_file(selection, grid)
    ref_file = _ref_file(selection.scene.item_ids, grid)
    if file.exists():
        with np.load(file, allow_pickle=False) as stored:
            bands = {name: stored[name] for name in stored.files}
    else:
        bands = {
            key: read_band(assets, grid, key == "scl")
            for key, assets in selection.assets.items()
        }
        _write(file, lambda path: np.savez_compressed(path, **bands))
    if not ref_file.exists() and selection.assets:
        record = selection.scene.model_dump_json()
        _write(ref_file, lambda path: path.write_text(record, "utf-8"))
    return bands


def worldcover_file(grid: Grid):
    return data_path("SCENE_STORE", "var/scenes") / f"worldcover-{_key([], grid)}.npy"


def worldcover(aoi: AOI, grid: Grid):
    """ESA WorldCover classes on `grid`, cached locally after the first download."""
    file = worldcover_file(grid)
    if file.exists():
        return np.load(file, allow_pickle=False)
    cover = stac.worldcover(aoi, grid)
    if cover is not None:
        _write(file, lambda path: np.save(path, cover))
    return cover
