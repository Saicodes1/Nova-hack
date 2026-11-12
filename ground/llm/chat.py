import json

from fastapi import HTTPException
from openai import OpenAIError
from pydantic import ValidationError

from .client import client, configured, model
from .explainer import RULES, evidence, template
from .guard import check


def function(name, description, properties):
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


TOOLS = [
    function(
        "get_next_clear_pass", "Get the next predicted pass with low cloud cover.", {}
    ),
    function(
        "explain_point",
        "Read measured indices for a user-specified coordinate.",
        {"lon": {"type": "number"}, "lat": {"type": "number"}},
    ),
    function(
        "run_analysis",
        "Start another analysis for the same area when the user requests it.",
        {
            "date_a": {"type": "string"},
            "date_b": {"type": "string"},
            "intent": {
                "type": "string",
                "enum": [
                    "water_gain",
                    "water_loss",
                    "veg_gain",
                    "veg_loss",
                    "urban_growth",
                    "visible_change",
                    "burn",
                ],
            },
        },
    ),
]


async def reply(packet, previous, request, run_tool):
    fallback = template(packet, request.language)
    baseline = {"reply": f"{fallback.headline} {fallback.body}", "numbers_ok": True}
    if not configured() or any(scene.source == "synthetic" for scene in packet.scenes):
        return baseline
    payload = evidence(packet)
    outcomes, followup = [], None
    system = (
        RULES + " Forecasts are predictions. Never include an analysis ID in the reply."
    )
    try:
        async with client() as llm:
            response = await llm.responses.create(
                model=model("MAIN"),
                instructions=system,
                input=json.dumps(
                    {
                        "packet": payload,
                        "previous": previous.model_dump(),
                        "message": request.message,
                        "language": request.language,
                    }
                ),
                tools=TOOLS,
                parallel_tool_calls=False,
            )
            calls = [item for item in response.output if item.type == "function_call"]
            if calls:
                outputs = []
                for call in calls[:1]:
                    try:
                        result = await run_tool(call.name, json.loads(call.arguments))
                    except (
                        KeyError,
                        ValueError,
                        ValidationError,
                        RuntimeError,
                        HTTPException,
                    ):
                        result = {"message": "That evidence is unavailable."}
                    followup = result.get("analysis_id")
                    outcomes.append(result)
                    outputs.append(
                        {
                            "type": "function_call_output",
                            "call_id": call.call_id,
                            "output": json.dumps(result),
                        }
                    )
                response = await llm.responses.create(
                    model=model("MAIN"),
                    instructions=system,
                    previous_response_id=response.id,
                    input=outputs,
                )
            if response.output_text and not check(
                response.output_text, dict(payload, tool_outputs=outcomes)
            ):
                return {
                    "reply": response.output_text,
                    "numbers_ok": True,
                    "new_analysis_id": followup,
                }
    except OpenAIError:
        pass
    return dict(baseline, new_analysis_id=followup)
