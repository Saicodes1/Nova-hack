"""Standing questions.

A watch asks the satellite the same question on every overpass of an area.
The ground station only schedules the passes. The node decides onboard whether
a pass is worth downlinking: a cloudy pass or a change below the reporting
floor comes back as a short "nothing to report" packet, and only a real change
comes back with imagery and outlines.

A watch over past dates replays the real overpasses one after another. A watch
with no end date catches up to today and then checks for new acquisitions.
"""

import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from satcore import store
from satcore.config import data_path
from satcore.geo import build_grid
from satcore.models import AOI, Intent, Language

logger = logging.getLogger(__name__)
POLL_SECONDS = 1800
BASELINE_CLOUD_PCT = 10
watches: dict[str, dict] = {}
loops: dict[str, asyncio.Task] = {}


class WatchRequest(BaseModel):
    aoi: AOI
    intent: Intent
    question: str = Field("", max_length=2000)
    baseline_date: date
    start_date: date
    end_date: date | None = None
    floor_ha: float = Field(5, ge=0.5, le=5000)
    link_mode: Literal["demo-fast", "sband-2m", "uhf-9k6"] | None = None
    language: Language = "en"


def _file():
    return data_path("WATCH_FILE", "var/watches.json")


def _save():
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(watches.values())), encoding="utf-8")


def restore():
    """Load saved watches. A replay that was cut off is marked stopped."""
    path = _file()
    if not path.is_file():
        return []
    for watch in json.loads(path.read_text(encoding="utf-8")):
        if watch["status"] in {"starting", "running"}:
            watch["status"] = "stopped"
        watch["entries"] = [e for e in watch["entries"] if e["outcome"] != "processing"]
        watches[watch["id"]] = watch
    return [w for w in watches.values() if w["status"] == "armed"]


def summary(watch):
    entries = [e for e in watch["entries"] if e["outcome"] != "processing"]
    sent = sum(e["packet_bytes"] or 0 for e in entries)
    raw = [e["raw_bytes"] for e in entries if e["outcome"] != "no_scene"]
    return dict(
        watch,
        totals={
            "passes": len(entries),
            "alerts": sum(e["outcome"] == "alert" for e in entries),
            "quiet": sum(e["outcome"] == "quiet" for e in entries),
            "cloudy": sum(e["outcome"] == "cloudy" for e in entries),
            "sent_bytes": sent,
            "raw_bytes": sum(raw) if raw and all(r is not None for r in raw) else None,
        },
    )


def create(request: WatchRequest):
    build_grid(request.aoi)
    if request.baseline_date >= request.start_date:
        raise ValueError("The reference date must be earlier than the first pass.")
    if request.end_date and request.end_date < request.start_date:
        raise ValueError("The watch must end after it starts.")
    watch = dict(
        request.model_dump(mode="json"),
        id=uuid4().hex[:12],
        status="starting",
        message="",
        baseline=None,
        entries=[],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    watches[watch["id"]] = watch
    _save()
    return watch


def stop(identifier):
    watch = watches.get(identifier)
    if watch is None:
        return None
    task = loops.pop(identifier, None)
    if task:
        task.cancel()
    if watch["status"] in {"starting", "running", "armed"}:
        watch["status"] = "stopped"
    watch["entries"] = [e for e in watch["entries"] if e["outcome"] != "processing"]
    _save()
    return watch


def remove(identifier):
    stop(identifier)
    found = watches.pop(identifier, None)
    _save()
    return found


def _outcome(packet):
    if packet["status"] == "refused_cloudy":
        return "cloudy"
    if packet["status"] == "no_scenes":
        return "no_scene"
    return "alert" if packet["report"] == "full" else "quiet"


async def run(watch, analyse):
    """`analyse(watch, baseline_day, pass_day)` returns (analysis_id, packet dict)."""
    identifier = watch["id"]
    try:
        aoi = AOI.model_validate(watch["aoi"])
        grid = build_grid(aoi)
        if watch["baseline"] is None:
            wanted = date.fromisoformat(watch["baseline_date"])
            before = await asyncio.to_thread(
                store.passes, aoi, grid, wanted - timedelta(days=45), wanted
            )
            clear = [p for p in before if p["cloud_pct"] < BASELINE_CLOUD_PCT]
            if not clear:
                watch.update(
                    status="failed",
                    message="No clear reference image in the 45 days before "
                    f"{wanted}. Choose another reference date.",
                )
                return
            watch["baseline"] = clear[-1]
        baseline = date.fromisoformat(watch["baseline"]["date"])
        watch["status"] = "running"
        _save()
        while True:
            seen = {entry["date"] for entry in watch["entries"]}
            cursor = max(
                [date.fromisoformat(watch["start_date"]), baseline + timedelta(days=1)]
                + [date.fromisoformat(day) + timedelta(days=1) for day in seen]
            )
            final = date.fromisoformat(watch["end_date"]) if watch["end_date"] else None
            until = final or date.today()
            due = (
                await asyncio.to_thread(store.passes, aoi, grid, cursor, until)
                if cursor <= until
                else []
            )
            for overpass in due:
                entry = dict(
                    date=overpass["date"],
                    time_utc=overpass["time_utc"],
                    platform=overpass["platform"],
                    outcome="processing",
                    analysis_id=None,
                    verdict="",
                    changed_ha=None,
                    clear_pct=None,
                    trust_level=None,
                    packet_bytes=None,
                    raw_bytes=None,
                )
                watch["entries"].append(entry)
                watch["status"] = "running"
                try:
                    analysis_id, packet = await analyse(
                        watch, baseline, date.fromisoformat(overpass["date"])
                    )
                    stats = packet["stats"] or {}
                    entry.update(
                        outcome=_outcome(packet),
                        analysis_id=analysis_id,
                        verdict=packet["onboard_verdict"],
                        changed_ha=stats.get("changed_ha"),
                        clear_pct=stats.get("valid_overlap_pct"),
                        trust_level=packet["trust"]["level"],
                        packet_bytes=packet["link"]["packet_bytes"],
                        raw_bytes=packet.get("raw_latest_bytes"),
                    )
                except Exception as exc:
                    logger.exception("Watch %s failed on %s", identifier, overpass)
                    entry.update(outcome="error", verdict=str(exc)[:200])
                _save()
                await asyncio.sleep(1.2)
            if final:
                watch["status"] = "done"
                return
            watch["status"] = "armed"
            _save()
            await asyncio.sleep(POLL_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Watch %s stopped", identifier)
        watch.update(status="failed", message=str(exc)[:300])
    finally:
        watch["entries"] = [e for e in watch["entries"] if e["outcome"] != "processing"]
        loops.pop(identifier, None)
        _save()
