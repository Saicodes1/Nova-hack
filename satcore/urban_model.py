"""Local inference for the RGB Sentinel-2 FresUNet checkpoint published by IPOL."""

import importlib.util
from functools import lru_cache
from pathlib import Path

import numpy as np

from .config import data_path

TILE = 256
HALO = 48
MODEL_NAME = "FresUNet (IPOL RGB checkpoint)"


def model_path() -> Path:
    return data_path("URBAN_MODEL_PATH", "var/models/fresunet.onnx")


def model_identity() -> tuple[int, int] | None:
    path = model_path()
    if path.is_file() and importlib.util.find_spec("onnxruntime") is not None:
        stats = path.stat()
        return stats.st_size, stats.st_mtime_ns
    return None


@lru_cache(maxsize=1)
def session(path: Path, identity: tuple[int, int]):
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    return ort.InferenceSession(
        str(path), sess_options=options, providers=["CPUExecutionProvider"]
    )


def prepare(bands: dict[str, np.ndarray], valid: np.ndarray) -> np.ndarray:
    rgb = np.stack([bands[key] for key in ("red", "green", "blue")]).astype(np.float32)
    clear = valid & np.isfinite(rgb).all(axis=0)
    if not clear.any():
        return np.zeros_like(rgb)
    samples = rgb[:, clear]
    normal = (rgb - samples.mean()) / max(samples.std(), 1e-5)
    return np.where(clear, normal, 0)


def predict(before: dict, after: dict, valid: np.ndarray) -> np.ndarray | None:
    identity = model_identity()
    if identity is None:
        return None
    model = session(model_path(), identity)
    images = (prepare(before, valid), prepare(after, valid))
    height, width = valid.shape
    stride = TILE - 2 * HALO
    padded = [
        np.pad(
            image, ((0, 0), (HALO, HALO + stride), (HALO, HALO + stride)), mode="edge"
        )
        for image in images
    ]
    result = np.empty((height, width), dtype=np.float32)
    for y in range(0, height, stride):
        for x in range(0, width, stride):
            inputs = {
                role: image[None, :, y : y + TILE, x : x + TILE]
                for role, image in zip(("before", "after"), padded, strict=True)
            }
            output = model.run(["log_probability"], inputs)[0]
            h, w = min(stride, height - y), min(stride, width - x)
            result[y : y + h, x : x + w] = np.exp(
                output[0, 1, HALO : HALO + h, HALO : HALO + w]
            )
    return np.where(valid, result, 0)
