"""USPTO PatentsView patent search.

Phase 1 patent backend — USPTO PatentsView REST API.
Free, requires X-Api-Key (get at https://patentsview.org/api).
Coverage: ~8M US granted patents from 1976. Future phases will
add EPO OPS (Espacenet, EU/WIPO coverage) and The Lens (global)
following the same multi-source pattern as papersearch.search().
"""
import logging
import os

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

_PATENTSVIEW_API = "https://search.patentsview.org/api/v1/patent/"


def _empty(p: dict, source: str) -> dict:
    """Normalize a backend row into the shared schema."""
    return {
        "title": (p.get("title") or "").strip(),
        "patent_number": p.get("patent_number") or "",
        "date": p.get("date") or "",
        "year": p.get("year"),
        "inventors": p.get("inventors") or [],
        "assignee": p.get("assignee") or "",
        "abstract": (p.get("abstract") or "").strip(),
        "claims_count": p.get("claims_count"),
        "url": p.get("url") or "",
        "source": source,
    }


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
async def _patentsview(query: str, limit: int) -> list[dict]:
    """USPTO PatentsView v1. Free with X-Api-Key.
    Skipped silently when no key is set so search() can still run
    (returning empty) instead of crashing."""
    key = os.getenv("PATENTSVIEW_API_KEY", "").strip()
    if not key:
        log.info("patentsview: no PATENTSVIEW_API_KEY set, skipping")
        return []
    body = {
        # Match every word of the query in title OR abstract.
        # _text_all is "all words must appear (any order)" — better
        # signal than _text_any for multi-word queries.
        "q": {
            "_or": [
                {"_text_all": {"patent_title": query}},
                {"_text_all": {"patent_abstract": query}},
            ]
        },
        "f": [
            "patent_id", "patent_title", "patent_date",
            "patent_abstract", "inventors", "assignees",
            "patent_num_claims",
        ],
        # Sort newest first — PatentsView doesn't expose a pure
        # relevance score, and `_text_all` already filters to
        # docs that hit every query term. Newest-first surfaces
        # current state-of-the-art ahead of historical filings
        # which is what "patent search for X" queries usually want.
        "s": [{"patent_date": "desc"}],
        "o": {"size": min(limit, 25)},
    }
    headers = {"X-Api-Key": key, "User-Agent": "SecondBrainBot"}
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers=headers) as c:
        r = await c.post(_PATENTSVIEW_API, json=body)
        r.raise_for_status()
    out = []
    for p in r.json().get("patents", []):
        pid = (p.get("patent_id") or "").strip()
        title = (p.get("patent_title") or "").strip()
        date = (p.get("patent_date") or "").strip()
        year = None
        if date[:4].isdigit():
            year = int(date[:4])
        inv: list[str] = []
        for i in (p.get("inventors") or [])[:3]:
            first = (i.get("inventor_name_first") or "").strip()
            last = (i.get("inventor_name_last") or "").strip()
            name = f"{first} {last}".strip()
            if name:
                inv.append(name)
        assignee = ""
        for a in (p.get("assignees") or [])[:1]:
            assignee = (a.get("assignee_organization") or "").strip()
            if not assignee:
                first = (a.get("assignee_first_name") or "").strip()
                last = (a.get("assignee_last_name") or "").strip()
                assignee = f"{first} {last}".strip()
        # Google Patents resolves USPTO numbers reliably (no
        # netacgi nph-Parser URL building, no auth needed).
        url = f"https://patents.google.com/patent/US{pid}" if pid else ""
        out.append(_empty({
            "title": title,
            "patent_number": f"US{pid}" if pid else "",
            "date": date,
            "year": year,
            "inventors": inv,
            "assignee": assignee,
            "abstract": p.get("patent_abstract") or "",
            "claims_count": p.get("patent_num_claims"),
            "url": url,
        }, "USPTO"))
    return out


async def search(query: str, limit: int = 15) -> list[dict]:
    """Patent search entry point.

    Phase 1: PatentsView only (US patents). When the key isn't set,
    or the backend fails, returns []. Future phases will add EPO
    OPS + The Lens with a domain-routed multi-source fan-out
    mirroring papersearch.search().
    """
    try:
        return await _patentsview(query, limit)
    except Exception as e:
        log.warning("patent_search patentsview failed (%s)",
                    type(e).__name__)
        return []
