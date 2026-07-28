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
_S2_FIELDS = "title,abstract,authors.name,year,venue,openAccessPdf,externalIds,url,citationCount"
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
#   {title, year, venue, authors, abstract, citations, url, pdf, doi, arxiv, source}
# `citations` = peer citation count when the backend exposes it
# (S2 only today). `source` tags which backend produced the row.
# ---------------------------------------------------------------------------

def _empty(p: dict, source: str) -> dict:
    """Normalize partial dicts into the shared schema.

    Core fields always present (title, year, venue, authors, etc.);
    OpenAlex-specific richer fields (is_oa, oa_status, concepts,
    institutions, referenced_count, paper_type, authors_total) pass
    through when the backend provides them. Other backends just lack
    those fields → the formatter silently skips empty entries."""
    return {
        "title": (p.get("title") or "").strip(),
        "year": p.get("year"),
        "venue": p.get("venue") or "",
        "authors": p.get("authors") or [],
        "authors_total": p.get("authors_total") or len(p.get("authors") or []),
        "institutions": p.get("institutions") or [],
        "abstract": (p.get("abstract") or "").strip(),
        "citations": p.get("citations"),
        "referenced_count": p.get("referenced_count"),
        "is_oa": p.get("is_oa") or False,
        "oa_status": p.get("oa_status") or "",
        "concepts": p.get("concepts") or [],
        "paper_type": p.get("paper_type") or "",
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
            "citations": p.get("citationCount"),
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
    out = [_openalex_to_unified(w) for w in r.json().get("results", [])]
    return [row for row in out if row]


def _openalex_to_unified(w: dict) -> dict | None:
    """Map one OpenAlex `work` JSON object into the unified paper
    schema. Pulls the richer fields beyond title/authors: citation
    count, OA status, concepts (subject classification), institutions
    (affiliations of the 3 lead authors), reference count, paper type."""
    title = (w.get("title") or w.get("display_name") or "").strip()
    if not title:
        return None
    year = w.get("publication_year")
    venue = ""
    host = w.get("primary_location") or {}
    src = host.get("source") or {}
    if src:
        venue = src.get("display_name") or ""
    authorships = w.get("authorships") or []
    authors: list[str] = []
    institutions: list[str] = []
    for a in authorships[:5]:  # first 5 for richer display
        au = a.get("author") or {}
        if au.get("display_name"):
            authors.append(au["display_name"])
        for inst in (a.get("institutions") or [])[:2]:
            name = inst.get("display_name")
            if name and name not in institutions:
                institutions.append(name)
    doi_raw = w.get("doi") or ""
    doi = doi_raw.replace("https://doi.org/", "") if doi_raw else ""
    pdf = host.get("pdf_url") or ""
    oa = w.get("open_access") or {}
    if not pdf:
        pdf = oa.get("oa_url") or ""
    is_oa = bool(oa.get("is_oa"))
    oa_status = oa.get("oa_status") or ""
    landing = host.get("landing_page_url") or doi_raw or ""
    abstract = ""
    inv = w.get("abstract_inverted_index")
    if inv:
        positions = []
        for word, idxs in inv.items():
            for i in idxs:
                positions.append((i, word))
        positions.sort()
        abstract = " ".join(w_ for _, w_ in positions)
    # Concepts (subject classifications) — OpenAlex returns scored
    # concepts; keep the top 3 by score so they actually represent
    # the paper.
    concepts_raw = w.get("concepts") or []
    concepts: list[str] = []
    for c in sorted(concepts_raw, key=lambda x: x.get("score") or 0,
                    reverse=True)[:3]:
        name = (c.get("display_name") or "").strip()
        if name:
            concepts.append(name)
    paper_type = (w.get("type") or "").strip()
    return _empty({
        "title": title,
        "year": year,
        "venue": venue,
        "authors": authors,
        "authors_total": len(authorships),
        "institutions": institutions,
        "abstract": abstract,
        "citations": w.get("cited_by_count"),
        "referenced_count": w.get("referenced_works_count"),
        "is_oa": is_oa,
        "oa_status": oa_status,
        "concepts": concepts,
        "paper_type": paper_type,
        "url": landing,
        "pdf": pdf,
        "doi": doi,
    }, "OpenAlex")


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
        # No sort_field / sort_order — IEEE API doesn't expose a
        # 'relevance' sort value (only article_number / title / author
        # / publication_year), so omitting the parameter entirely lets
        # IEEE's own search-ranking engine (keyword + citation + venue
        # weight) order the results. Previously hard-coded
        # `article_number desc` which is newest-by-article-id and
        # buried landmark papers under whatever IEEE indexed today.
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


# ---------------------------------------------------------------------------
# OpenAlex-backed advanced search + stats. We route /search_papers_advanced
# and /paper_stats through OpenAlex alone because (a) it has the broadest
# coverage, (b) the structured `filter=` query language supports
# author/venue/year/oa/citations refinements natively, (c) the JSON
# response is consistent enough for clean aggregation. The other 5
# backends keep their existing roles in the general /search_papers
# fan-out — this only adds new capabilities, doesn't replace.
# ---------------------------------------------------------------------------


def _build_openalex_filters(author: str = "", venue: str = "",
                            year_from: int | None = None,
                            year_to: int | None = None,
                            oa_only: bool = False,
                            min_citations: int | None = None,
                            concept: str = "",
                            type_: str = "") -> str:
    """Compose OpenAlex `filter=` parameter from optional refinements.
    Returns the comma-separated filter string (empty when no filters).

    OpenAlex filter docs:
      authorships.author.display_name.search
      primary_location.source.display_name.search
      publication_year (range with -)
      is_oa, cited_by_count (>N), concepts.display_name.search, type
    """
    parts: list[str] = []
    if author:
        parts.append(
            f"authorships.author.display_name.search:{author.strip()}")
    if venue:
        parts.append(
            f"primary_location.source.display_name.search:{venue.strip()}")
    if year_from and year_to:
        parts.append(f"publication_year:{year_from}-{year_to}")
    elif year_from:
        parts.append(f"publication_year:>{year_from - 1}")
    elif year_to:
        parts.append(f"publication_year:<{year_to + 1}")
    if oa_only:
        parts.append("is_oa:true")
    if min_citations and min_citations > 0:
        parts.append(f"cited_by_count:>{min_citations - 1}")
    if concept:
        parts.append(
            f"concepts.display_name.search:{concept.strip()}")
    if type_:
        parts.append(f"type:{type_.strip()}")
    return ",".join(parts)


async def search_advanced(query: str, limit: int = 15,
                          author: str = "", venue: str = "",
                          year_from: int | None = None,
                          year_to: int | None = None,
                          oa_only: bool = False,
                          min_citations: int | None = None,
                          concept: str = "",
                          type_: str = "") -> list[dict]:
    """OpenAlex-backed paper search with structured filters."""
    filters = _build_openalex_filters(
        author=author, venue=venue,
        year_from=year_from, year_to=year_to,
        oa_only=oa_only, min_citations=min_citations,
        concept=concept, type_=type_,
    )
    if not (query.strip() or filters):
        return []
    params: dict[str, str] = {"per-page": str(min(max(limit, 1), 50))}
    if query.strip():
        params["search"] = query.strip()
    if filters:
        params["filter"] = filters
    # Sort: relevance for keyword queries, recency when filter-only.
    if query.strip():
        params["sort"] = "relevance_score:desc"
    else:
        params["sort"] = "publication_date:desc"
    mailto = os.getenv("OPENALEX_MAILTO", "").strip()
    ua = (f"SecondBrainBot/1.0 (mailto:{mailto})"
          if mailto else "SecondBrainBot/1.0")
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                     headers={"User-Agent": ua}) as c:
            r = await c.get(_OPENALEX_API, params=params)
        if r.status_code != 200:
            log.warning("openalex advanced %d: %r",
                        r.status_code, r.text[:300])
            return []
        data = r.json()
    except Exception as e:
        log.warning("openalex advanced failed: %s", e)
        return []
    out: list[dict] = []
    for w in data.get("results", []):
        row = _openalex_to_unified(w)
        if row:
            out.append(row)
        if len(out) >= limit:
            break
    return out


