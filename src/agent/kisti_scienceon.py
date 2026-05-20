"""KISTI ScienceON Open API client (vendored, adapted from
ansua79/kisti-mcp v0.3.12b0 — CC-BY-NC-4.0).

ScienceON is the Korean Institute of Science and Technology
Information's unified science portal. Through this client we expose:
  • Korean papers (SCIE / SCOPUS / KSCI 99%+ coverage) by keyword
    and by CN (control number) detail lookup.
  • Korean + foreign patent records indexed by KISTI by keyword,
    CN detail, and citation network.
  • R&D reports (national R&D project deliverables) by keyword and
    CN detail.

Auth flow (different from KIPRIS, more complex):
  1. POST {datetime, mac_address} encrypted with AES-CBC using
     SCIENCEON_API_KEY → token request URL.
  2. GET that URL with SCIENCEON_CLIENT_ID → access_token (JSON).
  3. Use access_token + client_id on /openapicall.do (XML response).

Three env vars are required (all from
https://scienceon.kisti.re.kr/por/oapi/openApi.do):
  • SCIENCEON_API_KEY     — AES key
  • SCIENCEON_CLIENT_ID   — request URL identifier
  • SCIENCEON_MAC_ADDRESS — bound to the registered MAC at signup
                            (use the VM's MAC; if you redeploy to
                            a different VM you'll need to re-register).

All public methods return [] / None on missing keys or backend
failure so callers can render graceful empty messages instead of
crashing the bot.
"""
import asyncio
import base64
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

_SCIENCEON_BASE = "https://apigateway.kisti.re.kr"

# In-process token cache. ScienceON tokens are ~30 min lived; we
# cache for 25 min (5-min buffer for in-flight requests). Lazy
# asyncio.Lock init since module import may run before any event
# loop exists (test paths, etc.). Single-process bot → in-memory
# cache is enough; no Redis.
_SCIENCEON_TOKEN_CACHE: dict = {"token": None, "expires_at": 0.0}
_SCIENCEON_TOKEN_TTL_SECONDS = 25 * 60  # 25 min
_SCIENCEON_TOKEN_LOCK: "asyncio.Lock | None" = None


def _scienceon_token_lock() -> "asyncio.Lock":
    global _SCIENCEON_TOKEN_LOCK
    if _SCIENCEON_TOKEN_LOCK is None:
        _SCIENCEON_TOKEN_LOCK = asyncio.Lock()
    return _SCIENCEON_TOKEN_LOCK
_AES_IV = "jvHJ1EFA0IXBrxxz"  # fixed IV defined by ScienceON spec


def _aes_encrypt(plain_txt: str, key: str) -> str:
    """AES-CBC encrypt with PKCS#7 padding, base64-urlsafe encode,
    URL-quote. Matches the ScienceON spec exactly (ported from
    upstream AESTestClass)."""
    from Crypto.Cipher import AES
    block_size = 16
    pad_len = block_size - len(plain_txt) % block_size
    padded = plain_txt + (chr(pad_len) * pad_len)
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC,
                     _AES_IV.encode("utf-8"))
    encrypted = cipher.encrypt(padded.encode("utf-8"))
    return quote(base64.urlsafe_b64encode(encrypted).decode("utf-8"))


def _have_credentials() -> bool:
    return bool(
        os.getenv("SCIENCEON_API_KEY", "").strip()
        and os.getenv("SCIENCEON_CLIENT_ID", "").strip()
        and os.getenv("SCIENCEON_MAC_ADDRESS", "").strip()
    )


