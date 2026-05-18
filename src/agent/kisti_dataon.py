"""KISTI DataON Open API client (vendored from ansua79/kisti-mcp,
CC-BY-NC-4.0).

DataON = 국가연구데이터플랫폼. Two endpoints:
  • Dataset search — keyword search over public research datasets
    (DataON_ResearchData_API_KEY).
  • Dataset metadata detail by svcId — full metadata + license +
    DOI for a single dataset (DataON_ResearchDataMetadata_API_KEY,
    a separate key issued via a different 활용신청).

JSON throughout, simple URL-param `key` auth. No token exchange.
"""
import json
import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

_DATAON_BASE = "https://dataon.kisti.re.kr"


def _search_key() -> str:
    return os.getenv("DATAON_RESEARCHDATA_API_KEY", "").strip()


def _detail_key() -> str:
    return os.getenv("DATAON_RESEARCHDATAMETADATA_API_KEY", "").strip()


def _extract_items(data: Any) -> list[dict[str, Any]]:
    """DataON wraps results under various keys depending on
    endpoint. Try the documented paths in order."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for path in ("items", "datasets", "results", "data", "list"):
            v = data.get(path)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                for sub in ("items", "list", "datasets"):
                    if isinstance(v.get(sub), list):
                        return v[sub]
        # Single-record response — return as a one-element list.
        return [data]
    return []


async def search_research_data(
    query: str, limit: int = 10, from_pos: int = 0,
    sort_con: str = "", sort_arr: str = "desc",
) -> list[dict[str, Any]]:
    """Public research dataset search.
    Returns dataset rows with svcId / title / creator / DOI /
    publishedDate / license etc."""
    key = _search_key()
    if not key:
        log.info("dataon: no DATAON_RESEARCHDATA_API_KEY, skipping")
        return []
    params: dict[str, Any] = {
        "key": key,
        "query": query,
        "from": max(0, from_pos),
        "size": min(max(1, limit), 100),
    }
    if sort_con:
        params["sortCon"] = sort_con
    if sort_arr:
        params["sortArr"] = sort_arr
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            r = await http.get(
                f"{_DATAON_BASE}/rest/api/search/dataset/",
                params=params,
            )
            if r.status_code != 200:
                log.warning("dataon search %d: %r",
                            r.status_code, r.text[:200])
                return []
            try:
                data = r.json()
            except json.JSONDecodeError as e:
                log.warning("dataon search JSON decode failed: %s", e)
                return []
            return _extract_items(data)
    except Exception as e:
        log.warning("dataon search_research_data failed: %s", e)
        return []


async def get_research_data_detail(svc_id: str) -> dict[str, Any] | None:
    """Dataset metadata detail by svcId (from search results).
    Uses the SEPARATE metadata API key — search endpoint won't
    work here, the user must have activated both keys."""
    key = _detail_key()
    if not (key and svc_id):
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            r = await http.get(
                f"{_DATAON_BASE}/rest/api/search/dataset/{svc_id}",
                params={"key": key},
            )
            if r.status_code != 200:
                log.warning("dataon detail %d: %r",
                            r.status_code, r.text[:200])
                return None
            try:
                return r.json()
            except json.JSONDecodeError as e:
                log.warning("dataon detail JSON decode failed: %s", e)
                return None
    except Exception as e:
        log.warning("dataon get_research_data_detail failed: %s", e)
        return None
