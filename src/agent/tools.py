"""Tool implementations exposed to the Gemini agent.

Each tool is async, returns a JSON-serializable dict, and is wrapped by a
FunctionDeclaration in TOOL_DECLARATIONS for the model."""
import asyncio
import logging
import re
from urllib.parse import urlparse

from google import genai
from google.genai import types

from .. import config
from ..store import meta, vector, cost
from ..ingest import pipeline
from . import (retrieve, papersearch, patentsearch,
               kisti_scienceon, kisti_ntis, translate)

log = logging.getLogger(__name__)

_search_client = genai.Client(api_key=config.GOOGLE_API_KEY)

# compare_papers quality filters — drop noise that inflates Pro
# synthesis cost without adding signal. The earlier behaviour pulled
# 50 docs by recency × semantic alone, so a 'Micro LED' query would
# sweep in raw-URL stubs, daily channel digests, 1-line ticker
# updates, etc. that share vocabulary with the real material.
#  - MAX_DISTANCE: cosine ceiling (loose — still on-topic below this).
#  - MIN_SUMMARY_CHARS: docs shorter are stubs (timestamps, one-line
#    forwards) with nothing to synthesize.
#  - DIGEST_TITLE_RE: multi-topic daily/period digests get
#    soft-penalised, not banned outright (see digest handling below).
_COMPARE_MAX_DISTANCE = 0.55
_COMPARE_MIN_SUMMARY_CHARS = 100
_DIGEST_TITLE_RE = re.compile(
    # Daily-aggregator emoji + year prefix — catches the user's auto
    # rollups regardless of their suffix wording. Real document titles
    # don't start with this combo (📊 한화솔루션 보고서, 📰 OCI 분석
    # never start with '📊 2026년'). Picks up '📊 2026년 05월 07일
    # 한국 목표가 상승여력' etc that the older 'requires 요약' regex
    # let through.
    r"^\s*[📊📋📰📈]\s*\d{4}년|"
    # Catch-all for the same titles when the bot stripped the emoji
    # at ingest time.
    r"\d{4}년\s*\d{1,2}월\s*\d{1,2}일.*"
    r"(요약|한국\s*목표가|한국\s*신고가|상승여력|신저가|급등주|급락주)|"
    r"채널\s*요약|"
    r"Substack\s*요약|"
    r"^\s*https?://[^\s]+\s*$|"
    r"^\s*\d{1,2}:\d{2}\s*$|"
    r"^\s*📅",
    re.IGNORECASE,
)

# Digest handling — three-pronged compromise so daily roundups stay
# usable for recent context but don't drown out focused 1차 자료:
#  A. Cap how many digests can appear in the final set.
#  B. Drop digests older than the recency window (their content has
#     usually been re-summarised in 1차 자료 by now).
#  C. Multiply digest rank score by the penalty so they only surface
#     when there aren't enough on-topic primary docs anyway.
_DIGEST_MAX_QUOTA = 5
_DIGEST_RECENCY_DAYS = 30
# Softened (2026-05) from 0.4 → 0.7. The aggressive 0.4 buried digests
# that had relevant info even when no primary source competed (e.g.
# digest semantic 0.85 × 0.4 = 0.34 lost to a barely-related 0.5 primary).
# The hard quota of 5 + recency floor still cap digest noise; the score
# penalty just needs to keep them below equivalent primary sources.
_DIGEST_RANK_PENALTY = 0.7


def _is_digest_title(title: str) -> bool:
    return bool(title and _DIGEST_TITLE_RE.search(title))


def _doc_age_days(doc: dict) -> float | None:
    """Days since ingest, or None if ingested_at is missing/malformed."""
    from datetime import datetime, timezone
    raw = (doc or {}).get("ingested_at")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
        if ts.tzinfo is not None:
            # A tz-aware row would make the naive-utcnow subtraction
            # raise TypeError mid-scoring; normalise instead.
            ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
        return max(0.0, (datetime.utcnow() - ts).total_seconds() / 86400.0)
    except Exception:
        return None


def _analyst_meta(doc: dict) -> dict:
    """Pull analyst-report metadata (company / brokerage / analyst /
    report_date) off a doc row so retrieval tools can surface it to the
    agent. The (F) company-analysis lens needs these to attribute
    guidance numbers per analyst and to A./F.-tag rows by date. Stored
    as a JSON string in the 'metadata' column at ingest; missing /
    corrupt → {} (never blocks retrieval)."""
    raw = doc.get("metadata")
    if not raw:
        return {}
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw)
        except Exception:
            return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k in ("company", "brokerage", "analyst", "report_date"):
        v = raw.get(k)
        if v:
            out[k] = v
    return out


