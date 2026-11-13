"""Prepare real (pinned) demo cases so they play offline.

    python -m scripts.prep_demo list --case ID [--window 15]   # network: candidate scenes + thumbnails
    python -m scripts.prep_demo pin --case ID --a ITEM[,ITEM] --b ITEM[,ITEM] [--catalog planetary-computer]
    python -m scripts.prep_demo fetch [--case ID]              # network: cache bands + WorldCover
    python -m scripts.prep_demo run [--case ID]                # full ground flow, en/ar/hi, into the cache
    python -m scripts.prep_demo verify [--case ID]             # offline: recompute, fail if ha differ > 2%
    python -m scripts.prep_demo load                           # load committed explanations into the cache

Real demo cases are the entries in config/demo_cases.yaml with item_a and item_b. After
`fetch` and `run`, start the ground station with OFFLINE=1 to play them without a network.
"""

import argparse
import json
from datetime import date
from uuid import uuid4

import numpy as np
from PIL import Image

from satcore import pipeline, stac, store
from satcore.config import ROOT, data_path
from satcore.demo import cases
from satcore.geo import build_grid
from satcore.load import read_band
from satcore.models import AOI, UplinkCommand

CASES_FILE = ROOT / "config/demo_cases.yaml"
LANGUAGES = ("en", "ar", "hi")
HA_TOLERANCE = 0.02


def real_cases(only=None):
    found = [case for case in cases() if not case["synthetic"]]
    if only:
        found = [case for case in found if case["id"] == only]
        if not found:
            raise SystemExit(f"Not a pinned (real) demo case: {only}")
    return found


def command(case) -> UplinkCommand:
    return UplinkCommand(
        analysis_id=uuid4(),
        aoi=AOI.model_validate(case["aoi"]),
        date_a=date.fromisoformat(str(case["date_a"])),
        date_b=date.fromisoformat(str(case["date_b"])),
        intent=case["intent"],
        item_a=case.get("item_a"),
        item_b=case.get("item_b"),
        catalog=case.get("catalog", "earth-search"),
    )


# ---------------------------------------------------------------- list / pin


def thumbnail(selection, aoi, grid, path):
    small, inside = stac._preview_grid(aoi, grid)
    rgb = np.dstack(
        [read_band(selection.assets[k], small) for k in ("red", "green", "blue")]
    )
    finite = rgb[inside & np.isfinite(rgb).all(axis=-1)]
    low, high = np.percentile(finite, [2, 98]) if finite.size else (0, 0.3)
    pixels = np.nan_to_num(np.clip((rgb - low) / max(high - low, 1e-3), 0, 1)) ** (
        1 / 1.2
    )
    Image.fromarray((pixels * 255).astype(np.uint8)).resize(
        (small.width * 3, small.height * 3), Image.NEAREST
    ).save(path)


def list_scenes(case, window):
    aoi = AOI.model_validate(case["aoi"])
    grid = build_grid(aoi)
    folder = data_path("PREP_DIR", "var/prep") / case["id"]
    folder.mkdir(parents=True, exist_ok=True)
    for role in ("A", "B"):
        target = date.fromisoformat(str(case[f"date_{role.lower()}"]))
        print(f"{case['id']} date {role} (requested {target}):")
        for backup in (False, True):
            catalog = "planetary-computer" if backup else "earth-search"
            try:
                found = stac.ranked_scenes(
                    aoi, grid, target, role, window, backup, None, limit=8
                )
            except Exception as exc:  # one catalog down: show the other
                print(f"  {catalog}: unavailable ({type(exc).__name__})")
                continue
            for selection in found:
                scene = selection.scene
                name = f"{role}_{scene.datetime.date()}_{catalog}.png"
                thumbnail(selection, aoi, grid, folder / name)
                print(
                    f"  {scene.datetime.date()} {scene.aoi_cloud_pct:5.1f}% cloud  {catalog:18s} "
                    f"{','.join(scene.item_ids)}  -> {folder / name}"
                )


