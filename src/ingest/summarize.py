import asyncio
import hashlib
import json
import logging
import os
import re

from ..llm.gemini import complete
from .. import config
from .chunker import token_len, split

log = logging.getLogger(__name__)

_SYSTEM = """You compress documents for a personal RAG knowledge base.
Output a Korean summary that preserves: key claims, named entities, numbers, dates,
methods, conclusions. Bullet points. No fluff. Keep technical terms verbatim.
Target length: 300-500 Korean characters per 1000 source tokens."""

_COMBINED_SYSTEM = (
    "You output a single JSON object with TWO fields combined in one call: "
    "(1) a Korean RAG summary preserving key claims/entities/numbers/dates "
    "in bullet form (300-500 chars per 1000 source tokens, technical terms "
    "verbatim, no fluff), and (2) structured metadata. Output JSON only."
)

_COMBINED_USER_TMPL = """제목: {title}
Type: {doc_type}

본문:
{body}

위 본문에서 아래 JSON 한 객체만 출력:
{{
  "summary": "한국어 요약, 불릿 형식, 핵심 주장·고유명사·숫자·날짜·결론 보존. 전문용어는 원어 그대로.",
  "company": "주 분석/언급 대상 회사명 한 개. 모호하면 빈 문자열. 삼성전자/삼성전기/삼성SDI 같은 계열사 구분 정확히. 산업 동향·매크로면 빈 문자열.",
  "tags": ["반도체"|"AI"|"바이오"|"방산"|"로봇"|"보고서"|"뉴스"|"실적"|"분석"|"차트"|"공시"|"인터뷰"|"리포트" 등 1~5개],
  "report_date": "본문 발행일 YYYY.MM 또는 YYYY.MM.DD 형태, 없으면 빈 문자열",
  "brokerage": "리포트 발행 증권사/연구소/언론사 이름 (NH투자증권/신한투자증권/SK증권/현대차증권/Goldman Sachs/Bloomberg 등). 본문 첫 페이지·하단 footer·작성자 라인에서 추출. 없으면 빈 문자열.",
  "analyst": "리포트 작성 애널리스트 이름 1명 (예: 김XX 또는 John Smith). 본문 첫 페이지 헤더/footer/email 라인에서 추출. 여러 명이면 첫 번째만. 없으면 빈 문자열."
}}"""


async def summarize(title: str, text: str, hint: str | None = None) -> str:
    """Summary-only path. Kept for callers that don't need metadata
    (e.g., chained final-pass on long docs).

    Same 12k single-call / 8k partial sizing as summarize_and_extract
    so behaviour stays consistent across both entry points."""
    if hint and config.HINT_SUMMARY_MIN_CHARS <= len(hint) <= config.HINT_SUMMARY_MAX_CHARS:
        return hint.strip()
    if token_len(text) <= 400:
        return text.strip()
    if token_len(text) <= 12000:
        return await _summarize_one(title, text)
    parts = split(text, size=8000, overlap=200)
    partials = await _summarize_partials(title, parts)
    combined = "\n\n".join(partials)
    if token_len(combined) <= 4000:
        return combined
    return await _summarize_one(title, combined)


