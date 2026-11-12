from datetime import date

from pydantic import BaseModel

from satcore.models import Intent, Language


class AnalysisPlan(BaseModel):
    intent: Intent | None
    place_name: str | None
    date_a: date | None
    date_b: date | None
    language: Language
    clarification: str | None


class Explanation(BaseModel):
    headline: str
    body: str
    caveats: list[str]
    numbers_used: list[float]