async def search_my_brain(query: str, k: int = 10) -> dict:
    """Hybrid retrieval over the user's saved corpus.

    Runs the original query plus up to 2 LLM-generated facet variants
    in parallel, dedupes by doc_id, and keeps the top-k hits ranked by
    each variant's existing score. Costs ~₩2/query for vague inputs;
    specific inputs short-circuit to the original-only path so cost
    stays at ~₩0.6."""
    variants = await retrieve.expand_query(query)
    queries = [query] + variants
    if len(queries) == 1:
        all_hits_lists = [await retrieve.hybrid(query, k=k)]
    else:
        all_hits_lists = await asyncio.gather(
            *[retrieve.hybrid(q, k=k) for q in queries],
            return_exceptions=True,
        )
        all_hits_lists = [h for h in all_hits_lists if isinstance(h, list)]

    seen_docs: set[str] = set()
    merged: list[dict] = []
    for hits in all_hits_lists:
        for h in hits:
            doc_id = h["metadata"]["doc_id"]
            if doc_id in seen_docs:
                continue
            seen_docs.add(doc_id)
            merged.append(h)
            if len(merged) >= k:
                break
        if len(merged) >= k:
            break

    out = []
    for h in merged[:k]:
        doc_id = h["metadata"]["doc_id"]
        doc = meta.get_doc(doc_id) or {}
        item = {
            "doc_id": doc_id,
            "title": doc.get("title", ""),
            "type": doc.get("type", ""),
            "kind": h["metadata"]["kind"],
            "snippet": h["text"][:800],
        }
        item.update(_analyst_meta(doc))
        out.append(item)

    # P2 (wiki-first): when the LLM-Wiki has a synthesized page that
    # confidently matches this query, lead with it so the agent answers
    # from accumulated, cross-referenced knowledge instead of only
    # re-retrieved chunks (the whole point of the wiki pattern). This is
    # purely ADDITIVE — wiki_context() returns None unless WIKI_QUERY_FIRST
    # is on AND a page name matches, so retrieval recall is never reduced;
    # at worst nothing changes. Self-guarded so it can't break a query.
    try:
        from ..store import wiki
        wctx = wiki.wiki_context(query, max_chars=2500)
        if wctx:
            out.insert(0, {
                "doc_id": f"wiki:{wctx['topic']}",
                "title": f"📚 종합 위키: {wctx['topic']}",
                "type": "wiki",
                "kind": "wiki",
                "snippet": wctx["text"],
            })
            out = out[:k]
    except Exception:
        pass
    return {"hits": out, "count": len(out), "variants": variants}


async def search_papers(query: str, limit: int = 15) -> dict:
    results = await papersearch.search(query, limit=limit)
    # Translate first so the agent sees Korean titles+abstracts in
    # the tool result. Per user policy: every paper/patent rendered
    # to the user is Korean regardless of source. Overwrite path so
    # the slim dict below picks up the Korean strings directly.
    await translate.translate_and_overwrite(results)
    slim = []
    for p in results:
        slim.append({
            "title": p["title"],
            "year": p.get("year"),
            "venue": p.get("venue"),
            "authors": p.get("authors") or [],
            # Abstract cap raised 1200→2500 so the agent has room to
            # write 3-5 line summaries (methodology / results /
            # contribution) for the top papers instead of 1-line
            # title paraphrases. Input-side cost rises by ~₩1-2/call
            # (Flash @ ₩420/1M); negligible.
            "abstract": (p.get("abstract") or "")[:2500],
            "citations": p.get("citations"),
            "url": p.get("url") or "",
            "pdf": p.get("pdf") or "",
            "doi": p.get("doi") or "",
            "arxiv": p.get("arxiv"),
            "source": p.get("source") or "",
        })
    return {"results": slim, "count": len(slim)}


async def search_company_patents(applicant: str, limit: int = 50) -> dict:
    """KIPRIS Plus applicant-name patent lookup. Korean patents only.

    Different from search_patents: input is a company/applicant name
    (출원인), NOT a free-text query. KIPRIS's applicantNameSearchInfo
    doesn't keyword-match on title/abstract — it filters by exact
    applicant string. Use for "삼성전기 특허 알려줘" type questions.
    """
    results = await patentsearch.search_by_applicant(applicant, limit=limit)
    await translate.translate_and_overwrite(results)
    slim = []
    for p in results:
        slim.append({
            "title": p["title"],
            "patent_number": p.get("patent_number") or "",
            "date": p.get("date") or "",
            "year": p.get("year"),
            "inventors": p.get("inventors") or [],
            "assignee": p.get("assignee") or "",
            "abstract": (p.get("abstract") or "")[:2500],
            "claims_count": p.get("claims_count"),
            "url": p.get("url") or "",
            "source": p.get("source") or "",
        })
    return {"results": slim, "count": len(slim)}


