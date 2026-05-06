import re
from rank_bm25 import BM25Okapi
from ..store import vector
from .. import config


_TOKEN_RE = re.compile(r"[\w가-힣]+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


async def hybrid(query: str, k: int = config.TOP_K) -> list[dict]:
    """Summary-first retrieval: prefer summary chunks, fall back to raw chunks
    only when needed. Combines dense + BM25 lexical scores."""
    summary_hits = await vector.query(query, k=k, kind="summary")
    chunk_hits = await vector.query(query, k=k, kind="chunk")

    dense = {h["id"]: (1.0 - h["distance"], h) for h in summary_hits + chunk_hits}

    all_chunks = vector.all_documents_text()
    if all_chunks:
        ids, docs, _ = zip(*all_chunks)
        bm25 = BM25Okapi([_tokenize(d) for d in docs])
        scores = bm25.get_scores(_tokenize(query))
        max_s = max(scores) if scores.any() else 1.0
        for cid, s in zip(ids, scores):
            n = (s / max_s) if max_s else 0
            if cid in dense:
                old, hit = dense[cid]
                dense[cid] = (old + 0.4 * n, hit)
            elif n > 0.55:
                doc_idx = ids.index(cid)
                dense[cid] = (0.4 * n, {
                    "id": cid, "text": docs[doc_idx],
                    "metadata": all_chunks[doc_idx][2], "distance": 1.0 - n,
                })

    ranked = sorted(dense.values(), key=lambda x: x[0], reverse=True)
    summaries = [h for s, h in ranked if h["metadata"]["kind"] == "summary"][:k]
    chunks = [h for s, h in ranked if h["metadata"]["kind"] == "chunk"][:k]

    seen = set()
    out = []
    for h in summaries + chunks:
        if h["id"] in seen:
            continue
        seen.add(h["id"])
        out.append(h)
        if len(out) >= k:
            break
    return out
