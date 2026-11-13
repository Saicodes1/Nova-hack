"""select_scene falls back to Planetary Computer when Earth Search has no usable scene."""

from datetime import date, datetime, timezone

import pytest

from satcore import stac
from satcore.models import SceneRef

TARGET = date(2022, 7, 4)


def selection(backup: bool, cloud: float = 0.0) -> stac.Selection:
    source = "planetary-computer" if backup else "earth-search"
    scene = SceneRef(
        role="B",
        item_ids=[f"{source}-item"],
        datetime=datetime(2022, 7, 4, tzinfo=timezone.utc),
        requested_date=TARGET,
        days_from_requested=0,
        aoi_cloud_pct=cloud,
        source=source,
        asset_hrefs={},
    )
    return stac.Selection(scene, {})


def fake_catalogs(monkeypatch, results):
    """results[(window_days, backup)] -> Selection | None | Exception. Returns call log."""
    calls = []

    def search(aoi, grid, target, role, window, backup, after):
        calls.append((window, backup))
        outcome = results.get((window, backup))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(stac, "_search", search)
    return calls


def select():
    return stac.select_scene(None, None, TARGET, "B", 15)


def test_primary_empty_uses_backup(monkeypatch):
    calls = fake_catalogs(monkeypatch, {(15, False): None, (15, True): selection(True)})
    found = select()
    assert found.scene.source == "planetary-computer"
    assert calls == [(15, False), (15, True)]


def test_primary_error_uses_backup(monkeypatch):
    calls = fake_catalogs(
        monkeypatch, {(15, False): TimeoutError("down"), (15, True): selection(True)}
    )
    assert select().scene.source == "planetary-computer"
    assert calls == [(15, False), (15, True)]


def test_both_empty_returns_none(monkeypatch):
    calls = fake_catalogs(monkeypatch, {})
    assert select() is None
    assert calls == [(15, False), (15, True), (30, False), (30, True)]


def test_backup_is_tried_before_widening(monkeypatch):
    calls = fake_catalogs(
        monkeypatch,
        {
            (15, False): selection(False, cloud=80),  # too cloudy to use
            (15, True): selection(True, cloud=5),
            (30, False): selection(False, cloud=0),
        },
    )
    assert select().scene.source == "planetary-computer"
    assert calls == [(15, False), (15, True)]


def test_widens_on_both_catalogs_and_keeps_least_cloudy(monkeypatch):
    calls = fake_catalogs(
        monkeypatch,
        {
            (15, False): selection(False, cloud=90),
            (15, True): selection(True, cloud=70),
            (30, False): selection(False, cloud=65),
        },
    )
    found = select()
    assert found.scene.source == "earth-search" and found.scene.aoi_cloud_pct == 65
    assert calls == [(15, False), (15, True), (30, False), (30, True)]


def test_failed_catalog_is_not_retried_and_both_failing_raises(monkeypatch):
    calls = fake_catalogs(
        monkeypatch, {(15, False): TimeoutError("a"), (15, True): ConnectionError("b")}
    )
    with pytest.raises(RuntimeError, match="Both Sentinel-2 catalogs"):
        select()
    assert calls == [(15, False), (15, True)]


def item(item_id, tile, generated, **mgrs):
    from types import SimpleNamespace

    props = {"s2:generation_time": generated}
    props.update(
        {"s2:mgrs_tile": tile} if tile else {f"mgrs:{k}": v for k, v in mgrs.items()}
    )
    return SimpleNamespace(id=item_id, properties=props)


def test_duplicate_tiles_keep_the_newest_processing():
    items = [
        item("SPA-2022", "11SPA", "2022-07-20T05:23:50Z"),
        item("SPA-2024", "11SPA", "2024-07-17T23:03:28Z"),
        item("SPV-2022", "11SPV", "2022-07-20T05:19:56Z"),
        item("SPV-2024", "11SPV", "2024-07-17T23:03:28Z"),
    ]
    kept = sorted(i.id for i in stac.newest_per_tile(items))
    assert kept == ["SPA-2024", "SPV-2024"]


def test_tile_id_reads_earth_search_mgrs_fields():
    es = item(
        "S2A_T11SLT_20250102T183754_L2A",
        None,
        "2025-01-02T22:16:46Z",
        utm_zone=11,
        latitude_band="S",
        grid_square="LT",
    )
    assert stac.tile_id(es) == "11SLT"
    distinct = [
        es,
        item(
            "other",
            None,
            "2025-01-02T22:16:46Z",
            utm_zone=11,
            latitude_band="S",
            grid_square="LU",
        ),
    ]
    assert len(stac.newest_per_tile(distinct)) == 2