async def search_patents(query: str, limit: int = 15) -> dict:
    """Global free-text patent search via EPO OPS (DOCDB — EP/WO/US/
    KR/JP/DE/CN coverage). Free 4GB/month tier.

    Returns the same skinny shape as search_papers so the agent can
    render both with the same '(P-2) 특허 결과 작성 형식' block.
    abstract is capped at 2500 chars to leave the model room to
    write a 3-5 sentence summary per top patent.
    """
    results = await patentsearch.search(query, limit=limit)
    await translate.translate_and_overwrite(results)
    slim = []
    for p in results:
        slim.append({
            "title": p["title"],
            "patent_number": p.get("patent_number") or "",
            "date": p.get("date") or "",
            "year": p.get("year"),
            "inventors": p.get("inventors") or [],
            "assignee": p.get("assignee") or "",
            "abstract": (p.get("abstract") or "")[:2500],
            "claims_count": p.get("claims_count"),
            "url": p.get("url") or "",
            "source": p.get("source") or "",
        })
    return {"results": slim, "count": len(slim)}


# ---------------------------------------------------------------------------
# KISTI ScienceON tools — Korean / international research portal.
# Records come back as metaCode-keyed dicts (TI=Title, AU=Author,
# AB=Abstract, CN=control number, DOI, IPC for patents, etc.). We
# pass them through largely unchanged so the agent can pick the
# fields it needs per question; CN is the key for deep-link
# follow-ups (paper_detail / patent_detail / patent_citations /
# report_detail all take a CN).
# ---------------------------------------------------------------------------

async def search_kr_papers(query: str, limit: int = 10) -> dict:
    rows = await kisti_scienceon.search_papers(query, limit=limit)
    await translate.translate_kisti_rows(rows)
    return {"results": rows, "count": len(rows)}


async def get_kr_paper_detail(cn: str) -> dict:
    row = await kisti_scienceon.get_paper_detail(cn)
    return {"result": row, "found": row is not None}


async def search_kr_patents_kisti(query: str, limit: int = 10) -> dict:
    """KISTI's patent index (international + KR) — keyword search,
    different from /search_patents (EPO OPS, global DOCDB) and
    /company_patents (KIPRIS applicant-only). ScienceON's coverage
    overlaps but adds Korean-language metadata not in EPO."""
    rows = await kisti_scienceon.search_patents(query, limit=limit)
    await translate.translate_kisti_rows(rows)
    return {"results": rows, "count": len(rows)}


async def get_kr_patent_detail(cn: str) -> dict:
    row = await kisti_scienceon.get_patent_detail(cn)
    return {"result": row, "found": row is not None}


async def get_kr_patent_citations(cn: str) -> dict:
    rows = await kisti_scienceon.get_patent_citations(cn)
    return {"results": rows, "count": len(rows)}


async def search_kr_reports(query: str, limit: int = 10) -> dict:
    rows = await kisti_scienceon.search_reports(query, limit=limit)
    await translate.translate_kisti_rows(rows)
    return {"results": rows, "count": len(rows)}


async def get_kr_report_detail(cn: str) -> dict:
    row = await kisti_scienceon.get_report_detail(cn)
    return {"result": row, "found": row is not None}


# Additional ScienceON contents (5/6 new wrappers — same pattern as
# above). All 9 ScienceON targets share metaCode result shape so the
# agent learns one render pattern for all.

async def search_kr_trends(query: str, limit: int = 10) -> dict:
    rows = await kisti_scienceon.search_trends(query, limit=limit)
    await translate.translate_kisti_rows(rows)
    return {"results": rows, "count": len(rows)}


async def search_kr_researchers(query: str, limit: int = 10) -> dict:
    rows = await kisti_scienceon.search_researchers(query, limit=limit)
    await translate.translate_kisti_rows(rows)
    return {"results": rows, "count": len(rows)}


async def search_kr_organs(query: str, limit: int = 10) -> dict:
    rows = await kisti_scienceon.search_organs(query, limit=limit)
    await translate.translate_kisti_rows(rows)
    return {"results": rows, "count": len(rows)}


