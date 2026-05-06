import json
from ..llm.claude import complete, cached_system
from .. import config

_SYSTEM = """You are a routing agent for a personal Korean knowledge base.
Classify the user's message into ONE of:
- "chitchat": greetings, small talk, no knowledge needed
- "memory": general world knowledge you can answer directly without retrieval
- "rag": specific question that likely needs the user's saved documents

Reply with strict JSON: {"route": "...", "rewrite": "..."}
"rewrite" is a search-friendly Korean reformulation of the query (1 short sentence)
when route is "rag", else empty string."""


async def route(message: str) -> dict:
    raw = await complete(
        model=config.ROUTER_MODEL,
        system=cached_system(_SYSTEM),
        messages=[{"role": "user", "content": message}],
        max_tokens=200,
        temperature=0,
    )
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        raw = raw.rsplit("```", 1)[0]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"route": "rag", "rewrite": message}
    if data.get("route") not in {"chitchat", "memory", "rag"}:
        data["route"] = "rag"
    return data
