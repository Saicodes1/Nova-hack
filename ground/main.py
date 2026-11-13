import asyncio
import json
import logging
import os
from collections import deque
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAIError
from pydantic import BaseModel, Field
from shapely.geometry import shape

from node.local_node import LocalNode
from node.remote_node import NodeUnavailable, RemoteNode
from satcore import store
from satcore.config import ROOT, data_path
from satcore.demo import cases, presets, watch_presets
from satcore.geo import build_grid, place_box
from satcore.models import AOI, AnswerPacket, Intent, Language, UplinkCommand

from . import cache, geocode, orbit, watch
from .events import EventLog
from .link import RATES, downlink
from .llm.client import configured
from .llm.explainer import explain
from .llm.local_planner import place_candidates, plan_locally
from .llm.planner import plan_question
from .llm.schemas import AnalysisPlan, Explanation

logger = logging.getLogger(__name__)
LinkMode = Literal["demo-fast", "sband-2m", "uhf-9k6"]
node_url = os.getenv("NODE_URL", "http://localhost:8100").rstrip("/")
node_mode = os.getenv("NODE_MODE", "local")
node = RemoteNode(node_url) if node_mode == "remote" else LocalNode()
jobs: dict[UUID, EventLog] = {}
job_modes: dict[UUID, str] = {}
tasks: set[asyncio.Task] = set()
# One question reaches the satellite at a time; the rest wait their turn.
uplink_turn = asyncio.Lock()
waiting: list[UUID] = []
recent: deque[dict] = deque(maxlen=40)


@asynccontextmanager
async def lifespan(_app):
    for armed in watch.restore():
        watch.loops[armed["id"]] = asyncio.create_task(watch.run(armed, watch_pass))
    yield
    for loop in list(watch.loops.values()):
        loop.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="Ask the Satellite", lifespan=lifespan)


class Ask(BaseModel):
    question: str = Field("", max_length=2000)
    aoi: AOI | None = None
    date_a: date | None = None
    date_b: date | None = None
    intent: Intent | None = None
    language: Language = "en"
    demo_id: str | None = None
    link_mode: LinkMode | None = None
    # Use the acquisition of exactly that day, however cloudy.
    exact_a: bool = False
    exact_b: bool = False
    report_floor_ha: float | None = Field(None, ge=0)
    # "phone" marks a question sent from the audience page behind the QR code.
    source: Literal["desk", "phone"] = "desk"


class PassQuery(BaseModel):
    aoi: AOI
    start: date
    end: date


class ChatRequest(BaseModel):
    analysis_id: UUID
    message: str = Field(min_length=1, max_length=2000)
    language: Language = "en"


