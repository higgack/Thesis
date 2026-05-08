import logging
import threading

import chromadb
from chromadb.config import Settings
from .. import config
from ..llm.embed import embed

log = logging.getLogger(__name__)

_client = chromadb.PersistentClient(
    path=str(config.DATA_DIR / "chroma"),
    settings=Settings(anonymized_telemetry=False),
)
_collection = _client.get_or_create_collection(
    name="knowledge", metadata={"hnsw:space": "cosine"}
)


async def add_chunks(doc_id: str, chunks: list[dict]) -> None:
    """chunks: [{id, text, kind: 'summary'|'chunk', idx}]"""
    if not chunks:
        return
    vectors = await embed([c["text"] for c in chunks])
    _collection.add(
        ids=[c["id"] for c in chunks],
        embeddings=vectors,
        documents=[c["text"] for c in chunks],
        metadatas=[{"doc_id": doc_id, "kind": c["kind"], "idx": c["idx"]} for c in chunks],
    )


async def query(text: str, k: int = 5, kind: str | None = None) -> list[dict]:
    vec = (await embed([text], task_type="RETRIEVAL_QUERY"))[0]
    where = {"kind": kind} if kind else None
    res = _collection.query(query_embeddings=[vec], n_results=k, where=where)
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


def chunk_count() -> int:
    return _collection.count()
