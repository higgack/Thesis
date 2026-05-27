"""Post-deploy smoke test — runs INSIDE the container after each restart.

Why this exists: local `ast.parse` + stubbed unit tests (what runs on a
dev machine) cannot catch the bugs that actually reach the user, because
the dev env has no numpy / real ChromaDB / google-genai / the real
multi-hundred-thousand-chunk corpus. The two incidents this was built
after prove it:
  - a numpy array truthiness error (`arr or []`) only fires with real
    Chroma arrays, never with stub lists;
  - a 58s BM25 rebuild only blocks the loop at real corpus scale.

This exercises the real READ-ONLY hot path against the live corpus —
retrieval (Chroma + BM25 + rerank) and embedding — and reports failures
or perf regressions at deploy time. Read-only: it queries + embeds,
never writes, so it can never pollute the corpus or the retry queue.

Driven by a run_once job ~3 min after boot (so the BM25 cache + reranker
have warmed); also runnable manually: `python -m src.smoke`.
"""
import logging
import time

log = logging.getLogger("smoke")

# A healthy hybrid query (dense + cached BM25 score, both offloaded, +
# rerank) returns in a few seconds even on a large corpus. Well above
# this means something is blocking the event loop again (the
# BM25-rebuild-on-loop class of bug) — surface it.
SLOW_QUERY_SEC = 30.0


async def run_smoke() -> tuple[bool, str]:
    """Run the read-only smoke checks. Returns (ok, message)."""
    from .agent import retrieve
    from .llm.embed import embed

    # Warm-up query (untimed): loads the lazy reranker model + warms the
    # caches so the timed pass reflects steady-state, not cold-load cost.
    try:
        await retrieve.hybrid("워밍업", k=3)
    except Exception as e:
        log.exception("smoke: retrieval (warm-up) failed")
        return False, f"❌ 스모크 실패 (검색): {type(e).__name__}: {e}"

    # Timed steady-state retrieval: Chroma query + BM25 + rerank.
    t0 = time.monotonic()
    try:
        hits = await retrieve.hybrid("스모크 테스트 검색 점검", k=3)
    except Exception as e:
        log.exception("smoke: retrieval failed")
        return False, f"❌ 스모크 실패 (검색): {type(e).__name__}: {e}"
    query_sec = time.monotonic() - t0

    # Embedding path: real Gemini embed of a tiny string.
    try:
        vecs = await embed(["스모크 테스트 임베딩"])
        if not vecs or not vecs[0]:
            return False, "❌ 스모크 실패 (임베딩): 빈 벡터 반환"
    except Exception as e:
        log.exception("smoke: embed failed")
        return False, f"❌ 스모크 실패 (임베딩): {type(e).__name__}: {e}"

    if query_sec > SLOW_QUERY_SEC:
        return False, (
            f"⚠️ 스모크 경고: 검색 {query_sec:.0f}s (느림 — 이벤트 루프 "
            f"블록 의심). hits={len(hits)}"
        )

    msg = f"✅ 스모크 OK (검색 {query_sec:.1f}s · hits {len(hits)} · 임베딩 OK)"
    log.info(msg)
    return True, msg


if __name__ == "__main__":
    import asyncio
    _ok, _msg = asyncio.run(run_smoke())
    print(_msg)
    raise SystemExit(0 if _ok else 1)
