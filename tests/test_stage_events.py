"""Freeze run_analysis's progress events: stage names, order, status and payloads.

The web UI keys its progress list and map preview on these events, so a refactor
of the pipeline must not change them. Timings (`ms`) are excluded because they vary.
Regenerate the snapshot only for an intended change:
    python -m tests.test_stage_events
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import UUID

from satcore import demo, pipeline, stac, store
from satcore.geo import place_box
from satcore.models import SceneRef, UplinkCommand

SNAPSHOT = Path(__file__).with_name("snapshots") / "stage_events.json"
RUN_ID = UUID("00000000-0000-0000-0000-000000000001")


class Verdict:
    def write(self, packet):
        return "verdict"


def offline_live(monkeypatch):
    """Run the non-demo path on synthetic bands with no network."""

    def select(aoi, grid, target, role, window, after=None):
        moment = datetime.combine(target, datetime.min.time(), timezone.utc)
        ref = SceneRef(
            role=role,
            item_ids=[f"LIVE-{role}"],
            datetime=moment,
            requested_date=target,
            days_from_requested=0,
            aoi_cloud_pct=0,
            source="earth-search",
            asset_hrefs={},
        )
        return stac.Selection(ref, {})

    monkeypatch.setattr(stac, "select_scene", select)
    monkeypatch.setattr(stac, "worldcover", lambda aoi, grid: None)
    monkeypatch.setattr(
        store,
        "load_scene",
        lambda selection, grid: demo.bands(grid, "dubai_south", selection.scene.role),
    )


def record(case, monkeypatch, tmp_path):
    monkeypatch.setenv("ANALYSES_DIR", str(tmp_path))
    entry = {c["id"]: c for c in demo.cases()}[case["demo"]]
    cmd = UplinkCommand(
        analysis_id=RUN_ID,
        aoi=place_box(entry["lon"], entry["lat"], entry["name"]),
        date_a=date.fromisoformat(entry["date_a"]),
        date_b=date.fromisoformat(entry["date_b"]),
        intent=case.get("intent", entry["intent"]),
        demo_id=None if case.get("live") else entry["id"],
    )
    events = []

    def emit(stage, status, detail):
        events.append(
            {
                "stage": stage,
                "status": status,
                "detail": {k: v for k, v in detail.items() if k != "ms"},
            }
        )

    pipeline.run_analysis(cmd, emit, Verdict())
    # Round-trip through JSON so tuples and lists compare equal.
    return json.loads(json.dumps(events))


CASES = {
    "demo_ok": {"demo": "dubai_flood"},
    "demo_refused": {"demo": "dubai_cloudy"},
    "live_urban": {"demo": "dubai_south", "live": True},
}


def test_stage_events_match_snapshot(monkeypatch, tmp_path):
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    offline_live(monkeypatch)
    for name, case in CASES.items():
        assert record(case, monkeypatch, tmp_path / name) == expected[name], name


if __name__ == "__main__":
    import tempfile

    import pytest

    with pytest.MonkeyPatch.context() as patch, tempfile.TemporaryDirectory() as tmp:
        offline_live(patch)
        result = {
            name: record(case, patch, Path(tmp) / name) for name, case in CASES.items()
        }
    SNAPSHOT.parent.mkdir(exist_ok=True)
    SNAPSHOT.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print("wrote", SNAPSHOT)
