from ..llm.gemini import complete
from .. import config
from ..store import meta
from . import retrieve

_ANSWER_SYSTEM = """You are the user's second-brain assistant in Korean.
Answer concisely and factually using ONLY the provided context blocks.
If the context is insufficient, say so honestly and suggest what to search.
Always cite source titles in brackets like [제목] at the end of relevant sentences.
Prefer information from [요약] blocks; only quote raw chunks for specifics."""


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


async def answer(message: str, deep: bool = False) -> dict:
    hits = await retrieve.hybrid(message, k=config.TOP_K)
    context, titles = _format_context(hits)
    user_block = f"질문: {message}\n\n# 관련 자료\n{context}"
    model = config.DEEP_MODEL if deep else config.ANSWER_MODEL
    text = await complete(
        model=model,
        system=_ANSWER_SYSTEM,
        user=user_block,
        max_tokens=1024 if not deep else 2048,
    )
    return {"text": text, "model": model, "sources": titles}