def schedule(coroutine):
    task = asyncio.create_task(coroutine)
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def run(cmd, plan, request, mode, active_node, artifact_mode):
    identifier = cmd.analysis_id
    log = jobs[identifier]
    try:
        from satcore.urban_model import model_identity

        model_analysis = (
            cmd.intent in {"urban_growth", "visible_change"} and not cmd.demo_id
        )
        detector = (
            model_identity() if model_analysis and artifact_mode == "local" else None
        )
        key = cache.cache_key(cmd, detector)
        hit = (
            None
            if model_analysis and artifact_mode == "remote"
            else await asyncio.to_thread(cache.cached, key)
        )
        if (
            hit
            and detector
            and "construction_candidates"
            in json.loads(hit["packet_json"])["trust"]["flags"]
        ):
            if (
                "fresunet_assisted"
                not in json.loads(hit["packet_json"])["trust"]["flags"]
            ):
                hit = None
        artifact_id = identifier
        if hit:
            artifact_id = hit["artifact_id"]
            artifact_mode = hit["artifact_mode"]
            job_modes[identifier] = artifact_mode
            packet = AnswerPacket.model_validate_json(hit["packet_json"]).model_copy(
                update={"analysis_id": identifier, "aoi": cmd.aoi, "link": None}
            )
            for event in json.loads(hit["events_json"]):
                if event["stage"] in {"downlink", "explain", "done", "error"}:
                    continue
                detail = event["detail"].copy()
                if event["stage"] == "render" and event["status"] == "done":
                    detail["artifact_id"] = str(artifact_id)
                log.push(event["stage"], event["status"], detail, event["ms"], True)
        else:
            waiting.append(identifier)
            try:
                if uplink_turn.locked():
                    log.push("queued", "started", {"ahead": len(waiting) - 1})
                async with uplink_turn:
                    if log.events and log.events[-1]["stage"] == "queued":
                        log.push("queued", "done")
                    log.push("uplink", "started")
                    log.push("uplink", "done")
                    packet = None
                    async for event in active_node.uplink(cmd):
                        detail = event.detail.copy()
                        if event.stage == "render" and event.status == "done":
                            detail["artifact_id"] = str(identifier)
                        log.push(event.stage, event.status, detail, event.ms)
                        if event.packet:
                            packet = event.packet
            finally:
                waiting.remove(identifier)
            if packet is None:
                raise RuntimeError("The node returned an incomplete packet.")
        log.push("downlink", "started", {"mode": mode})
        packet.link = await downlink(packet, mode)
        log.push("downlink", "done", {"packet_bytes": packet.link.packet_bytes})
        log.push("explain", "started")
        prior_plan = AnalysisPlan.model_validate_json(hit["plan_json"]) if hit else None
        reusable = hit and prior_plan.language == plan.language and not configured()
        explanation = (
            Explanation.model_validate_json(hit["explanation_json"])
            if reusable
            else await explain(packet, plan.language)
        )
        log.push("explain", "done", replayed=bool(reusable))
        await asyncio.to_thread(
            cache.save,
            identifier,
            key,
            request.question,
            plan,
            packet,
            explanation,
            log.events,
            artifact_id,
            artifact_mode,
        )
        log.push("done", "done", {"analysis_id": str(identifier)})
    except Exception as exc:
        logger.exception("Analysis failed for %s", identifier)
        message = (
            str(exc)
            if isinstance(exc, NodeUnavailable)
            else "The analysis could not finish. Check imagery access or try another area."
        )
        log.push("error", "failed", {"message": message})
    finally:
        if log.events[-1]["stage"] == "done":
            jobs.pop(identifier, None)
        elif len(jobs) > 128:
            for old_id, old_log in list(jobs.items()):
                if old_id != identifier and old_log.terminal:
                    jobs.pop(old_id, None)
                    break
        job_modes.pop(identifier, None)


