from satcore.models import AnswerPacket

LABELS = {
    "water_gain": "New surface water",
    "water_loss": "Surface water loss",
    "veg_gain": "Vegetation gain",
    "veg_loss": "Vegetation loss",
    "urban_growth": "Likely built-up growth",
    "visible_change": "Candidate visible change",
    "burn": "Likely burn-related change",
}


class TemplateVerdict:
    def write(self, packet: AnswerPacket) -> str:
        if packet.status == "refused_cloudy":
            return "Cloud or missing coverage prevented an estimate."
        if packet.status == "no_scenes":
            return "A usable scene pair was unavailable."
        if packet.status == "no_change":
            return "No change above the detection floor."
        return (
            f"{LABELS[packet.intent]} detected over {packet.stats.changed_ha:g} ha "
            f"({packet.stats.changed_pct:g}% of clear coverage)."
        )
