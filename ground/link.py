import asyncio

from satcore.models import LinkReport
from satcore.packet import packet_bytes

RATES = {"demo-fast": 2_000_000, "sband-2m": 2_000_000, "uhf-9k6": 9600}


async def downlink(packet, mode):
    size = packet_bytes(packet)
    raw = packet.raw_equivalent_bytes
    seconds = size * 8 / RATES[mode]
    if mode != "demo-fast":
        await asyncio.sleep(min(seconds, 6) if mode == "uhf-9k6" else seconds)
    return LinkReport(
        mode=mode,
        packet_bytes=size,
        raw_equivalent_bytes=raw,
        saved_pct=round(100 * (1 - size / raw), 4) if raw else None,
        simulated_seconds=seconds,
        raw_simulated_seconds=raw * 8 / RATES[mode] if raw else None,
    )