def _summary_cache_key(title: str, body: str, doc_type: str,
                       hint: str | None, skip_meta: bool) -> str:
    """Stable hash of everything that determines the summarize output.
    Any change to title / body / doc_type / skip_meta / hint yields a
    different key, so a cache hit guarantees byte-identical inputs."""
    raw = "\x00".join([
        doc_type or "", "1" if skip_meta else "0",
        hint or "", title or "", body or "",
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


async def summarize_and_extract(
    title: str, body: str, doc_type: str,
    hint: str | None = None, skip_meta: bool = False,
) -> tuple[str, dict]:
    """Combined summary + metadata extraction in one Lite call.

    Replaces the previous pattern of two parallel calls (summary +
    metadata). Saves one Lite call per ingest (~₩0.5/doc) for ~50%
    of docs that take this main path.

    Content-keyed cache (cost cut, 2026-05): a retry of a doc that
    failed at the embedding stage — or a re-ingest of byte-identical
    content — reuses the previously produced summary instead of paying
    the Lite model again. Only LLM-backed results are cached;
    passthrough / hint-only paths cost nothing so they skip the cache.

    Returns (summary, metadata). Metadata is {} when body is too short,
    skip_meta is True, or the LLM extraction failed (failures swallowed
    so they never block ingest)."""
    from ..store import meta as _meta

    key = _summary_cache_key(title, body, doc_type, hint, skip_meta)
    # Both cache calls are sync SQLite and MUST stay off the loop.
    # summary_cache_remember takes meta.py's _W_LOCK, whose contract is
    # "callers already run in a to_thread worker" — calling it inline
    # from this async def broke that and froze the loop for 431s on
    # 2026-08-26 (data/loop_stalls.log caught the stack: the lock was
    # held by worker-thread fts_upsert during a bulk ingest). The _get
    # is lock-free (WAL readers) but still disk I/O on the loop.
    hit = await asyncio.to_thread(_meta.summary_cache_get, key)
    if hit is not None:
        return hit["summary"], hit["metadata"]

    summary, metadata, llm_used = await _summarize_and_extract_impl(
        title, body, doc_type, hint, skip_meta)
    if llm_used:
        await asyncio.to_thread(
            _meta.summary_cache_remember, key, summary, metadata)
    return summary, metadata


async def _summarize_and_extract_impl(
    title: str, body: str, doc_type: str,
    hint: str | None, skip_meta: bool,
) -> tuple[str, dict, bool]:
    """Core summarize logic. Returns (summary, metadata, llm_used);
    llm_used is False for the no-cost passthrough / hint-only paths so
    the wrapper knows not to cache them."""
    # Hint fallback — free summary, still need metadata
    if hint and config.HINT_SUMMARY_MIN_CHARS <= len(hint) <= config.HINT_SUMMARY_MAX_CHARS:
        summary = hint.strip()
        if skip_meta or len(body) < 100:
            return summary, {}, False
        return summary, await _extract_metadata_only(title, body, doc_type), True

    # Body too short — passthrough, no LLM
    if token_len(body) <= 400:
        return body.strip(), {}, False

    # Caller said meta is noise (short forwarded text etc.) — summary only
    if skip_meta:
        return await summarize(title, body), {}, True

    # Main path: single combined Lite call.
    # Threshold raised 6000 → 12000 tokens (2026-05). Flash-Lite handles
    # 12k-token inputs in one shot fine and the per-call output cost
    # dominates input, so a single 12k call is cheaper than three
    # chained 4k calls + a final combine. 60-75% Flash-Lite call
    # reduction on medium-long PDFs.
    if token_len(body) <= 12000:
        summary, metadata = await _combined_call(title, body, doc_type)
        return summary, metadata, True

    # Long doc — chain summarize first (multiple calls), then combine
    # the final pass with metadata. Partial size raised 4000 → 8000 so
    # a 100k-token PDF needs ~13 partials instead of 25 — same content
    # coverage, half the calls.
    parts = split(body, size=8000, overlap=200)
    partials = await _summarize_partials(title, parts)
    combined_text = "\n\n".join(partials)
    if token_len(combined_text) <= 12000:
        summary, metadata = await _combined_call(title, combined_text, doc_type)
        return summary, metadata, True
    # Very long after chain — fall back to old behaviour (rare)
    final_summary = await _summarize_one(title, combined_text)
    return final_summary, await _extract_metadata_only(title, body, doc_type), True


async def _summarize_one(title: str, text: str,
                         max_tokens: int | None = None) -> str:
    """max_tokens defaults to the FINAL summary cap. Partial passes
    pass the smaller SUMMARY_PARTIAL_MAX_TOKENS — see the note on that
    setting in config.py for why the two must not share one value."""
    return await complete(
        model=config.SUMMARY_MODEL,
        system=_SYSTEM,
        user=f"제목: {title}\n\n본문:\n{text}",
        max_tokens=max_tokens or config.SUMMARY_MAX_TOKENS,
        temperature=0.1,
        purpose="ingest",
    )


# Bound partial-summary parallelism GLOBALLY so a long doc (12+ 8k
# partials) — or several at once — can't fan out into a Gemini
# rate-limit flood. Shared across all concurrent ingests.
#
# 6 → 10 (2026-08-31). This waits on Gemini, it does not use the CPU,
# and 6 was chosen while the local reranker was pinning both cores; the
# box now idles at 16%. A 102-chunk PDF splits into 13 partials, so 6
# meant three waves and 10 means two. The cap still exists for the
# rate limit, which is the only reason it was ever 6 — env-tunable
# because a 429 storm is the failure mode to back out of quickly, and
# complete()'s fallback chain absorbs it slowly rather than loudly.
_PARTIAL_SUMMARY_SEM = asyncio.Semaphore(
    max(1, int(os.getenv("PARTIAL_SUMMARY_CONCURRENCY", "10"))))


async def _summarize_partials(title: str, parts: list[str]) -> list[str]:
    """Summarize each chunk concurrently (was sequential — a 12-partial
    PDF paid ~12×call latency in series). gather preserves input order,
    so the downstream join keeps document order. Same call count → same
    cost; identical per-partial output → same quality. The semaphore
    caps concurrent Gemini calls so it stays safe under load."""
    async def _one(p: str) -> str:
        async with _PARTIAL_SUMMARY_SEM:
            return await _summarize_one(
                title, p, max_tokens=config.SUMMARY_PARTIAL_MAX_TOKENS)
    return list(await asyncio.gather(*[_one(p) for p in parts]))


async def _combined_call(title: str, body: str, doc_type: str) -> tuple[str, dict]:
    """One Lite call returning {summary, company, tags, report_date}.
    Falls back to summary-only on JSON parse failure."""
    try:
        resp = await complete(
            model=config.SUMMARY_MODEL,
            system=_COMBINED_SYSTEM,
            user=_COMBINED_USER_TMPL.format(
                title=title, doc_type=doc_type, body=body,
            ),
            max_tokens=config.SUMMARY_MAX_TOKENS + 300,
            temperature=0.1,
            purpose="ingest",
        )
        match = re.search(r"\{.*\}", resp, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            summary = (data.get("summary") or "").strip()
            tags_raw = data.get("tags") or []
            if not isinstance(tags_raw, list):
                tags_raw = []
            metadata = {
                "company": (data.get("company") or "").strip() or None,
                "tags": [t.strip() for t in tags_raw
                         if isinstance(t, str) and t.strip()][:8],
                "report_date": (data.get("report_date") or "").strip() or None,
                "brokerage": (data.get("brokerage") or "").strip() or None,
                "analyst": (data.get("analyst") or "").strip() or None,
            }
            if summary:
                return summary, metadata
    except Exception as e:
        log.warning("combined summarize+extract failed: %s", e)
    # Fallback — single old-style summarize call
    return await _summarize_one(title, body), {}


async def _extract_metadata_only(title: str, body: str, doc_type: str) -> dict:
    """Metadata-only path used when summary came from hint."""
    sample = (body[:1500] + "\n...\n" + body[-300:]) if len(body) > 2000 else body
    user_msg = (
        "Output JSON only:\n"
        '{"company": "주 분석/언급 대상 회사명 한 개 (모호하면 빈 문자열)",\n'
        ' "tags": ["반도체"|"AI"|"바이오"|"방산"|"로봇"|"보고서"|"뉴스"|"실적"|'
        '"분석"|"차트"|"공시"|"인터뷰"|"리포트" 등 1~5개],\n'
        ' "report_date": "본문 발행일 YYYY.MM 또는 YYYY.MM.DD, 없으면 빈 문자열",\n'
        ' "brokerage": "발행 증권사/연구소/언론사 (예: NH투자증권, Goldman Sachs), 없으면 빈 문자열",\n'
        ' "analyst": "리포트 작성 애널리스트 1명 (본문 첫 페이지/footer/email 라인), 없으면 빈 문자열"}\n\n'
        f"Title: {title}\nType: {doc_type}\nBody:\n{sample}"
    )
    try:
        resp = await complete(
            model=config.SUMMARY_MODEL,
            system="Output JSON only.",
            user=user_msg,
            max_tokens=300,
            temperature=0.0,
            purpose="ingest",
        )
        match = re.search(r"\{.*\}", resp, re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            tags_raw = data.get("tags") or []
            if not isinstance(tags_raw, list):
                tags_raw = []
            return {
                "company": (data.get("company") or "").strip() or None,
                "tags": [t.strip() for t in tags_raw
                         if isinstance(t, str) and t.strip()][:8],
                "report_date": (data.get("report_date") or "").strip() or None,
                "brokerage": (data.get("brokerage") or "").strip() or None,
                "analyst": (data.get("analyst") or "").strip() or None,
            }
    except Exception as e:
        log.warning("metadata extract failed: %s", e)
    return {}
