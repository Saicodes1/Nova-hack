"""Download the imagery for the real presets so they replay fast and offline.

    python -m scripts.prewarm

Run it on whichever machine hosts the node (the Jetson in remote mode).
Imagery lands in var/scenes and is reused by every later run.
"""

from datetime import date
from time import perf_counter
from uuid import uuid4

from node.verdict import TemplateVerdict
from satcore.demo import presets
from satcore.models import AOI, UplinkCommand
from satcore.pipeline import run_analysis

for case in presets():
    start = perf_counter()
    packet = run_analysis(
        UplinkCommand(
            analysis_id=uuid4(),
            aoi=AOI.model_validate(case["aoi"]),
            date_a=date.fromisoformat(case["date_a"]),
            date_b=date.fromisoformat(case["date_b"]),
            intent=case["intent"],
        ),
        lambda *_: None,
        TemplateVerdict(),
    )
    dates = " → ".join(str(scene.datetime.date()) for scene in packet.scenes)
    print(
        f"{case['id']:<18}{packet.status:<10}{dates:<26}"
        f"{packet.onboard_verdict}  [{perf_counter() - start:.0f} s]"
    )
