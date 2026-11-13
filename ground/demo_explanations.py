"""Committed explanations for the real demo cases (config/demo_explanations/*.json).

Each file holds one demo's explanations in en/ar/hi, written for one specific result: the
result cache key and the thresholds version they were generated from. They are loaded into
the SQLite cache at startup, so demos show model-written explanations offline and without an
API key. An explanation is only used when its key and thresholds version match the current
result and its numbers pass the guard against the current packet; otherwise the normal path
runs (the packet template when offline), so a stale explanation is never shown.
"""

import json
from datetime import datetime, timezone

from satcore.config import ROOT, THRESHOLDS

from . import cache
from .llm.explainer import evidence
from .llm.guard import check
from .llm.schemas import Explanation

FOLDER = ROOT / "config/demo_explanations"


def _table(db):
    db.execute(
        """CREATE TABLE IF NOT EXISTS demo_explanations (
            cache_key TEXT, language TEXT, thresholds_version INTEGER, demo_id TEXT,
            explanation_json TEXT, PRIMARY KEY (cache_key, language))"""
    )


def install(folder=FOLDER) -> int:
    """Load every committed explanation file into the cache; returns rows loaded."""
    rows = []
    for file in sorted(folder.glob("*.json")):
        data = json.loads(file.read_text(encoding="utf-8"))
        for language, explanation in data["explanations"].items():
            rows.append(
                (
                    data["cache_key"],
                    language,
                    int(data["thresholds_version"]),
                    data["demo_id"],
                    json.dumps(explanation, ensure_ascii=False),
                )
            )
    with cache.connection() as db:
        _table(db)
        db.executemany(
            "INSERT OR REPLACE INTO demo_explanations VALUES (?, ?, ?, ?, ?)", rows
        )
    return len(rows)


def lookup(key: str, language: str, packet) -> Explanation | None:
    """The committed explanation for this exact result, or None if stale or unsupported."""
    with cache.connection() as db:
        _table(db)
        row = db.execute(
            "SELECT * FROM demo_explanations WHERE cache_key = ? AND language = ?",
            (key, language),
        ).fetchone()
    if row is None or row["thresholds_version"] != THRESHOLDS["version"]:
        return None
    explanation = Explanation.model_validate_json(row["explanation_json"])
    if problems(explanation, packet):
        return None
    return explanation


def problems(explanation: Explanation, packet) -> list[str]:
    """Numbers or wording the guard rejects for this packet (empty = safe to show)."""
    text = " ".join(
        [
            explanation.headline,
            explanation.body,
            *explanation.caveats,
            *map(str, explanation.numbers_used),
        ]
    )
    return check(text, evidence(packet))


def export(demo_id: str, key: str, explanations: dict[str, dict], folder=FOLDER):
    """Write one demo's explanations (language -> Explanation dict) to its JSON file."""
    folder.mkdir(parents=True, exist_ok=True)
    data = {
        "demo_id": demo_id,
        "thresholds_version": THRESHOLDS["version"],
        "cache_key": key,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "explanations": explanations,
    }
    path = folder / f"{demo_id}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", "utf-8")
    return path
