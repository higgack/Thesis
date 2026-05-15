"""Academic paper search — multi-source with smart routing.

Backends:
  • Semantic Scholar (S2)  — general, needs S2_API_KEY for stability
  • arXiv                  — CS/physics/math (free, no key)
  • OpenAlex               — best general coverage (~250M works, no key)
  • CrossRef               — DOI registry, all publishers (no key)
  • IEEE Xplore            — engineering / semiconductor (needs IEEE_API_KEY)
  • PubMed                 — biomedical / clinical (NCBI E-utilities, no key)

The `search()` entry point inspects the query, picks the best 1-3
backends for that domain, and merges results in parallel. Falls back
to a broad multi-source fan-out when no strong domain signal is
present.

Every result carries `url` (landing page) and `pdf` (direct PDF
download URL when known) so the bot can render clickable source
links the user taps to download immediately.
"""
import asyncio
import logging
import os
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
_S2_API = "https://api.semanticscholar.org/graph/v1/paper/search"
_S2_FIELDS = "title,abstract,authors.name,year,venue,openAccessPdf,externalIds,url"
_ARXIV_API = "https://export.arxiv.org/api/query"
_OPENALEX_API = "https://api.openalex.org/works"
_CROSSREF_API = "https://api.crossref.org/works"
_IEEE_API = "https://ieeexploreapi.ieee.org/api/v1/search/articles"
_PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
_PUBMED_SUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

_ATOM_NS = {"a": "http://www.w3.org/2005/Atom",
            "arxiv": "http://arxiv.org/schemas/atom"}

# ---------------------------------------------------------------------------
# Domain routing
# ---------------------------------------------------------------------------
# Keyword → backend preference. Order matters: first list = primary,
# rest fill in. Detection is case-insensitive substring; users typing
# the canonical English term get the targeted backend, everyone else
# gets the broad fan-out.
_DOMAIN_HINTS = {
    # Semiconductor / packaging / EE — IEEE is the canonical venue
    "semiconductor": ["ieee", "openalex", "s2"],
    "packaging": ["ieee", "openalex", "s2"],
    "hybrid bonding": ["ieee", "openalex", "s2"],
    "cu-cu bonding": ["ieee", "openalex", "s2"],
    "chiplet": ["ieee", "openalex", "s2"],
    "wafer": ["ieee", "openalex", "s2"],
    "fan-out": ["ieee", "openalex", "s2"],
    "hbm": ["ieee", "openalex", "s2"],
    "tsv": ["ieee", "openalex", "s2"],
    "lithography": ["ieee", "openalex", "s2"],
    "euv": ["ieee", "openalex", "s2"],
    "transistor": ["ieee", "openalex", "s2"],
    "mosfet": ["ieee", "openalex", "s2"],
    "finfet": ["ieee", "openalex", "s2"],
    "gaa": ["ieee", "openalex", "s2"],
    "advanced packaging": ["ieee", "openalex", "s2"],
    "die-to-wafer": ["ieee", "openalex", "s2"],
    "mlcc": ["ieee", "openalex", "s2"],
    # Biomedical / clinical — PubMed is canonical
    "clinical": ["pubmed", "openalex", "s2"],
    "cancer": ["pubmed", "openalex", "s2"],
    "tumor": ["pubmed", "openalex", "s2"],
    "drug": ["pubmed", "openalex", "s2"],
    "patient": ["pubmed", "openalex", "s2"],
    "trial": ["pubmed", "openalex", "s2"],
    "vaccine": ["pubmed", "openalex", "s2"],
    "genome": ["pubmed", "openalex", "s2"],
    "protein": ["pubmed", "openalex", "s2"],
    # ML / AI — arXiv is canonical (most papers post here pre-publication)
    "transformer": ["arxiv", "s2", "openalex"],
    "neural network": ["arxiv", "s2", "openalex"],
    "diffusion model": ["arxiv", "s2", "openalex"],
    "reinforcement learning": ["arxiv", "s2", "openalex"],
    "llm": ["arxiv", "s2", "openalex"],
    "language model": ["arxiv", "s2", "openalex"],
    "deep learning": ["arxiv", "s2", "openalex"],
    "embedding": ["arxiv", "s2", "openalex"],
}


