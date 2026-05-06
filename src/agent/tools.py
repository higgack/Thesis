"""Tool implementations exposed to the Gemini agent.

Each tool is async, returns a JSON-serializable dict, and is wrapped by a
FunctionDeclaration in TOOL_DECLARATIONS for the model."""
import logging
from google.genai import types

from ..store import meta, vector
from ..ingest import pipeline
from . import retrieve, papersearch

log = logging.getLogger(__name__)


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


TOOL_DISPATCH = {
    "search_my_brain": search_my_brain,
    "search_papers": search_papers,
    "ingest_url": ingest_url,
    "recent_docs": recent_docs,
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
])
