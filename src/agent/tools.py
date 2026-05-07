"""Tool implementations exposed to the Gemini agent.

Each tool is async, returns a JSON-serializable dict, and is wrapped by a
FunctionDeclaration in TOOL_DECLARATIONS for the model."""
import logging
from urllib.parse import urlparse

from google import genai
from google.genai import types

from .. import config
from ..store import meta, vector
from ..ingest import pipeline
from . import retrieve, papersearch

log = logging.getLogger(__name__)

_search_client = genai.Client(api_key=config.GOOGLE_API_KEY)


async def search_my_brain(query: str, k: int = 5) -> dict:
    hits = await retrieve.hybrid(query, k=k)
    out = []
    for h in hits:
        doc_id = h["metadata"]["doc_id"]
        doc = meta.get_doc(doc_id) or {}
        out.append({
            "doc_id": doc_id,
            "title": doc.get("title", ""),
            "type": doc.get("type", ""),
            "kind": h["metadata"]["kind"],
            "snippet": h["text"][:800],
        })
    return {"hits": out, "count": len(out)}


async def search_papers(query: str, limit: int = 5) -> dict:
    results = await papersearch.search(query, limit=limit)
    slim = []
    for p in results:
        slim.append({
            "title": p["title"],
            "year": p.get("year"),
            "venue": p.get("venue"),
            "authors": p.get("authors") or [],
            "abstract": (p.get("abstract") or "")[:1200],
            "url": p.get("url") or "",
            "arxiv": p.get("arxiv"),
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
    """Cross-document overview: gather many summaries at once."""
    limit = max(1, min(int(limit), 80))
    hits = await vector.query(topic, k=limit * 2, kind="summary")
    seen = set()
    bundles = []
    for h in hits:
        doc_id = h["metadata"]["doc_id"]
        if doc_id in seen:
            continue
        seen.add(doc_id)
        doc = meta.get_doc(doc_id) or {}
        if type_filter and doc.get("type") != type_filter:
            continue
        bundles.append({
            "doc_id": doc_id,
            "title": doc.get("title", ""),
            "type": doc.get("type", ""),
            "summary": doc.get("summary", "")[:1500] or h["text"][:1500],
        })
        if len(bundles) >= limit:
            break
    return {"papers": bundles, "count": len(bundles)}


TOOL_DISPATCH = {
    "search_my_brain": search_my_brain,
    "search_papers": search_papers,
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
                    description="Max results (1-10). Default 5.",
                ),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="search_papers",
        description=(
            "Search external academic papers via Semantic Scholar / arXiv. "
            "Use when the user asks to FIND or DISCOVER papers, not for "
            "questions answerable from the saved brain."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(type=types.Type.STRING),
                "limit": types.Schema(
                    type=types.Type.INTEGER,
                    description="Max results (1-10). Default 5.",
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
            "search_my_brain."
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
            "Live Google search via Gemini grounding. STRICT GATING: only "
            "call when the user EXPLICITLY asks for web/external info — "
            "e.g. '웹/구글/인터넷에서', '검색해줘', '최신 추가해서', "
            "'외부에서', or clearly time-sensitive phrases like '오늘/방금/"
            "실시간/지금 시세/현재 주가/오늘 발표'. Do NOT call just because "
            "the user mentioned a company name or asked a general opinion "
            "question — try search_my_brain / compare_papers first. Do NOT "
            "auto-fallback to web_search when the brain returns few results; "
            "instead say what's missing."
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