async def _openalex_bulk(query: str, max_count: int = 400,
                         author: str = "", venue: str = "",
                         year_from: int | None = None,
                         year_to: int | None = None,
                         oa_only: bool = False,
                         min_citations: int | None = None,
                         concept: str = "",
                         type_: str = "") -> list[dict]:
    """Paginate OpenAlex up to max_count papers. Used by /paper_stats
    so the aggregate analytics work off a statistically meaningful
    sample. OpenAlex per-page max=200; we do 2 calls for 400 docs."""
    filters = _build_openalex_filters(
        author=author, venue=venue,
        year_from=year_from, year_to=year_to,
        oa_only=oa_only, min_citations=min_citations,
        concept=concept, type_=type_,
    )
    if not (query.strip() or filters):
        return []
    max_count = max(1, min(max_count, 1000))
    page_size = 200
    mailto = os.getenv("OPENALEX_MAILTO", "").strip()
    ua = (f"SecondBrainBot/1.0 (mailto:{mailto})"
          if mailto else "SecondBrainBot/1.0")
    out: list[dict] = []
    seen: set[str] = set()
    page = 1
    while len(out) < max_count:
        remaining = max_count - len(out)
        per_page = min(page_size, remaining)
        params: dict[str, str] = {
            "per-page": str(per_page),
            "page": str(page),
        }
        if query.strip():
            params["search"] = query.strip()
            params["sort"] = "relevance_score:desc"
        else:
            params["sort"] = "publication_date:desc"
        if filters:
            params["filter"] = filters
        try:
            async with httpx.AsyncClient(timeout=40,
                                         follow_redirects=True,
                                         headers={"User-Agent": ua}) as c:
                r = await c.get(_OPENALEX_API, params=params)
        except Exception as e:
            log.warning("openalex bulk page %d failed: %s", page, e)
            break
        if r.status_code != 200:
            log.warning("openalex bulk %d: %r",
                        r.status_code, r.text[:200])
            break
        try:
            data = r.json()
        except Exception:
            log.warning("openalex bulk JSON decode failed")
            break
        results = data.get("results") or []
        if not results:
            break
        added = 0
        for w in results:
            row = _openalex_to_unified(w)
            if not row:
                continue
            key = row.get("doi") or row.get("title", "").lower()[:80]
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
            added += 1
            if len(out) >= max_count:
                break
        if added == 0:
            break
        page += 1
        # OpenAlex returns 200 results per page; if we got less, that
        # was the last page.
        if len(results) < per_page:
            break
    return out


