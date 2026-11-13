"""Real demo cases (pinned scenes) and OFFLINE=1 mode."""

import json
from datetime import date, datetime, timezone
from uuid import uuid4

import numpy as np
import pytest
from fastapi.testclient import TestClient

from satcore import demo, pipeline, stac, store
from satcore.geo import build_grid, place_box
from satcore.models import SceneRef, UplinkCommand

ITEMS = {"A": ["REAL-A-1", "REAL-A-2"], "B": ["REAL-B-1"]}


def no_network(*args, **kwargs):
    raise AssertionError("network access attempted")


@pytest.fixture
def cached_pins(monkeypatch, tmp_path):
    """Pinned scenes present in the local cache (bands + catalog record), network blocked."""
    monkeypatch.setenv("SCENE_STORE", str(tmp_path / "scenes"))
    monkeypatch.setenv("ANALYSES_DIR", str(tmp_path / "analyses"))
    for name in ("select_scene", "pinned_scene", "worldcover", "_search"):
        monkeypatch.setattr(stac, name, no_network)
    monkeypatch.setattr(store, "read_band", no_network)
    aoi = place_box(55.22, 24.93, "Dubai South")
    grid = build_grid(aoi)
    for role, ids in ITEMS.items():
        ref = SceneRef(
            role=role,
            item_ids=sorted(ids),
            datetime=datetime(2024, 3 if role == "A" else 4, 22, tzinfo=timezone.utc),
            requested_date=date(2024, 3, 22),
            days_from_requested=0,
            aoi_cloud_pct=0,
            source="earth-search",
            asset_hrefs={},
        )
        selection = stac.Selection(ref, {})
        bands = demo.bands(grid, "dubai_flood", role)
        file = store.scene_file(selection, grid)
        file.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(file, **bands)
        store._ref_file(sorted(ids), grid).write_text(ref.model_dump_json())
    # Prepared demos also have WorldCover cached (`prep_demo fetch`): bare land here.
    cover = np.full((grid.height, grid.width), 60, np.uint8)
    np.save(store.worldcover_file(grid), cover)
    return aoi


def test_pinned_scenes_run_from_the_cache_without_network(cached_pins):
    cmd = UplinkCommand(
        analysis_id=uuid4(),
        aoi=cached_pins,
        date_a=date(2024, 3, 22),
        date_b=date(2024, 4, 22),
        intent="water_gain",
        item_a=ITEMS["A"],
        item_b=ITEMS["B"],
    )
    verdict = type("V", (), {"write": lambda self, packet: ""})()
    packet = pipeline.run_analysis(cmd, lambda *args: None, verdict)
    assert packet.status == "ok" and packet.stats.changed_ha > 0
    assert [s.item_ids for s in packet.scenes] == [sorted(ITEMS["A"]), ITEMS["B"]]
    assert all(s.source == "earth-search" for s in packet.scenes)
    assert packet.stats.from_to  # land-cover context came from the cache


def test_offline_mode_refuses_live_queries_and_disables_openai(monkeypatch, tmp_path):
    from ground.llm.client import configured
    from ground.main import app

    monkeypatch.setenv("CACHE_DB", str(tmp_path / "cache.db"))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used")
    monkeypatch.setenv("OFFLINE", "1")
    assert configured() is False
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        assert health["offline"] is True and health["openai"]["planner"] is False
        aoi = place_box(55.22, 24.93, "x").model_dump()
        reply = client.post(
            "/api/ask",
            json={
                "aoi": aoi,
                "date_a": "2024-01-01",
                "date_b": "2024-06-01",
                "intent": "water_gain",
            },
        )
        assert reply.status_code == 503 and "Offline" in reply.json()["detail"]
        # Synthetic demos still run offline.
        assert (
            client.post("/api/ask", json={"demo_id": "dubai_flood"}).status_code == 200
        )


def test_cached_result_prefers_the_requested_language(monkeypatch, tmp_path):
    from ground import cache

    monkeypatch.setenv("CACHE_DB", str(tmp_path / "cache.db"))
    plan = type("P", (), {})
    for language in ("ar", "en", "hi"):  # hi is the newest row
        p = plan()
        p.model_dump_json = lambda lang=language: json.dumps({"language": lang})
        cache.save(
            uuid4(),
            "k",
            "",
            p,
            type("X", (), {"model_dump_json": lambda s: "{}"})(),
            type("X", (), {"model_dump_json": lambda s: "{}"})(),
            [],
            "a",
            "local",
        )
    pick = lambda lang: json.loads(cache.cached("k", lang)["plan_json"])["language"]  # noqa: E731
    assert pick("en") == "en" and pick("ar") == "ar"
    assert pick(None) == "hi"  # no preference: newest


