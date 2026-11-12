import base64
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

from .geo import project
from .models import Grid


def render(directory: Path, grid: Grid, inside, a, b, labels, edge, loss):
    directory.mkdir(parents=True, exist_ok=True)
    rgb = [
        np.stack([layers[k] for k in ("red", "green", "blue")], axis=-1)
        for layers in (a, b)
    ]
    values = np.concatenate([image[inside] for image in rgb])
    finite = values[np.isfinite(values)]
    low, high = np.percentile(finite, [2, 98]) if finite.size else (0, 1)
    images = {}
    for role, image in zip(("a", "b"), rgb, strict=True):
        pixels = np.nan_to_num(
            np.clip((image - low) / max(high - low, 0.001), 0, 1)
        ) ** (1 / 1.2)
        alpha = (inside & np.isfinite(image).all(axis=-1)) * 255
        images[role] = Image.fromarray(
            np.dstack([pixels * 255, alpha]).astype(np.uint8)
        )
    mask = np.zeros((*inside.shape, 4), dtype=np.uint8)
    if labels is not None:
        mask[labels > 0] = (220, 38, 38, 200) if loss else (22, 163, 74, 200)
        mask[(labels > 0) & edge] = (120, 120, 120, 160)
    images["change"] = Image.fromarray(mask)
    thumbnails = {}
    for name, image in images.items():
        image.save(directory / f"{name}.png")
        image.thumbnail((256, 256))
        buffer = BytesIO()
        image.save(buffer, format="WEBP", quality=60)
        thumbnails[name] = base64.b64encode(buffer.getvalue()).decode()
    x0, y0, x1, y1 = grid.bounds
    convert = project(grid.crs_epsg, 4326)
    return thumbnails, [
        list(convert(x, y)) for x, y in [(x0, y1), (x1, y1), (x1, y0), (x0, y0)]
    ]
