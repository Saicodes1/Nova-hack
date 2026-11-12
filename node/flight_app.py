import logging
from typing import Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from satcore.config import data_path
from satcore.models import UplinkCommand

from .local_node import LocalNode
from .protocol import NodeEvent

app = FastAPI(title="Ask the Satellite Flight Node")
node = LocalNode()
logger = logging.getLogger(__name__)


@app.get("/health")
async def health():
    return {"node": "online"}


@app.get("/telemetry")
async def telemetry():
    return await node.telemetry()


@app.post("/uplink")
async def uplink(cmd: UplinkCommand):
    async def stream():
        try:
            async for event in node.uplink(cmd):
                yield f"event: {event.stage}\ndata: {event.model_dump_json()}\n\n"
        except Exception:
            logger.exception("Flight analysis failed")
            event = NodeEvent(
                analysis_id=cmd.analysis_id,
                stage="error",
                status="failed",
                detail={"message": "The flight node could not complete this analysis."},
            )
            yield f"event: error\ndata: {event.model_dump_json()}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/artifacts/{analysis_id}/{name}.png")
async def artifacts(analysis_id: UUID, name: Literal["a", "b", "change"]):
    image = data_path("ANALYSES_DIR", "var/analyses") / str(analysis_id) / f"{name}.png"
    if not image.is_file():
        raise HTTPException(404, "Image not available.")
    return FileResponse(image, media_type="image/png")