def _route_backends(query: str) -> list[str]:
    """Pick which backends to hit for this query.

    Returns an ordered list of backend names; the first 2-3 run in
    parallel inside `search()`. When no hint matches, fall back to a
    broad set covering both general (OpenAlex/S2) and CS (arXiv).
    """
    q = (query or "").lower()
    for hint, backends in _DOMAIN_HINTS.items():
        if hint in q:
            log.info("paper_search route: %r → %s (hint=%r)",
                     query[:80], backends, hint)
            return backends
    # Generic — OpenAlex has best cross-domain coverage; S2 catches
    # papers OpenAlex misses; arXiv covers preprints not yet on either.
    return ["openalex", "s2", "arxiv"]


# ---------------------------------------------------------------------------
# Backend clients — each returns a list[dict] with the unified schema:
#   {title, year, venue, authors, abstract, url, pdf, doi, arxiv, source}
# `source` tags which backend produced the row so the bot can show it.
# ---------------------------------------------------------------------------

def _empty(p: dict, source: str) -> dict:
    """Normalize partial dicts into the shared schema."""
    return {
        "title": (p.get("title") or "").strip(),
        "year": p.get("year"),
        "venue": p.get("venue") or "",
        "authors": p.get("authors") or [],
        "abstract": (p.get("abstract") or "").strip(),
        "url": p.get("url") or "",
        "pdf": p.get("pdf") or "",
        "doi": p.get("doi") or "",
        "arxiv": p.get("arxiv"),
        "source": source,
    }


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
async def _semantic_scholar(query: str, limit: int) -> list[dict]:
    """Primary fallback. Anonymous traffic is heavily rate-limited
    (frequent 429) so S2_API_KEY is strongly recommended."""
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
        doi = ex.get("DOI") or ""
        pdf = (p.get("openAccessPdf") or {}).get("url") or ""
        link = (
            f"https://arxiv.org/abs/{arxiv}" if arxiv
            else (p.get("url") or (f"https://doi.org/{doi}" if doi else pdf))
        )
        out.append(_empty({
            "title": p.get("title", ""),
            "year": p.get("year"),
            "venue": p.get("venue"),
            "authors": [a.get("name") for a in (p.get("authors") or [])][:3],
            "abstract": p.get("abstract") or "",
            "url": link,
            "pdf": pdf,
            "doi": doi,
            "arxiv": arxiv,
        }, "S2"))
    return out


async def _arxiv(query: str, limit: int) -> list[dict]:
    """arXiv API — sorted by relevance (default). Previously we forced
    submittedDate desc which buried domain-specific matches under
    irrelevant newest-of-the-day papers."""
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": limit,
        # No sortBy/sortOrder → arXiv returns by relevance, which is
        # what "find papers about X" almost always means.
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
        out.append(_empty({
            "title": title,
            "year": year,
            "venue": "arXiv",
            "authors": authors,
            "abstract": summary,
            "url": abs_link,
            "pdf": pdf_link,
            "arxiv": arxiv_id,
        }, "arXiv"))
    return out


async def _openalex(query: str, limit: int) -> list[dict]:
    """OpenAlex — free, no key, broadest coverage (~250M works incl.
    most IEEE/ACM/Springer/Elsevier papers indexed via Crossref).
    Polite tier asks for a mailto in the User-Agent."""
    params = {
        "search": query,
        "per-page": min(limit, 25),
    }
    mailto = os.getenv("OPENALEX_MAILTO", "").strip()
    ua = f"SecondBrainBot/1.0 (mailto:{mailto})" if mailto else "SecondBrainBot/1.0"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": ua}) as c:
        r = await c.get(_OPENALEX_API, params=params)
        r.raise_for_status()
    out = []
    for w in r.json().get("results", []):
        title = (w.get("title") or w.get("display_name") or "").strip()
        year = w.get("publication_year")
        venue = ""
        host = w.get("primary_location") or {}
        src = host.get("source") or {}
        if src:
            venue = src.get("display_name") or ""
        authors = []
        for a in (w.get("authorships") or [])[:3]:
            au = a.get("author") or {}
            if au.get("display_name"):
                authors.append(au["display_name"])
        # OpenAlex stores DOI as full URL ("https://doi.org/10.x/y")
        doi_raw = w.get("doi") or ""
        doi = doi_raw.replace("https://doi.org/", "") if doi_raw else ""
        pdf = host.get("pdf_url") or ""
        # Try open-access version if primary lacks PDF
        if not pdf:
            oa = w.get("open_access") or {}
            pdf = oa.get("oa_url") or ""
        landing = host.get("landing_page_url") or doi_raw or ""
        abstract = ""
        inv = w.get("abstract_inverted_index")
        if inv:
            # OpenAlex returns abstract as inverted index {word: [positions]};
            # reconstruct sequentially.
            positions = []
            for word, idxs in inv.items():
                for i in idxs:
                    positions.append((i, word))
            positions.sort()
            abstract = " ".join(w for _, w in positions)
        out.append(_empty({
            "title": title,
            "year": year,
            "venue": venue,
            "authors": authors,
            "abstract": abstract,
            "url": landing,
            "pdf": pdf,
            "doi": doi,
        }, "OpenAlex"))
    return out