async def _get_token(http: httpx.AsyncClient) -> str | None:
    """Get a cached or fresh ScienceON access_token via the
    encrypted-payload tokenrequest.do flow.

    Caching: tokens last ~30 min per ScienceON spec; we cache for
    25 min (5-min safety buffer for in-flight requests). asyncio
    lock prevents a thundering herd of token requests when multiple
    concurrent searches arrive at once.

    Returns None on missing creds / encrypt failure / HTTP error
    (callers fall back to empty results so the bot doesn't crash)."""
    api_key = os.getenv("SCIENCEON_API_KEY", "").strip()
    client_id = os.getenv("SCIENCEON_CLIENT_ID", "").strip()
    mac = os.getenv("SCIENCEON_MAC_ADDRESS", "").strip()
    if not (api_key and client_id and mac):
        return None
    async with _scienceon_token_lock():
        now = time.time()
        cached = _SCIENCEON_TOKEN_CACHE.get("token")
        if cached and _SCIENCEON_TOKEN_CACHE["expires_at"] > now + 60:
            return cached
        # ScienceON datetime payload: digits only from current timestamp
        # ("2026-05-18 12:34:56" → "20260518123456"), encrypted into the
        # `accounts` URL param.
        time_str = "".join(re.findall(r"\d", datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S")))
        plain = json.dumps(
            {"datetime": time_str, "mac_address": mac},
            separators=(",", ":"),
        )
        try:
            encrypted = _aes_encrypt(plain, api_key)
        except Exception as e:
            log.warning("scienceon AES encrypt failed: %s", e)
            return None
        url = (f"{_SCIENCEON_BASE}/tokenrequest.do?"
               f"client_id={client_id}&accounts={encrypted}")
        try:
            r = await http.get(url)
            if r.status_code != 200:
                log.warning("scienceon token %d: %r",
                            r.status_code, r.text[:200])
                return None
            try:
                data = r.json()
            except json.JSONDecodeError:
                log.warning("scienceon token JSON decode failed: %r",
                            r.text[:200])
                return None
        except Exception as e:
            log.warning("scienceon token fetch failed: %s", e)
            return None
        token = data.get("access_token")
        if not token:
            return None
        _SCIENCEON_TOKEN_CACHE["token"] = token
        _SCIENCEON_TOKEN_CACHE["expires_at"] = now + _SCIENCEON_TOKEN_TTL_SECONDS
        return token


def _parse_xml(text: str) -> ET.Element | None:
    """Return the XML root element, or None if the response is an
    error envelope. ScienceON wraps errors in a statusCode block we
    skip rather than parsing into result lists."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        log.warning("scienceon XML parse failed: %s", e)
        return None
    status = root.find(".//statusCode")
    if status is not None and (status.text or "").strip() != "200":
        msg = root.find(".//errorMessage")
        log.warning("scienceon error %s: %r",
                    status.text,
                    (msg.text or "")[:200] if msg is not None else "")
        return None
    return root


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RUN_RE = re.compile(r"[\s\xa0]+")
# ScienceON reports wrap LaTeX commands in <TEX>$ command $</TEX>
# blocks: ○ as <TEX>$ circ$</TEX>, · as <TEX>$ cdot$</TEX>, □ as
# <TEX>$ square$</TEX> / <TEX>$ Box$</TEX>. The generic tag stripper
# would leave "$ circ$" residue in the visible text, so handle these
# explicitly first.
_TEX_BLOCK_RE = re.compile(r"<TEX>\$\s*([A-Za-z]+)?\s*\$</TEX>")
_TEX_COMMAND_TO_UNICODE = {
    "circ": "○", "cdot": "·", "square": "□", "Box": "□",
    "bullet": "•", "ast": "*", "times": "×", "div": "÷",
    "pm": "±", "leq": "≤", "geq": "≥", "neq": "≠",
    "approx": "≈", "infty": "∞",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "mu": "µ", "pi": "π", "sigma": "σ", "omega": "ω",
    "Delta": "Δ", "Omega": "Ω",
    "rightarrow": "→", "leftarrow": "←",
    "Rightarrow": "⇒", "Leftarrow": "⇐",
}


def _replace_tex(match: "re.Match[str]") -> str:
    cmd = (match.group(1) or "").strip()
    return _TEX_COMMAND_TO_UNICODE.get(cmd, "")


def _clean_field(text: str) -> str:
    """Decode HTML entities, replace LaTeX commands, and strip
    leftover tags / control chars from a ScienceON metaCode value.

    Order matters:
      1. Unescape HTML entities (twice — the double-encoded
         &amp;#xD; needs two passes).
      2. Replace <TEX>$ command $</TEX> blocks with Unicode symbols
         (○ · □ etc.) since the generic tag stripper would otherwise
         leave bare LaTeX command names ('$ circ$') visible.
      3. Strip remaining tags (<P>, <SUB>, <sup>, <jats:p>, ...).
      4. Collapse runs of whitespace (including \\r left by &#xD;
         and \\xa0 from &nbsp;) into single spaces.
    """
    if not text:
        return ""
    import html as _html
    s = _html.unescape(_html.unescape(text))
    s = _TEX_BLOCK_RE.sub(_replace_tex, s)
    s = _HTML_TAG_RE.sub("", s)
    s = _WHITESPACE_RUN_RE.sub(" ", s)
    return s.strip()


def _record_to_dict(rec: ET.Element) -> dict[str, str]:
    """Each ScienceON record is <record><item metaCode='Title'>...
    </item> × N</record>. Flatten the metaCode-keyed items into a
    dict so callers can pull fields like Title / Author / Abstract /
    JournalName / DOI / etc.

    Two cleanups happen here so every consumer (bot formatter, agent
    tools, future MCP) gets sanitized data:
      1. itertext() (not .text) captures descendant text past inline
         tags like <span class="search_word">.
      2. _clean_field() decodes HTML entities (twice for
         double-encoded ones like &amp;#xD;) and strips leftover
         tags like <P>, <jats:p>, <SUB>, <sup>.
    """
    out: dict[str, str] = {}
    for item in rec.findall("item"):
        code = (item.get("metaCode") or "").strip()
        if not code:
            continue
        text = "".join(item.itertext())
        out[code] = _clean_field(text)
    return out


async def _call(http: httpx.AsyncClient, action: str, target: str,
                token: str, client_id: str,
                extra_params: dict[str, str]) -> list[dict[str, str]]:
    """Generic /openapicall.do request → list of flat record dicts."""
    params = {
        "client_id": client_id,
        "token": token,
        "version": "1.0",
        "action": action,
        "target": target,
    }
    params.update(extra_params)
    try:
        r = await http.get(f"{_SCIENCEON_BASE}/openapicall.do",
                           params=params)
        if r.status_code != 200:
            log.warning("scienceon %s/%s %d: %r",
                        action, target, r.status_code, r.text[:200])
            return []
        root = _parse_xml(r.text)
        if root is None:
            return []
        return [_record_to_dict(rec) for rec in root.findall(".//record")]
    except Exception as e:
        log.warning("scienceon %s/%s failed: %s", action, target, e)
        return []


# ---------------------------------------------------------------------------
# Public entry points — each returns [] / None on missing creds or
# backend failure so the bot never crashes on KISTI hiccups.
# ---------------------------------------------------------------------------

async def search_papers(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Korean / international paper search via ScienceON (target=ARTI).
    Returns a list of flat dicts with metaCode keys (TI/AU/AB/CN/DOI
    etc.). Empty list on auth failure."""
    if not _have_credentials():
        log.info("scienceon: no credentials set, skipping paper search")
        return []
    async with httpx.AsyncClient(timeout=30.0) as http:
        token = await _get_token(http)
        if not token:
            return []
        client_id = os.getenv("SCIENCEON_CLIENT_ID", "").strip()
        search_query = json.dumps({"BI": query}, ensure_ascii=False)
        return await _call(http, "search", "ARTI", token, client_id, {
            "searchQuery": search_query,
            "curPage": "1",
            "rowCount": str(min(max(1, limit), 100)),
        })


async def get_paper_detail(cn: str) -> dict[str, Any] | None:
    """Paper detail by CN (control number from search results)."""
    if not _have_credentials() or not cn:
        return None
    async with httpx.AsyncClient(timeout=30.0) as http:
        token = await _get_token(http)
        if not token:
            return None
        client_id = os.getenv("SCIENCEON_CLIENT_ID", "").strip()
        rows = await _call(http, "browse", "ARTI", token, client_id, {
            "cn": cn,
        })
        return rows[0] if rows else None


async def search_patents(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Patent search via ScienceON (target=PATENT). Returns
    international patent records indexed by KISTI — different
    coverage than KIPRIS which is KR-only and applicant-only."""
    if not _have_credentials():
        return []
    async with httpx.AsyncClient(timeout=30.0) as http:
        token = await _get_token(http)
        if not token:
            return []
        client_id = os.getenv("SCIENCEON_CLIENT_ID", "").strip()
        search_query = json.dumps({"BI": query}, ensure_ascii=False)
        return await _call(http, "search", "PATENT", token, client_id, {
            "searchQuery": search_query,
            "curPage": "1",
            "rowCount": str(min(max(1, limit), 100)),
        })


async def get_patent_detail(cn: str) -> dict[str, Any] | None:
    if not _have_credentials() or not cn:
        return None
    async with httpx.AsyncClient(timeout=30.0) as http:
        token = await _get_token(http)
        if not token:
            return None
        client_id = os.getenv("SCIENCEON_CLIENT_ID", "").strip()
        rows = await _call(http, "browse", "PATENT", token, client_id, {
            "cn": cn,
        })
        return rows[0] if rows else None


async def get_patent_citations(cn: str) -> list[dict[str, Any]]:
    """Citing / cited patents for a given CN. ScienceON returns
    forward + backward citation rows in one response."""
    if not _have_credentials() or not cn:
        return []
    async with httpx.AsyncClient(timeout=30.0) as http:
        token = await _get_token(http)
        if not token:
            return []
        client_id = os.getenv("SCIENCEON_CLIENT_ID", "").strip()
        return await _call(http, "citation", "PATENT", token, client_id, {
            "cn": cn,
        })


async def search_reports(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """R&D report search via ScienceON (target=REPORT). National
    R&D project deliverables indexed by KISTI."""
    if not _have_credentials():
        return []
    async with httpx.AsyncClient(timeout=30.0) as http:
        token = await _get_token(http)
        if not token:
            return []
        client_id = os.getenv("SCIENCEON_CLIENT_ID", "").strip()
        search_query = json.dumps({"BI": query}, ensure_ascii=False)
        return await _call(http, "search", "REPORT", token, client_id, {
            "searchQuery": search_query,
            "curPage": "1",
            "rowCount": str(min(max(1, limit), 100)),
        })


async def get_report_detail(cn: str) -> dict[str, Any] | None:
    if not _have_credentials() or not cn:
        return None
    async with httpx.AsyncClient(timeout=30.0) as http:
        token = await _get_token(http)
        if not token:
            return None
        client_id = os.getenv("SCIENCEON_CLIENT_ID", "").strip()
        rows = await _call(http, "browse", "REPORT", token, client_id, {
            "cn": cn,
        })
        return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Additional ScienceON contents — same /openapicall.do action=search/
# browse pattern, just a different `target` API ticket per content type.
# All 6 require their own 활용신청 approval on the ScienceON portal
# (user activated all 9 contents at registration time, so these all
# work with the existing SCIENCEON_API_KEY + CLIENT_ID + MAC).
#
# Naming follows the upstream API ticket convention:
#   ATT        해외과학기술동향 (overseas tech trends)
#   SCENT      과학향기 (science magazine column)
#   RESEARCHER 식별된 연구자 인덱스 (research authority)
#   ORGAN      식별된 연구기관 인덱스
#   TREND      ScienceON Trend (curated topic trend)
#   SNEWS      금주의 과학기술뉴스
# ---------------------------------------------------------------------------


async def _scienceon_search(target: str, query: str,
                            limit: int) -> list[dict[str, Any]]:
    """Internal generic search across any ScienceON target. All 9
    content tickets use the same {"BI": query} searchQuery shape, so
    a single helper covers them all — per-content wrappers below just
    pass the right target string."""
    if not _have_credentials():
        return []
    async with httpx.AsyncClient(timeout=30.0) as http:
        token = await _get_token(http)
        if not token:
            return []
        client_id = os.getenv("SCIENCEON_CLIENT_ID", "").strip()
        search_query = json.dumps({"BI": query}, ensure_ascii=False)
        return await _call(http, "search", target, token, client_id, {
            "searchQuery": search_query,
            "curPage": "1",
            "rowCount": str(min(max(1, limit), 100)),
        })


async def _scienceon_browse(target: str,
                            cn: str) -> dict[str, Any] | None:
    """Generic browse-by-CN for any target."""
    if not _have_credentials() or not cn:
        return None
    async with httpx.AsyncClient(timeout=30.0) as http:
        token = await _get_token(http)
        if not token:
            return None
        client_id = os.getenv("SCIENCEON_CLIENT_ID", "").strip()
        rows = await _call(http, "browse", target, token, client_id, {
            "cn": cn,
        })
        return rows[0] if rows else None


async def search_trends(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """ATT — 해외과학기술동향 검색. Overseas S&T trend articles
    indexed by KISTI (NTIS 과학기술 정책동향 different from this)."""
    return await _scienceon_search("ATT", query, limit)


async def get_trend_detail(cn: str) -> dict[str, Any] | None:
    return await _scienceon_browse("ATT", cn)


async def search_science_columns(query: str,
                                 limit: int = 10) -> list[dict[str, Any]]:
    """SCENT — 과학향기 칼럼 검색. Science popularization magazine
    articles (column / common-sense pieces). Less research-grade than
    ARTI/REPORT, useful for context / background reading."""
    return await _scienceon_search("SCENT", query, limit)


async def get_science_column_detail(cn: str) -> dict[str, Any] | None:
    return await _scienceon_browse("SCENT", cn)


async def search_researchers(query: str,
                             limit: int = 10) -> list[dict[str, Any]]:
    """RESEARCHER — 국내 식별 연구자 인덱스 검색. Returns researcher
    profile rows (보통 이름·소속·연구분야 + 그 연구자의 논문/보고서/
    특허 목록 링크). Different from /search_papers's author field —
    this is an explicit researcher-identity index."""
    return await _scienceon_search("RESEARCHER", query, limit)


async def get_researcher_detail(cn: str) -> dict[str, Any] | None:
    return await _scienceon_browse("RESEARCHER", cn)


async def search_organs(query: str,
                        limit: int = 10) -> list[dict[str, Any]]:
    """ORGAN — 국내 식별 연구기관 인덱스 검색. Returns institution
    profile rows + that institution's publications. 회사 분석 시
    KIPRIS 출원인 검색과 조합하면 더 풍부."""
    return await _scienceon_search("ORGAN", query, limit)


async def get_organ_detail(cn: str) -> dict[str, Any] | None:
    return await _scienceon_browse("ORGAN", cn)


async def search_science_trends(query: str,
                                limit: int = 10) -> list[dict[str, Any]]:
    """TREND — ScienceON Trend 검색. KISTI 큐레이션 토픽별 트렌드
    리포트 (논문/특허 통계 + 전문가 해설)."""
    return await _scienceon_search("TREND", query, limit)


async def get_science_trend_detail(cn: str) -> dict[str, Any] | None:
    return await _scienceon_browse("TREND", cn)


async def search_science_news(query: str,
                              limit: int = 10) -> list[dict[str, Any]]:
    """SNEWS — 금주의 과학기술뉴스 검색. 주차별/월별 큐레이션 국내외
    과기 뉴스 (Naver/언론사 원본 링크 포함)."""
    return await _scienceon_search("SNEWS", query, limit)


async def get_science_news_detail(cn: str) -> dict[str, Any] | None:
    return await _scienceon_browse("SNEWS", cn)
