"""Gemini call wrapper with model fallback on overload (503/429)."""
import asyncio
import logging

from google.genai import types

from .. import config
from ..store import cost

log = logging.getLogger(__name__)
_client = config.make_genai_client()

# When a model returns 503 / 429 / RESOURCE_EXHAUSTED, immediately retry on
# the next entry instead of waiting for the same overloaded model. The
# chain is now cyclic-ish — flash-lite escalates to flash so cheap-tier
# ingest doesn't die alone when AI Studio rate-limits flash-lite. The
# seen-set in _chain_for breaks the cycle after one pass so we never
# loop forever.
_FALLBACK_CHAIN = {
    "gemini-2.5-pro": "gemini-2.5-flash",
    "gemini-2.5-flash": "gemini-2.5-flash-lite",
    "gemini-2.5-flash-lite": "gemini-2.5-flash",
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


def _build_config(system: str, max_tokens: int, temperature: float,
                  thinking_budget: int | None, model: str):
    """Build GenerateContentConfig, disabling 'thinking' when asked.

    complete() is only ever called for single-shot extraction /
    summary / translation / rerank / keyword / audit tasks (all on
    Flash-Lite) — none of them benefit from chain-of-thought, and the
    thinking tokens are billed at the output rate. Pinning the budget
    to 0 caps that spend + latency. The agent's answer synthesis and
    /deep go through generate_content directly (not complete()), so
    they keep dynamic thinking.

    gemini-2.5-pro cannot disable thinking (min budget 128), so we skip
    the field for any pro model. If the installed SDK predates
    ThinkingConfig, fall back to a plain config so we never break the
    call."""
    base = dict(
        system_instruction=system,
        max_output_tokens=max_tokens,
        temperature=temperature,
    )
    if thinking_budget is not None and "pro" not in model:
        try:
            return types.GenerateContentConfig(
                **base,
                thinking_config=types.ThinkingConfig(
                    thinking_budget=thinking_budget),
            )
        except Exception:
            log.debug("thinking_config unsupported by SDK; plain config")
    return types.GenerateContentConfig(**base)


async def complete(
    model: str,
    system: str,
    user: str,
    max_tokens: int = 1024,
    temperature: float = 0.2,
    purpose: str = "unknown",
    thinking_budget: int | None = 0,
) -> str:
    chain = _chain_for(model)
    last_err: BaseException | None = None

    for m in chain:
        for attempt in range(3):
            try:
                # Per-call 60s timeout: without it, a network-stalled
                # generate_content can block forever and pin a
                # semaphore slot for hours (a forwarded text-only
                # message sat at '처리 중 20초' for 1h+ because the
                # underlying Gemini call had no upper bound).
                resp = await asyncio.wait_for(
                    _client.aio.models.generate_content(
                        model=m,
                        contents=user,
                        config=_build_config(
                            system, max_tokens, temperature,
                            thinking_budget, m),
                    ),
                    timeout=60,
                )
                cost.record_resp(m, resp, purpose=purpose)
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