def test_pins_change_the_cache_key_but_unpinned_keys_are_stable():
    from ground.cache import cache_key

    base = UplinkCommand(
        analysis_id=uuid4(),
        aoi=place_box(55.22, 24.93, "x"),
        date_a=date(2024, 3, 1),
        date_b=date(2024, 4, 1),
        intent="water_gain",
    )
    pinned = base.model_copy(update={"item_a": ["A"], "item_b": ["B"]})
    assert cache_key(base) != cache_key(pinned)
    assert cache_key(base) == cache_key(
        base.model_copy(update={"analysis_id": uuid4()})
    )


def test_demo_menu_lists_real_cases_first_and_labels_synthetic_ones():
    entries = demo.cases()
    real = [c for c in entries if not c["synthetic"]]
    assert real and entries[0] is real[0]
    assert all(c["item_a"] and c["item_b"] for c in real)
    assert all(c["title"].startswith("Synthetic") for c in entries if c["synthetic"])


def committed_setup(monkeypatch, tmp_path, explanation, version=None):
    from ground import demo_explanations
    from satcore.config import THRESHOLDS

    monkeypatch.setenv("CACHE_DB", str(tmp_path / "cache.db"))
    monkeypatch.setenv("ANALYSES_DIR", str(tmp_path / "analyses"))
    entry = {c["id"]: c for c in demo.cases()}["dubai_flood"]
    cmd = UplinkCommand(
        analysis_id=uuid4(),
        aoi=place_box(entry["lon"], entry["lat"], entry["name"]),
        date_a=date.fromisoformat(entry["date_a"]),
        date_b=date.fromisoformat(entry["date_b"]),
        intent=entry["intent"],
        demo_id="dubai_flood",
    )
    verdict = type("V", (), {"write": lambda self, packet: "v"})()
    packet = pipeline.run_analysis(cmd, lambda *args: None, verdict)
    folder = tmp_path / "explanations"
    path = demo_explanations.export("demo", "key-1", {"en": explanation}, folder)
    if version is not None:
        data = json.loads(path.read_text())
        data["thresholds_version"] = version
        path.write_text(json.dumps(data))
    assert demo_explanations.install(folder) == 1
    return demo_explanations, packet, THRESHOLDS["version"]


def explanation_with(number):
    return {
        "headline": f"Water over {number} ha.",
        "body": "Likely flooding.",
        "caveats": [],
        "numbers_used": [number],
    }


def test_committed_explanation_used_only_for_its_result(monkeypatch, tmp_path):
    ha = packet_ha(monkeypatch, tmp_path)
    module, packet, _ = committed_setup(monkeypatch, tmp_path, explanation_with(ha))
    assert module.lookup("key-1", "en", packet) is not None
    assert module.lookup("key-1", "ar", packet) is None  # no Arabic stored
    assert module.lookup("other-key", "en", packet) is None  # different result


def packet_ha(monkeypatch, tmp_path):
    entry = {c["id"]: c for c in demo.cases()}["dubai_flood"]
    monkeypatch.setenv("ANALYSES_DIR", str(tmp_path / "pre"))
    cmd = UplinkCommand(
        analysis_id=uuid4(),
        aoi=place_box(entry["lon"], entry["lat"], "x"),
        date_a=date.fromisoformat(entry["date_a"]),
        date_b=date.fromisoformat(entry["date_b"]),
        intent=entry["intent"],
        demo_id="dubai_flood",
    )
    verdict = type("V", (), {"write": lambda self, packet: "v"})()
    return pipeline.run_analysis(cmd, lambda *args: None, verdict).stats.changed_ha


def test_stale_version_or_unsupported_numbers_are_never_shown(monkeypatch, tmp_path):
    from satcore.config import THRESHOLDS

    ha = packet_ha(monkeypatch, tmp_path)
    module, packet, _ = committed_setup(
        monkeypatch, tmp_path, explanation_with(ha), version=THRESHOLDS["version"] - 1
    )
    assert module.lookup("key-1", "en", packet) is None  # older thresholds version
    module, packet, _ = committed_setup(
        monkeypatch, tmp_path / "b", explanation_with(9999.5)
    )
    assert module.lookup("key-1", "en", packet) is None  # number not in the packet
