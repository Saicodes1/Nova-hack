"""Standing watch: quiet reports from the node and the pass-by-pass loop."""

import asyncio
from datetime import date
from uuid import uuid4

from ground import watch
from node.verdict import TemplateVerdict
from satcore.demo import cases
from satcore.models import AOI, UplinkCommand
from satcore.packet import packet_bytes
from satcore.pipeline import run_analysis


def command(**extra):
    case = next(item for item in cases() if item["id"] == "dubai_flood")
    return UplinkCommand(
        analysis_id=uuid4(),
        aoi=AOI.model_validate(case["aoi"]),
        date_a=case["date_a"],
        date_b=case["date_b"],
        intent=case["intent"],
        demo_id=case["id"],
        **extra,
    )


def run(cmd):
    return run_analysis(cmd, lambda *_: None, TemplateVerdict())


def test_change_above_the_floor_is_reported_in_full(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYSES_DIR", str(tmp_path))
    packet = run(command(report_floor_ha=1))
    assert packet.report == "full"
    assert packet.status == "ok" and packet.thumbnails and packet.change_geojson


def test_change_below_the_floor_sends_a_short_packet(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYSES_DIR", str(tmp_path))
    full = run(command())
    quiet = run(command(report_floor_ha=full.stats.changed_ha + 1))
    assert quiet.report == "quiet"
    assert not quiet.thumbnails and quiet.change_geojson is None
    assert "Nothing to report" in quiet.onboard_verdict
    assert packet_bytes(quiet) < 3000 < packet_bytes(full)


def test_watch_walks_every_pass_and_counts_what_was_sent(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_FILE", str(tmp_path / "watches.json"))
    days = {
        "2024-03-28": 0.0,
        "2024-04-02": 100.0,
        "2024-04-12": 5.0,
        "2024-04-22": 0.0,
    }

    def passes(_aoi, _grid, start, end):
        return [
            {"date": day, "time_utc": f"{day}T07:00:00+00:00", "platform": "s2",
             "cloud_pct": cloud, "source": "test"}
            for day, cloud in days.items()
            if start <= date.fromisoformat(day) <= end
        ]  # fmt: skip

    async def analyse(_item, baseline, day):
        assert baseline == date(2024, 3, 28)
        outcome = {
            "2024-04-02": ("refused_cloudy", "quiet", None),
            "2024-04-12": ("ok", "quiet", 2.0),
            "2024-04-22": ("ok", "full", 240.0),
        }[str(day)]
        return "id-" + str(day), {
            "status": outcome[0],
            "report": outcome[1],
            "stats": outcome[2] and {"changed_ha": outcome[2], "valid_overlap_pct": 99},
            "onboard_verdict": "text",
            "trust": {"level": "High"},
            "link": {"packet_bytes": 90_000 if outcome[1] == "full" else 1_800},
            "raw_latest_bytes": 1_500_000_000,
        }

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(watch.store, "passes", passes)
    monkeypatch.setattr(watch.asyncio, "sleep", no_wait)
    watch.watches.clear()
    case = next(item for item in cases() if item["id"] == "dubai_flood")
    item = watch.create(
        watch.WatchRequest(
            aoi=AOI.model_validate(case["aoi"]),
            intent="water_gain",
            baseline_date="2024-03-30",
            start_date="2024-04-01",
            end_date="2024-04-30",
            floor_ha=10,
        )
    )
    asyncio.run(watch.run(item, analyse))
    result = watch.summary(item)
    assert result["status"] == "done"
    assert result["baseline"]["date"] == "2024-03-28"
    assert [e["outcome"] for e in result["entries"]] == ["cloudy", "quiet", "alert"]
    assert result["totals"] == {
        "passes": 3, "alerts": 1, "quiet": 1, "cloudy": 1,
        "sent_bytes": 93_600, "raw_bytes": 4_500_000_000,
    }  # fmt: skip
    watch.watches.clear()
