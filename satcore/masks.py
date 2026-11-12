import numpy as np
from scipy.ndimage import binary_dilation


def valid_mask(scl: np.ndarray) -> np.ndarray:
    return np.isin(scl, [2, 4, 5, 6, 7])


def aoi_cloud_pct(scl: np.ndarray, inside: np.ndarray) -> float:
    return round(
        100
        * np.count_nonzero(inside & np.isin(scl, [3, 8, 9, 10]))
        / max(1, inside.sum()),
        1,
    )


def cloud_edge(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return binary_dilation(~(valid_mask(a) & valid_mask(b)), iterations=2)
