from collections.abc import AsyncIterator
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from satcore.models import AnswerPacket, UplinkCommand


class NodeEvent(BaseModel):
    analysis_id: UUID
    stage: str
    status: Literal["started", "done", "failed", "refused"]
    ms: int | None = None
    detail: dict = Field(default_factory=dict)
    packet: AnswerPacket | None = None


class Telemetry(BaseModel):
    mode: Literal["local", "remote"]
    state: Literal["idle", "uplink", "processing", "downlink"]
    power_w: float | None = None
    temp_c: float | None = None
    mem_used_mb: int
    last_packet_bytes: int | None = None


class SatelliteNode(Protocol):
    def uplink(self, cmd: UplinkCommand) -> AsyncIterator[NodeEvent]: ...
    async def telemetry(self) -> Telemetry: ...