async def search_kr_science_trends(query: str, limit: int = 10) -> dict:
    rows = await kisti_scienceon.search_science_trends(query, limit=limit)
    await translate.translate_kisti_rows(rows)
    return {"results": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# KISTI NTIS tools — national R&D project + classification +
# related-content. NTIS_API_KEY single env var covers all three
# endpoints (still needs separate 활용신청 per service on ntis.go.kr).
# ---------------------------------------------------------------------------

async def search_kr_rnd_projects(query: str, limit: int = 10) -> dict:
    rows = await kisti_ntis.search_projects(query, limit=limit)
    await translate.translate_kisti_rows(rows)
    return {"results": rows, "count": len(rows)}



async def get_kr_related_content(
    pjt_id: str, collection_type: str = "researchreport",
) -> dict:
    rows = await kisti_ntis.related_content(
        pjt_id, collection_type=collection_type,
    )
    return {"results": rows, "count": len(rows)}


# Four NTIS 전체용 services pre-built 2026-05-20 alongside the user's
# 활용신청. Calls degrade to empty rows until approval comes in.
async def search_kr_rnd_outcomes(
    query: str, kind: str = "paper", limit: int = 10,
) -> dict:
    rows = await kisti_ntis.search_outcomes(query, kind=kind, limit=limit)
    await translate.translate_kisti_rows(rows)
    return {"results": rows, "count": len(rows), "kind": kind}


async def search_kr_govt_reports(query: str, limit: int = 10) -> dict:
    rows = await kisti_ntis.search_research_reports(query, limit=limit)
    await translate.translate_kisti_rows(rows)
    return {"results": rows, "count": len(rows)}


async def search_kr_agency_rnd(query: str, limit: int = 10) -> dict:
    rows = await kisti_ntis.search_agency_rnd(query, limit=limit)
    await translate.translate_kisti_rows(rows)
    return {"results": rows, "count": len(rows)}


async def search_kr_rnd_issues(query: str, limit: int = 10) -> dict:
    rows = await kisti_ntis.search_rnd_issues(query, limit=limit)
    await translate.translate_kisti_rows(rows)
    return {"results": rows, "count": len(rows)}


async def ingest_url(url: str) -> dict:
    r = await pipeline.ingest_url(url)
    return {
        "status": r.get("status"),
        "doc_id": r.get("doc_id"),
        "title": r.get("title"),
        "type": r.get("type"),
        "chunks": r.get("chunks"),
    }


async def recent_docs(limit: int = 10) -> dict:
    items = meta.recent(limit)
    return {"items": items, "count": len(items)}


async def web_search(query: str) -> dict:
    """Live Google search via Gemini grounding.

    A separate Gemini call with only google_search enabled returns a brief
    factual summary plus grounding metadata. Wrapped as a custom tool so the
    outer agent (which uses function_declarations) can call it; Gemini API
    refuses to mix built-in google_search with function_declarations in the
    same request, hence the indirection."""
    # 120s cap — a stalled grounding call otherwise pins the agent step
    # until the outer 600s guard kills the whole answer.
    resp = await asyncio.wait_for(
        _search_client.aio.models.generate_content(
            model=config.ANSWER_MODEL,
            contents=query,
            config=types.GenerateContentConfig(
                system_instruction=(
                    "You are a search assistant. Use Google Search to fetch the "
                    "most recent factual information. Reply in Korean, in 3-7 "
                    "concise bullet points. Always include source domain in "
                    "brackets at end of each bullet, e.g., [bloomberg.com]."
                ),
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.1,
                max_output_tokens=1024,
            ),
        ),
        timeout=120,
    )
    cost.record_resp(config.ANSWER_MODEL, resp, purpose="query")
    text = ""
    sources: list[dict] = []
    if resp.candidates and resp.candidates[0]:
        cand = resp.candidates[0]
        if cand.content and cand.content.parts:
            text = "".join(p.text or "" for p in cand.content.parts if p.text)
        gm = getattr(cand, "grounding_metadata", None)
        if gm and getattr(gm, "grounding_chunks", None):
            for chunk in gm.grounding_chunks:
                web = getattr(chunk, "web", None)
                if web and getattr(web, "uri", None):
                    sources.append({
                        "url": web.uri,
                        "title": getattr(web, "title", "") or "",
                    })
    return {"answer": text.strip(), "sources": sources, "count": len(sources)}


async def compare_papers(topic: str, limit: int = 50,
                         type_filter: str = "") -> dict:
    """Cross-document overview: gather many summaries at once.

    Filters applied before ranking so Pro synthesis doesn't pay for
    noise:
      * semantic floor (distance > 0.55) — off-topic.
      * minimum summary length (<100 chars) — stubs, single-line
        forwards, timestamps.
      * digest handling — daily/period roundups get a rank penalty,
        a 30-day recency window, AND a hard quota of 5 in the final
        set. They aren't banned outright because recent digests can
        be the only source of intermediate market context."""
    limit = max(1, min(int(limit), 80))
    hits = await vector.query(topic, k=limit * 4, kind="summary")

    pre_count = len(hits)
    hits = [h for h in hits
            if float(h.get("distance", 1.0)) <= _COMPARE_MAX_DISTANCE]
    low_relevance_dropped = pre_count - len(hits)

    # Apply recency factor — newer docs surface first while older
    # comprehensive analyses still get a fair shot (0.55 floor).
    # Pure semantic similarity left to itself pulls dense old reports
    # ahead of recent daily summaries; this is what made queries like
    # "반도체 강세 이유" cite 2024 reports despite May-2026 ingest.
    from .retrieve import _recency_factor, _depth_bonus
    # One batched, off-loop metadata fetch. _rank + the bundle loop used
    # to call meta.get_doc per hit (up to limit*4 = 320 fresh SQLite
    # connects, twice) synchronously on the event loop.
    doc_ids = list({h["metadata"]["doc_id"] for h in hits
                    if h["metadata"].get("doc_id")})
    docs_map = await asyncio.to_thread(meta.get_docs_batch, doc_ids)
    def _rank(h):
        doc = docs_map.get(h["metadata"]["doc_id"]) or {}
        recency = _recency_factor(doc.get("ingested_at") or "")
        semantic = 1.0 - float(h.get("distance", 0) or 0)
        score = semantic * recency * _depth_bonus(doc)
        if _is_digest_title(doc.get("title") or ""):
            score *= _DIGEST_RANK_PENALTY
        return score
    hits = sorted(hits, key=_rank, reverse=True)

    seen: set[str] = set()
    bundles: list[dict] = []
    short_dropped = 0
    digest_old_dropped = 0
    digest_quota_dropped = 0
    digest_kept = 0
    for h in hits:
        doc_id = h["metadata"]["doc_id"]
        if doc_id in seen:
            continue
        seen.add(doc_id)
        doc = docs_map.get(doc_id) or {}
        if type_filter and doc.get("type") != type_filter:
            continue
        title = (doc.get("title") or "").strip()
        summary = (doc.get("summary") or h.get("text") or "").strip()
        if len(summary) < _COMPARE_MIN_SUMMARY_CHARS:
            short_dropped += 1
            continue
        if _is_digest_title(title):
            age_days = _doc_age_days(doc)
            if age_days is not None and age_days > _DIGEST_RECENCY_DAYS:
                digest_old_dropped += 1
                continue
            if digest_kept >= _DIGEST_MAX_QUOTA:
                digest_quota_dropped += 1
                continue
            digest_kept += 1
        bundle = {
            # doc_id intentionally omitted from the bundle. Earlier we
            # exposed it for follow-up lookups, but the model latched
            # onto the hex string as a citation key (writing
            # `[dbd2c79191dd6a96]` inline), the citation renumberer
            # then surfaced those hashes in the 출처 legend instead of
            # the human-readable titles. Title alone is enough — the
            # bot resolves citations back to docs via meta.search_title.
            "title": title,
            "type": doc.get("type", ""),
            "summary": summary[:1500],
        }
        # Analyst-report metadata (brokerage / analyst / report_date /
        # company) so the (F) company-analysis lens can build the
        # per-analyst guidance table and A./F.-tag rows by date. These
        # are human-readable so they don't reintroduce the hex-citation
        # problem that doc_id omission avoids.
        bundle.update(_analyst_meta(doc))
        bundles.append(bundle)
        if len(bundles) >= limit:
            break

    if (low_relevance_dropped or short_dropped
            or digest_old_dropped or digest_quota_dropped):
        log.info(
            "compare_papers filters: low_relevance=%d short=%d "
            "digest_old=%d digest_quota=%d digest_kept=%d "
            "→ kept %d/%d",
            low_relevance_dropped, short_dropped,
            digest_old_dropped, digest_quota_dropped, digest_kept,
            len(bundles), pre_count,
        )

    return {
        "papers": bundles,
        "count": len(bundles),
        "filtered": {
            "low_relevance": low_relevance_dropped,
            "short": short_dropped,
            "digest_old": digest_old_dropped,
            "digest_quota": digest_quota_dropped,
            "digest_kept": digest_kept,
        },
    }


TOOL_DISPATCH = {
    "search_my_brain": search_my_brain,
    "search_papers": search_papers,
    "search_patents": search_patents,
    "search_company_patents": search_company_patents,
    "search_kr_papers": search_kr_papers,
    "get_kr_paper_detail": get_kr_paper_detail,
    "search_kr_patents_kisti": search_kr_patents_kisti,
    "get_kr_patent_detail": get_kr_patent_detail,
    "get_kr_patent_citations": get_kr_patent_citations,
    "search_kr_reports": search_kr_reports,
    "get_kr_report_detail": get_kr_report_detail,
    "search_kr_trends": search_kr_trends,
    "search_kr_researchers": search_kr_researchers,
    "search_kr_organs": search_kr_organs,
    "search_kr_science_trends": search_kr_science_trends,
    "search_kr_rnd_projects": search_kr_rnd_projects,

    "get_kr_related_content": get_kr_related_content,
    "search_kr_rnd_outcomes": search_kr_rnd_outcomes,
    "search_kr_govt_reports": search_kr_govt_reports,
    "search_kr_agency_rnd": search_kr_agency_rnd,
    "search_kr_rnd_issues": search_kr_rnd_issues,
    "ingest_url": ingest_url,
    "recent_docs": recent_docs,
    "compare_papers": compare_papers,
    "web_search": web_search,
}


TOOL_DECLARATIONS = types.Tool(function_declarations=[
    types.FunctionDeclaration(
        name="search_my_brain",
        description=(
            "Search the user's personal saved knowledge base (papers, blogs, "
            "YouTube transcripts, notes). Use this first for any question "
            "that might be answerable from saved material."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(
                    type=types.Type.STRING,
                    description="Search query in Korean or English.",
                ),
                "k": types.Schema(
                    type=types.Type.INTEGER,
                    description="Max results (1-15). Default 10.",
                ),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="search_papers",
        description=(
            "Search external academic papers across multiple sources: "
            "Semantic Scholar, arXiv, OpenAlex, CrossRef, IEEE Xplore, "
            "PubMed. The router auto-picks the best 2-3 sources based on "
            "the query domain (e.g. semiconductor/packaging → IEEE; "
            "biomedical → PubMed; ML/AI → arXiv). Each result returns "
            "url + pdf when available so the user can download directly. "
            "Use when the user asks to FIND or DISCOVER papers, not for "
            "questions answerable from the saved brain."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(type=types.Type.STRING),
                "limit": types.Schema(
                    type=types.Type.INTEGER,
                    description="Max results (1-15). Default 15.",
                ),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="search_patents",
        description=(
            "Free-text patent search across global jurisdictions "
            "(EP / WO / US / KR / JP / DE / CN ...) via EPO OPS "
            "(European Patent Office Open Patent Services). Returns "
            "title + applicant + inventors + publication date + "
            "abstract + Google Patents URL per row. CQL backend "
            "(txt=<query> spans title + abstract + claims). Free "
            "4GB/month tier. Use when the question targets "
            "international patents or doesn't specify a Korean "
            "applicant. For applicant-name lookup of a specific "
            "Korean company prefer search_company_patents (KIPRIS)."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(type=types.Type.STRING),
                "limit": types.Schema(
                    type=types.Type.INTEGER,
                    description="Max results (1-15). Default 15.",
                ),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="search_company_patents",
        description=(
            "Look up Korean patents filed by a specific company via "
            "KIPRIS Plus (Korean Intellectual Property Office). Input "
            "is an APPLICANT NAME (출원인) — Korean company / "
            "university / institution — NOT a free-text keyword. "
            "Returns the most-recent KR patents that entity filed, "
            "with application/registration numbers, dates, titles, "
            "and Google Patents URLs. Use when the question targets "
            "what a specific Korean entity has patented "
            "(e.g., '삼성전기 특허 알려줘', 'SK하이닉스가 보유한 HBM "
            "특허'). For free-text keyword search use search_patents."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "applicant": types.Schema(
                    type=types.Type.STRING,
                    description=(
                        "Korean applicant name, exactly as it appears "
                        "in KIPRIS records. Examples: '삼성전기', "
                        "'SK하이닉스', '한양대학교 산학협력단'."
                    ),
                ),
                "limit": types.Schema(
                    type=types.Type.INTEGER,
                    description="Max results (1-30). Default 15.",
                ),
            },
            required=["applicant"],
        ),
    ),
    types.FunctionDeclaration(
        name="ingest_url",
        description=(
            "Save a URL (article, blog, YouTube, arXiv abs page, etc.) into "
            "the brain: fetch, chunk, summarize, embed, write to Obsidian. "
            "Call ONLY when the user explicitly asks to save/learn/remember a URL."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "url": types.Schema(type=types.Type.STRING),
            },
            required=["url"],
        ),
    ),
    types.FunctionDeclaration(
        name="recent_docs",
        description="List the user's most recently ingested documents.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "limit": types.Schema(
                    type=types.Type.INTEGER,
                    description="How many to return (1-30). Default 10.",
                ),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="compare_papers",
        description=(
            "Pull MANY relevant document summaries at once for cross-document "
            "synthesis: comparison tables, comprehensive overviews, literature "
            "reviews. Use when the user asks to compare/integrate/summarize "
            "ACROSS many saved papers ('하이브리드 본딩 논문 전체 정리해줘', "
            "'X와 Y 분야 차이 비교해줘'). For a single specific question, prefer "
            "search_my_brain. Note: results are pre-filtered to drop "
            "off-topic, short, and daily-digest docs — so the returned "
            "`count` is already the high-signal set."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "topic": types.Schema(
                    type=types.Type.STRING,
                    description="Topic / theme to gather around.",
                ),
                "limit": types.Schema(
                    type=types.Type.INTEGER,
                    description="Max documents to gather (1-80). Default 50. Beyond 50 the model starts skipping middle entries, so prefer 30-50.",
                ),
                "type_filter": types.Schema(
                    type=types.Type.STRING,
                    description=(
                        "Optional document type filter: 'paper', 'pdf', 'url',"
                        " 'youtube', 'text'. Empty for all types."
                    ),
                ),
            },
            required=["topic"],
        ),
    ),
    types.FunctionDeclaration(
        name="web_search",
        description=(
            "Live Google search via Gemini grounding. Trigger rule is "
            "simple: call ONLY when the user's message contains one of "
            "'웹', '구글', '인터넷' literally. Any other question — even "
            "with '최근/요즘/오늘' — goes to search_my_brain or "
            "compare_papers. No other triggers."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(
                    type=types.Type.STRING,
                    description="Search query in Korean or English.",
                ),
            },
            required=["query"],
        ),
    ),
    # KISTI ScienceON tools — 7 functions covering paper / patent /
    # report search + their CN-based detail lookups + patent citation
    # network. Use when the question targets Korean research output
    # specifically. Each call needs SCIENCEON_API_KEY + CLIENT_ID +
    # MAC_ADDRESS in .env (registered at scienceon.kisti.re.kr).
    types.FunctionDeclaration(
        name="search_kr_papers",
        description=(
            "Search Korean / international papers via KISTI ScienceON "
            "(99%+ SCIE / SCOPUS / KSCI coverage). Use when the user "
            "wants Korean academic research specifically, or when "
            "search_papers returns weak results for a Korean-language "
            "topic. Returns CN-keyed records — feed CN to "
            "get_kr_paper_detail for full abstract / authors / DOI."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(type=types.Type.STRING),
                "limit": types.Schema(
                    type=types.Type.INTEGER,
                    description="Max results (1-100). Default 10.",
                ),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_kr_paper_detail",
        description=(
            "Full paper detail (abstract, DOI, keywords, related "
            "papers) by ScienceON CN. Call after search_kr_papers "
            "when the user wants depth on a specific row."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "cn": types.Schema(
                    type=types.Type.STRING,
                    description="ScienceON control number from search results.",
                ),
            },
            required=["cn"],
        ),
    ),
    types.FunctionDeclaration(
        name="search_kr_patents_kisti",
        description=(
            "Patent search via KISTI ScienceON — overlaps with "
            "/search_patents (EPO OPS) but adds Korean-language "
            "metadata. Different from /company_patents (KIPRIS "
            "applicant-only). Returns CN-keyed records — feed CN to "
            "get_kr_patent_detail / get_kr_patent_citations for IPC, "
            "status, citation links."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(type=types.Type.STRING),
                "limit": types.Schema(
                    type=types.Type.INTEGER,
                    description="Max results (1-100). Default 10.",
                ),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_kr_patent_detail",
        description=(
            "Patent detail (IPC classifications, status, applicant) "
            "by ScienceON CN. Call after search_kr_patents_kisti."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "cn": types.Schema(type=types.Type.STRING),
            },
            required=["cn"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_kr_patent_citations",
        description=(
            "Forward + backward citation network for a patent CN. "
            "Use for prior-art / influence analysis."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "cn": types.Schema(type=types.Type.STRING),
            },
            required=["cn"],
        ),
    ),
    types.FunctionDeclaration(
        name="search_kr_reports",
        description=(
            "R&D report search via KISTI ScienceON — national R&D "
            "project deliverables (정부 R&D 보고서) and technology "
            "trend reports. Use for '국가 R&D 보고서 / 기술동향 "
            "보고서' style questions. Returns CN-keyed records — "
            "feed CN to get_kr_report_detail for full body + "
            "citation refs."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(type=types.Type.STRING),
                "limit": types.Schema(
                    type=types.Type.INTEGER,
                    description="Max results (1-100). Default 10.",
                ),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_kr_report_detail",
        description=(
            "R&D report detail (full bibliographic + citation refs) "
            "by ScienceON CN. Call after search_kr_reports."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "cn": types.Schema(type=types.Type.STRING),
            },
            required=["cn"],
        ),
    ),
    # Additional ScienceON contents (ATT/RESEARCHER/ORGAN/TREND).
    # All keyword-search; CN-keyed for detail lookup. SCENT/SNEWS
    # removed 2026-05 — undocumented searchField code.
    # via the existing browse pattern. Use whichever best matches
    # the user's intent — "해외 동향" → trends, "연구자 누구" →
    # researcher, "기관 활동" → organ, etc.
    types.FunctionDeclaration(
        name="search_kr_trends",
        description=(
            "Korean overseas S&T trends via KISTI ScienceON (ATT). "
            "Curated review articles about foreign tech developments. "
            "Different from NTIS 정책동향 — ATT is research-grade trend "
            "synthesis, not policy briefings."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(type=types.Type.STRING),
                "limit": types.Schema(
                    type=types.Type.INTEGER,
                    description="Max results (1-100). Default 10.",
                ),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="search_kr_researchers",
        description=(
            "KISTI ScienceON identified researcher index (RESEARCHER). "
            "Search Korean researcher profiles by name or field. "
            "Returns researcher identity + their publications/patents/"
            "reports list. Use when the question targets a SPECIFIC "
            "person ('김XX 연구자 논문', '양자컴퓨팅 연구자 누구')."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(type=types.Type.STRING),
                "limit": types.Schema(
                    type=types.Type.INTEGER,
                    description="Max results (1-100). Default 10.",
                ),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="search_kr_organs",
        description=(
            "KISTI ScienceON identified institution index (ORGAN). "
            "Search Korean research institutions / universities / "
            "companies + their publications. Combine with "
            "search_company_patents (KIPRIS) for richer company profile."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(type=types.Type.STRING),
                "limit": types.Schema(
                    type=types.Type.INTEGER,
                    description="Max results (1-100). Default 10.",
                ),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="search_kr_science_trends",
        description=(
            "KISTI ScienceON Trend (TREND) — curated topic-trend "
            "reports with paper/patent statistics + expert commentary. "
            "Different from search_kr_trends (overseas ATT) — TREND "
            "is meta-analysis of Korean + international literature."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(type=types.Type.STRING),
                "limit": types.Schema(
                    type=types.Type.INTEGER,
                    description="Max results (1-100). Default 10.",
                ),
            },
            required=["query"],
        ),
    ),
    # KISTI NTIS tools — government R&D projects + classification
    # codes + related content recommendations. Single NTIS_API_KEY
    # covers all three (still needs per-service 활용신청 on the
    # NTIS portal). Use when the question is about who is doing
    # what national R&D ("국가 R&D 과제 / 정부 지원사업 / 연구 분류").
    types.FunctionDeclaration(
        name="search_kr_rnd_projects",
        description=(
            "Korean national R&D project search via NTIS. Returns "
            "rows with 과제번호 (pjtId, used by "
            "get_kr_related_content) + 과제명 + 수행기관 + "
            "연구책임자 + 연구비 + 기간. Use for '정부 R&D 사업', "
            "'국가 R&D 과제', 'XX 과제 누가 하나' style questions."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(type=types.Type.STRING),
                "limit": types.Schema(
                    type=types.Type.INTEGER,
                    description="Max results (1-100). Default 10.",
                ),
            },
            required=["query"],
        ),
    ),

    types.FunctionDeclaration(
        name="get_kr_related_content",
        description=(
            "Given a project ID (pjtId from search_kr_rnd_projects), "
            "surface related content. collection_type chooses what "
            "to surface: 'paper' / 'patent' / 'researchreport' "
            "(default) / 'project'."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "pjt_id": types.Schema(
                    type=types.Type.STRING,
                    description="과제번호 from search_kr_rnd_projects.",
                ),
                "collection_type": types.Schema(
                    type=types.Type.STRING,
                    description=(
                        "'paper' / 'patent' / 'researchreport' / 'project'."
                    ),
                ),
            },
            required=["pjt_id"],
        ),
    ),
    # NTIS 전체용 extras (4 services pre-built 2026-05-20 alongside
    # 활용신청; degrade to zero rows until approval). 추가될 endpoint:
    # public_paper/patent/equipment, public_report, public_organization,
    # public_issue.
    types.FunctionDeclaration(
        name="search_kr_rnd_outcomes",
        description=(
            "NTIS 국가R&D 성과검색 — 정부R&D 과제에서 산출된 논문 / "
            "특허 / 연구시설장비 메타. ScienceON ARTI/PATENT 와 다른 "
            "인덱스 (정부R&D 한정). kind='paper' (default) / 'patent' "
            "/ 'equipment'. 활용신청 승인 대기 — 승인 전에는 빈 결과."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(type=types.Type.STRING),
                "kind": types.Schema(
                    type=types.Type.STRING,
                    description="'paper' / 'patent' / 'equipment'.",
                ),
                "limit": types.Schema(
                    type=types.Type.INTEGER,
                    description="Max results (1-100). Default 10.",
                ),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="search_kr_govt_reports",
        description=(
            "NTIS 국가R&D 연구보고서 검색 — 정부R&D 과제에서 산출된 "
            "연구보고서 메타. ScienceON REPORT 와 보완 (NTIS 가 예산 "
            "/ 주관기관 / 과제번호 같은 행정 메타에 더 정확). 활용신청 "
            "승인 대기."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(type=types.Type.STRING),
                "limit": types.Schema(
                    type=types.Type.INTEGER,
                    description="Max results (1-100). Default 10.",
                ),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="search_kr_agency_rnd",
        description=(
            "NTIS 수행기관 R&D현황 — 기관명으로 그 기관의 정부R&D "
            "수행 과제 / 예산 / 논문 통계 조회. 회사 분석 / IR 자료 / "
            "출연(연) 비교에 유용. 활용신청 승인 대기."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(
                    type=types.Type.STRING,
                    description="기관명 (예: KAIST / 삼성전자).",
                ),
                "limit": types.Schema(
                    type=types.Type.INTEGER,
                    description="Max results (1-100). Default 10.",
                ),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="search_kr_rnd_issues",
        description=(
            "NTIS 이슈로보는R&D — 최신 과학기술 이슈 + 관련 정부R&D "
            "현황 / 키워드 / 트렌드. ScienceON TREND 와 비슷하지만 "
            "정부R&D 한정 코퍼스. 활용신청 승인 대기."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(type=types.Type.STRING),
                "limit": types.Schema(
                    type=types.Type.INTEGER,
                    description="Max results (1-100). Default 10.",
                ),
            },
            required=["query"],
        ),
    ),
])
