"""Gemini embedding wrapper with extended retry on overload."""
import asyncio
import logging

from google import genai
from google.genai import types

from .. import config
from ..store import cost

log = logging.getLogger(__name__)
_client = genai.Client(api_key=config.GOOGLE_API_KEY)

_OVERLOAD_MARKERS = (
    "503",
    "429",
    "UNAVAILABLE",
    "RESOURCE_EXHAUSTED",
    "high demand",
)


def _is_overloaded(exc: BaseException) -> bool:
    s = f"{type(exc).__name__} {exc}"
    return any(m in s for m in _OVERLOAD_MARKERS)


_BATCH_LIMIT = 100  # Gemini embed_content hard cap per request


async def _embed_one(texts: list[str], task_type: str) -> list[list[float]]:
    last_err: BaseException | None = None
    for attempt in range(6):
        try:
            resp = await _client.aio.models.embed_content(
                model=config.EMBED_MODEL,
                contents=texts,
                config=types.EmbedContentConfig(task_type=task_type),
            )
            # embed_content rarely surfaces usage_metadata; fall back
            # to a char-based heuristic so daily cost reflects embedding
            # spend even when the API stays quiet.
            purpose = "query" if task_type == "RETRIEVAL_QUERY" else "ingest"
            usd_in = cost.record_resp(config.EMBED_MODEL, resp, purpose=purpose)
            if usd_in == 0.0:
                approx_tokens = sum(len(t) for t in texts) // 4
                cost.record(config.EMBED_MODEL, approx_tokens, 0,
                            purpose=purpose)
            return [e.values for e in resp.embeddings]
        except Exception as e:
            last_err = e
            if not _is_overloaded(e) and attempt >= 2:
                raise
            wait = min(2 ** attempt, 60) + (1 if _is_overloaded(e) else 0)
            log.warning("embed retry %d/6 in %ds: %s", attempt + 1, wait, str(e)[:120])
            await asyncio.sleep(wait)
    assert last_err is not None
    raise last_err


async def embed(texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    if not texts:
        return []
    if len(texts) <= _BATCH_LIMIT:
        return await _embed_one(texts, task_type)
    out: list[list[float]] = []
    for i in range(0, len(texts), _BATCH_LIMIT):
        batch = texts[i:i + _BATCH_LIMIT]
        out.extend(await _embed_one(batch, task_type))
    return out
