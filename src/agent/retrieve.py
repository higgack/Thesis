import asyncio
import json
import logging
import math
import os
import re
import threading
from datetime import datetime

from rank_bm25 import BM25Okapi

from .. import config
from ..llm.gemini import complete
from ..store import meta, vector

# Local cross-encoder reranker (BGE-reranker-base) — purpose-built for
# IR rerank, runs on CPU in <300ms for ~25 pairs. Replaces the
# Flash-Lite prompt-based rerank: same/better Korean quality, no LLM
# round trip, no per-call cost. Loaded lazily on first use so the
# model download (~380MB) doesn't block startup; cached under
# /app/data/hf_cache via HF_HOME so a container rebuild reuses it.
_LOCAL_RERANKER = None
_LOCAL_RERANKER_LOCK = threading.Lock()
_LOCAL_RERANKER_LOAD_FAILED = False


def _load_local_reranker():
    """Lazy singleton. Returns the CrossEncoder or None on failure;
    callers must fall back to the LLM reranker when None."""
    global _LOCAL_RERANKER, _LOCAL_RERANKER_LOAD_FAILED
    if _LOCAL_RERANKER is not None:
        return _LOCAL_RERANKER
    if _LOCAL_RERANKER_LOAD_FAILED:
        return None
    with _LOCAL_RERANKER_LOCK:
        if _LOCAL_RERANKER is not None:
            return _LOCAL_RERANKER
        if _LOCAL_RERANKER_LOAD_FAILED:
            return None
        try:
            os.environ.setdefault("HF_HOME", "/app/data/hf_cache")
            from sentence_transformers import CrossEncoder
            log.info("loading local reranker BAAI/bge-reranker-base ...")
            _LOCAL_RERANKER = CrossEncoder(
                "BAAI/bge-reranker-base", max_length=512,
            )
            log.info("local reranker ready")
        except Exception as e:
            log.warning("local reranker load failed (%s); will use LLM rerank", e)
            _LOCAL_RERANKER_LOAD_FAILED = True
            return None
    return _LOCAL_RERANKER


async def _local_rerank(query: str, candidates: list[dict], k: int) -> list[dict] | None:
    """Cross-encoder ranking. Returns None if the local model isn't
    available so the caller can fall back to Gemini.

    Enabled by default (LOCAL_RERANKER_ENABLED defaults to '1'). The
    BGE-reranker-base model is ~400MB resident; the bot container's
    mem_limit is 12GB (n2-standard-4 / 16GB VM), so there's ample
    headroom — this was previously gated OFF for an old 2GB e2-small
    VM with a 1500m cap that no longer exists. Set the env to '0' to
    force the Gemini Flash-Lite rerank fallback (e.g. for A/B testing
    or if a future VM downsize reintroduces a memory squeeze)."""
    if os.getenv("LOCAL_RERANKER_ENABLED", "1") != "1":
        return None
    if len(candidates) <= k:
        return candidates
    model = await asyncio.to_thread(_load_local_reranker)
    if model is None:
        return None
    try:
        pairs = [(query, c["text"][:512]) for c in candidates]
        scores = await asyncio.to_thread(model.predict, pairs)
        ranked = sorted(zip(scores, candidates), key=lambda x: float(x[0]), reverse=True)
        return [c for _, c in ranked[:k]]
    except Exception as e:
        log.warning("local rerank predict failed (%s); falling back to LLM", e)
        return None


def _recency_factor(ingested_at_iso: str) -> float:
    """Soft exponential decay so recent docs outrank stale ones with the
    same semantic relevance. Important for time-sensitive material like
    brokerage reports where last week's view supersedes last year's.

    Tuned (2026-05) to be gentler than the original 0.55+0.45·exp(-d/180)
    curve, which crushed older but deeper analyses (1y broker report
    semantic 0.92 lost to yesterday's 500-char alert with semantic 0.78).
    New curve: 0.70 + 0.30·exp(-d/365). 0d → 1.0, 6mo → ~0.85,
    1yr → ~0.81, 2yr+ → ~0.71. Half-life doubled, floor raised so old
    deep work stays competitive when recency × semantic conflict."""
    try:
        ts = datetime.fromisoformat(ingested_at_iso)
    except Exception:
        return 1.0
    days = max(0.0, (datetime.utcnow() - ts).total_seconds() / 86400.0)
    return 0.70 + 0.30 * math.exp(-days / 365.0)


def _depth_bonus(doc: dict) -> float:
    """Reward documents with substantial bodies. Broker reports,
    Substack long-form articles, and academic summaries earn a small
    score multiplier; one-line alerts or short forwards stay neutral.
    Combined with softer recency, this keeps deep analysis surfacing
    even when several short same-day docs cluster at the top."""
    summary = doc.get("summary") or ""
    chars = len(summary)
    if chars >= 8000:
        return 1.15
    if chars >= 4000:
        return 1.08
    return 1.0

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[\w가-힣]+", re.UNICODE)
_RERANK_INDEX_RE = re.compile(r"\[[\d,\s]+\]")
_EXPAND_JSON_RE = re.compile(r"\[.*?\]", re.DOTALL)


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