async def _crossref(query: str, limit: int) -> list[dict]:
    """CrossRef — DOI registry, every major publisher. Faster than S2
    for many engineering queries. No key needed; polite tier asks for
    a mailto in the User-Agent."""
    params = {
        "query": query,
        "rows": min(limit, 20),
    }
    mailto = os.getenv("OPENALEX_MAILTO", "").strip()
    ua = f"SecondBrainBot/1.0 (mailto:{mailto})" if mailto else "SecondBrainBot/1.0"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": ua}) as c:
        r = await c.get(_CROSSREF_API, params=params)
        r.raise_for_status()
    out = []
    for w in r.json().get("message", {}).get("items", []):
        titles = w.get("title") or []
        title = (titles[0] if titles else "").strip()
        year = None
        for k in ("published-print", "published-online", "issued"):
            d = (w.get(k) or {}).get("date-parts") or []
            if d and d[0] and d[0][0]:
                year = d[0][0]
                break
        venue = (w.get("container-title") or [""])[0]
        authors = []
        for a in (w.get("author") or [])[:3]:
            name = " ".join(
                p for p in (a.get("given"), a.get("family")) if p
            )
            if name:
                authors.append(name)
        doi = w.get("DOI") or ""
        pdf = ""
        for link in w.get("link") or []:
            if "pdf" in (link.get("content-type") or "").lower():
                pdf = link.get("URL") or ""
                break
        landing = f"https://doi.org/{doi}" if doi else (w.get("URL") or "")
        out.append(_empty({
            "title": title,
            "year": year,
            "venue": venue,
            "authors": authors,
            "abstract": w.get("abstract") or "",
            "url": landing,
            "pdf": pdf,
            "doi": doi,
        }, "CrossRef"))
    return out


async def _ieee(query: str, limit: int) -> list[dict]:
    """IEEE Xplore — gold standard for semiconductor / EE / circuits.
    Needs IEEE_API_KEY (free tier 200 queries/day at
    https://developer.ieee.org/). Skipped when no key is set."""
    key = os.getenv("IEEE_API_KEY", "").strip()
    if not key:
        log.info("ieee: no IEEE_API_KEY set, skipping")
        return []
    params = {
        "apikey": key,
        "querytext": query,
        "max_records": min(limit, 25),
        "sort_order": "desc",
        "sort_field": "article_number",
    }
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": "SecondBrainBot"}) as c:
        r = await c.get(_IEEE_API, params=params)
        r.raise_for_status()
    out = []
    for a in r.json().get("articles", []):
        authors = []
        for au in (a.get("authors") or {}).get("authors", [])[:3]:
            n = au.get("full_name")
            if n:
                authors.append(n)
        out.append(_empty({
            "title": a.get("title") or "",
            "year": a.get("publication_year"),
            "venue": a.get("publication_title") or "IEEE Xplore",
            "authors": authors,
            "abstract": a.get("abstract") or "",
            "url": a.get("html_url") or a.get("abstract_url") or "",
            "pdf": a.get("pdf_url") or "",
            "doi": a.get("doi") or "",
        }, "IEEE"))
    return out