def pin(case_id, item_a, item_b, catalog):
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedSeq

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)  # match the file: "  - id: ..."
    data = yaml.load(CASES_FILE)
    entry = next((c for c in data["cases"] if c["id"] == case_id), None)
    if entry is None:
        raise SystemExit(f"Unknown demo case: {case_id}")
    for key, items in (("item_a", item_a), ("item_b", item_b)):
        seq = CommentedSeq(items)
        seq.fa.set_flow_style()  # keep pins on one line: item_a: [ID, ID]
        entry[key] = seq
    if catalog != "earth-search":
        entry["catalog"] = catalog
    elif "catalog" in entry:
        del entry["catalog"]
    with CASES_FILE.open("w", encoding="utf-8") as handle:
        yaml.dump(data, handle)
    print(f"pinned {case_id}: A={item_a} B={item_b} ({catalog})")


# ---------------------------------------------------------------- fetch / run / verify


def fetch(case):
    cmd = command(case)
    grid = build_grid(cmd.aoi)
    backup = cmd.catalog == "planetary-computer"
    for role, items, target in (
        ("A", cmd.item_a, cmd.date_a),
        ("B", cmd.item_b, cmd.date_b),
    ):
        selection = store.pinned_selection(cmd.aoi, grid, items, role, target, backup)
        store.load_scene(selection, grid)
    cover = store.worldcover(cmd.aoi, grid)
    print(
        f"{case['id']}: scenes cached; WorldCover {'cached' if cover is not None else 'unavailable'}"
    )


def result_key(case) -> str:
    """The ground cache key of a demo's result (as computed by ground.main.run)."""
    from ground import cache
    from satcore.urban_model import model_identity

    cmd = command(case)
    detector = (
        model_identity() if cmd.intent in {"urban_growth", "visible_change"} else None
    )
    return cache.cache_key(cmd, detector)


def explanation_source(explanation: dict, packet: dict, language: str) -> str:
    """'template' if this is the packet template (model unavailable or rejected), else 'model'."""
    from ground.llm.explainer import template
    from satcore.models import AnswerPacket

    fallback = template(AnswerPacket.model_validate(packet), language).model_dump()
    same = all(explanation[k] == fallback[k] for k in ("headline", "body"))
    return "template" if same else "model"


def run(case, languages):
    """Full ground flow per language; exports the explanations to config/demo_explanations/."""
    from fastapi.testclient import TestClient

    from ground import demo_explanations
    from ground.llm.schemas import Explanation
    from ground.main import app
    from satcore.models import AnswerPacket

    explanations = {}
    with TestClient(app) as client:
        for language in languages:
            reply = client.post(
                "/api/ask",
                json={
                    "demo_id": case["id"],
                    "language": language,
                    "question": case.get("question", ""),
                },
            )
            reply.raise_for_status()
            identifier = reply.json()["analysis_id"]
            stream = client.get(f"/api/analyses/{identifier}/events").text
            last = [
                json.loads(row[6:])
                for row in stream.splitlines()
                if row.startswith("data: ")
            ][-1]
            if last["stage"] != "done":
                raise SystemExit(
                    f"{case['id']} ({language}): {last['detail'].get('message')}"
                )
            result = client.get(f"/api/analyses/{identifier}").json()
            stats = result["packet"]["stats"] or {}
            explanation = result["explanation"]
            packet = AnswerPacket.model_validate(result["packet"])
            bad = demo_explanations.problems(Explanation(**explanation), packet)
            if bad:
                raise SystemExit(f"{case['id']} ({language}): guard rejected {bad}")
            explanations[language] = explanation
            print(
                f"{case['id']} [{language}] {result['packet']['status']} "
                f"{stats.get('changed_ha')} ha · "
                f"{explanation_source(explanation, result['packet'], language)} · "
                f"{explanation['headline'][:70]}"
            )
    path = demo_explanations.export(case["id"], result_key(case), explanations)
    print(f"  wrote {path.relative_to(ROOT)}")


