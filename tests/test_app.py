import json
from base64 import b64decode
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from ground.llm.guard import check
from ground.main import app
from satcore.geo import build_grid, place_box, utm_epsg, validate_aoi


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("CACHE_DB", str(tmp_path / "cache.db"))
    monkeypatch.setenv("ANALYSES_DIR", str(tmp_path / "analyses"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with TestClient(app) as test:
        yield test


def complete(client, demo_id, **fields):
    response = client.post("/api/ask", json={"demo_id": demo_id, **fields})
    assert response.status_code == 200
    identifier = response.json()["analysis_id"]
    stream = client.get(f"/api/analyses/{identifier}/events")
    events = [
        json.loads(row[6:])
        for row in stream.text.splitlines()
        if row.startswith("data: ")
    ]
    assert events[-1]["stage"] == "done"
    return identifier, events, client.get(f"/api/analyses/{identifier}").json()


def test_demo_and_replay(client):
    identifier, events, result = complete(client, "dubai_flood")
    packet = result["packet"]
    assert packet["status"] == "ok"
    assert packet["stats"]["changed_ha"] > 0
    assert all(scene["source"] == "synthetic" for scene in packet["scenes"])
    assert [event["stage"] for event in events if event["status"] == "done"] == [
        "uplink",
        "scene_select_a",
        "scene_select_b",
        "load",
        "cloud_check",
        "indices",
        "change",
        "landcover",
        "trust",
        "render",
        "verdict",
        "packet",
        "downlink",
        "explain",
        "done",
    ]
    assert Image.open(BytesIO(b64decode(packet["thumbnails"]["a"]))).format == "WEBP"
    picture = client.get(f"/api/analyses/{identifier}/images/a")
    assert picture.status_code == 200
    replay_id, replay_events, replay = complete(
        client, "dubai_flood", language="ar", link_mode="sband-2m"
    )
    assert replay_id != identifier
    assert replay["packet"]["stats"] == packet["stats"]
    assert replay["packet"]["link"]["mode"] == "sband-2m"
    assert replay["plan"]["language"] == "ar"
    assert any(row["stage"] == "change" and row["replayed"] for row in replay_events)
    assert client.get(f"/api/analyses/{replay_id}/images/a").content == picture.content


def test_cloud_refusal(client):
    _, events, result = complete(client, "dubai_cloudy")
    packet = result["packet"]
    assert packet["status"] == "refused_cloudy"
    assert packet["stats"] is None
    assert packet["trust"]["level"] == "Low"
    assert any(event["status"] == "refused" for event in events)
    assert not any(event["stage"] == "change" for event in events)


@pytest.mark.parametrize("case", ["rondonia", "dubai_south"])
def test_other_demo_cases(client, case):
    _, _, result = complete(client, case)
    assert result["packet"]["stats"]["changed_ha"] > 0


def test_validation_and_no_key_planner(client):
    assert (
        "clarification"
        in client.post("/api/ask", json={"question": "Was there flooding?"}).json()
    )
    aoi = place_box(55.22, 24.93, "Dubai")
    assert 24 < validate_aoi(aoi) < 26
    assert build_grid(aoi).crs_epsg == 32640
    assert utm_epsg(-63.95, -9.28) == 32720
    response = client.post(
        "/api/ask",
        json={
            "aoi": aoi.model_dump(),
            "date_a": "2024-05-01",
            "date_b": "2024-04-01",
            "intent": "water_gain",
        },
    )
    assert response.status_code == 422


def test_number_guard():
    evidence = {
        "stats": {"changed_ha": 1234.5, "changed_pct": 8.1},
        "scenes": [
            {"datetime": "2024-04-21T07:18:00Z", "requested_date": "2024-04-21"}
        ],
    }
    assert not check("2024-04-21: ١٬٢٣٤٫٥ ha and ८.१%", evidence)
    assert check("99999 ha", evidence) == ["99999"]
    assert check("guaranteed", evidence) == ["unsupported certainty"]
