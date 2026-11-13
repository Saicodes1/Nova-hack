import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from shapely.geometry import mapping, shape

from satcore.config import THRESHOLDS_VERSION, data_path


def cache_key(cmd, detector=None):
    def rounded(value):
        return (
            [rounded(v) for v in value]
            if isinstance(value, (tuple, list))
            else round(value, 5)
        )

    coordinates = rounded(mapping(shape(cmd.aoi.geometry).normalize())["coordinates"])
    identity = [
        coordinates,
        str(cmd.date_a),
        str(cmd.date_b),
        cmd.intent,
        cmd.demo_id,
        cmd.window_days,
        cmd.season_baseline,
        cmd.exact_a,
        cmd.exact_b,
        cmd.report_floor_ha,
        THRESHOLDS_VERSION,
        detector,
    ]
    return hashlib.sha256(
        json.dumps(identity, separators=(",", ":")).encode()
    ).hexdigest()


@contextmanager
def connection():
    path = data_path("CACHE_DB", "var/cache.sqlite")
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS analyses (
                analysis_id TEXT PRIMARY KEY, cache_key TEXT, status TEXT, question TEXT,
                plan_json TEXT, packet_json TEXT, explanation_json TEXT, events_json TEXT,
                created_at TEXT, artifact_id TEXT, artifact_mode TEXT)""")
            db.execute("CREATE INDEX IF NOT EXISTS ix_cache_key ON analyses(cache_key)")
            yield db
    finally:
        db.close()


def find(identifier):
    with connection() as db:
        return db.execute(
            "SELECT * FROM analyses WHERE analysis_id = ?", (str(identifier),)
        ).fetchone()


def cached(key):
    with connection() as db:
        return db.execute(
            """SELECT * FROM analyses WHERE cache_key = ? AND status = 'done'
                             ORDER BY created_at DESC LIMIT 1""",
            (key,),
        ).fetchone()


def save(
    identifier,
    key,
    question,
    plan,
    packet,
    explanation,
    events,
    artifact_id,
    artifact_mode,
):
    with connection() as db:
        db.execute(
            "INSERT INTO analyses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(identifier),
                key,
                "done",
                question,
                plan.model_dump_json(),
                packet.model_dump_json(),
                explanation.model_dump_json(),
                json.dumps(events),
                datetime.now(timezone.utc).isoformat(),
                str(artifact_id),
                artifact_mode,
            ),
        )
