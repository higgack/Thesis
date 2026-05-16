"""The Lens patent search.

Phase 1 patent backend — The Lens (https://www.lens.org).
Free with Bearer token (LENS_API_KEY env, register at
https://www.lens.org/lens/user/subscriptions → Patent API token).
Coverage: 95M+ global patents (US + EU + WIPO + JP + KR + ...).

Earlier draft targeted USPTO PatentsView but PatentsView has been
folded into USPTO Open Data Portal whose registration requires
ID.me identity verification (video call for international users,
1-3 days). The Lens registers in ~5 min with no identity check
and gives broader global coverage, so it's the better fit for a
personal research bot. Phase 2/3 can still layer EPO OPS or
USPTO ODP on top following papersearch.search()'s multi-source
pattern when needed.
"""
import logging
import os

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

_LENS_API = "https://api.lens.org/patent/search"


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


def _pick_lang_text(items, prefer_lang: str = "en") -> str:
    """The Lens returns title/abstract as
    [{"text": "...", "lang": "en"}, ...] for multilingual coverage.
    Pick the English entry first, otherwise the first non-empty."""
    if not items:
        return ""
    if isinstance(items, dict):
        items = [items]
    for x in items:
        if (x.get("lang") or "").startswith(prefer_lang):
            t = (x.get("text") or "").strip()
            if t:
                return t
    for x in items:
        t = (x.get("text") or "").strip()
        if t:
            return t
    return ""


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
async def _lens(query: str, limit: int) -> list[dict]:
    """The Lens patent search. Free with Bearer token.
    Silently skips when no key set so search() returns []
    instead of crashing."""
    key = os.getenv("LENS_API_KEY", "").strip()
    if not key:
        log.info("lens: no LENS_API_KEY set, skipping")
        return []
    body = {
        # `query_string` runs the full Lucene query syntax across
        # title + abstract + claims. The Lens default ranking is
        # BM25 relevance — what "find me patents about X" wants —
        # so we don't override `sort`.
        "query": {"query_string": {"query": query}},
        "size": min(limit, 25),
        # Trim the response to fields we actually render — Lens
        # bibliographic records are huge by default.
        "include": [
            "lens_id", "doc_key", "doc_number",
            "biblio.invention_title", "biblio.publication_reference",
            "biblio.parties.inventors", "biblio.parties.applicants",
            "biblio.abstract", "abstract",
            "claim_count",
        ],
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "SecondBrainBot",
    }
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers=headers) as c:
        r = await c.post(_LENS_API, json=body)
        if r.status_code != 200:
            # Log the body so we can see WHY Lens rejected us — the
            # bare status code (401/403) doesn't tell us if it's
            # "token not activated", "wrong API scope", "invalid
            # format" etc. Truncate to keep logs sane on huge HTML
            # error pages.
            body_preview = r.text[:500].replace("\n", " ")
            log.warning(
                "lens %d on patent/search — key_prefix=%r body=%r",
                r.status_code, key[:8], body_preview,
            )
        r.raise_for_status()

    out = []
    for p in r.json().get("data", []):
        biblio = p.get("biblio") or {}
        # Title (multilingual list)
        title = _pick_lang_text(biblio.get("invention_title"))

        # Patent number = jurisdiction + doc_number (US11234567, EP3456789)
        pub_ref = biblio.get("publication_reference") or {}
        juris = (pub_ref.get("jurisdiction") or "").strip()
        doc_num = (pub_ref.get("doc_number") or p.get("doc_number") or "").strip()
        patent_number = f"{juris}{doc_num}" if juris and doc_num else doc_num

        date = (pub_ref.get("date") or "").strip()
        year = int(date[:4]) if date[:4].isdigit() else None

        # Parties (inventors + applicants/assignees)
        parties = biblio.get("parties") or {}
        inventors: list[str] = []
        for i in (parties.get("inventors") or [])[:3]:
            name = ((i.get("extracted_name") or {}).get("value") or "").strip()
            if name:
                inventors.append(name)
        assignee = ""
        for a in (parties.get("applicants") or [])[:1]:
            assignee = ((a.get("extracted_name") or {}).get("value") or "").strip()

        # Abstract — Lens nests it under biblio.abstract in newer
        # versions but legacy data sometimes still uses root-level
        # abstract. Try both.
        abstract = _pick_lang_text(biblio.get("abstract"))
        if not abstract:
            abstract = _pick_lang_text(p.get("abstract"))

        # URL — Google Patents resolves the {jurisdiction+number}
        # form reliably for any jurisdiction. Falls back to Lens
        # detail page when patent_number is missing.
        if patent_number:
            url = f"https://patents.google.com/patent/{patent_number}"
        elif p.get("lens_id"):
            url = f"https://www.lens.org/lens/patent/{p['lens_id']}"
        else:
            url = ""

        out.append(_empty({
            "title": title,
            "patent_number": patent_number,
            "date": date,
            "year": year,
            "inventors": inventors,
            "assignee": assignee,
            "abstract": abstract,
            "claims_count": p.get("claim_count"),
            "url": url,
        }, "Lens"))
    return out


async def search(query: str, limit: int = 15) -> list[dict]:
    """Patent search entry point.

    Phase 1: The Lens only (95M+ global patents). Phase 2/3 may
    add EPO OPS or USPTO ODP using the same multi-source pattern
    papersearch.search() uses, when the use case justifies the
    extra registration overhead.

    Failures swallowed — returns [] so the agent can continue
    answering rather than erroring out the whole turn.
    """
    try:
        return await _lens(query, limit)
    except Exception as e:
        log.warning("patent_search lens failed (%s)",
                    type(e).__name__)
        return []
