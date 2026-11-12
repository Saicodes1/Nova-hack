import numpy as np

from .models import FromTo

CLASSES = {
    10: "Tree cover",
    20: "Shrubland",
    30: "Grassland",
    40: "Cropland",
    50: "Built-up",
    60: "Bare or sparse vegetation",
    70: "Snow and ice",
    80: "Permanent water",
    90: "Herbaceous wetland",
    95: "Mangroves",
    100: "Moss and lichen",
}


def breakdown(mask, land, pixel_ha):
    if land is None:
        return []
    classes, counts = np.unique(land[mask], return_counts=True)
    rows = [
        FromTo(from_class=CLASSES[code], ha=round(count * pixel_ha, 2))
        for code, count in zip(classes, counts, strict=True)
        if code in CLASSES and count * pixel_ha >= 0.1
    ]
    return sorted(rows, key=lambda row: row.ha, reverse=True)
