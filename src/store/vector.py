import asyncio
import hashlib
import logging
import os
import threading

import chromadb
from chromadb.config import Settings
from .. import config
from ..llm.embed import embed

log = logging.getLogger(__name__)


def _chunk_text_hash(text: str) -> str:
    """Stable SHA1 of light-normalised chunk text. Whitespace collapse +
    lowercase so trivial format differences (line wrap / capitalisation)
    map to the same cache key. Returns first 16 hex chars — 2^64
    namespace is plenty for personal-scale corpora."""
    norm = " ".join((text or "").split()).lower()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]

# Separate collection per embedding backend — different dimensions
# (Gemini 3072 vs BGE-M3 1024) can't coexist inside one Chroma
# collection. Switching EMBED_BACKEND to 'bge-m3' before the migration
# script has run yields an empty new collection, which is fine — the
# bot will repopulate it as docs are re-ingested or via migrate script.
_EMBED_BACKEND = os.getenv("EMBED_BACKEND", "gemini").lower()
COLLECTION_NAME = (
    "knowledge_bge_m3" if _EMBED_BACKEND == "bge-m3" else "knowledge"
)

_client = chromadb.PersistentClient(
    path=str(config.DATA_DIR / "chroma"),
    settings=Settings(anonymized_telemetry=False),
)
_collection = _client.get_or_create_collection(
    name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
)
log.info("vector collection=%s (backend=%s)", COLLECTION_NAME, _EMBED_BACKEND)

# Chroma calls now run via asyncio.to_thread to keep the event loop
# free (so the typing indicator + concurrent Q&A never stall behind a
# big HNSW write/search). This lock serialises those off-loop DB ops so
# we don't increase write concurrency past what the single-loop design
# already had — PersistentClient/SQLite can raise 'database is locked'
# under parallel writes. Held only around the DB call, never around the
# network embed() above it.
_CHROMA_LOCK = asyncio.Lock()


async def add_chunks(doc_id: str, chunks: list[dict]) -> None:
    """chunks: [{id, text, kind: 'summary'|'chunk', idx}].

    Chunk-level dedup ([B] cost cut, 2026-05): for kind='chunk' items
    we hash the text and check meta.chunk_embed_cache. Hits pull the
    embedding out of Chroma via _collection.get instead of calling
    Gemini again — recurring disclaimers / template footers / signature
    blocks across many docs share one embedding compute. Summary
    chunks are per-doc unique by construction so they always re-embed.
    """
    if not chunks:
        return
    from . import meta as _meta

    # Step 1: hash chunk-kind items; summary stays None (always re-embed).
    hashes: list[str | None] = [
        _chunk_text_hash(c["text"]) if c.get("kind") == "chunk" else None
        for c in chunks
    ]

    # Step 2: bulk lookup which hashes already have an embedding in
    # Chroma we can reuse.
    cache_keys = [h for h in hashes if h]
    cached_map = _meta.chunk_embed_lookup(cache_keys) if cache_keys else {}

    # Step 3: fetch the actual embedding vectors for cache hits from
    # Chroma in one batched .get call. If a cached chroma_id was
    # later /forgotten, Chroma returns no embedding for that id — we
    # fall back to fresh embedding for those.
    referenced_chroma_ids = sorted({cached_map[h] for h in cache_keys
                                    if h in cached_map})
    cached_vectors: dict[str, list[float]] = {}
    if referenced_chroma_ids:
        try:
            async with _CHROMA_LOCK:
                got = await asyncio.to_thread(
                    _collection.get, ids=referenced_chroma_ids,
                    include=["embeddings"],
                )
            # got["embeddings"] is a numpy array — `arr or []` raises
            # "truth value of an array is ambiguous", so guard with
            # `is None`. The old `or []` made every cache hit throw and
            # fall through to a needless re-embed (cost + load).
            _got_ids = got.get("ids")
            _got_embs = got.get("embeddings")
            _got_ids = [] if _got_ids is None else _got_ids
            _got_embs = [] if _got_embs is None else _got_embs
            for cid, vec in zip(_got_ids, _got_embs):
                if vec is not None and len(vec) > 0:
                    cached_vectors[cid] = (vec.tolist()
                                           if hasattr(vec, "tolist")
                                           else list(vec))
        except Exception:
            log.exception("chunk cache: chroma fetch failed, re-embedding")

    # Step 4: identify which chunks still need a fresh Gemini embed.
    need_fresh_idx: list[int] = []
    need_fresh_text: list[str] = []
    final_vectors: list[list[float] | None] = [None] * len(chunks)
    for i, (c, h) in enumerate(zip(chunks, hashes)):
        cached_id = cached_map.get(h) if h else None
        vec = cached_vectors.get(cached_id) if cached_id else None
        if vec is not None:
            final_vectors[i] = vec
        else:
            need_fresh_idx.append(i)
            need_fresh_text.append(c["text"])

    # Step 5: embed the misses (one batched Gemini call).
    if need_fresh_text:
        fresh = await embed(need_fresh_text)
        for slot, vec in zip(need_fresh_idx, fresh):
            final_vectors[slot] = vec

    reused = len(chunks) - len(need_fresh_idx)
    if reused:
        log.info("chunk dedup: reused %d/%d embeddings (saved %d Gemini calls)",
                 reused, len(chunks), reused)

    # Off the event loop — a large doc's Chroma write (HNSW insert +
    # SQLite) otherwise blocks the loop for ~seconds, freezing the
    # typing indicator + every concurrent Q&A.
    async with _CHROMA_LOCK:
        await asyncio.to_thread(
            _collection.add,
            ids=[c["id"] for c in chunks],
            embeddings=final_vectors,
            documents=[c["text"] for c in chunks],
            metadatas=[{"doc_id": doc_id, "kind": c["kind"], "idx": c["idx"]} for c in chunks],
        )

    # Step 6: remember newly-embedded chunks for future reuse. Only
    # the freshly-embedded ones — the cached hits already had a row.
    new_pairs = [
        (hashes[i], chunks[i]["id"])
        for i in need_fresh_idx
        if hashes[i] is not None
    ]
    if new_pairs:
        _meta.chunk_embed_remember(new_pairs)


