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
    """Most NTIS endpoints return <result><record>...</record>×N
    structures with flat child tags. Flatten each <record>'s
    children into a dict so callers can pull any field by tag
    name. Returns [] on parse failure or error status."""
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
    # NTIS uses several different record tag names across services
    # — pick whichever shows up. searchResult/record is common
    # for public_project; rcmncls returns <recommendItems> or
    # similar.
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
            return _parse_xml_records(r.text)
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
            return _parse_xml_records(r.text)
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
