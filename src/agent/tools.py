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
from . import retrieve, papersearch, patentsearch

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
    from datetime import datetime
    raw = (doc or {}).get("ingested_at")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except Exception:
        return None
    return max(0.0, (datetime.utcnow() - ts).total_seconds() / 86400.0)


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
        out.append({
            "doc_id": doc_id,
            "title": doc.get("title", ""),
            "type": doc.get("type", ""),
            "kind": h["metadata"]["kind"],
            "snippet": h["text"][:800],
        })
    return {"hits": out, "count": len(out), "variants": variants}


async def search_papers(query: str, limit: int = 15) -> dict:
    results = await papersearch.search(query, limit=limit)
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


async def search_patents(query: str, limit: int = 15) -> dict:
    """Phase 1: USPTO PatentsView only (US patents).

    Returns the same skinny shape as search_papers so the agent can
    render both with the same '(P-2) 특허 결과 작성 형식' block.
    abstract is capped at 2500 chars to leave the model room to
    write a 3-5 sentence summary per top patent (~₩1-2 input
    overhead per call, negligible).
    """
    results = await patentsearch.search(query, limit=limit)
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
    resp = await _search_client.aio.models.generate_content(
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
    def _rank(h):
        doc = meta.get_doc(h["metadata"]["doc_id"]) or {}
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
        doc = meta.get_doc(doc_id) or {}
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
        bundles.append({
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
        })
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
            "Search external patents via USPTO PatentsView (Phase 1: US "
            "patents, ~8M corpus). Each result returns patent number, "
            "title, abstract, inventors, assignee, filing date, claims "
            "count, plus a Google Patents URL. Use when the user asks "
            "to FIND patents, prior art, IP filings — keywords like "
            "'특허/patent/출원/IP/prior art'. Not for paper search."
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
])
