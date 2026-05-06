from ..llm.gemini import complete
from .. import config
from .chunker import token_len, split

_SYSTEM = """You compress documents for a personal RAG knowledge base.
Output a Korean summary that preserves: key claims, named entities, numbers, dates,
methods, conclusions. Bullet points. No fluff. Keep technical terms verbatim.
Target length: 300-500 Korean characters per 1000 source tokens."""


async def summarize(title: str, text: str, hint: str | None = None) -> str:
    if hint and config.HINT_SUMMARY_MIN_CHARS <= len(hint) <= config.HINT_SUMMARY_MAX_CHARS:
        return hint.strip()
    if token_len(text) <= 400:
        return text.strip()
    if token_len(text) <= 6000:
        return await _summarize_one(title, text)
    parts = split(text, size=4000, overlap=200)
    partials = [await _summarize_one(title, p) for p in parts]
    combined = "\n\n".join(partials)
    if token_len(combined) <= 2000:
        return combined
    return await _summarize_one(title, combined)


async def _summarize_one(title: str, text: str) -> str:
    return await complete(
        model=config.SUMMARY_MODEL,
        system=_SYSTEM,
        user=f"제목: {title}\n\n본문:\n{text}",
        max_tokens=config.SUMMARY_MAX_TOKENS,
        temperature=0.1,
    )
