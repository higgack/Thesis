import json
import logging
import math
import re
from datetime import datetime

from rank_bm25 import BM25Okapi

from .. import config
from ..llm.gemini import complete
from ..store import meta, vector


def _recency_factor(ingested_at_iso: str) -> float:
    """Soft exponential decay so recent docs outrank stale ones with the
    same semantic relevance. Important for time-sensitive material like
    brokerage reports where last week's view supersedes last year's.
    0d → 1.0, 6mo → ~0.72, 1yr → ~0.62, 2yr+ → ~0.56."""
    try:
        ts = datetime.fromisoformat(ingested_at_iso)
    except Exception:
        return 1.0
    days = max(0.0, (datetime.utcnow() - ts).total_seconds() / 86400.0)
    return 0.55 + 0.45 * math.exp(-days / 180.0)

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[\w가-힣]+", re.UNICODE)
_RERANK_INDEX_RE = re.compile(r"\[[\d,\s]+\]")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


async def _gemini_rerank(query: str, candidates: list[dict], k: int) -> list[dict]:
    """Ask Gemini Flash-Lite to pick the most relevant candidates.
    Falls back to embedding-only ranking if anything goes wrong."""
    if len(candidates) <= k:
        return candidates
    items = "\n\n".join(
        f"[{i}] {c['text'][:400]}" for i, c in enumerate(candidates)
    )
    prompt = (
        f"Query: {query}\n\nCandidates:\n{items}\n\n"
        f"Return the indices of the {k} candidates most relevant to the query, "
        "ordered by relevance, as a JSON array. Example: [3, 0, 5]"
    )
    try:
        resp = await complete(
            model=config.SUMMARY_MODEL,
            system="You rank documents by relevance. Output only a JSON array of indices.",
            user=prompt,
            max_tokens=200,
            temperature=0.0,
        )
        match = _RERANK_INDEX_RE.search(resp)
        if not match:
            return candidates[:k]
        indices = json.loads(match.group(0))
        ranked = [candidates[i] for i in indices if 0 <= i < len(candidates)]
        seen: set[str] = set()
        out: list[dict] = []
        for h in ranked:
            cid = h["id"]
            if cid in seen:
                continue
            seen.add(cid)
            out.append(h)
            if len(out) >= k:
                break
        return out or candidates[:k]
    except Exception as e:
        log.warning("rerank failed (%s); falling back to embedding order", e)
        return candidates[:k]


async def hybrid(query: str, k: int = config.TOP_K) -> list[dict]:
    """Summary-first retrieval with document diversity + Gemini rerank.

    1. Pull a wider candidate set (k * 4) from dense + BM25 hybrid.
    2. Dedupe so each saved document contributes at most one chunk.
    3. Ask Gemini Flash-Lite to rerank by query relevance, return top k.
    """
    over = max(k * 4, 25)
    summary_hits = await vector.query(query, k=over, kind="summary")
    chunk_hits = await vector.query(query, k=over, kind="chunk")

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

    recency_cache: dict[str, float] = {}
    for cid in list(dense.keys()):
        s, h = dense[cid]
        doc_id = h["metadata"].get("doc_id")
        if not doc_id:
            continue
        if doc_id not in recency_cache:
            d = meta.get_doc(doc_id)
            recency_cache[doc_id] = (
                _recency_factor(d["ingested_at"])
                if d and d.get("ingested_at") else 1.0
            )
        dense[cid] = (s * recency_cache[doc_id], h)

    ranked = sorted(dense.values(), key=lambda x: x[0], reverse=True)

    seen_docs: set[str] = set()
    candidates: list[dict] = []
    for _, h in ranked:
        doc_id = h["metadata"].get("doc_id")
        if doc_id in seen_docs:
            continue
        seen_docs.add(doc_id)
        candidates.append(h)
        if len(candidates) >= k * 3:
            break

    return await _gemini_rerank(query, candidates, k)
