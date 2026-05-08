"""Gemini call wrapper with model fallback on overload (503/429)."""
import asyncio
import logging

from google import genai
from google.genai import types

from .. import config
from ..store import cost

log = logging.getLogger(__name__)
_client = genai.Client(api_key=config.GOOGLE_API_KEY)

# When a model returns 503 / 429 / RESOURCE_EXHAUSTED, immediately retry on
# the next entry instead of waiting for the same overloaded model.
_FALLBACK_CHAIN = {
    "gemini-2.5-pro": "gemini-2.5-flash",
    "gemini-2.5-flash": "gemini-2.5-flash-lite",
    "gemini-2.5-flash-lite": None,
}

_OVERLOAD_MARKERS = (
    "503",
    "429",
    "UNAVAILABLE",
    "RESOURCE_EXHAUSTED",
    "high demand",
    "overloaded",
)


def _is_overloaded(exc: BaseException) -> bool:
    s = f"{type(exc).__name__} {exc}"
    return any(m in s for m in _OVERLOAD_MARKERS)


def _chain_for(model: str) -> list[str]:
    chain = [model]
    seen = {model}
    cur = _FALLBACK_CHAIN.get(model)
    while cur and cur not in seen:
        chain.append(cur)
        seen.add(cur)
        cur = _FALLBACK_CHAIN.get(cur)
    return chain


async def complete(
    model: str,
    system: str,
    user: str,
    max_tokens: int = 1024,
    temperature: float = 0.2,
) -> str:
    chain = _chain_for(model)
    last_err: BaseException | None = None

    for m in chain:
        for attempt in range(3):
            try:
                resp = await _client.aio.models.generate_content(
                    model=m,
                    contents=user,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        max_output_tokens=max_tokens,
                        temperature=temperature,
                    ),
                )
                cost.record_resp(m, resp)
                if m != model:
                    log.info("served by fallback model: %s", m)
                return resp.text or ""
            except Exception as e:
                last_err = e
                if _is_overloaded(e):
                    log.warning("model %s overloaded (%s); switching", m,
                                str(e)[:120])
                    break  # try next model in chain immediately
                if attempt < 2:
                    await asyncio.sleep(min(2 ** attempt, 5))
                    continue
                raise

    assert last_err is not None
    raise last_err
