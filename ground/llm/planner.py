import json
from datetime import datetime
from zoneinfo import ZoneInfo

from .client import client, model
from .schemas import AnalysisPlan


async def plan_question(request):
    today = datetime.now(ZoneInfo("Asia/Dubai")).date()
    instructions = (
        f"Today is {today}. Plan a Sentinel-2 change analysis in the requested language.\n"
        "Pick exactly one intent from what the user wants to find:\n"
        "- urban_growth: new buildings, houses, villas, towers, roads, construction, "
        "development, expansion of a city or industrial area, land being built on.\n"
        "- water_gain: flooding, storm or rain water, waterlogging, a lake, reservoir or "
        "river growing, new ponds or lagoons, reclaimed land being flooded.\n"
        "- water_loss: drought, drying, a lake or reservoir shrinking, receding water, "
        "land reclamation that replaces water.\n"
        "- veg_loss: deforestation, logging, clearing, cut trees, lost farmland or "
        "mangroves, crops harvested, vegetation dying.\n"
        "- veg_gain: greening, planting, reforestation, new farms, irrigation, parks, "
        "golf courses, vegetation recovering.\n"
        "- burn: wildfire, fire, burn scars, burned land.\n"
        "- visible_change: only when the user asks generally what changed and names "
        "no specific kind of change.\n"
        "Questions that ask for counts (for example how many buildings) map to the "
        "matching intent; the analysis reports area, not counts.\n"
        "Honor explicit fields. Resolve relative dates against today. If only date B is "
        "known, use the same day one year earlier for A so the seasons match. If required "
        "dates, intent, or a place without a supplied AOI cannot be inferred, ask a concise "
        "clarification. Never invent a place. Return dates as ISO date strings."
    )
    async with client() as llm:
        response = await llm.responses.parse(
            model=model("FAST"),
            instructions=instructions,
            input=json.dumps(request),
            text_format=AnalysisPlan,
        )
    if response.output_parsed is None:
        raise ValueError("OpenAI did not return an analysis plan.")
    return response.output_parsed
