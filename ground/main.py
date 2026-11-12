import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date
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
from satcore.config import ROOT, data_path
from satcore.demo import cases
from satcore.geo import build_grid, place_box
from satcore.models import AOI, AnswerPacket, Intent, Language, UplinkCommand

from . import cache, geocode, orbit
from .events import EventLog
from .link import RATES, downlink
from .llm.client import configured
from .llm.explainer import explain
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
SHUTDOWN_GRACE_S = 5


@asynccontextmanager
async def lifespan(_app):
    yield
    # Give running analyses a moment to finish, then cancel them so Ctrl+C
    # never hangs on a slow imagery download.
    if tasks:
        _done, running = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_S)
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)


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
        hit = await asyncio.to_thread(
            cached_result, key, model_analysis, artifact_mode, detector
        )
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


def cached_result(key, model_analysis, artifact_mode, detector):
    """Return a reusable cached analysis, or None to run a fresh one.

    Two cases are never reused:
    * model-assisted analyses on a remote node: the ground cannot tell which
      model version the node has, so the cached result may be stale;
    * urban results computed *without* FresUNet when the model is now
      installed locally: rerun so the model can contribute.
    """
    if model_analysis and artifact_mode == "remote":
        return None
    hit = cache.cached(key)
    if hit and detector:
        flags = json.loads(hit["packet_json"])["trust"]["flags"]
        if "construction_candidates" in flags and "fresunet_assisted" not in flags:
            return None
    return hit


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
    )
    mode = request.link_mode or os.getenv("LINK_MODE", "demo-fast")
    if mode not in RATES:
        mode = "demo-fast"
    active_node = demo_node if request.demo_id else node
    artifact_mode = "local" if request.demo_id else node_mode
    jobs[identifier] = EventLog()
    job_modes[identifier] = artifact_mode
    schedule(run(cmd, plan, request, mode, active_node, artifact_mode))
    return {"analysis_id": str(identifier)}


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
        if not configured():
            return {
                "clarification": "Choose an area, two dates, and an analysis type to run this without OpenAI."
            }
        try:
            plan = await plan_question(request.model_dump(mode="json"))
        except (OpenAIError, ValueError) as exc:
            raise HTTPException(
                502,
                "OpenAI could not plan the request. Check the configured key and model.",
            ) from exc
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
                return {"clarification": "Which place should be analyzed?"}
            locations = await lookup(plan.place_name)
            if not locations:
                return {
                    "clarification": "That place could not be located. Draw the area on the map."
                }
            if len(locations) > 1:
                return {
                    "clarification": "Several places match. Choose the intended location using map search."
                }
            place = locations[0]
            aoi = place_box(place["lon"], place["lat"], place["name"])
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
    return {"cases": cases()}


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
    return {
        "node": "configured" if node_mode == "remote" else "online",
        "stac_primary": os.getenv(
            "STAC_PRIMARY", "https://earth-search.aws.element84.com/v1"
        ),
        "openai": {"planner": configured(), "explainer": configured()},
    }


web = ROOT / "web/dist"
if web.is_dir():
    app.mount("/", StaticFiles(directory=web, html=True), name="web")