async def query(text: str, k: int = 5, kind: str | None = None) -> list[dict]:
    vec = (await embed([text], task_type="RETRIEVAL_QUERY"))[0]
    where = {"kind": kind} if kind else None
    # Off the event loop — HNSW search over the full corpus blocks the
    # loop (and the typing indicator) otherwise. On the Q&A hot path.
    async with _CHROMA_LOCK:
        res = await asyncio.to_thread(
            _collection.query, query_embeddings=[vec], n_results=k, where=where,
        )
    if not res["ids"] or not res["ids"][0]:
        return []
    out = []
    for i, cid in enumerate(res["ids"][0]):
        out.append({
            "id": cid,
            "text": res["documents"][0][i],
            "metadata": res["metadatas"][0][i],
            "distance": res["distances"][0][i],
        })
    return out


_BM25_CACHE: dict = {"count": -1, "data": None, "building": False}
_BM25_LOCK = threading.Lock()


def _scan_all_chunks() -> list[tuple[str, str, dict]]:
    """Paginated scan because Chroma's unbounded get() trips SQLite's
    bind-parameter limit on large corpora."""
    out: list[tuple[str, str, dict]] = []
    BATCH = 500
    offset = 0
    while True:
        res = _collection.get(
            include=["documents", "metadatas"],
            limit=BATCH, offset=offset,
        )
        ids = res.get("ids") or []
        if not ids:
            break
        out.extend(zip(ids, res["documents"], res["metadatas"]))
        if len(ids) < BATCH:
            break
        offset += BATCH
    return out


def _bm25_build_sync() -> None:
    """Heavy work: pull every chunk + memoize. Runs on a worker thread
    so the event loop stays responsive."""
    try:
        n = _collection.count()
        log.info("bm25 corpus scan starting (%d chunks)", n)
        data = _scan_all_chunks()
        with _BM25_LOCK:
            _BM25_CACHE["data"] = data
            _BM25_CACHE["count"] = n
            _BM25_CACHE["building"] = False
        log.info("bm25 corpus scan done (%d chunks cached)", len(data))
    except Exception:
        with _BM25_LOCK:
            _BM25_CACHE["building"] = False
        log.exception("bm25 corpus scan failed")


def all_documents_text() -> list[tuple[str, str, dict]] | None:
    """Cached corpus snapshot for BM25 indexing.

    Returns None if the cache isn't ready yet — caller should fall back
    to dense-only retrieval. Triggers a background rebuild whenever the
    chunk count drifts (after ingest) or the cache is empty. Avoids
    blocking the event loop on multi-thousand-chunk scans."""
    n = _collection.count()
    cached = _BM25_CACHE["data"]
    if cached is not None and _BM25_CACHE["count"] == n:
        return cached
    with _BM25_LOCK:
        if not _BM25_CACHE["building"]:
            _BM25_CACHE["building"] = True
            threading.Thread(target=_bm25_build_sync, daemon=True).start()
    return cached  # may still be a stale snapshot, or None on cold start


def warm_bm25_cache() -> None:
    """Kick off the corpus scan immediately so the very first user query
    doesn't pay the multi-thousand-chunk tax."""
    all_documents_text()


def delete_doc(doc_id: str) -> int:
    res = _collection.get(where={"doc_id": doc_id})
    if not res["ids"]:
        return 0
    _collection.delete(ids=res["ids"])
    return len(res["ids"])


def get_doc_chunks(doc_id: str) -> list[dict]:
    """Return every chunk for one doc, sorted by idx (summary first
    via idx=-1 convention). Used by /show to dump the full original
    body when the summary alone is insufficient. Each item is
    {id, text, idx, kind}."""
    if not doc_id:
        return []
    res = _collection.get(
        where={"doc_id": doc_id},
        include=["documents", "metadatas"],
    )
    ids = res.get("ids") or []
    docs = res.get("documents") or []
    metas = res.get("metadatas") or []
    out: list[dict] = []
    for cid, text, md in zip(ids, docs, metas):
        idx = (md or {}).get("idx", 0)
        kind = (md or {}).get("kind", "chunk")
        out.append({"id": cid, "text": text or "", "idx": idx, "kind": kind})
    out.sort(key=lambda c: c["idx"])
    return out


def chunk_count() -> int:
    return _collection.count()


def chunk_counts(doc_ids: list[str]) -> dict[str, int]:
    """Map doc_id → chunk count. Single ChromaDB metadata-only query
    so /find can label every result with its size (e.g. '35청크')
    without firing N separate get-by-doc round trips. Excludes the
    idx=-1 summary row so the number reflects body chunks only."""
    if not doc_ids:
        return {}
    res = _collection.get(
        where={"doc_id": {"$in": list(doc_ids)}},
        include=["metadatas"],
    )
    counts: dict[str, int] = {d: 0 for d in doc_ids}
    for md in res.get("metadatas") or []:
        did = (md or {}).get("doc_id")
        kind = (md or {}).get("kind", "chunk")
        if did in counts and kind == "chunk":
            counts[did] += 1
    return counts
