from datetime import date

import pytest

from ground.llm.local_planner import place_candidates, plan_locally
from node.hardware import Hardware
from node.verdict import OllamaVerdict, TemplateVerdict, make_verdict
from satcore.stac import Catalog, _offset

TODAY = date(2026, 11, 12)


@pytest.mark.parametrize(
    "question, intent, place, date_a, date_b",
    [
        ("Did the April 2024 storm flood Dubai?", "water_gain", "Dubai", "2024-03-27", "2024-04-30"),
        (
            "Where did forest disappear in Porto Velho between 2019 and 2024?",
            "veg_loss", "Porto Velho", "2019-11-12", "2024-11-12",
        ),
        (
            "How much did Dubai South build up from March 2017 to March 2025?",
            "urban_growth", "Dubai South", "2017-03-01", "2025-03-31",
        ),
        (
            "Was there flooding near Jebel Ali after 16 April 2024?",
            "water_gain", "Jebel Ali", "2024-04-10", "2024-04-22",
        ),
        (
            "هل حدث فيضان in Sharjah between 2024-04-12 and 2024-04-22",
            "water_gain", "Sharjah", "2024-04-12", "2024-04-22",
        ),
    ],
)  # fmt: skip
def test_local_planner(question, intent, place, date_a, date_b):
    plan = plan_locally({"question": question}, TODAY)
    assert plan.clarification is None
    assert (plan.intent, plan.place_name) == (intent, place)
    assert (str(plan.date_a), str(plan.date_b)) == (date_a, date_b)


def test_local_planner_asks_when_unsure():
    plan = plan_locally({"question": "What happened?"}, TODAY)
    assert plan.clarification and "place" in plan.clarification


def test_local_planner_reads_phone_style_questions():
    # No capitals, place first, short month names.
    plan = plan_locally(
        {"question": "telangana india, \nany flood effects from jan 2024 to jan 2025"},
        TODAY,
    )
    assert plan.clarification is None and plan.intent == "water_gain"
    assert (str(plan.date_a), str(plan.date_b)) == ("2024-01-01", "2025-01-31")
    assert place_candidates("telangana india, any flood effects")[:2] == [
        "telangana india",
        "telangana",
    ]
    # No dates: today against the same day last year.
    plan = plan_locally({"question": "has the forest near manaus shrunk?"}, TODAY)
    assert (plan.intent, plan.place_name) == ("veg_loss", "manaus")
    assert (str(plan.date_a), str(plan.date_b)) == ("2025-11-12", "2026-11-12")
    assert place_candidates("how much has new cairo grown since 2018")[0] == "new cairo"


class Item:
    def __init__(self, **properties):
        self.properties = properties


def test_reflectance_offset_is_applied_once():
    c1 = Catalog("earth-search", "u", "sentinel-2-c1-l2a")
    pc = Catalog("planetary-computer", "u", "sentinel-2-l2a", band_ids=True)
    assert _offset(Item(), {"offset": -0.1}, c1) == -0.1
    harmonised = Item(**{"earthsearch:boa_offset_applied": True})
    assert _offset(harmonised, {"offset": -0.1}, c1) == 0
    assert _offset(Item(**{"s2:processing_baseline": "05.10"}), {}, pc) == -0.1
    assert _offset(Item(**{"s2:processing_baseline": "02.12"}), {}, pc) == 0


def test_tegrastats_line():
    hardware = Hardware()
    hardware.parse(
        "11-12-2026 10:00:00 RAM 3012/7620MB CPU [3%@1510] cpu@48.5C soc2@47C "
        "VDD_IN 6234mW/5990mW VDD_CPU_GPU_CV 1203mW/1100mW"
    )
    assert (hardware.power_w, hardware.temp_c) == (6.23, 48.5)


def packet():
    from fastapi.testclient import TestClient

    from ground.main import app

    with TestClient(app) as client:
        identifier = client.post("/api/ask", json={"demo_id": "dubai_flood"}).json()[
            "analysis_id"
        ]
        client.get(f"/api/analyses/{identifier}/events")
        from satcore.models import AnswerPacket

        return AnswerPacket.model_validate(
            client.get(f"/api/analyses/{identifier}").json()["packet"]
        )


def test_onboard_model_verdict_is_number_checked(monkeypatch, tmp_path):
    monkeypatch.setenv("CACHE_DB", str(tmp_path / "cache.db"))
    monkeypatch.setenv("ANALYSES_DIR", str(tmp_path / "analyses"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = packet()
    plain = TemplateVerdict().write(result)
    writer = OllamaVerdict("http://ollama.invalid", "llama3.2:3b")

    class Reply:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

        def json(self):
            return {"response": self.text}

    good = f"New surface water detected over {result.stats.changed_ha} hectares, high confidence."
    monkeypatch.setattr("node.verdict.httpx.post", lambda *a, **k: Reply(good))
    assert writer.write(result) == good
    monkeypatch.setattr(
        "node.verdict.httpx.post",
        lambda *a, **k: Reply("Flooding confirmed over 9999 ha."),
    )
    assert writer.write(result) == plain

    monkeypatch.setenv("VERDICT_MODE", "ollama")
    assert make_verdict().name == "ollama:llama3.2:3b"


def test_question_only_runs_without_a_key(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import ground.main as main

    monkeypatch.setenv("CACHE_DB", str(tmp_path / "cache.db"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    started = {}

    async def places(_query):
        return [{"name": "Sharjah", "country": "UAE", "lat": 25.35, "lon": 55.42}]

    monkeypatch.setattr(main.geocode, "search", places)
    monkeypatch.setattr(
        main,
        "start",
        lambda aoi, plan, request: (
            started.update(aoi=aoi, plan=plan) or {"analysis_id": "x"}
        ),
    )
    with TestClient(main.app) as client:
        reply = client.post(
            "/api/ask",
            json={"question": "Did Sharjah flood between 2024-04-12 and 2024-04-22?"},
        )
    assert reply.json() == {"analysis_id": "x"}
    assert started["plan"].intent == "water_gain"
    assert started["aoi"].name == "Sharjah, UAE"
