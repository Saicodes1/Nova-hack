"""Question planner that needs no paid service.

It reads the analysis type, the dates and the place name out of a plain
question with keyword and date rules. It is used whenever no OpenAI key is
configured, so the whole product runs on free tools.
"""

import calendar
import re
from datetime import date, timedelta

from .schemas import AnalysisPlan

INTENTS = [
    # A forest that "shrank" is vegetation loss, not water loss.
    (
        "veg_loss",
        r"(?:forest|tree|vegetation|jungle|woodland)\w*\b.*"
        r"(?:shr[iu]nk|smaller|declin|los[ts]|cut|disappear|gone|destroy)",
    ),
    ("burn", r"burn|fire|wildfire|scorch|حريق|حرائق|आग"),
    (
        "water_loss",
        r"dr(?:y|ied|ying)|drought|shr[iu]nk|smaller|reced|water loss|جفاف|सूख",
    ),
    (
        "water_gain",
        r"flood|inundat|storm|rain|water|lake|river|reservoir|فيضان|سيول|مياه|أمطار|बाढ़|पानी|बारिश",
    ),
    (
        "veg_loss",
        r"deforest|logging|landslide|mudslide|clear(?:ed|ing)|forest loss|tree loss|vegetation (?:loss|decline)"
        r"|lost (?:forest|trees|vegetation)|إزالة الغابات|वनों की कटाई|कटाई",
    ),
    (
        "veg_gain",
        r"regrow|reforest|green(?:er|ing)|vegetation (?:gain|grow)|تشجير|हरियाली",
    ),
    (
        "urban_growth",
        r"urban|construct|buil[dt]|develop|city grow|expan|sprawl|housing|بناء|عمران|निर्माण|शहर",
    ),
    ("veg_loss", r"forest|vegetation|tree|crop|غابات|نبات|जंगल|वन"),
    ("urban_growth", r"gr[oe]wn?|bigger|larger"),
]
MONTHS = {
    name.lower(): number
    for number, names in enumerate(zip(calendar.month_name, calendar.month_abbr))
    for name in names
    if name
}
MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DAY_MONTH = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH})\.?,?\s+(\d{{4}})\b", re.I
)
MONTH_DAY = re.compile(
    rf"\b({MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I
)
MONTH_YEAR = re.compile(rf"\b({MONTH})\.?,?\s+(\d{{4}})\b", re.I)
YEAR = re.compile(r"\b(20[12]\d)\b")
PLACE = re.compile(
    r"\b(?:in|near|at|around|over|across|of)\s+((?:[A-Z][\w'’.-]*)(?:[ ,]+[A-Z][\w'’.-]*){0,3})"
)
NAME = re.compile(r"(?<=[a-z,] )([A-Z][\w'’-]+(?: [A-Z][\w'’-]+){0,3})")
STOPWORDS = {"i", "sentinel", "sentinel-2", "did", "was", "is", "the"}
# Words that are never part of a place name in a question about change.
FILLER = set(
    """a about after again all also am an and any are area areas around as at be been
    before between by can change changed changes compare compared comparison could
    damage decrease detect did do does during effect effects for from gain gained
    grew grow growth had happen happened has have how i if impact impacts in
    increase is it its last less like look lose lost many me more much my near
    of on or over past please region see show since so some surface tell than that
    the there this to until up us was we were what when where which with year years
    month months week weeks day days today now recent recently currently still yet
    satellite satellites image images imagery picture pictures map land cover level
    levels due because caused cause into out across there's whats what's vs versus
    and/or amount size bigger smaller larger shrink shrunk expanded expand going
    seen saw seeing experienced experience got get getting become became becoming loss
    happening occurred occur noticed notice visible ago yesterday earlier later
    ever just really actually currently""".split()
)
WORD = re.compile(r"[^\W\d_][\w'’.-]*", re.UNICODE)


def _safe(year, month, day):
    try:
        return date(year, month, min(day, calendar.monthrange(year, month)[1]))
    except ValueError:
        return None


UNITS = {"day": 1, "week": 7, "month": 30, "year": 365}
WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
         "six": 6, "few": 3, "couple": 2}  # fmt: skip
RELATIVE = re.compile(
    r"\b(?:(?:since|in|over|during|for|within)\s+)?(?:the\s+)?(?:last|past|previous)\s+"
    r"(?:(\d{1,3}|a|an|one|two|three|four|five|six|few|couple(?:\s+of)?)\s+)?"
    r"(day|week|month|year)s?\b"
    r"|\b(\d{1,3}|a|an|one|two|three|four|five|six|few)\s+(day|week|month|year)s?\s+ago\b"
    r"|\b(yesterday)\b|\bthis\s+(week|month|year)\b",
    re.I,
)


def _relative(text: str, today: date):
    """Dates for wording such as "since last week" or "in the past 3 months"."""
    match = RELATIVE.search(text)
    if not match:
        return None
    if match[5]:
        days = 1
    else:
        count = match[1] or match[3] or "1"
        unit = (match[2] or match[4] or match[6]).lower()
        number = (
            int(count) if count.isdigit() else WORDS.get(count.split()[0].lower(), 1)
        )
        days = number * UNITS[unit]
    return today - timedelta(days=days), today