def verify(case) -> bool:
    from ground import cache
    from node.verdict import TemplateVerdict

    cmd = command(case)
    key = result_key(case)
    rows = [cache.cached(key, language) for language in LANGUAGES]
    rows = [row for row in rows if row is not None]
    if not rows:
        print(f"{case['id']}: FAIL no cached result; run `prep_demo run`")
        return False
    fresh = pipeline.run_analysis(cmd, lambda *args: None, TemplateVerdict())
    fresh_ha = fresh.stats.changed_ha if fresh.stats else 0.0
    ok, cached_ha, languages = True, set(), set()
    for row in rows:
        ha = (json.loads(row["packet_json"])["stats"] or {}).get("changed_ha", 0.0)
        languages.add(json.loads(row["plan_json"])["language"])
        cached_ha.add(ha)
        drift = abs(ha - fresh_ha) / ha if ha else float(fresh_ha != 0)
        ok &= drift <= HA_TOLERANCE
    missing = set(LANGUAGES) - languages
    ok &= not missing
    note = f"; missing languages: {sorted(missing)}" if missing else ""
    explained, detail = committed_explanations(case, key, rows)
    ok &= explained
    print(
        f"{case['id']}: {'ok' if ok else 'FAIL'} cached {sorted(cached_ha)} ha, "
        f"recomputed {fresh_ha} ha{note}; explanations: {detail}"
    )
    return ok


def committed_explanations(case, key, rows) -> tuple[bool, str]:
    """Check config/demo_explanations/<id>.json matches this result and passes the guard."""
    from ground import demo_explanations
    from ground.llm.schemas import Explanation
    from satcore.config import THRESHOLDS
    from satcore.models import AnswerPacket

    path = demo_explanations.FOLDER / f"{case['id']}.json"
    if not path.exists():
        return False, "no committed file"
    data = json.loads(path.read_text("utf-8"))
    if data["cache_key"] != key or data["thresholds_version"] != THRESHOLDS["version"]:
        return (
            False,
            "stale (key or thresholds version differs; re-run `prep_demo run`)",
        )
    missing = set(LANGUAGES) - set(data["explanations"])
    if missing:
        return False, f"missing {sorted(missing)}"
    packet = AnswerPacket.model_validate_json(rows[0]["packet_json"])
    sources = []
    for language in LANGUAGES:
        explanation = data["explanations"][language]
        bad = demo_explanations.problems(Explanation(**explanation), packet)
        if bad:
            return False, f"{language} fails the number guard: {bad}"
        sources.append(
            f"{language}={explanation_source(explanation, json.loads(rows[0]['packet_json']), language)}"
        )
    return True, ", ".join(sources) + " (guard ok)"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command", choices=["list", "pin", "fetch", "run", "verify", "load"]
    )
    parser.add_argument("--case")
    parser.add_argument("--window", type=int, default=15)
    parser.add_argument("--a", help="pin: item ID(s) for date A, comma-separated")
    parser.add_argument("--b", help="pin: item ID(s) for date B, comma-separated")
    parser.add_argument(
        "--catalog",
        default="earth-search",
        choices=["earth-search", "planetary-computer"],
    )
    parser.add_argument("--languages", default=",".join(LANGUAGES))
    args = parser.parse_args(argv)
    if args.command == "pin":
        if not (args.case and args.a and args.b):
            raise SystemExit("pin needs --case, --a and --b")
        pin(args.case, args.a.split(","), args.b.split(","), args.catalog)
        return
    if args.command == "list":
        if not args.case:
            raise SystemExit("list needs --case")
        list_scenes(next(c for c in cases() if c["id"] == args.case), args.window)
        return
    if args.command == "load":
        from ground import demo_explanations

        print(
            "loaded",
            demo_explanations.install(),
            "committed explanations into the cache",
        )
        return
    selected = real_cases(args.case)
    if args.command == "fetch":
        for case in selected:
            fetch(case)
    elif args.command == "run":
        for case in selected:
            run(case, args.languages.split(","))
    else:
        results = [verify(case) for case in selected]
        raise SystemExit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
