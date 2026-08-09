import asyncio
import json
import logging
import math
import os
import re
import threading
from datetime import datetime, timezone

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
    BGE-reranker-base model is ~400MB resident and already counted in
    the bot's ~4GB idle RSS; it fits under the 5500m mem_limit on the
    live n2-standard-2 / 8GB VM. Was gated OFF on an old 2GB e2-small
    VM (1500m cap), re-enabled once there was headroom. Set the env to '0' to
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
        if ts.tzinfo is not None:
            # Stored values are naive UTC today, but fromisoformat accepts
            # offsets — a single aware row would make the subtraction
            # below raise TypeError inside the scoring loop and kill
            # retrieval entirely. Normalise instead.
            ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
        days = max(0.0, (datetime.utcnow() - ts).total_seconds() / 86400.0)
    except Exception:
        return 1.0
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
_BM25_OFF_LOGGED = False


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
    when the corpus snapshot drifts. Returns None on cold start OR when
    the corpus exceeds BM25_MAX_CHUNKS (vector layer returns no snapshot
    → dense-only); returns a possibly-stale bundle while a rebuild is in
    flight (better recall than dense-only). Returns a dict COPY taken
    under the lock so callers can't see index/ids from different builds
    (the multi-key read was a torn-read race)."""
    snap = vector.all_documents_text()
    if not snap:
        return None
    n = len(snap)
    with _BM25_LOCK:
        if _BM25["index"] is not None and _BM25["count"] == n:
            return dict(_BM25)
        if not _BM25["building"]:
            _BM25["building"] = True
            try:
                threading.Thread(target=_build_bm25_sync, args=(snap, n),
                                 daemon=True).start()
            except Exception:
                # Thread spawn failure would otherwise leave `building`
                # stuck True forever → BM25 frozen until restart.
                _BM25["building"] = False
                log.exception("bm25 build thread spawn failed")
        return dict(_BM25) if _BM25["index"] is not None else None


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


# Reciprocal Rank Fusion constant. k=60 is the standard choice (Cormack
# et al. 2009; also what Cerebras's internal RAG writeup uses) — large
# enough that a #1 vs #2 rank difference doesn't swing the fused score
# wildly, small enough that rank position still matters more than noise.
_RRF_K = 60


async def hybrid(query: str, k: int = config.TOP_K) -> list[dict]:
    """Summary-first retrieval with document diversity + local/Gemini rerank.

    1. Pull a wider candidate set (k * 4) from dense + keyword search.
    2. Fuse the two rankings via Reciprocal Rank Fusion (RRF) — combines
       differently-scaled systems (cosine similarity vs FTS5 bm25) by rank
       position instead of a hand-tuned score weight.
    3. Dedupe so each saved document contributes at most one chunk.
    4. Rerank (local cross-encoder, falling back to Gemini) and return top k.
    """
    over = max(k * 4, 25)
    # Embed the query ONCE and reuse for both kind-filtered searches —
    # each vector.query() call otherwise fires its own identical Gemini
    # embedding round-trip (2× latency + cost on every question).
    from ..llm.embed import embed as _embed
    qvec = (await _embed([query], task_type="RETRIEVAL_QUERY"))[0]
    summary_hits, chunk_hits = await asyncio.gather(
        vector.query(query, k=over, kind="summary", vec=qvec),
        vector.query(query, k=over, kind="chunk", vec=qvec),
    )

    # Dense ranking, best (lowest distance) first.
    dense_hits = sorted(summary_hits + chunk_hits, key=lambda h: h["distance"])
    dense_rank = {h["id"]: i for i, h in enumerate(dense_hits)}
    hit_by_id = {h["id"]: h for h in dense_hits}

    # Keyword half: FTS5 (persistent, scales past BM25_MAX_CHUNKS, ~0 RAM).
    # Supersedes the in-memory rank_bm25, which disabled itself above 50k
    # chunks → dense-only at our corpus size. FTS-only hits (keyword match
    # outside the dense top) are reconstructed from the chunk id
    # ("<doc_id>:<idx>", idx=-1 → summary). FTS unavailable/empty → the
    # fts_rank map stays empty (dense-only fusion = prior behavior).
    fts_hits = await asyncio.to_thread(meta.fts_search, query, over)
    fts_hits_sorted = sorted(fts_hits, key=lambda h: h["score"], reverse=True)
    fts_rank = {h["id"]: i for i, h in enumerate(fts_hits_sorted)}
    for h in fts_hits_sorted:
        cid = h["id"]
        if cid in hit_by_id:
            continue
        try:
            idx = int(cid.rsplit(":", 1)[1])
        except Exception:
            idx = 0
        hit_by_id[cid] = {
            "id": cid, "text": h["text"],
            "metadata": {"doc_id": h["doc_id"],
                         "kind": "summary" if idx == -1 else "chunk",
                         "idx": idx},
        }

    # RRF: sum of 1/(k + rank) across every ranking a hit appears in.
    # Replaces the old ad-hoc "0.4 * normalized FTS score" blend, which
    # required calibrating a weight between two differently-scaled
    # systems; RRF needs no such tuning and degrades gracefully when one
    # side is empty.
    fused: dict[str, float] = {}
    for cid in set(dense_rank) | set(fts_rank):
        s = 0.0
        if cid in dense_rank:
            s += 1.0 / (_RRF_K + dense_rank[cid])
        if cid in fts_rank:
            s += 1.0 / (_RRF_K + fts_rank[cid])
        fused[cid] = s

    # Batch-fetch parent doc metadata in one query (offloaded to thread
    # so we never block the event loop on SQLite).
    needed_ids = list({
        hit_by_id[cid]["metadata"].get("doc_id")
        for cid in fused
        if hit_by_id[cid]["metadata"].get("doc_id")
    })
    docs_map = await asyncio.to_thread(meta.get_docs_batch, needed_ids)

    mod_cache: dict[str, float] = {}
    for cid in list(fused.keys()):
        h = hit_by_id[cid]
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
        fused[cid] *= mod_cache[doc_id]

    ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)

    seen_docs: set[str] = set()
    candidates: list[dict] = []
    for cid, _ in ranked:
        h = hit_by_id[cid]
        doc_id = h["metadata"].get("doc_id")
        if doc_id in seen_docs:
            continue
        seen_docs.add(doc_id)
        candidates.append(h)
        if len(candidates) >= k * 3:
            break

    local = await _local_rerank(query, candidates, k)
    top = local if local is not None else await _gemini_rerank(query, candidates, k)
    return await _expand_context(top)


# Only splice in a short window from each neighbor chunk, not the full
# ~1000-token chunk — a full-chunk splice on both sides could triple the
# prompt tokens for every hit (k=10 hits × up to 3x = real cost on every
# Q&A). A few hundred chars of lead-in/lead-out context still fixes the
# "cut off mid-sentence/mid-table" problem the reranked chunk alone has,
# at a small, bounded token cost.
_EXPAND_CHARS = 300


async def _expand_context(hits: list[dict]) -> list[dict]:
    """Splice in a short window from the immediately-adjacent chunk (same
    doc, idx±1) around each reranked chunk hit, so the LLM sees a bit of
    surrounding context instead of an isolated slice cut at an arbitrary
    chunk boundary (Cerebras's RAG writeup calls this out as a cheap,
    high-value step after reranking). Summary hits (idx=-1) and any hit
    missing doc_id/idx are left untouched — 'summary' isn't part of the
    chunk sequence. Best-effort: a fetch failure just skips expansion for
    that hit."""
    want_ids: list[str] = []
    for h in hits:
        md = h.get("metadata") or {}
        if md.get("kind") != "chunk":
            continue
        doc_id, idx = md.get("doc_id"), md.get("idx")
        if not doc_id or not isinstance(idx, int) or idx < 0:
            continue
        if idx > 0:
            want_ids.append(f"{doc_id}:{idx - 1}")
        want_ids.append(f"{doc_id}:{idx + 1}")
    if not want_ids:
        return hits
    neighbor_text = await vector.get_by_ids(want_ids)
    if not neighbor_text:
        return hits
    out = []
    for h in hits:
        md = h.get("metadata") or {}
        if md.get("kind") != "chunk":
            out.append(h)
            continue
        doc_id, idx = md.get("doc_id"), md.get("idx")
        if not doc_id or not isinstance(idx, int) or idx < 0:
            out.append(h)
            continue
        before_full = neighbor_text.get(f"{doc_id}:{idx - 1}", "") if idx > 0 else ""
        after_full = neighbor_text.get(f"{doc_id}:{idx + 1}", "")
        if not before_full and not after_full:
            out.append(h)
            continue
        before = ("…" + before_full[-_EXPAND_CHARS:]) if before_full else ""
        after = (after_full[:_EXPAND_CHARS] + "…") if after_full else ""
        expanded = dict(h)
        expanded["text"] = "\n\n".join(t for t in (before, h["text"], after) if t)
        out.append(expanded)
    return out
