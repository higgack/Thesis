"""Embedding wrapper with two backends.

Default backend = `gemini` (gemini-embedding-001; 1536-dim since the
2026-10-21 store migration, 3072 before it — see store/chroma_migrate.py).
Set EMBED_BACKEND=bge-m3 in .env to switch to the local
sentence-transformers BAAI/bge-m3 model (1024-dim, free).

The local backend runs inside the bot container on CPU. First load
downloads the model (~2.3 GB) under HF_HOME=/app/data/hf_cache and
keeps it resident. Embedding 100 chunks ≈ 200 ms on e2-standard-2.
"""
import asyncio
import logging
import os
import threading

from google.genai import types

from .. import config
from ..store import cost

log = logging.getLogger(__name__)
_client = config.make_genai_client()

EMBED_BACKEND = os.getenv("EMBED_BACKEND", "gemini").lower()

_OVERLOAD_MARKERS = (
    "503",
    "429",
    "UNAVAILABLE",
    "RESOURCE_EXHAUSTED",
    "high demand",
)


def _is_overloaded(exc: BaseException) -> bool:
    s = f"{type(exc).__name__} {exc}"
    return any(m in s for m in _OVERLOAD_MARKERS)


_OUT_DIM: list = []


def _out_dim() -> int | None:
    """1536 once the vector store has been migrated (chroma_migrate) —
    the returned vectors are truncated to it; None = keep the model's
    full 3072, matching the old store. Must agree with
    the store vector.py opened, or Chroma rejects the dimension. Read
    once per process — the migration finishes before the first embed."""
    if not _OUT_DIM:
        from ..store import chroma_migrate
        d = chroma_migrate.active_dim()
        _OUT_DIM.append(d if d != chroma_migrate.OLD_DIM else None)
    return _OUT_DIM[0]


_BATCH_LIMIT = 100  # Gemini embed_content hard cap per request
# Concurrent embed requests for one document's batches. 4 keeps a
# 350-chunk PDF's four round-trips overlapping without turning a
# backlog drain into a rate-limit flood.
_EMBED_FANOUT = asyncio.Semaphore(int(os.getenv("EMBED_FANOUT", "4")))


async def _embed_gemini_batch(texts: list[str], task_type: str) -> list[list[float]]:
    last_err: BaseException | None = None
    for attempt in range(6):
        try:
            # 60s cap per call — same hang class gemini.complete() was
            # fixed for (a stalled HTTP call once hung >1h). A timeout
            # raises into the retry/backoff path below instead of pinning
            # the Q&A or ingest task until the outer 10-15min guard kills
            # the whole operation.
            resp = await asyncio.wait_for(
                _client.aio.models.embed_content(
                    model=config.EMBED_MODEL,
                    contents=texts,
                    config=types.EmbedContentConfig(task_type=task_type),
                ),
                timeout=60,
            )
            purpose = "query" if task_type == "RETRIEVAL_QUERY" else "ingest"
            usd_in = cost.record_resp(config.EMBED_MODEL, resp, purpose=purpose)
            if usd_in == 0.0:
                approx_tokens = sum(len(t) for t in texts) // 4
                cost.record(config.EMBED_MODEL, approx_tokens, 0,
                            purpose=purpose)
            dim = _out_dim()
            if dim is None:
                return [e.values for e in resp.embeddings]
            # Truncate the full 3072 vector HERE rather than asking the
            # API for output_dimensionality: it is byte-for-byte the same
            # operation chroma_migrate applied to the stored vectors, and
            # it cannot fail on a backend that rejects the option. A
            # truncated Matryoshka vector must be renormalised.
            out = []
            for e in resp.embeddings:
                v = e.values[:dim]
                n = sum(x * x for x in v) ** 0.5 or 1.0
                out.append([x / n for x in v])
            return out
        except Exception as e:
            last_err = e
            if not _is_overloaded(e) and attempt >= 2:
                raise
            wait = min(2 ** attempt, 60) + (1 if _is_overloaded(e) else 0)
            log.warning("embed retry %d/6 in %ds: %s", attempt + 1, wait, str(e)[:120])
            await asyncio.sleep(wait)
    assert last_err is not None
    raise last_err


# Local BGE-M3 — lazy singleton, loaded on first call.
_LOCAL_MODEL = None
_LOCAL_LOCK = threading.Lock()
_LOCAL_LOAD_FAILED = False


