import asyncio
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

from ground.llm import explainer, planner
from ground.llm.schemas import AnalysisPlan, Explanation
from node.verdict import TemplateVerdict
from satcore.demo import cases
from satcore.models import AOI, UplinkCommand
from satcore.pipeline import run_analysis


class FakeResponses:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.requests = []
        self.responses = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def parse(self, **request):
        self.requests.append(request)
        return SimpleNamespace(output_parsed=next(self.outputs))


def test_planner_uses_structured_responses(monkeypatch):
    expected = AnalysisPlan(
        intent="water_gain",
        place_name="Dubai",
        date_a=date(2024, 4, 8),
        date_b=date(2024, 4, 21),
        language="en",
        clarification=None,
    )
    fake = FakeResponses([expected])
    monkeypatch.setattr(planner, "client", lambda: fake)
    monkeypatch.setenv("OPENAI_MODEL_FAST", "test-model")
    result = asyncio.run(
        planner.plan_question({"question": "Did the Dubai storm cause flooding?"})
    )
    assert result == expected
    assert fake.requests[0]["text_format"] is AnalysisPlan
    assert fake.requests[0]["model"] == "test-model"


def test_explainer_retries_unsupported_number(monkeypatch, tmp_path):
    case = cases()[0]
    cmd = UplinkCommand(
        analysis_id=uuid4(),
        aoi=AOI.model_validate(case["aoi"]),
        date_a=case["date_a"],
        date_b=case["date_b"],
        intent=case["intent"],
        demo_id=case["id"],
    )
    monkeypatch.setenv("ANALYSES_DIR", str(tmp_path))
    packet = run_analysis(cmd, lambda *_: None, TemplateVerdict())
    for scene in packet.scenes:
        scene.source = "earth-search"
    valid = explainer.template(packet, "en")
    invalid = Explanation(
        headline="Detected 99999 ha", body="", caveats=[], numbers_used=[99999]
    )
    fake = FakeResponses([invalid, valid])
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(explainer, "client", lambda: fake)
    result = asyncio.run(explainer.explain(packet, "en"))
    assert result == valid
    assert len(fake.requests) == 2
    assert "99999" in fake.requests[1]["input"]


def test_refusals_always_suggest_the_next_clear_pass():
    from ground.llm.explainer import with_next_pass
    from ground.llm.schemas import Explanation

    refused = type("P", (), {"status": "refused_cloudy"})()
    done = type("P", (), {"status": "ok"})()
    plain = Explanation(
        headline="h", body="Not enough clear view.", caveats=[], numbers_used=[]
    )
    assert with_next_pass(plain, refused, "en").body.endswith(
        "next predicted clear pass."
    )
    assert "المدار" in with_next_pass(plain, refused, "ar").body
    already = plain.model_copy(update={"body": "See the Orbit tab."})
    assert with_next_pass(already, refused, "en").body == "See the Orbit tab."
    assert with_next_pass(plain, done, "en") == plain
