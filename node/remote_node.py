import httpx

from satcore.models import UplinkCommand

from .protocol import NodeEvent, Telemetry


class NodeUnavailable(RuntimeError):
    pass


class RemoteNode:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    async def uplink(self, cmd: UplinkCommand):
        try:
            # A first look at a new place can spend over a minute downloading
            # image crops between two progress events.
            patience = httpx.Timeout(10, read=300)
            async with httpx.AsyncClient(timeout=patience) as client:
                async with client.stream(
                    "POST", f"{self.url}/uplink", json=cmd.model_dump(mode="json")
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            event = NodeEvent.model_validate_json(line[6:])
                            if event.status == "failed":
                                raise NodeUnavailable(
                                    event.detail.get(
                                        "message", "Remote analysis failed."
                                    )
                                )
                            yield event
        except httpx.TimeoutException as exc:
            raise NodeUnavailable(
                "The satellite took too long to answer. Ask again: it has usually "
                "stored the images by now, so a second try is quick."
            ) from exc
        except httpx.HTTPError as exc:
            raise NodeUnavailable("Connection to the satellite node was lost.") from exc

    async def telemetry(self):
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"{self.url}/telemetry")
            response.raise_for_status()
            return Telemetry.model_validate(response.json()).model_copy(
                update={"mode": "remote"}
            )
