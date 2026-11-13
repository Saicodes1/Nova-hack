"""Send one uplink command to a flight node and print what comes back.

    python scripts/node_check.py http://192.168.55.1:8100            # real imagery
    python scripts/node_check.py http://192.168.55.1:8100 --demo dubai_flood
"""

import argparse
import json
import sys
import time
from uuid import uuid4

import httpx

sys.path.insert(0, ".")
from satcore.geo import place_box  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("node")
parser.add_argument("--demo")
args = parser.parse_args()

command = {
    "analysis_id": str(uuid4()),
    "aoi": place_box(55.112, 24.903, "Jebel Ali South").model_dump(),
    "date_a": "2024-04-12",
    "date_b": "2024-04-22",
    "intent": "water_gain",
    "demo_id": args.demo,
}
start = time.time()
packet = None
with httpx.stream(
    "POST", args.node.rstrip("/") + "/uplink", json=command, timeout=180
) as response:
    response.raise_for_status()
    for line in response.iter_lines():
        if not line.startswith("data: "):
            continue
        event = json.loads(line[6:])
        if event["status"] in {"done", "failed", "refused"}:
            print(f"  {event['stage']:<16}{event['status']:<8}{event.get('ms') or ''}")
        if event["status"] == "failed":
            sys.exit(event["detail"].get("message", "failed"))
        packet = event.get("packet") or packet
if packet is None:
    sys.exit("No packet returned.")
print(f"status   {packet['status']}  ({time.time() - start:.1f} s)")
print(f"verdict  {packet['onboard_verdict']}")
print(f"trust    {packet['trust']['score']}/100 {packet['trust']['level']}")
for scene in packet["scenes"]:
    print(f"scene {scene['role']}  {scene['datetime'][:10]}  {scene['source']}")
if packet["stats"]:
    print(f"changed  {packet['stats']['changed_ha']} ha")
