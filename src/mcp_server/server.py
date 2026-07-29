"""MCP server exposing this bot's knowledge base (RAG archive, wiki,
notes, knowledge graph) as read-only tools for external AI clients
(Claude Desktop, GitHub Copilot, Cursor, etc.) via Streamable HTTP.

Runs as its own lightweight container/process — deliberately does NOT
load the Chroma vector index. `vector.py`'s `chromadb.PersistentClient`
memory-maps the full HNSW index into whatever process opens it (~4GB+
in the bot process today); a second process doing the same would
roughly double vector-search RAM on a VM already shared with other
projects (2026-07-29 decision, see CLAUDE.md). So `search_knowledge_base`
below is FTS5 keyword search over meta.db (already built for the
dashboard/BM25 path) — not semantic search. Everything else here is
sqlite/file reads, same low footprint as src/dashboard/server.py.

Auth: a single shared bearer token (MCP_TOKEN), checked in
_BearerAuthMiddleware. Deliberately NOT using FastMCP's built-in
auth=/token_verifier= machinery — that requires standing up real OAuth
resource-server metadata (issuer_url, resource_server_url are mandatory
on AuthSettings), which is the wrong tool for "one shared secret,
configured as a header in an mcp.json file" (mirrors the simple
path-token pattern src/dashboard/server.py already uses elsewhere in
this repo).
"""
from __future__ import annotations

import hmac
import logging
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..notes import store as notes_store
from ..store import kg, meta, wiki

log = logging.getLogger(__name__)

mcp = FastMCP(
    "thesis-knowledge-base",
    instructions=(
        "개인 텔레그램 RAG 지식베이스(학습된 자료, 자동 합성 위키, 지식그래프, "
        "학습노트) 조회 전용 서버. 전부 읽기 전용 — 아무 것도 수정/삭제하지 "
        "않는다. search_knowledge_base는 의미기반(semantic) 검색이 아니라 "
        "키워드(FTS5) 매칭이므로 정확한 용어/고유명사로 질의할 것."
    ),
    # This server is reached over the VM's public IP by remote MCP
    # clients (Claude Desktop/Copilot on the user's laptop), not from a
    # browser page — so the DNS-rebinding Host-header check (meant to
    # stop a malicious webpage from reaching a "localhost-only" service)
    # doesn't apply, and its default allowlist would reject every real
    # request. _BearerAuthMiddleware below is the actual security
    # boundary — every request needs the shared token regardless of Host.
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False),
)


@mcp.tool()
def search_knowledge_base(query: str, k: int = 10) -> list[dict]:
    """RAG 아카이브(학습된 문서)에서 키워드로 검색.

    Args:
        query: 검색어 (한국어/영어, 정확한 용어일수록 결과가 좋음)
        k: 최대 결과 수 (기본 10)
    """
    k = max(1, min(k, 50))
    hits = meta.fts_search(query, k=k * 3)
    if not hits:
        return []
    best: dict[str, dict] = {}
    for h in hits:
        did = h["doc_id"]
        if did and (did not in best or h["score"] > best[did]["score"]):
            best[did] = h
    ordered = sorted(best.values(), key=lambda h: h["score"], reverse=True)[:k]
    docs = meta.get_docs_batch([h["doc_id"] for h in ordered])
    out = []
    for h in ordered:
        d = docs.get(h["doc_id"], {})
        out.append({
            "doc_id": h["doc_id"],
            "title": d.get("title"),
            "source": d.get("source"),
            "type": d.get("type"),
            "summary": d.get("summary"),
            "snippet": (h.get("text") or "")[:500],
            "score": round(h["score"], 3),
        })
    return out


@mcp.tool()
def list_wiki_topics() -> list[dict]:
    """자동 합성된 위키 토픽 전체 목록 (최신 갱신순). 각 항목은
    {topic, docs, updated, created}."""
    return wiki.list_topics()


@mcp.tool()
def get_wiki_page(topic: str) -> dict:
    """위키 토픽 하나의 전체 페이지(마크다운)를 가져온다. 정확한 이름이
    아니어도 별칭/부분일치로 기존 토픽에 매칭을 시도한다.

    Args:
        topic: 토픽 이름 (예: '삼성전자', 'HBM')
    """
    resolved = wiki.resolve_topic(topic)
    md = wiki.read_page(resolved)
    if md is None:
        return {"topic": topic, "found": False}
    return {"topic": topic, "resolved_topic": resolved, "found": True,
            "markdown": md}


@mcp.tool()
def search_notes(query: str, k: int = 10) -> list[dict]:
    """학습노트를 제목/카테고리 키워드로 검색 (의미기반 아님, 부분일치).

    Args:
        query: 검색어
        k: 최대 결과 수 (기본 10)
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    k = max(1, min(k, 50))
    out = []
    for n in notes_store.list_notes():
        hay = f"{n.get('title', '')} {n.get('category', '')}".lower()
        if q in hay:
            out.append({
                "id": n.get("id"), "title": n.get("title"),
                "category": n.get("category"), "updated": n.get("updated"),
                "source_type": n.get("source_type"),
            })
            if len(out) >= k:
                break
    return out


@mcp.tool()
def get_note(note_id: str) -> dict:
    """학습노트 하나의 전체 내용(마크다운 본문 포함)을 id로 가져온다."""
    n = notes_store.get_note(note_id)
    if not n:
        return {"id": note_id, "found": False}
    return {"found": True, **n}


@mcp.tool()
def kg_query(entity: str, limit: int = 40) -> list[dict]:
    """지식그래프에서 엔티티(회사/개념 등) 관련 관계(트리플)를 부분일치로
    검색한다.

    Args:
        entity: 엔티티 이름 (예: '삼성전자', 'HBM')
        limit: 최대 결과 수 (기본 40)
    """
    return kg.neighbors(entity, limit=max(1, min(limit, 200)))


@mcp.tool()
def kg_top_entities(limit: int = 15) -> list[dict]:
    """지식그래프에서 연결이 가장 많은(=중요한) 엔티티 목록. 각 항목은
    {name, deg}."""
    return kg.top_entities(limit=max(1, min(limit, 100)))


# ------------------------------------------------------------ auth ---

_TOKEN = (os.getenv("MCP_TOKEN") or "").strip()


def _eq(a: str, b: str) -> bool:
    try:
        return hmac.compare_digest(a, b)
    except TypeError:
        return a == b


class _BearerAuthMiddleware(BaseHTTPMiddleware):
    """Shared-secret gate. Fail-closed: an unset MCP_TOKEN rejects
    EVERY request (503), not "no auth" — same default as the
    dashboard's DASHBOARD_TOKEN check."""

    async def dispatch(self, request: Request, call_next):
        if not _TOKEN:
            return JSONResponse({"error": "MCP_TOKEN not configured"},
                               status_code=503)
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        if not token or not _eq(token, _TOKEN):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def build_app() -> Starlette:
    app = mcp.streamable_http_app()
    app.add_middleware(_BearerAuthMiddleware)
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    port = int(os.getenv("MCP_PORT", "8083"))
    if not _TOKEN:
        log.warning("MCP_TOKEN not set — server will reject ALL requests "
                    "until it's configured in .env")
    log.info("mcp server starting on 0.0.0.0:%d (endpoint: /mcp)", port)
    uvicorn.run(build_app(), host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
