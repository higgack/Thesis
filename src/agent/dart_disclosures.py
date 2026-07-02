"""OpenDART 최근 공시 목록 조회 — /kr_disclosures 의 데이터 계층.

기존 DART 연동은 수동(링크가 흘러들어와야 학습)이었다. 이 모듈은 능동 조회를
붙인다: 회사 이름 → corp_code → 최근 공시 목록(list.json). 목록의 각 건은
뷰어 URL(rcpNo=…)로 만들어 기존 인입 경로(loaders.load_dart + dart_cap)로
그대로 태운다 — 인입 파이프라인/일일 캡 재사용, LLM 비용 0.

corp_code 캐시: corpCode.xml API 는 전체 10만+ 법인의 ZIP 인데, 투자 용도는
상장사만 있으면 되므로 stock_code 가 있는 ~3천여 곳만 추려
data/dart_corp_codes.json 에 30일 캐시한다(atomic write, 손상 시 재다운로드).

키: load_dart 와 동일한 DART_API_KEY (.env, opendart.fss.or.kr 무료 발급).
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .. import config

log = logging.getLogger(__name__)
_KST = timezone(timedelta(hours=9))

_CACHE_PATH = Path(config.DATA_DIR) / "dart_corp_codes.json"
_CACHE_DAYS = 30

_CORP_CODE_API = "https://opendart.fss.or.kr/api/corpCode.xml"
_LIST_API = "https://opendart.fss.or.kr/api/list.json"

VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"


def api_key() -> str:
    return os.getenv("DART_API_KEY", "").strip()


def _parse_corp_zip(content: bytes) -> list[dict]:
    """ZIP(CORPCODE.xml) → 상장사만 [{n:이름, c:corp_code, s:종목코드}].
    10만+ 엔트리 XML 파싱이라 CPU ~1-2s — 호출측이 to_thread 로 돌린다."""
    zf = zipfile.ZipFile(io.BytesIO(content))
    xml_bytes = zf.read(zf.namelist()[0])
    root = ET.fromstring(xml_bytes)
    corps: list[dict] = []
    for el in root.iter("list"):
        stock = (el.findtext("stock_code") or "").strip()
        if not stock or stock == " ":
            continue
        name = (el.findtext("corp_name") or "").strip()
        code = (el.findtext("corp_code") or "").strip()
        if name and code:
            corps.append({"n": name, "c": code, "s": stock})
    return corps


async def _load_corps() -> list[dict]:
    """30일 캐시된 상장사 목록. 캐시 miss/만료/손상 → API 재다운로드."""
    try:
        d = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(d["fetched"])
        if (datetime.now(_KST) - fetched) < timedelta(days=_CACHE_DAYS) \
                and d.get("corps"):
            return d["corps"]
    except Exception:
        pass
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(_CORP_CODE_API, params={"crtfc_key": api_key()})
        r.raise_for_status()
        if not r.content[:2] == b"PK":   # ZIP magic; 아니면 에러 XML
            raise RuntimeError(
                "corpCode 응답이 ZIP이 아님 — DART_API_KEY 확인 필요")
    corps = await asyncio.to_thread(_parse_corp_zip, r.content)
    if corps:
        tmp = _CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"fetched": datetime.now(_KST).isoformat(), "corps": corps},
            ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _CACHE_PATH)
    return corps


async def find_corp(name: str) -> list[dict]:
    """이름 → 상장사 후보. 정확 일치 우선, 없으면 부분 일치 상위 8."""
    corps = await _load_corps()
    nl = (name or "").strip().lower()
    if not nl:
        return []
    exact = [x for x in corps if x["n"].lower() == nl]
    if exact:
        return exact
    return [x for x in corps if nl in x["n"].lower()][:8]


async def list_filings(corp_code: str, days: int = 90,
                       count: int = 10) -> list[dict]:
    """최근 `days`일 공시 최신순 최대 `count`건.
    [{rcept_no, report_nm, rcept_dt, flr_nm}]. 공시 없음(013) → []."""
    end = datetime.now(_KST)
    bgn = end - timedelta(days=days)
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(_LIST_API, params={
            "crtfc_key": api_key(), "corp_code": corp_code,
            "bgn_de": bgn.strftime("%Y%m%d"), "end_de": end.strftime("%Y%m%d"),
            "page_no": 1, "page_count": max(1, min(int(count), 100)),
        })
        r.raise_for_status()
        j = r.json()
    status = str(j.get("status", ""))
    if status == "013":      # 조회된 데이터가 없습니다
        return []
    if status != "000":
        raise RuntimeError(f"OpenDART {status}: {j.get('message', '')}")
    out = []
    for row in j.get("list") or []:
        out.append({
            "rcept_no": str(row.get("rcept_no") or ""),
            "report_nm": (row.get("report_nm") or "").strip(),
            "rcept_dt": str(row.get("rcept_dt") or ""),
            "flr_nm": (row.get("flr_nm") or "").strip(),
        })
    return out
