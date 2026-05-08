import logging
import os
import xml.etree.ElementTree as ET

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

_S2_API = "https://api.semanticscholar.org/graph/v1/paper/search"
_S2_FIELDS = "title,abstract,authors.name,year,venue,openAccessPdf,externalIds,url"
_ARXIV_API = "https://export.arxiv.org/api/query"
_ATOM_NS = {"a": "http://www.w3.org/2005/Atom",
            "arxiv": "http://arxiv.org/schemas/atom"}


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
async def _semantic_scholar(query: str, limit: int) -> list[dict]:
    """Primary path. Unauthenticated traffic is heavily rate-limited
    (frequent 429), so we forward S2_API_KEY when set."""
    params = {"query": query, "limit": limit, "fields": _S2_FIELDS}
    headers = {"User-Agent": "SecondBrainBot"}
    if key := os.getenv("S2_API_KEY", "").strip():
        headers["x-api-key"] = key
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers=headers) as c:
        r = await c.get(_S2_API, params=params)
        r.raise_for_status()
    out = []
    for p in r.json().get("data", []):
        ex = p.get("externalIds") or {}
        arxiv = ex.get("ArXiv")
        pdf = (p.get("openAccessPdf") or {}).get("url")
        link = (
            f"https://arxiv.org/abs/{arxiv}" if arxiv
            else (p.get("url") or pdf or "")
        )
        out.append({
            "title": p.get("title", "").strip(),
            "year": p.get("year"),
            "venue": p.get("venue"),
            "authors": [a.get("name") for a in (p.get("authors") or [])][:3],
            "abstract": (p.get("abstract") or "").strip(),
            "url": link,
            "pdf": pdf,
            "arxiv": arxiv,
        })
    return out


async def _arxiv(query: str, limit: int) -> list[dict]:
    """Fallback. arXiv's public API needs no auth and is far more
    stable than Semantic Scholar for anonymous traffic. Returns Atom
    XML — parse the bits we expose to the agent."""
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": limit,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": "SecondBrainBot"}) as c:
        r = await c.get(_ARXIV_API, params=params)
        r.raise_for_status()
    root = ET.fromstring(r.text)
    out = []
    for entry in root.findall("a:entry", _ATOM_NS):
        title = (entry.findtext("a:title", default="", namespaces=_ATOM_NS) or "").strip()
        summary = (entry.findtext("a:summary", default="", namespaces=_ATOM_NS) or "").strip()
        published = entry.findtext("a:published", default="", namespaces=_ATOM_NS) or ""
        year = int(published[:4]) if published[:4].isdigit() else None
        authors = [
            (a.findtext("a:name", default="", namespaces=_ATOM_NS) or "").strip()
            for a in entry.findall("a:author", _ATOM_NS)
        ][:3]
        abs_link = ""
        pdf_link = ""
        for link in entry.findall("a:link", _ATOM_NS):
            href = link.get("href") or ""
            if link.get("title") == "pdf":
                pdf_link = href
            elif link.get("rel") == "alternate":
                abs_link = href
        arxiv_id = abs_link.rsplit("/abs/", 1)[-1] if "/abs/" in abs_link else None
        out.append({
            "title": title,
            "year": year,
            "venue": "arXiv",
            "authors": authors,
            "abstract": summary,
            "url": abs_link,
            "pdf": pdf_link,
            "arxiv": arxiv_id,
        })
    return out


async def search(query: str, limit: int = 5) -> list[dict]:
    """Try Semantic Scholar first; on any failure, fall back to arXiv
    so a flaky S2 rate-limit doesn't kill the whole agent turn."""
    try:
        results = await _semantic_scholar(query, limit)
        if results:
            return results
        log.info("semantic scholar empty for %r — trying arxiv", query)
    except Exception as e:
        log.warning("semantic scholar failed (%s) — falling back to arxiv",
                    type(e).__name__)
    return await _arxiv(query, limit)
