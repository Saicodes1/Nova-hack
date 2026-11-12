import math
import re
import unicodedata

NUMBER = re.compile(r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
FORBIDDEN = re.compile(r"\b(safe|confirmed|guaranteed)\b", re.I)


def normalize(text):
    punctuation = {"٫": ".", "٬": ",", "−": "-"}
    return "".join(
        str(unicodedata.decimal(c)) if c.isdecimal() else punctuation.get(c, c)
        for c in text
    )


def _values(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key not in {"thumbnails", "change_geojson", "asset_hrefs"}:
                yield from _values(child)
    elif isinstance(value, (tuple, list)):
        for child in value:
            yield from _values(child)
    elif type(value) in (int, float) and math.isfinite(value):
        yield value


def check(text, evidence):
    dates = set()
    for scene in evidence.get("scenes", []):
        dates.update([str(scene["datetime"])[:10], str(scene["requested_date"])])
    text = DATE.sub(
        lambda match: " " if match[0] in dates else match[0], normalize(text)
    )
    allowed = {
        round(value, digits) for value in _values(evidence) for digits in (0, 1, 2, 4)
    }
    allowed.update(_values(evidence))
    bad = []
    for match in NUMBER.finditer(text):
        value = float(match[0].replace(",", ""))
        if not any(
            abs(value - source) <= max(0.1, abs(source) * 0.005) for source in allowed
        ):
            bad.append(match[0])
    if FORBIDDEN.search(text):
        bad.append("unsupported certainty")
    return bad
