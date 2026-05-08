import chromadb
from chromadb.config import Settings
from .. import config
from ..llm.embed import embed

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


_BM25_CACHE: dict = {"count": -1, "data": None}


def all_documents_text() -> list[tuple[str, str, dict]]:
    """Return all chunks for BM25 indexing: (id, text, metadata).

    Paginated because an unbounded `_collection.get()` on a large corpus
    trips Chroma's SQLite bind-parameter limit ("too many SQL
    variables"). Result is cached in-process keyed by chunk count, so
    we only rescan when the collection actually grew."""
    n = _collection.count()
    if _BM25_CACHE["count"] == n and _BM25_CACHE["data"] is not None:
        return _BM25_CACHE["data"]
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
    _BM25_CACHE["count"] = n
    _BM25_CACHE["data"] = out
    return out


def delete_doc(doc_id: str) -> int:
    res = _collection.get(where={"doc_id": doc_id})
    if not res["ids"]:
        return 0
    _collection.delete(ids=res["ids"])
    return len(res["ids"])


def chunk_count() -> int:
    return _collection.count()
