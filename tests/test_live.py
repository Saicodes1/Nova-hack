from datetime import date
from uuid import uuid4

import pytest

from node.verdict import TemplateVerdict
from satcore.geo import place_box
from satcore.models import UplinkCommand
from satcore.pipeline import run_analysis


@pytest.mark.live
def test_live_dubai():
    cmd = UplinkCommand(
        analysis_id=uuid4(),
        aoi=place_box(55.22, 24.93, "Dubai South"),
        date_a=date(2024, 4, 8),
        date_b=date(2024, 4, 21),
        intent="water_gain",
    )
    packet = run_analysis(cmd, lambda *_: None, TemplateVerdict())
    assert len(packet.scenes) == 2
    assert packet.scenes[0].source != "synthetic"
    assert packet.stats is not None
    assert len(packet.image_bounds) == 4