# ---------------------------------------------------------------------------
# Pure aggregation helpers — no I/O. Mirror the patentsearch stats
# layer but tuned for paper metadata (authors, venues, citations,
# concepts).
# ---------------------------------------------------------------------------


def _norm_author(name: str) -> str:
    """Collapse minor casing/spacing variants of an author name.
    Strips JR/SR/II/III/IV suffixes then Title-Cases everything so
    'LAU John H' / 'LAU JOHN H' / 'lau john h' all collapse to one
    'Lau John H' entry. Author names rarely contain meaningful
    acronyms (unlike corp names where TSMC/SK matters), so a flat
    Title Case is the right normalisation here."""
    if not name:
        return ""
    s = name.strip()
    for sfx in (" JR.", " JR", " SR.", " SR", " III", " II", " IV"):
        if s.upper().endswith(sfx):
            s = s[:len(s) - len(sfx)].strip()
            break
    # Title-case every token. str.title() lowercases the rest of
    # each token after the first letter, which is exactly the
    # author-name display convention.
    return s.title()


def compute_paper_stats(papers: list[dict]) -> dict:
    """Aggregate-by-everything overview: counts by author, venue,
    year, concept, OA share, citation distribution."""
    from collections import Counter
    by_author = Counter()
    by_venue = Counter()
    by_year = Counter()
    by_concept = Counter()
    by_institution = Counter()
    by_type = Counter()
    oa_count = 0
    citation_buckets = Counter()
    for p in papers:
        for au in (p.get("authors") or []):
            n = _norm_author(au)
            if n:
                by_author[n] += 1
        v = (p.get("venue") or "").strip()
        if v:
            by_venue[v[:80]] += 1
        yr = p.get("year")
        if yr:
            by_year[yr] += 1
        for c in (p.get("concepts") or []):
            by_concept[c] += 1
        for inst in (p.get("institutions") or []):
            by_institution[inst[:60]] += 1
        if p.get("is_oa"):
            oa_count += 1
        t = p.get("paper_type") or ""
        if t:
            by_type[t] += 1
        cit = p.get("citations") or 0
        if cit >= 1000:
            citation_buckets["1000+"] += 1
        elif cit >= 100:
            citation_buckets["100-999"] += 1
        elif cit >= 10:
            citation_buckets["10-99"] += 1
        elif cit >= 1:
            citation_buckets["1-9"] += 1
        else:
            citation_buckets["0"] += 1
    return {
        "total": len(papers),
        "by_author": by_author.most_common(15),
        "by_venue": by_venue.most_common(10),
        "by_year": sorted(by_year.items(), reverse=True),
        "by_concept": by_concept.most_common(10),
        "by_institution": by_institution.most_common(10),
        "by_type": by_type.most_common(5),
        "oa_count": oa_count,
        "oa_share": (oa_count / len(papers) * 100) if papers else 0,
        "citation_buckets": citation_buckets.most_common(),
    }


