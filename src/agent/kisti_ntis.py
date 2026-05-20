"""KISTI NTIS Open API client (vendored from ansua79/kisti-mcp,
CC-BY-NC-4.0).

NTIS = 국가과학기술지식정보서비스. Three services unified under a
single NTIS_API_KEY (each one needs separate 활용신청 on
ntis.go.kr but the issued key works across all three):

  1. 국가R&D 과제검색 (public_project) — government-funded R&D
     project search. Returns project ID + 수행기관 + 연구비 +
     과제번호 + 책임자 + 기간.
  2. 과학기술표준분류 추천 (rcmncls) — given a research abstract,
     recommends Korea's standard sci-tech classification codes.
  3. 연관콘텐츠 추천 (ConnectionContent) — given a project ID,
     surfaces related papers / patents / reports / projects.

No token exchange — just GET with `apprvKey` URL param. JSON or
XML response depending on endpoint.
"""
import json
import logging
import os
import xml.etree.ElementTree as ET
from typing import Any

import httpx

log = logging.getLogger(__name__)

_NTIS_BASE = "https://www.ntis.go.kr"


def _key() -> str:
    return os.getenv("NTIS_API_KEY", "").strip()


def _parse_xml_records(text: str) -> list[dict[str, str]]:
    """Legacy NTIS schema parser — <result><record>...</record>×N
    with flat child tags. Returns [] on parse failure or error
    status. Kept for any classic-schema endpoint that hasn't moved
    to the new <RESULTSET><HIT> structure yet (rcmncls might still
    use this)."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        log.warning("ntis XML parse failed: %s", e)
        return []
    # NTIS error envelope: <result><resultCode>X</resultCode>
    # <resultMsg>...</resultMsg></result>. Skip when not 00.
    rc = root.find(".//resultCode")
    if rc is not None and (rc.text or "").strip() not in ("00", "200"):
        msg = root.find(".//resultMsg")
        log.warning("ntis error %s: %r",
                    rc.text,
                    (msg.text or "")[:200] if msg is not None else "")
        return []
    out: list[dict[str, str]] = []
    for tag in ("record", "rec", "item", "recommendItem"):
        for rec in root.findall(f".//{tag}"):
            row: dict[str, str] = {}
            for child in rec:
                if child.text and child.tag:
                    row[child.tag] = child.text.strip()
            if row:
                out.append(row)
        if out:
            break
    return out


# Strip <span class="search_word">...</span> HTML highlight markers
# NTIS injects into matched text. The XML carries them as &lt;span&gt;
# entities; ElementTree decodes back to literal <span>...</span> in
# the text() value, and we strip with a non-greedy regex. Multiline
# safe because none of these spans cross newlines.
import re as _re
_SEARCH_HIGHLIGHT_RE = _re.compile(r"<[^>]+>")


def _strip_highlight(text: str | None) -> str:
    if not text:
        return ""
    return _SEARCH_HIGHLIGHT_RE.sub("", text).strip()


def _ntis_hit_to_dict(hit: "ET.Element") -> dict[str, str]:
    """Map one <HIT> element from NTIS public_project (new schema,
    confirmed live 2026-05) into the flat-dict shape the bot's
    _format_ntis_projects formatter expects (ProjectNumber /
    ProjectTitle / ResearchLeader / ResearchAgency / Abstract /
    Goal / Keyword / Researchers).

    New schema example:
      <HIT NO="1">
        <ProjectNumber>...</ProjectNumber>
        <ProjectTitle><Korean>...</Korean><English>...</English></ProjectTitle>
        <Manager><Name>...</Name></Manager>
        <OrderAgency><Name>...</Name></OrderAgency>
        <Researchers><Name>...</Name><ManCount>...</ManCount>
                     <WomanCount>...</WomanCount></Researchers>
        <Goal><Full>...</Full><Teaser>...</Teaser></Goal>
        <Abstract><Full>...</Full><Teaser>...</Teaser></Abstract>
        <Keyword><Korean>...</Korean><English>...</English></Keyword>
      </HIT>
    """
    out: dict[str, str] = {}

    def _t(path: str) -> str:
        el = hit.find(path)
        return _strip_highlight(el.text) if el is not None else ""

    pn = _t("ProjectNumber")
    if pn:
        out["ProjectNumber"] = pn
    title = _t("ProjectTitle/Korean") or _t("ProjectTitle/English")
    if title:
        out["ProjectTitle"] = title
    mgr = _t("Manager/Name")
    if mgr:
        out["ResearchLeader"] = mgr
    agency = _t("OrderAgency/Name")
    if agency:
        out["ResearchAgency"] = agency
    # Researchers — keep the name list (semicolon-separated) +
    # man/woman count. Useful for "이 과제 누가 했나" type queries.
    rnames = _t("Researchers/Name")
    rmc = _t("Researchers/ManCount")
    rwc = _t("Researchers/WomanCount")
    if rmc or rwc:
        bits = []
        if rmc:
            bits.append(f"남 {rmc}")
        if rwc:
            bits.append(f"여 {rwc}")
        out["Researchers"] = " · ".join(bits)
        if rnames:
            out["ResearcherNames"] = rnames
    abs_text = _t("Abstract/Teaser") or _t("Abstract/Full")
    if abs_text:
        out["Abstract"] = abs_text
    goal = _t("Goal/Full") or _t("Goal/Teaser")
    if goal:
        out["Goal"] = goal
    effect = _t("Effect/Full") or _t("Effect/Teaser")
    if effect:
        out["Effect"] = effect
    kw = _t("Keyword/Korean")
    if kw:
        out["Keyword"] = kw
    # Year / budget / period fields may show up further in the HIT —
    # extract opportunistically from any direct text child that looks
    # like a year (4 digits) or has a known tag name.
    for child in hit:
        tag = child.tag
        if tag in ("ResearchYear", "Year", "ProjectYear") and child.text:
            out["ResearchYear"] = child.text.strip()
        elif tag in ("ResearchPeriod", "Period") and child.text:
            out["ResearchPeriod"] = child.text.strip()
        elif tag in ("ResearchExpenses", "Budget",
                     "TotalBudget", "Expense") and child.text:
            out["ResearchExpenses"] = child.text.strip()
    return out


def _parse_ntis_projects_response(text: str) -> list[dict[str, str]]:
    """Parse the public_project XML response. Tries the new
    <RESULT><RESULTSET><HIT> schema first; falls back to the legacy
    <result><record> shape (or returns [] for genuine empty / error
    cases)."""
    # Error envelope: <error>유효한 인증키가 아닙니다. 인증키 : ...</error>
    if text.lstrip().startswith("<error>") or "<error>" in text[:200]:
        # Try to extract the message for the log.
        try:
            root = ET.fromstring(text)
            if root.tag == "error":
                log.warning("ntis error: %r", (root.text or "")[:200])
                return []
        except ET.ParseError:
            log.warning("ntis error response (raw): %r", text[:200])
            return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        log.warning("ntis projects XML parse failed: %s "
                    "(first 200 chars: %r)", e, text[:200])
        return []
    # New schema (2026-05+): <RESULT>/<RESULTSET>/<HIT>
    hits = root.findall(".//HIT")
    if hits:
        out = [_ntis_hit_to_dict(h) for h in hits]
        return [r for r in out if r]
    # Fall through to legacy parser for endpoints that haven't
    # migrated (the classification-recommend service might still
    # return old <record> structure).
    return _parse_xml_records(text)


def _parse_json_records(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        log.warning("ntis JSON parse failed: %s", e)
        return []
    # NTIS 연관콘텐츠 returns {"items": [...]} or similar shapes.
    # Try the common candidates.
    for path in ("items", "result", "records", "data"):
        items = data.get(path)
        if isinstance(items, list):
            return items
        if isinstance(items, dict):
            for sub in ("items", "list", "records"):
                if isinstance(items.get(sub), list):
                    return items[sub]
    # Fallback — return the dict as a single row.
    return [data] if isinstance(data, dict) else []


async def search_projects(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """국가 R&D 과제 검색. Returns rows with 과제번호 (pjtId),
    과제명, 수행기관, 연구책임자, 연구기간, 연구비 etc."""
    key = _key()
    if not key:
        log.info("ntis: no NTIS_API_KEY set, skipping projects")
        return []
    params = {
        "apprvKey": key,
        "userId": "",
        "collection": "project",
        "SRWR": query,
        "searchFd": "",
        "addQuery": "",
        "searchRnkn": "",
        "startPosition": 1,
        "displayCnt": min(max(1, limit), 100),
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            r = await http.get(f"{_NTIS_BASE}/rndopen/openApi/public_project",
                               params=params)
            if r.status_code != 200:
                log.warning("ntis project %d: %r",
                            r.status_code, r.text[:200])
                return []
            return _parse_ntis_projects_response(r.text)
    except Exception as e:
        log.warning("ntis search_projects failed: %s", e)
        return []


async def recommend_classifications(
    abstract: str, classification_type: str = "standard",
) -> list[dict[str, Any]]:
    """과학기술 분류코드 추천. abstract = 연구 초록 / 과제 요약.
    classification_type: 'standard' (과학기술표준분류), 'health'
    (보건의료기술), 'industry' (산업기술)."""
    key = _key()
    if not key:
        return []
    configs = {
        "standard": "rcmncls",
        "health": "rcmnhtcls",
        "industry": "rcmnitcls",
    }
    coll = configs.get(classification_type, "rcmncls")
    params = {
        "apprvKey": key,
        "collection": coll,
        "rqstDes": abstract,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            r = await http.get(f"{_NTIS_BASE}/rndopen/openApi/rcmncls",
                               params=params)
            if r.status_code != 200:
                log.warning("ntis classify %d: %r",
                            r.status_code, r.text[:200])
                return []
            rows = _parse_xml_records(r.text)
            # Diagnostic: log first 400 chars of raw XML response when
            # parser returns zero rows, so we can confirm whether NTIS
            # genuinely matched nothing or our parser missed the tag.
            if not rows:
                log.info(
                    "ntis classify zero rows for %r (raw resp len=%d, "
                    "first 400 chars: %r)",
                    abstract[:80], len(r.text), r.text[:400],
                )
            return rows
    except Exception as e:
        log.warning("ntis recommend_classifications failed: %s", e)
        return []


async def related_content(
    pjt_id: str, collection_type: str = "researchreport",
) -> list[dict[str, Any]]:
    """과제 ID 로 연관 콘텐츠 (논문/특허/보고서/관련과제) 추천.
    collection_type 옵션:
      'paper' / 'patent' / 'researchreport' / 'project'."""
    key = _key()
    if not (key and pjt_id):
        return []
    params = {
        "apprvKey": key,
        "pjtId": pjt_id,
        "collection": collection_type,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            r = await http.get(
                f"{_NTIS_BASE}/rndopen/openApi/ConnectionContent",
                params=params,
            )
            if r.status_code != 200:
                log.warning("ntis related %d: %r",
                            r.status_code, r.text[:200])
                return []
            # 연관콘텐츠 returns JSON unlike the other NTIS endpoints
            return _parse_json_records(r.text)
    except Exception as e:
        log.warning("ntis related_content failed: %s", e)
        return []
