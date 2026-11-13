import hashlib
import os
from uuid import uuid4

import numpy as np

from .config import data_path
from .load import read_band
from .models import Grid
from .stac import Selection


def scene_file(selection: Selection, grid: Grid):
    """Cache file for a scene's bands cropped to `grid`."""
    identity = "|".join([*selection.scene.item_ids, grid.model_dump_json()])
    key = hashlib.sha256(identity.encode()).hexdigest()
    return data_path("SCENE_STORE", "var/scenes") / f"{key}.npz"


def load_scene(selection: Selection, grid: Grid):
    file = scene_file(selection, grid)
    if file.exists():
        with np.load(file, allow_pickle=False) as stored:
            return {name: stored[name] for name in stored.files}
    bands = {
        key: read_band(assets, grid, key == "scl")
        for key, assets in selection.assets.items()
    }
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name(f".{file.stem}-{uuid4().hex}.npz")
    try:
        np.savez_compressed(temporary, **bands)
        os.replace(temporary, file)
    finally:
        temporary.unlink(missing_ok=True)
    return bands