# BM25 index cache. Building the index (tokenize every chunk + IDF
# tables) over the full corpus is the expensive part — on a 176k-chunk
# corpus it pegged the event loop ~58s PER QUERY (retrieve rebuilt it
# inline every call), starving the heartbeat → watchdog restarts. Now
# the index is built once in a daemon thread and reused; per-query we
# only run get_scores (offloaded). Rebuild triggers when the corpus
# snapshot drifts; the `building` flag prevents pile-ups during ingest
# bursts. ids/docs/metas are stored aligned with the index so scoring
# maps back without an O(n) ids.index() lookup.
_BM25: dict = {"count": -1, "index": None, "ids": None,
               "docs": None, "metas": None, "building": False}
_BM25_LOCK = threading.Lock()


def _build_bm25_sync(chunks: list, count: int) -> None:
    try:
        ids = [c[0] for c in chunks]
        docs = [c[1] for c in chunks]
        metas = [c[2] for c in chunks]
        index = BM25Okapi([_tokenize(d) for d in docs])
        with _BM25_LOCK:
            _BM25.update(count=count, index=index, ids=ids,
                         docs=docs, metas=metas, building=False)
        log.info("bm25 index built (%d docs)", len(docs))
    except Exception:
        with _BM25_LOCK:
            _BM25["building"] = False
        log.exception("bm25 index build failed")


def _ensure_bm25() -> dict | None:
    """Return the cached BM25 bundle, kicking off a background rebuild
    when the corpus snapshot drifts. Returns None on cold start (caller
    falls back to dense-only this turn); returns a possibly-stale bundle
    while a rebuild is in flight (better recall than dense-only)."""
    snap = vector.all_documents_text()
    if not snap:
        return None
    n = len(snap)
    if _BM25["index"] is not None and _BM25["count"] == n:
        return _BM25
    with _BM25_LOCK:
        if not _BM25["building"]:
            _BM25["building"] = True
            threading.Thread(target=_build_bm25_sync, args=(snap, n),
                             daemon=True).start()
    return _BM25 if _BM25["index"] is not None else None


async def expand_query(query: str) -> list[str]:
    """Spawn 0-2 alternate phrasings that surface different facets of
    the same question. Cheap Flash-Lite call (~₩0.3) — caller still
    includes the original. Returns [] when the query is already
    specific enough so we don't waste tokens on '삼성전기 4분기 OPM'
    style queries."""
    if len(query) > 50 or len(query.split()) > 6:
        return []
    try:
        resp = await complete(
            model=config.SUMMARY_MODEL,
            system="검색어 확장 도우미. JSON 배열만 출력.",
            user=(
                "원본 검색어를 다른 측면을 강조하는 변형 2개로 확장해줘.\n"
                "예) '삼성전기' → [\"삼성전기 MLCC 실적\", \"삼성전기 IR 코멘트\"]\n"
                "예) 'HBM 동향' → [\"HBM 공급 양산 일정\", \"HBM 고객사 경쟁\"]\n"
                "원본은 포함하지 말고 변형만. 이미 충분히 좁으면 [].\n\n"
                f"원본: {query}\n출력:"
            ),
            max_tokens=120,
            temperature=0.3,
            purpose="query",
        )
        m = _EXPAND_JSON_RE.search(resp)
        if not m:
            return []
        data = json.loads(m.group(0))
        return [s.strip() for s in data
                if isinstance(s, str) and s.strip() and s.strip() != query][:2]
    except Exception as e:
        log.warning("query expand failed: %s", e)
        return []


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
            purpose="query",
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
    summary_hits, chunk_hits = await asyncio.gather(
        vector.query(query, k=over, kind="summary"),
        vector.query(query, k=over, kind="chunk"),
    )

    dense = {h["id"]: (1.0 - h["distance"], h) for h in summary_hits + chunk_hits}

    bundle = await asyncio.to_thread(_ensure_bm25)
    if bundle is not None and bundle["index"] is not None:
        index = bundle["index"]
        ids = bundle["ids"]
        docs = bundle["docs"]
        metas = bundle["metas"]
        scores = await asyncio.to_thread(index.get_scores, _tokenize(query))
        max_s = float(scores.max()) if len(scores) else 0.0
        for i, (cid, s) in enumerate(zip(ids, scores)):
            n = (s / max_s) if max_s else 0
            if cid in dense:
                old, hit = dense[cid]
                dense[cid] = (old + 0.4 * n, hit)
            elif n > 0.55:
                dense[cid] = (0.4 * n, {
                    "id": cid, "text": docs[i],
                    "metadata": metas[i], "distance": 1.0 - n,
                })
    else:
        log.info("bm25 index cold — dense-only retrieval this turn")

    # Batch-fetch parent doc metadata in one query (offloaded to thread
    # so we never block the event loop on SQLite).
    needed_ids = list({
        h["metadata"].get("doc_id")
        for _, (_, h) in dense.items()
        if h["metadata"].get("doc_id")
    })
    docs_map = await asyncio.to_thread(meta.get_docs_batch, needed_ids)

    mod_cache: dict[str, float] = {}
    for cid in list(dense.keys()):
        s, h = dense[cid]
        doc_id = h["metadata"].get("doc_id")
        if not doc_id:
            continue
        if doc_id not in mod_cache:
            d = docs_map.get(doc_id) or {}
            recency = (
                _recency_factor(d["ingested_at"])
                if d.get("ingested_at") else 1.0
            )
            mod_cache[doc_id] = recency * _depth_bonus(d)
        dense[cid] = (s * mod_cache[doc_id], h)

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

    local = await _local_rerank(query, candidates, k)
    if local is not None:
        return local
    return await _gemini_rerank(query, candidates, k)