def compute_paper_trend(papers: list[dict],
                        top_authors: int = 5) -> dict:
    """Year-over-year time series for the TOP-N authors."""
    from collections import Counter, defaultdict
    counts = Counter()
    for p in papers:
        for au in (p.get("authors") or []):
            n = _norm_author(au)
            if n:
                counts[n] += 1
    top = [name for name, _ in counts.most_common(top_authors)]
    years_set: set[int] = set()
    grid: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for p in papers:
        yr = p.get("year")
        if not yr:
            continue
        years_set.add(yr)
        for au in (p.get("authors") or []):
            n = _norm_author(au)
            if n in top:
                grid[n][yr] += 1
    years = sorted(years_set)
    series = {name: [grid[name].get(y, 0) for y in years] for name in top}
    return {"years": years, "series": series, "top_authors": top}


def compute_paper_newcomers(papers: list[dict],
                            cutoff_months: int = 12) -> list[dict]:
    """Authors whose FIRST publication in this corpus is within
    cutoff_months. Useful for spotting rising researchers."""
    from collections import defaultdict
    from datetime import datetime
    first_year: dict[str, int] = {}
    totals: dict[str, int] = defaultdict(int)
    for p in papers:
        yr = p.get("year")
        if not yr:
            continue
        for au in (p.get("authors") or []):
            n = _norm_author(au)
            if not n:
                continue
            totals[n] += 1
            cur = first_year.get(n)
            if cur is None or yr < cur:
                first_year[n] = yr
    now_year = datetime.utcnow().year
    cutoff_year = now_year - (cutoff_months // 12)
    newcomers: list[dict] = []
    for name, fy in first_year.items():
        if fy >= cutoff_year:
            newcomers.append({
                "name": name,
                "first_year": fy,
                "total": totals[name],
            })
    newcomers.sort(key=lambda x: x["total"], reverse=True)
    return newcomers[:20]


def compute_paper_coauthors(papers: list[dict]) -> list[dict]:
    """Pairs of authors who appear together on the same paper.
    Surfaces collaboration networks. Papers naturally have richer
    pair data than patents since most papers have 3-10+ authors."""
    from collections import Counter
    from itertools import combinations
    pair_counts: Counter = Counter()
    for p in papers:
        authors = sorted({_norm_author(a)
                          for a in (p.get("authors") or []) if a})
        # Skip single-author papers and ones with too many authors
        # (paper-mill style 50+ author lists would explode the
        # combinations).
        if len(authors) < 2 or len(authors) > 10:
            continue
        for a, b in combinations(authors, 2):
            if not (a and b):
                continue
            pair_counts[(a, b)] += 1
    return [{"a": a, "b": b, "count": c}
            for (a, b), c in pair_counts.most_common(20)]


async def extract_paper_keywords(papers: list[dict],
                                 max_phrases: int = 30) -> list[str]:
    """Gemini Flash-Lite call to mine technical noun phrases from
    paper abstracts. Mirrors the patent version — same prompt shape
    so the agent learns one pattern."""
    from .. import config
    from ..llm.gemini import complete
    import json as _json
    import re as _re
    if not papers:
        return []
    snippets = []
    for i, p in enumerate(papers[:80], 1):
        ab = (p.get("abstract") or "")[:600]
        if ab:
            snippets.append(f"[{i}] {ab}")
    if not snippets:
        return []
    user = "\n".join(snippets)
    system = (
        "You are a technical-domain keyword extractor. Read the paper "
        "abstracts below and return the most representative technical "
        "noun phrases (concepts, methods, materials, model names, "
        "architectures). Prefer multi-word phrases over single words. "
        "Preserve original casing for acronyms (HBM, CMP, LLM, BERT, "
        "GAN, transformer). Output JSON only:\n"
        '{"keywords": ["phrase1", "phrase2", ...]}'
        f"\nReturn at most {max_phrases} phrases."
    )
    try:
        resp = await complete(
            model=config.SUMMARY_MODEL,
            system=system,
            user=user,
            max_tokens=2048,
            temperature=0.2,
            purpose="paper_keywords",
        )
        m = _re.search(r"\{.*\}", resp, _re.DOTALL)
        if not m:
            return []
        data = _json.loads(m.group(0))
    except Exception:
        log.exception("paper keyword extraction failed")
        return []
    out: list[str] = []
    seen: set[str] = set()
    for kw in (data.get("keywords") or []):
        kw_clean = (kw or "").strip()
        if not kw_clean:
            continue
        key = kw_clean.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(kw_clean)
        if len(out) >= max_phrases:
            break
    return out
