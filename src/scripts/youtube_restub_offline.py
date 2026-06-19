"""Offline cleanup for YouTube docs learned as the loader's failed-fetch
stub ('📺 자막 자동 fetch 실패…') while yt-dlp was broken.

Why offline (not the /youtube_restub_rescan command): the bot's live
ChromaDB collection object is shared with retry_pending_ingest ticks,
dashboard refreshes, BM25 caching, in-flight ingests, and the smoke test;
heavy where/$in or get-by-id batches on the 178k-chunk collection get
serialized behind that traffic and effectively never finish. Running the
same logic in a one-shot process with the bot STOPPED bypasses every
contention point — out-of-process timing already proved 8.6s end-to-end.

Run:
  docker stop thesis-bot-1
  docker compose run --rm bot python -m src.scripts.youtube_restub_offline
  docker start thesis-bot-1

What it does:
  1. meta.find_youtube_docs() — every doc whose source is a YouTube URL.
  2. vector get by id `{doc_id}:0` for every candidate — the stub body is
     a single short chunk so the marker only ever lives in idx=0.
  3. For docs whose first chunk contains the stub marker:
       - vector.delete_docs(stub_ids)  — one batched where-$in delete
       - meta.delete(doc_id)           — single-row indexed delete
       - append `{kind:"url", url, chat_id, attempts:0}` to retry_queue.json
         (dedup against URLs already present; atomic write with .bak).
  4. Print a summary line per phase plus a final totals line.

Safe to re-run: dedup-by-URL on the retry queue prevents double-queueing,
and stub docs that were already cleaned up just won't match in step 2.
"""
import json
import os
import sys
from pathlib import Path

from .. import config
from ..store import meta, vector

_MARKER = "자막 자동 fetch 실패"
_RETRY_QUEUE_PATH = config.DATA_DIR / "retry_queue.json"
_OWNER_CHAT_ID = int(os.getenv("TELEGRAM_OWNER_ID", "0")) or None


def _load_queue() -> list[dict]:
    if not _RETRY_QUEUE_PATH.exists():
        return []
    try:
        data = json.loads(_RETRY_QUEUE_PATH.read_text("utf-8"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        bak = _RETRY_QUEUE_PATH.with_suffix(_RETRY_QUEUE_PATH.suffix + ".bak")
        if bak.exists():
            print(f"!! retry_queue.json corrupt — falling back to .bak", flush=True)
            return json.loads(bak.read_text("utf-8"))
        raise


def _atomic_write_queue(queue: list[dict]) -> None:
    tmp = _RETRY_QUEUE_PATH.with_suffix(_RETRY_QUEUE_PATH.suffix + ".tmp")
    bak = _RETRY_QUEUE_PATH.with_suffix(_RETRY_QUEUE_PATH.suffix + ".bak")
    tmp.write_text(json.dumps(queue, ensure_ascii=False, indent=2), "utf-8")
    if _RETRY_QUEUE_PATH.exists():
        try:
            os.replace(_RETRY_QUEUE_PATH, bak)
        except OSError:
            pass
    os.replace(tmp, _RETRY_QUEUE_PATH)


def main() -> int:
    print(">> youtube_restub_offline starting", flush=True)

    # Step 1: candidates from sqlite (cheap).
    docs = meta.find_youtube_docs()
    print(f"[1/4] meta.find_youtube_docs: {len(docs)} candidates", flush=True)
    if not docs:
        print("nothing to do.", flush=True)
        return 0

    # Step 2: by-id chunk fetch (one Chroma call, no metadata scan).
    doc_ids = [d["id"] for d in docs]
    first_ids = [f"{d}:0" for d in doc_ids]
    res = vector._collection.get(ids=first_ids, include=["documents"])
    got_ids = res.get("ids") or []
    bodies = res.get("documents") or []
    stub_ids: list[str] = []
    for cid, text in zip(got_ids, bodies):
        if _MARKER in (text or ""):
            stub_ids.append(cid.rsplit(":", 1)[0])
    print(f"[2/4] stub hits: {len(stub_ids)}", flush=True)
    if not stub_ids:
        print("no stubs found — exit clean.", flush=True)
        return 0

    stub_set = set(stub_ids)
    stub_docs = [d for d in docs if d["id"] in stub_set]

    # Step 3: vector batch delete + meta single-row deletes.
    chunks_removed = vector.delete_docs(stub_ids)
    for did in stub_ids:
        try:
            meta.delete(did)
        except Exception as e:  # log + continue
            print(f"!! meta.delete failed for {did}: {e}", flush=True)
    print(f"[3/4] purged {len(stub_ids)} docs / {chunks_removed} chunks",
          flush=True)

    # Step 4: requeue source URLs (dedup against existing queue).
    queue = _load_queue()
    queued_urls = {it.get("url") for it in queue if it.get("kind") == "url"}
    requeued = 0
    skipped_dup = 0
    for s in stub_docs:
        url = s.get("source") or ""
        if not url.startswith("http"):
            continue
        if url in queued_urls:
            skipped_dup += 1
            continue
        queue.append({
            "kind": "url",
            "url": url,
            "chat_id": _OWNER_CHAT_ID,
            "attempts": 0,
        })
        queued_urls.add(url)
        requeued += 1
    _atomic_write_queue(queue)
    print(f"[4/4] queue: +{requeued} urls (skipped {skipped_dup} already queued)",
          flush=True)

    print(f">> done. start the bot — retry_pending_ingest will drain "
          f"these {requeued} urls.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
