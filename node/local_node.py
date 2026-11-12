import asyncio
from uuid import UUID

import psutil

from satcore.models import UplinkCommand
from satcore.packet import packet_bytes
from satcore.pipeline import run_analysis

from .protocol import NodeEvent, Telemetry
from .verdict import TemplateVerdict


class LocalNode:
    def __init__(self):
        self.running: set[UUID] = set()
        self.last_packet_bytes: int | None = None
        self.slot = asyncio.Semaphore(2)

    async def uplink(self, cmd: UplinkCommand):
        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def emit(stage, status, detail):
            if stage != "packet" or status != "done":
                event = NodeEvent(
                    analysis_id=cmd.analysis_id,
                    stage=stage,
                    status=status,
                    detail=detail.copy(),
                    ms=detail.get("ms"),
                )
                loop.call_soon_threadsafe(queue.put_nowait, event)

        async def run():
            try:
                async with self.slot:
                    self.running.add(cmd.analysis_id)
                    packet = await asyncio.to_thread(
                        run_analysis, cmd, emit, TemplateVerdict()
                    )
                    self.last_packet_bytes = packet_bytes(packet)
                    queue.put_nowait(
                        NodeEvent(
                            analysis_id=cmd.analysis_id,
                            stage="packet",
                            status="done",
                            ms=packet.timings_ms["packet"],
                            packet=packet,
                        )
                    )
            except Exception as exc:
                queue.put_nowait(exc)
            finally:
                self.running.discard(cmd.analysis_id)
                queue.put_nowait(None)

        task = asyncio.create_task(run())
        try:
            while (event := await queue.get()) is not None:
                if isinstance(event, Exception):
                    raise event
                yield event
        finally:
            await asyncio.shield(task)

    async def telemetry(self):
        return Telemetry(
            mode="local",
            state="processing" if self.running else "idle",
            mem_used_mb=psutil.Process().memory_info().rss // (1024 * 1024),
            last_packet_bytes=self.last_packet_bytes,
        )