def _load_local_embed():
    global _LOCAL_MODEL, _LOCAL_LOAD_FAILED
    if _LOCAL_MODEL is not None:
        return _LOCAL_MODEL
    if _LOCAL_LOAD_FAILED:
        return None
    with _LOCAL_LOCK:
        if _LOCAL_MODEL is not None:
            return _LOCAL_MODEL
        if _LOCAL_LOAD_FAILED:
            return None
        try:
            os.environ.setdefault("HF_HOME", "/app/data/hf_cache")
            import torch
            # Auto-detect: prefer GPU when the container is on a CUDA-
            # enabled host (Dockerfile.gpu + GCE T4/L4/A100). Falls back
            # to multi-threaded CPU otherwise. EMBED_DEVICE=cpu forces
            # CPU even on a GPU box (useful for A/B perf testing).
            device_pref = os.getenv("EMBED_DEVICE", "auto").lower()
            if device_pref == "cpu" or not torch.cuda.is_available():
                device = "cpu"
                n_threads = os.cpu_count() or 2
                torch.set_num_threads(n_threads)
                log.info("bge-m3 device=cpu (threads=%d)", n_threads)
            else:
                device = "cuda"
                log.info("bge-m3 device=cuda (%s)",
                         torch.cuda.get_device_name(0))
            from sentence_transformers import SentenceTransformer
            log.info("loading BAAI/bge-m3 (first-time download ~2.3 GB)...")
            _LOCAL_MODEL = SentenceTransformer("BAAI/bge-m3", device=device)
            log.info("bge-m3 ready (dim=%d)",
                     _LOCAL_MODEL.get_sentence_embedding_dimension())
        except Exception as e:
            log.exception("bge-m3 load failed: %s", e)
            _LOCAL_LOAD_FAILED = True
            return None
    return _LOCAL_MODEL


async def _embed_bge_batch(texts: list[str]) -> list[list[float]]:
    """Local BGE-M3 encoding. normalize_embeddings=True so cosine
    distance maps to dot-product (matches Chroma's cosine space)."""
    model = await asyncio.to_thread(_load_local_embed)
    if model is None:
        # Fall back to Gemini if the local model failed to load — the
        # backend swap should never break ingest.
        log.warning("bge-m3 unavailable, falling back to Gemini for this batch")
        return await _embed_gemini_batch(texts, "RETRIEVAL_DOCUMENT")
    # GPU can chew through much bigger batches without OOM (T4 has
    # 16 GB VRAM, BGE-M3 weights take ~2 GB). CPU stays at 64 to keep
    # the per-batch wall time low so single-doc latency doesn't spike.
    import torch
    batch_size = 256 if torch.cuda.is_available() else 64
    vectors = await asyncio.to_thread(
        model.encode, texts, normalize_embeddings=True,
        batch_size=batch_size,
    )
    return [list(map(float, v)) for v in vectors]


async def embed(texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    if not texts:
        return []
    if EMBED_BACKEND == "bge-m3":
        # Local model — no API rate limit, batch size is just a memory
        # knob. Keep the same 100-chunk loop as the Gemini path for
        # consistency.
        if len(texts) <= _BATCH_LIMIT:
            return await _embed_bge_batch(texts)
        out: list[list[float]] = []
        for i in range(0, len(texts), _BATCH_LIMIT):
            out.extend(await _embed_bge_batch(texts[i:i + _BATCH_LIMIT]))
        return out
    # Default: Gemini
    if len(texts) <= _BATCH_LIMIT:
        return await _embed_gemini_batch(texts, task_type)
    # Batches ran sequentially: a 350-chunk PDF paid four full network
    # round-trips back to back for work that has no ordering
    # dependency. gather() overlaps them, bounded by _EMBED_FANOUT so a
    # very large document cannot fan out into a rate-limit flood (the
    # same reasoning as _PARTIAL_SUMMARY_SEM in summarize.py).
    # asyncio.gather preserves input order, so the returned vectors
    # still line up with `texts`.
    slices = [texts[i:i + _BATCH_LIMIT]
              for i in range(0, len(texts), _BATCH_LIMIT)]

    async def _one(batch: list[str]) -> list[list[float]]:
        async with _EMBED_FANOUT:
            return await _embed_gemini_batch(batch, task_type)

    out: list[list[float]] = []
    for part in await asyncio.gather(*[_one(b) for b in slices]):
        out.extend(part)
    return out
