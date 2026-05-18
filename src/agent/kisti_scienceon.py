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
import base64
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

_SCIENCEON_BASE = "https://apigateway.kisti.re.kr"
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
    """Request a fresh access_token. ScienceON tokens are short-
    lived (~30 min) so we don't cache — every search re-tokens.
    Returns None on any error (callers fall back to empty results)."""
    api_key = os.getenv("SCIENCEON_API_KEY", "").strip()
    client_id = os.getenv("SCIENCEON_CLIENT_ID", "").strip()
    mac = os.getenv("SCIENCEON_MAC_ADDRESS", "").strip()
    if not (api_key and client_id and mac):
        return None
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
            return r.json().get("access_token")
        except json.JSONDecodeError:
            log.warning("scienceon token JSON decode failed: %r",
                        r.text[:200])
            return None
    except Exception as e:
        log.warning("scienceon token fetch failed: %s", e)
        return None


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


def _record_to_dict(rec: ET.Element) -> dict[str, str]:
    """Each ScienceON record is <record><item metaCode='TI'>...</item>
    × N</record>. Flatten the metaCode-keyed items into a dict so
    callers can pull fields like Title (TI) / Author (AU) /
    Abstract (AB) / DOI / etc."""
    out: dict[str, str] = {}
    for item in rec.findall("item"):
        code = (item.get("metaCode") or "").strip()
        text = (item.text or "").strip()
        if code:
            out[code] = text
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
