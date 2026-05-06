from ..llm.claude import complete, cached_system
from .. import config
from ..store import meta
from . import retrieve, router

_ANSWER_SYSTEM = """You are the user's second-brain assistant in Korean.
Answer concisely and factually using ONLY the provided context blocks.
If the context is insufficient, say so honestly and suggest what to search.
Always cite source titles in brackets like [제목] at the end of relevant sentences.
Prefer information from [요약] blocks; only quote raw chunks for specifics."""

_CHITCHAT_SYSTEM = """You are a friendly Korean assistant. Keep replies short (1-2 sentences)."""

_MEMORY_SYSTEM = """You are a knowledgeable Korean assistant. Answer concisely.
If asked about specific user-saved data, defer and ask the user to confirm."""


def _format_context(hits: list[dict]) -> tuple[str, list[str]]:
    if not hits:
        return "(저장된 자료 없음)", []
    parts = []
    titles = []
    for h in hits:
        doc_id = h["metadata"]["doc_id"]
        kind = h["metadata"]["kind"]
        doc = meta.get_doc(doc_id) or {}
        title = doc.get("title", doc_id)
        if title not in titles:
            titles.append(title)
        tag = "요약" if kind == "summary" else f"청크{h['metadata']['idx']}"
        parts.append(f"[{title} | {tag}]\n{h['text']}")
    return "\n\n---\n\n".join(parts), titles


async def answer(message: str) -> dict:
    decision = await router.route(message)
    route_name = decision["route"]

    if route_name == "chitchat":
        text = await complete(
            model=config.ROUTER_MODEL,
            system=cached_system(_CHITCHAT_SYSTEM),
            messages=[{"role": "user", "content": message}],
            max_tokens=200,
        )
        return {"text": text, "route": route_name, "sources": []}

    if route_name == "memory":
        text = await complete(
            model=config.ROUTER_MODEL,
            system=cached_system(_MEMORY_SYSTEM),
            messages=[{"role": "user", "content": message}],
            max_tokens=600,
        )
        return {"text": text, "route": route_name, "sources": []}

    query = decision.get("rewrite") or message
    hits = await retrieve.hybrid(query, k=config.TOP_K)
    context, titles = _format_context(hits)
    user_block = f"질문: {message}\n\n# 관련 자료\n{context}"
    text = await complete(
        model=config.ANSWER_MODEL,
        system=cached_system(_ANSWER_SYSTEM),
        messages=[{"role": "user", "content": user_block}],
        max_tokens=1024,
    )
    return {"text": text, "route": route_name, "sources": titles}