async def _pubmed(query: str, limit: int) -> list[dict]:
    """PubMed via NCBI E-utilities. No key needed for ≤3 req/s.
    Two-step: esearch returns PMIDs, esummary fills in metadata."""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": "SecondBrainBot"}) as c:
        es = await c.get(_PUBMED_SEARCH, params={
            "db": "pubmed", "term": query, "retmax": min(limit, 25),
            "retmode": "json", "sort": "relevance",
        })
        es.raise_for_status()
        ids = es.json().get("esearchresult", {}).get("idlist") or []
        if not ids:
            return []
        su = await c.get(_PUBMED_SUMMARY, params={
            "db": "pubmed", "id": ",".join(ids), "retmode": "json",
        })
        su.raise_for_status()
    summaries = (su.json().get("result") or {})
    out = []
    for pmid in ids:
        d = summaries.get(pmid) or {}
        if not d:
            continue
        title = (d.get("title") or "").strip()
        year = None
        pubdate = d.get("pubdate") or ""
        if pubdate[:4].isdigit():
            year = int(pubdate[:4])
        authors = [a.get("name") for a in (d.get("authors") or [])[:3]
                   if a.get("name")]
        # DOI lives in articleids list
        doi = ""
        for aid in d.get("articleids") or []:
            if aid.get("idtype") == "doi":
                doi = aid.get("value") or ""
                break
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        out.append(_empty({
            "title": title,
            "year": year,
            "venue": d.get("source") or "PubMed",
            "authors": authors,
            "abstract": "",  # esummary doesn't include abstract; would
                             # need a third efetch call
            "url": url,
            "pdf": "",
            "doi": doi,
        }, "PubMed"))
    return out


_BACKENDS = {
    "s2": _semantic_scholar,
    "arxiv": _arxiv,
    "openalex": _openalex,
    "crossref": _crossref,
    "ieee": _ieee,
    "pubmed": _pubmed,
}


# ---------------------------------------------------------------------------
# Merge + dedup
# ---------------------------------------------------------------------------

_TITLE_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def _normalize_title(title: str) -> str:
    return _TITLE_NORMALIZE_RE.sub(" ", (title or "").lower()).strip()


def _merge_dedup(per_backend: list[list[dict]], limit: int) -> list[dict]:
    """Interleave results from each backend (round-robin) so we get
    diversity in the top-N instead of one backend dominating; dedup by
    DOI then by normalized title. Promote rows with PDF links over
    landing-page-only rows when titles match."""
    seen_doi: set[str] = set()
    seen_title: set[str] = set()
    merged: list[dict] = []
    idx = 0
    while len(merged) < limit and any(idx < len(b) for b in per_backend):
        for backend_rows in per_backend:
            if idx >= len(backend_rows):
                continue
            row = backend_rows[idx]
            title = row.get("title") or ""
            if not title:
                continue
            tnorm = _normalize_title(title)
            doi = (row.get("doi") or "").lower()
            if doi and doi in seen_doi:
                # Same paper already merged — prefer the row with a PDF
                # link by patching the existing entry.
                if row.get("pdf"):
                    for existing in merged:
                        if (existing.get("doi") or "").lower() == doi:
                            if not existing.get("pdf"):
                                existing["pdf"] = row["pdf"]
                            break
                continue
            if tnorm and tnorm in seen_title:
                if row.get("pdf"):
                    for existing in merged:
                        if _normalize_title(existing.get("title") or "") == tnorm:
                            if not existing.get("pdf"):
                                existing["pdf"] = row["pdf"]
                            break
                continue
            if doi:
                seen_doi.add(doi)
            if tnorm:
                seen_title.add(tnorm)
            merged.append(row)
            if len(merged) >= limit:
                break
        idx += 1
    return merged


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def search(query: str, limit: int = 15) -> list[dict]:
    """Smart-routed multi-source search.

    Picks 2-3 backends based on the query domain and runs them in
    parallel. Merges results round-robin and dedups by DOI / title.
    On total failure (every backend errored or returned empty), falls
    back to a final arXiv attempt so the user gets *something*."""
    backends = _route_backends(query)
    # Cap parallel fan-out at 3 to keep the agent turn snappy.
    selected = backends[:3]
    # Each backend gets up to `limit` so round-robin can fill the
    # final list even if one source dominates relevance.
    coros = [_BACKENDS[name](query, limit) for name in selected]
    settled = await asyncio.gather(*coros, return_exceptions=True)
    per_backend: list[list[dict]] = []
    for name, result in zip(selected, settled):
        if isinstance(result, Exception):
            log.warning("paper_search backend %s failed (%s)",
                        name, type(result).__name__)
            continue
        if result:
            per_backend.append(result)
        else:
            log.info("paper_search backend %s returned empty", name)

    merged = _merge_dedup(per_backend, limit)
    if merged:
        return merged

    # Last-ditch arXiv fallback when everything else failed/empty.
    log.warning("paper_search: all routed backends failed for %r, "
                "trying arXiv last-ditch", query)
    try:
        return await _arxiv(query, limit)
    except Exception:
        log.exception("paper_search: arxiv last-ditch failed")
        return []
