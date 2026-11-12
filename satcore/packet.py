from .models import AnswerPacket


def packet_bytes(packet: AnswerPacket) -> int:
    return len(packet.model_dump_json(exclude={"link"}).encode("utf-8"))


def raw_equivalent(selections) -> int | None:
    sizes = [
        asset.size
        for selection in selections
        for key, assets in selection.assets.items()
        if key != "scl"
        for asset in assets
    ]
    return sum(sizes) if sizes and all(s is not None for s in sizes) else None
