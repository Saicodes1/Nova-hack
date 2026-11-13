import json
import logging
import os
import re
import threading
import time

import httpx

from satcore.models import AnswerPacket

logger = logging.getLogger(__name__)

LABELS = {
    "water_gain": "New surface water",
    "water_loss": "Surface water loss",
    "veg_gain": "Vegetation gain",
    "veg_loss": "Vegetation loss",
    "urban_growth": "Likely built-up growth",
    "visible_change": "Candidate visible change",
    "burn": "Likely burn-related change",
}
NUMBER = re.compile(r"\d+(?:\.\d+)?")


class TemplateVerdict:
    name = "template"

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


class OllamaVerdict:
    """One-sentence verdict written by a small model running on the node.

    The model only words the result. Every number it writes must match the
    measured statistics, otherwise the template sentence is used instead.
    """

    def __init__(self, url: str, model: str, timeout: float = 25):
        self.url, self.model, self.timeout = url.rstrip("/"), model, timeout
        self.name = f"ollama:{model}"
        self.fallback = TemplateVerdict()
        self.ready = threading.Event()
        self.ready.set()

    def warm_in_background(self, load_timeout: float = 600):
        """Load the model before the first analysis needs it.

        On a Jetson Orin Nano the first load of llama3.2:3b took 158 s, far
        longer than the per-verdict timeout, and a request that gives up makes
        Ollama abort the load. Until the model answers once, write() uses the
        template instead of calling it.
        """
        self.ready.clear()

        def load():
            while True:
                try:
                    start = time.perf_counter()
                    httpx.post(
                        f"{self.url}/api/generate",
                        json={
                            "model": self.model,
                            "prompt": "Reply with OK.",
                            "stream": False,
                            "keep_alive": -1,
                            "options": {"temperature": 0, "num_predict": 2},
                        },
                        timeout=load_timeout,
                    ).raise_for_status()
                    logger.warning(
                        "Onboard model %s ready after %.0f s.",
                        self.model,
                        time.perf_counter() - start,
                    )
                    self.ready.set()
                    return
                except httpx.HTTPError as exc:
                    logger.warning("Onboard model not ready (%s); retrying.", exc)
                    time.sleep(30)

        threading.Thread(target=load, daemon=True).start()

    def write(self, packet: AnswerPacket) -> str:
        plain = self.fallback.write(packet)
        if packet.status != "ok" or packet.stats is None:
            return plain
        if not self.ready.is_set():
            logger.warning("Onboard model still loading; using the template.")
            return plain
        # Plain key names and few facts: with a longer list llama3.2:3b wrote
        # "1123.59 hectares of clear area showing a 44.9% increase".
        facts = {
            "finding": LABELS[packet.intent],
            "changed_area_hectares": packet.stats.changed_ha,
            "changed_share_of_cloud_free_area_percent": packet.stats.changed_pct,
            "confidence": packet.trust.level,
            "largest_single_patch_hectares": packet.stats.largest_polygon_ha,
        }
        prompt = (
            "You are the onboard computer of an Earth-observation satellite. "
            "Write ONE short sentence (at most 30 words) reporting this measured result. "
            "State the finding, the changed area in hectares and its share of the "
            "cloud-free area. Do not explain causes. Use only the numbers given. "
            "Say 'detected' or 'likely', never 'confirmed'. "
            "No preamble.\n" + json.dumps(facts)
        )
        try:
            response = httpx.post(
                f"{self.url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "keep_alive": -1,  # stay loaded: a reload costs minutes
                    "options": {"temperature": 0, "num_predict": 80},
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            text = " ".join(response.json().get("response", "").split()).strip('"')
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Onboard model unavailable (%s); using the template.", exc)
            return plain
        allowed = [float(v) for v in facts.values() if isinstance(v, (int, float))]
        numbers = [float(match) for match in NUMBER.findall(text.replace(",", ""))]
        supported = all(
            any(abs(n - a) <= max(0.05, abs(a) * 0.005) for a in allowed)
            for n in numbers
        )
        if (
            not text
            or len(text) > 280
            or not supported
            or re.search(r"\b(confirmed|guaranteed|safe)\b", text, re.I)
        ):
            logger.warning("Onboard model verdict rejected: %r", text)
            return plain
        return text


def make_verdict():
    """VERDICT_MODE=ollama uses the onboard model; anything else the template."""
    if os.getenv("VERDICT_MODE", "template").lower() == "ollama":
        return OllamaVerdict(
            os.getenv("OLLAMA_URL", "http://127.0.0.1:11434"),
            os.getenv("OLLAMA_MODEL", "llama3.2:3b"),
        )
    return TemplateVerdict()
