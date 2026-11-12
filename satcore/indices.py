import numpy as np


def ratio(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    denominator = a + b
    return np.divide(
        a - b,
        denominator,
        out=np.full(a.shape, np.nan, np.float32),
        where=np.isfinite(denominator) & (denominator != 0),
    )


def calculate(bands: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    pairs = {
        "ndvi": ("nir", "red"),
        "ndwi": ("green", "nir"),
        "mndwi": ("green", "swir16"),
        "ndbi": ("swir16", "nir"),
        "nbr": ("nir", "swir22"),
    }
    return {name: ratio(bands[a], bands[b]) for name, (a, b) in pairs.items()}
