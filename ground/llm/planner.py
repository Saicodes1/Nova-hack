import json
from datetime import datetime
from zoneinfo import ZoneInfo

from .client import client, model
from .schemas import AnalysisPlan


async def plan_question(request):
    today = datetime.now(ZoneInfo("Asia/Dubai")).date()
    instructions = (
        f"Today is {today}. Plan a Sentinel-2 change analysis in the requested language. "
        "Select water_gain for flooding or expansion, water_loss for drying, veg_loss for "
        "deforestation, veg_gain for regrowth, urban_growth for new construction, burn for fire, "
        "or visible_change for a general visual change comparison. "
        "Honor explicit fields. Resolve relative dates. If only date B is known, use one year "
        "earlier for A. If required dates, intent, or a place without a supplied AOI cannot be inferred, "
        "ask a concise clarification. Never invent a place. Return dates as ISO date strings."
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
