import asyncio
import json
from dataclasses import dataclass, field


@dataclass
class EventLog:
    events: list[dict] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    terminal: bool = False

    def push(self, stage, status, detail=None, ms=None, replayed=False):
        self.events.append(
            dict(
                stage=stage,
                status=status,
                detail=detail or {},
                ms=ms,
                replayed=replayed,
            )
        )
        self.terminal = stage in {"done", "error"}
        self.changed.set()

    async def stream(self, cursor=0):
        while True:
            for event in self.events[cursor:]:
                cursor += 1
                yield f"id: {cursor}\nevent: {event['stage']}\ndata: {json.dumps(event)}\n\n"
            if cursor < len(self.events):
                continue
            if self.terminal:
                return
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), 15)
            except TimeoutError:
                yield ": keep-alive\n\n"
