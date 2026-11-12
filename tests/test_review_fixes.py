from types import SimpleNamespace

from satcore.stac import _assets


def item(baseline, offset=None):
    raster = (
        {"scale": 0.0001} if offset is None else {"scale": 0.0001, "offset": offset}
    )
    assets = {
        name: SimpleNamespace(
            href=f"https://example.test/{name}.tif",
            extra_fields={"raster:bands": [raster]},
        )
        for name in ("B02", "B03", "B04", "B08", "B11", "B12", "SCL")
    }
    return SimpleNamespace(
        id="x", assets=assets, properties={"s2:processing_baseline": baseline}
    )


def offsets(items, monkeypatch):
    monkeypatch.setattr("satcore.stac.planetary_computer.sign", lambda href: href)
    return {key: assets[0].offset for key, assets in _assets(items, True).items()}


def test_backup_catalog_applies_the_2022_reflectance_offset(monkeypatch):
    new = offsets([item("05.10")], monkeypatch)
    assert new["red"] == -0.1 and new["scl"] == 0.0
    assert offsets([item("02.12")], monkeypatch)["red"] == 0.0


def test_stated_offset_is_kept(monkeypatch):
    assert offsets([item("05.10", offset=-0.1)], monkeypatch)["nir"] == -0.1
    assert offsets([item("05.10", offset=0)], monkeypatch)["nir"] == 0


def test_flight_node_sends_keep_alive_during_slow_stages(monkeypatch):
    import asyncio
    from uuid import uuid4

    from fastapi.testclient import TestClient

    from node import flight_app
    from node.protocol import NodeEvent

    class SlowNode:
        async def uplink(self, cmd):
            await asyncio.sleep(0.25)
            yield NodeEvent(analysis_id=cmd.analysis_id, stage="load", status="done")

    monkeypatch.setattr(flight_app, "node", SlowNode())
    monkeypatch.setattr(flight_app, "KEEP_ALIVE_S", 0.05)
    command = {
        "analysis_id": str(uuid4()),
        "aoi": {
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [55.2, 25.0],
                        [55.21, 25.0],
                        [55.21, 25.01],
                        [55.2, 25.01],
                        [55.2, 25.0],
                    ]
                ],
            }
        },
        "date_a": "2024-01-01",
        "date_b": "2024-06-01",
        "intent": "water_gain",
    }
    with TestClient(flight_app.app) as client:
        body = client.post("/uplink", json=command).text
    assert ": keep-alive" in body
    assert "event: load" in body


def test_guard_accepts_dates_and_rejects_threshold_numbers():
    from ground.llm.guard import check

    evidence = {
        "scenes": [
            {"datetime": "2019-07-08T06:54:40Z", "requested_date": "2019-07-10"},
            {"datetime": "2026-11-08T07:01:05Z", "requested_date": "2026-11-18"},
        ],
        "stats": {"changed_ha": 154.3, "changed_pct": 12.6},
        "trust": {
            "caveats": ["No change above the 0.5 ha detection floor."],
            "thresholds": {"seed_built": 1.6, "window_px": 11},
        },
    }
    assert (
        check(
            "Built-up growth of 154.3 ha between July 2019 and 8 November 2026.",
            evidence,
        )
        == []
    )
    assert check("Patches under 0.5 ha are ignored.", evidence) == []
    assert check("Growth covered 1.6 ha.", evidence) == ["1.6"]
