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


# Fields whose numbers are not evidence: images, geometry, and the detector's
# configuration (thresholds would otherwise whitelist almost any small number).
SKIPPED = {"thumbnails", "change_geojson", "asset_hrefs", "thresholds", "image_bounds"}


def _values(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key not in SKIPPED:
                yield from _values(child)
    elif isinstance(value, (tuple, list)):
        for child in value:
            yield from _values(child)
    elif type(value) in (int, float) and math.isfinite(value):
        yield value


def _strings(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key not in SKIPPED:
                yield from _strings(child)
    elif isinstance(value, (tuple, list)):
        for child in value:
            yield from _strings(child)
    elif isinstance(value, str):
        yield value


def check(text, evidence):
    dates, calendar = set(), set()
    for scene in evidence.get("scenes", []):
        for stamp in (str(scene["datetime"])[:10], str(scene["requested_date"])):
            dates.add(stamp)
            year, month, day = (int(part) for part in stamp.split("-"))
            calendar.update({year, month, day})  # "8 July 2019", "in 2024"
    text = DATE.sub(
        lambda match: " " if match[0] in dates else match[0], normalize(text)
    )
    allowed = {
        round(value, digits) for value in _values(evidence) for digits in (0, 1, 2, 4)
    }
    allowed.update(_values(evidence))
    allowed.update(calendar)
    # Numbers already written into the packet's own caveats (e.g. "0.5 ha floor").
    for sentence in _strings(evidence):
        allowed.update(
            float(match.replace(",", ""))
            for match in NUMBER.findall(normalize(sentence))
        )
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
