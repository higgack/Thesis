import json
import logging
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
  "report_date": "본문 발행일 YYYY.MM 형태, 없으면 빈 문자열"
}}"""


async def summarize(title: str, text: str, hint: str | None = None) -> str:
    """Summary-only path. Kept for callers that don't need metadata
    (e.g., chained final-pass on long docs)."""
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


async def summarize_and_extract(
    title: str, body: str, doc_type: str,
    hint: str | None = None, skip_meta: bool = False,
) -> tuple[str, dict]:
    """Combined summary + metadata extraction in one Lite call.

    Replaces the previous pattern of two parallel calls (summary +
    metadata). Saves one Lite call per ingest (~₩0.5/doc) for ~50%
    of docs that take this main path.

    Returns (summary, metadata). Metadata is {} when body is too short,
    skip_meta is True, or the LLM extraction failed (failures swallowed
    so they never block ingest)."""
    # Hint fallback — free summary, still need metadata
    if hint and config.HINT_SUMMARY_MIN_CHARS <= len(hint) <= config.HINT_SUMMARY_MAX_CHARS:
        summary = hint.strip()
        if skip_meta or len(body) < 100:
            return summary, {}
        return summary, await _extract_metadata_only(title, body, doc_type)

    # Body too short — passthrough, no LLM
    if token_len(body) <= 400:
        return body.strip(), {}

    # Caller said meta is noise (short forwarded text etc.) — summary only
    if skip_meta:
        return await summarize(title, body), {}

    # Main path: single combined Lite call
    if token_len(body) <= 6000:
        return await _combined_call(title, body, doc_type)

    # Long doc — chain summarize first (multiple calls), then combine
    # the final pass with metadata.
    parts = split(body, size=4000, overlap=200)
    partials = [await _summarize_one(title, p) for p in parts]
    combined_text = "\n\n".join(partials)
    if token_len(combined_text) <= 6000:
        return await _combined_call(title, combined_text, doc_type)
    # Very long after chain — fall back to old behaviour (rare)
    final_summary = await _summarize_one(title, combined_text)
    return final_summary, await _extract_metadata_only(title, body, doc_type)


async def _summarize_one(title: str, text: str) -> str:
    return await complete(
        model=config.SUMMARY_MODEL,
        system=_SYSTEM,
        user=f"제목: {title}\n\n본문:\n{text}",
        max_tokens=config.SUMMARY_MAX_TOKENS,
        temperature=0.1,
        purpose="ingest",
    )


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
        ' "report_date": "본문 발행일 YYYY.MM 형태, 없으면 빈 문자열"}\n\n'
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
            }
    except Exception as e:
        log.warning("metadata extract failed: %s", e)
    return {}
