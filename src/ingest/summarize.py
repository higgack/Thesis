from ..llm.claude import complete, cached_system
from .. import config
from .chunker import token_len, split

_SYSTEM = """You compress documents for a personal RAG knowledge base.
Output a Korean summary that preserves: key claims, named entities, numbers, dates,
methods, conclusions. Bullet points. No fluff. Keep technical terms verbatim.
Target length: 300-500 Korean characters per 1000 source tokens."""


async def summarize(title: str, text: str) -> str:
    if token_len(text) <= 400:
        return text.strip()
    if token_len(text) <= 6000:
        return await _summarize_one(title, text)
    parts = split(text, size=4000, overlap=200)
    partials = []
    for p in parts:
        partials.append(await _summarize_one(title, p))
    combined = "\n\n".join(partials)
    if token_len(combined) <= 2000:
        return combined
    return await _summarize_one(title, combined)


async def _summarize_one(title: str, text: str) -> str:
    return await complete(
        model=config.ROUTER_MODEL,
        system=cached_system(_SYSTEM),
        messages=[{"role": "user", "content": f"제목: {title}\n\n본문:\n{text}"}],
        max_tokens=config.SUMMARY_MAX_TOKENS,
        temperature=0.1,
    )