def start(aoi, plan, request):
    from satcore.urban_model import model_identity

    try:
        build_grid(aoi)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if (
        not plan.date_a
        or not plan.date_b
        or plan.date_a >= plan.date_b
        or not plan.intent
    ):
        raise HTTPException(
            422, "Choose an earlier and a later date and an analysis type."
        )
    if (
        plan.intent == "visible_change"
        and node_mode == "local"
        and model_identity() is None
    ):
        raise HTTPException(
            503, "Visible change requires the FresUNet model on the satellite node."
        )
    identifier = uuid4()
    cmd = UplinkCommand(
        analysis_id=identifier,
        aoi=aoi,
        date_a=plan.date_a,
        date_b=plan.date_b,
        intent=plan.intent,
        demo_id=request.demo_id,
        exact_a=request.exact_a and not request.demo_id,
        exact_b=request.exact_b and not request.demo_id,
        report_floor_ha=request.report_floor_ha,
    )
    mode = request.link_mode or os.getenv("LINK_MODE", "demo-fast")
    if mode not in RATES:
        mode = "demo-fast"
    active_node = demo_node if request.demo_id else node
    artifact_mode = "local" if request.demo_id else node_mode
    jobs[identifier] = EventLog()
    job_modes[identifier] = artifact_mode
    if request.report_floor_ha is None:
        recent.append(
            {
                "analysis_id": str(identifier),
                "question": request.question[:200],
                "place": aoi.name,
                "intent": plan.intent,
                "source": request.source,
                "asked_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    schedule(run(cmd, plan, request, mode, active_node, artifact_mode))
    # The plan goes back at once so the map can fly to the area while the
    # node is still working.
    return {
        "analysis_id": str(identifier),
        "aoi": aoi.model_dump(),
        "date_a": str(plan.date_a),
        "date_b": str(plan.date_b),
        "intent": plan.intent,
    }


demo_node = node if node_mode == "local" else LocalNode()


@app.post("/api/ask")
async def ask(request: Ask):
    if request.demo_id:
        entry = next((item for item in cases() if item["id"] == request.demo_id), None)
        if entry is None:
            raise HTTPException(404, "Unknown demonstration case.")
        request = request.model_copy(
            update={
                "aoi": AOI.model_validate(entry["aoi"]),
                "date_a": date.fromisoformat(entry["date_a"]),
                "date_b": date.fromisoformat(entry["date_b"]),
                "intent": entry["intent"],
            }
        )
    aoi = request.aoi
    if aoi and request.date_a and request.date_b and request.intent:
        plan = AnalysisPlan(
            intent=request.intent,
            place_name=aoi.name,
            date_a=request.date_a,
            date_b=request.date_b,
            language=request.language,
            clarification=None,
        )
    else:
        if not request.question.strip():
            return {
                "clarification": "Enter a question or choose an area, two dates, and an analysis type."
            }
        if configured():
            try:
                plan = await plan_question(request.model_dump(mode="json"))
            except (OpenAIError, ValueError):
                logger.warning("OpenAI planner unavailable; using the local planner.")
                plan = plan_locally(request.model_dump(mode="json"))
        else:
            plan = plan_locally(request.model_dump(mode="json"))
        plan = plan.model_copy(
            update={
                "language": request.language,
                **{
                    key: getattr(request, key)
                    for key in ("intent", "date_a", "date_b")
                    if getattr(request, key) is not None
                },
            }
        )
        if plan.clarification or not plan.intent or not plan.date_a or not plan.date_b:
            return {
                "clarification": plan.clarification
                or "Which dates and type of change should be compared?"
            }
        if aoi is None:
            if not plan.place_name:
                return {
                    "clarification": "Which place? Add a city, district or "
                    "landmark, for example: Did Hyderabad flood in September 2024?"
                }
            # The planner's first guess, then every other wording of the place
            # found in the question, until the geocoder recognises one.
            names = [plan.place_name] + [
                name
                for name in place_candidates(request.question)
                if name != plan.place_name
            ]
            locations = []
            for name in names[:8]:
                if len(name) >= 2:
                    locations = await lookup(name)
                if locations:
                    break
            if not locations:
                return {
                    "clarification": f'I could not find a place called "{plan.place_name}". '
                    "Try a city, district or landmark name."
                }
            # Prefer a match in a country the question names ("... india").
            asked = request.question.lower()
            place = next(
                (
                    item
                    for item in locations
                    if item["country"] and item["country"].lower() in asked
                ),
                locations[0],
            )
            label = ", ".join(
                dict.fromkeys(
                    part for part in (place["name"], place["country"]) if part
                )
            )
            if place.get("broad"):
                # A state or country is far larger than one analysis. Say so in
                # the name that appears on every answer.
                label = f"Centre of {label} (5 km sample)"
            aoi = place_box(place["lon"], place["lat"], label)
    return start(aoi, plan, request)


@app.get("/api/analyses/{identifier}/events")
async def events(identifier: UUID, last_event_id: str | None = Header(None)):
    log = jobs.get(identifier)
    if log is None:
        row = await asyncio.to_thread(cache.find, identifier)
        if row is None:
            raise HTTPException(404, "Analysis not found.")
        log = EventLog(events=json.loads(row["events_json"]))
        log.push("done", "done", {"analysis_id": str(identifier)})
    cursor = (
        int(last_event_id)
        if last_event_id and last_event_id.isdecimal() and len(last_event_id) <= 9
        else 0
    )
    cursor = min(cursor, len(log.events) - int(log.terminal))
    return StreamingResponse(
        log.stream(max(0, cursor)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/analyses/{identifier}")
async def analysis(identifier: UUID):
    row = await asyncio.to_thread(cache.find, identifier)
    if row:
        return {
            "plan": json.loads(row["plan_json"]),
            "packet": json.loads(row["packet_json"]),
            "explanation": json.loads(row["explanation_json"]),
            "status": row["status"],
        }
    log = jobs.get(identifier)
    if log:
        return {"status": "failed" if log.terminal else "processing"}
    raise HTTPException(404, "Analysis not found.")


@app.get("/api/analyses/{identifier}/images/{name}")
async def image(identifier: UUID, name: Literal["a", "b", "change"]):
    row = await asyncio.to_thread(cache.find, identifier)
    artifact_id = row["artifact_id"] if row else str(identifier)
    mode = row["artifact_mode"] if row else job_modes.get(identifier, node_mode)
    if mode == "remote":
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                result = await client.get(
                    f"{node_url}/artifacts/{artifact_id}/{name}.png"
                )
            if result.is_success:
                return Response(result.content, media_type="image/png")
        except httpx.HTTPError as exc:
            raise HTTPException(502, "Remote image unavailable.") from exc
    else:
        path = data_path("ANALYSES_DIR", "var/analyses") / artifact_id / f"{name}.png"
        if path.is_file():
            return FileResponse(path, media_type="image/png")
    raise HTTPException(404, "Image not available.")


@app.post("/api/chat")
async def chat(request: ChatRequest):
    from .llm.chat import reply

    row = await asyncio.to_thread(cache.find, request.analysis_id)
    if row is None:
        raise HTTPException(404, "Analysis not found.")
    packet = AnswerPacket.model_validate_json(row["packet_json"])
    previous = Explanation.model_validate_json(row["explanation_json"])

    async def tools(name, args):
        if name == "get_next_clear_pass":
            center = shape(packet.aoi.geometry).centroid
            passes = await orbit.passes(center.y, center.x)
            return next(
                (
                    item
                    for item in passes
                    if item["cloud_pct"] is not None and item["cloud_pct"] < 20
                ),
                {},
            )
        if name == "explain_point":
            if row["artifact_mode"] == "remote":
                return {"message": "Point evidence is available on the local node."}
            return await asyncio.to_thread(
                point, row["artifact_id"], args["lon"], args["lat"]
            )
        if name == "run_analysis":
            if any(scene.source == "synthetic" for scene in packet.scenes):
                return {"message": "Start a live query to analyze new dates."}
            followup = Ask(
                aoi=packet.aoi,
                date_a=args["date_a"],
                date_b=args["date_b"],
                intent=args["intent"],
                language=request.language,
                question=request.message,
            )
            return await ask(followup)
        return {"message": "Unknown tool."}

    return await reply(packet, previous, request, tools)


def point(artifact_id, lon, lat):
    import numpy as np
    from rasterio.transform import rowcol

    from satcore.geo import affine, project
    from satcore.models import Grid

    file = data_path("ANALYSES_DIR", "var/analyses") / artifact_id / "indices.npz"
    if not file.is_file():
        return {"message": "Point evidence is unavailable for this analysis."}
    with np.load(file, allow_pickle=False) as data:
        grid = Grid.model_validate_json(str(data["grid"]))
        x, y = project(4326, grid.crs_epsg)(lon, lat)
        if not np.isfinite([x, y]).all():
            return {"message": "Use a valid map coordinate."}
        row, col = rowcol(affine(grid), x, y)
        if not (
            0 <= row < grid.height and 0 <= col < grid.width and data["valid"][row, col]
        ):
            return {"message": "This point lies outside clear coverage."}
        return {
            key: round(float(value), 4)
            for key in data.files
            if key.startswith(("a_", "b_"))
            and np.isfinite(value := data[key][row, col])
        }


@app.get("/api/geocode")
async def lookup(q: str = Query(min_length=2, max_length=120)):
    try:
        matches = await geocode.search(q)
        return [
            dict(
                place,
                aoi=place_box(place["lon"], place["lat"], place["name"]).model_dump(),
            )
            for place in matches
            if -80 < place["lat"] < 84
        ]
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Place search is temporarily unavailable.") from exc


@app.get("/api/orbit/passes")
async def orbit_passes(
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    days: int = Query(10, ge=1, le=16),
):
    try:
        return await orbit.passes(lat, lon, days)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            502, "Orbit predictions are currently unavailable."
        ) from exc


@app.get("/api/orbit/tracks")
async def orbit_tracks(hours: int = Query(3, ge=1, le=12)):
    try:
        return await orbit.tracks(hours)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "Ground tracks are currently unavailable.") from exc


@app.get("/api/demo-cases")
async def demo_cases():
    return {"cases": cases(), "presets": presets(), "watches": watch_presets()}


@app.post("/api/passes")
async def passes(query: PassQuery):
    """Real Sentinel-2 overpasses of an area, with cloud cover over that area."""
    if query.end < query.start or (query.end - query.start).days > 120:
        raise HTTPException(422, "Choose a period of up to 120 days.")
    try:
        grid = build_grid(query.aoi)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        return await asyncio.to_thread(
            store.passes, query.aoi, grid, query.start, min(query.end, date.today())
        )
    except Exception as exc:
        logger.warning("Pass listing failed: %s", exc)
        raise HTTPException(502, "The imagery catalog is unavailable.") from exc


async def watch_pass(item, baseline, day):
    """Task the node with one overpass of a watch and wait for its packet."""
    request = Ask(
        question=item["question"],
        aoi=AOI.model_validate(item["aoi"]),
        date_a=baseline,
        date_b=day,
        intent=item["intent"],
        language=item["language"],
        link_mode=item["link_mode"],
        exact_a=True,
        exact_b=True,
        report_floor_ha=item["floor_ha"],
    )
    identifier = UUID((await ask(request))["analysis_id"])
    while (log := jobs.get(identifier)) and not log.terminal:
        await asyncio.sleep(0.2)
    row = await asyncio.to_thread(cache.find, identifier)
    if row is None:
        failure = log.events[-1]["detail"].get("message") if log else None
        raise RuntimeError(failure or "The node did not answer this pass.")
    return str(identifier), json.loads(row["packet_json"])


@app.post("/api/watches")
async def create_watch(request: watch.WatchRequest):
    try:
        item = watch.create(request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    watch.loops[item["id"]] = asyncio.create_task(watch.run(item, watch_pass))
    return watch.summary(item)


@app.get("/api/watches")
async def list_watches():
    return [watch.summary(item) for item in reversed(watch.watches.values())]


@app.get("/api/watches/{identifier}")
async def get_watch(identifier: str):
    item = watch.watches.get(identifier)
    if item is None:
        raise HTTPException(404, "Watch not found.")
    return watch.summary(item)


@app.post("/api/watches/{identifier}/stop")
async def stop_watch(identifier: str):
    item = watch.stop(identifier)
    if item is None:
        raise HTTPException(404, "Watch not found.")
    return watch.summary(item)


@app.delete("/api/watches/{identifier}")
async def delete_watch(identifier: str):
    if watch.remove(identifier) is None:
        raise HTTPException(404, "Watch not found.")
    return {"deleted": identifier}


@app.get("/api/recent")
async def recent_questions():
    """Latest questions, newest first, so the big screen can show what the
    room is asking from their phones."""
    return list(reversed(recent))


@app.get("/api/telemetry")
async def telemetry():
    try:
        result = (await node.telemetry()).model_dump()
    except httpx.HTTPError as exc:
        raise HTTPException(503, "Satellite node unavailable.") from exc
    result["link_mode"] = os.getenv("LINK_MODE", "demo-fast")
    return result


@app.get("/api/health")
async def health():
    info = None
    if node_mode == "remote":
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                info = (await client.get(f"{node_url}/health")).json()
        except (httpx.HTTPError, ValueError):
            info = None
    return {
        "node": "configured" if node_mode == "remote" else "online",
        "node_mode": node_mode,
        "node_info": info,
        "public_url": os.getenv("PUBLIC_URL", "").rstrip("/"),
        "stac_primary": os.getenv(
            "STAC_PRIMARY", "https://earth-search.aws.element84.com/v1"
        ),
        "openai": {"planner": configured(), "explainer": configured()},
        "planner": "openai" if configured() else "local-rules",
    }


@app.get("/api/validation/{name}")
async def validation(name: str):
    """Accuracy check of a preset against an independent reference map."""
    if not name.replace("_", "").isalnum():
        raise HTTPException(404, "No reference check for this case.")
    path = ROOT / "config/validation" / f"{name}.json"
    if not path.is_file():
        raise HTTPException(404, "No reference check for this case.")
    return json.loads(path.read_text(encoding="utf-8"))


web = ROOT / "web/dist"
if web.is_dir():
    app.mount("/", StaticFiles(directory=web, html=True), name="web")
