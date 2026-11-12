import numpy as np
from scipy.ndimage import binary_dilation

# Sentinel-2 L2A scene classification (SCL) codes.
CLEAR = (2, 4, 5, 6, 7)  # dark/shadow, vegetation, bare, water, unclassified
CLOUDY = (3, 8, 9, 10)  # cloud shadow, cloud medium, cloud high, cirrus
DARK = 2  # dark area pixels / cast shadows: easily mistaken for water


def valid_mask(scl: np.ndarray, *, exclude_dark: bool = False) -> np.ndarray:
    classes = [c for c in CLEAR if not (exclude_dark and c == DARK)]
    return np.isin(scl, classes)


def aoi_cloud_pct(scl: np.ndarray, inside: np.ndarray) -> float:
    return round(
        100 * np.count_nonzero(inside & np.isin(scl, CLOUDY)) / max(1, inside.sum()),
        1,
    )


def near_clouds(a: np.ndarray, b: np.ndarray, px: int) -> np.ndarray:
    """Pixels within `px` pixels of cloud or cloud shadow in either date.

    The SCL cloud and shadow classes usually miss the soft outer fringe of a
    cloud, so change detected right next to them is mostly false. These pixels
    are excluded from detection; they still count as clear coverage.
    """
    cloudy = np.isin(a, CLOUDY) | np.isin(b, CLOUDY)
    if px <= 0:
        return cloudy
    return binary_dilation(cloudy, iterations=px)


def cloud_edge(a: np.ndarray, b: np.ndarray, px: int = 2) -> np.ndarray:
    """Zone around any unusable pixel; patches mostly inside it are flagged."""
    return binary_dilation(~(valid_mask(a) & valid_mask(b)), iterations=max(1, px))