def _dates(text: str, today: date):
    """Return (date_a, date_b), either of which may be None."""
    relative = _relative(text, today)
    if relative and not ISO.search(text) and not MONTH_YEAR.search(text):
        return relative
    spans, exact, months = [], [], []

    def taken(match):
        return any(match.start() < end and match.end() > start for start, end in spans)

    for match in ISO.finditer(text):
        exact.append((match.start(), _safe(*map(int, match.groups()))))
        spans.append(match.span())
    for pattern, order in ((DAY_MONTH, (2, 1, 0)), (MONTH_DAY, (2, 0, 1))):
        for match in pattern.finditer(text):
            if taken(match):
                continue
            parts = match.groups()
            year, month, day = (parts[i] for i in order)
            exact.append(
                (match.start(), _safe(int(year), MONTHS[month.lower()], int(day)))
            )
            spans.append(match.span())
    for match in MONTH_YEAR.finditer(text):
        if not taken(match):
            months.append((match.start(), int(match[2]), MONTHS[match[1].lower()]))
            spans.append(match.span())
    years = [(m.start(), int(m[1])) for m in YEAR.finditer(text) if not taken(m)]

    found = sorted(
        [(pos, value, value) for pos, value in exact if value]
        + [
            (pos, date(year, month, 1), _safe(year, month, 31))
            for pos, year, month in months
        ]
        + [
            (
                pos,
                _safe(year, today.month, today.day),
                _safe(year, today.month, today.day),
            )
            for pos, year in years
        ]
    )
    if not found:
        return None, None
    if len(found) == 1:
        _, first, last = found[0]
        if first != last:  # one whole month: compare its start with its end
            return first - timedelta(days=5), last
        if any(pos == found[0][0] for pos, _ in years):  # one bare year
            if re.search(r"\b(?:since|from|after)\b", text, re.I) and first < today:
                return first, today  # "since 2020": that year against today
            return _safe(first.year - 1, first.month, first.day), first
        # one exact day: a few days before against a few days after
        return first - timedelta(days=6), first + timedelta(days=6)
    ordered = sorted(found[:2], key=lambda row: row[1])
    return ordered[0][1], ordered[1][2]


def place_candidates(text: str) -> list[str]:
    """Possible place names in a question, most likely first.

    People type on phones without capitals ("telangana india, any flood ...")
    so this does not rely on them. It first keeps the old capital-letter and
    "in/near ..." rules, then falls back to whatever words are left once dates,
    change words and filler are removed.
    """
    named = [
        match[1].strip(" ,.")
        for match in PLACE.finditer(text)
        if match[1].split()[0].lower().strip(".,") not in MONTHS
    ]
    if not named:
        named = [
            match[1]
            for match in NAME.finditer(text)
            if not all(
                word.lower() in MONTHS or word.lower() in STOPWORDS
                for word in match[1].split()
            )
        ]
    found = list(reversed(named))
    plain = text
    for pattern in (ISO, DAY_MONTH, MONTH_DAY, MONTH_YEAR, YEAR):
        plain = pattern.sub(" | ", plain)
    chunks, current = [], []
    for piece in re.split(r"([,;:?!|()\n]|\s+)", plain):
        token = piece.strip()
        if not token:
            continue
        word = WORD.fullmatch(token)
        lowered = token.lower().strip(".'’")
        skip = (
            not word
            or lowered in FILLER
            or lowered in MONTHS
            or any(
                re.fullmatch(rf"(?:{pattern})\w*", lowered) for _, pattern in INTENTS
            )
        )
        if skip:
            if current:
                chunks.append(" ".join(current))
            current = []
        else:
            current.append(token.strip("."))
    if current:
        chunks.append(" ".join(current))
    chunks = [chunk for chunk in chunks if chunk.lower() not in {"new", "city", "town"}]
    # In order of appearance: "riyadh, saudi arabia" means Riyadh.
    for chunk in chunks:
        words = chunk.split()[:5]
        # "telangana india" may only be known as "telangana"; try shorter forms.
        for option in (
            " ".join(words),
            " ".join(words[:-1]),
            " ".join(words[1:]),
            words[0],
        ):
            if (
                len(option) > 2
                and option.lower() not in {"new", "city", "town", "old"}
                and option.lower() not in map(str.lower, found)
            ):
                found.append(option)
    return found


def plan_locally(request: dict, today: date | None = None) -> AnalysisPlan:
    today = today or date.today()
    text = request.get("question") or ""
    lowered = text.lower()
    intent = request.get("intent") or next(
        (name for name, pattern in INTENTS if re.search(pattern, lowered)), None
    )
    date_a, date_b = _dates(text, today)
    if date_b and date_b > today:
        date_b = today
    places = place_candidates(text)
    if date_a is None and date_b is None:
        # No dates given: compare today with the same season a year ago.
        date_b = today
        date_a = _safe(today.year - 1, today.month, today.day)
    missing = []
    if not intent:
        missing.append(
            "what kind of change to look for (water, vegetation, buildings, fire)"
        )
    if not (request.get("date_a") or date_a) or not (request.get("date_b") or date_b):
        missing.append("the two dates to compare")
    if not request.get("aoi") and not places:
        missing.append("a place name, such as a city or district")
    return AnalysisPlan(
        intent=intent,
        place_name=places[0] if places else None,
        date_a=date_a,
        date_b=date_b,
        language=request.get("language") or "en",
        clarification=("Please add " + "; ".join(missing) + ".") if missing else None,
    )
