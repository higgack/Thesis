"""One-shot migration: Gemini embeddings → BAAI/bge-m3 local.

Reads every chunk from the old `knowledge` collection (3072-dim,
Gemini), re-encodes the text with BGE-M3 (1024-dim), and writes to a
new `knowledge_bge_m3` collection. Idempotent — re-running just
upserts the same vectors. Old collection is left untouched so the
bot can fall back to Gemini if anything goes wrong.

After this completes successfully:
  1. Set EMBED_BACKEND=bge-m3 in .env
  2. docker compose up -d --build bot
  3. /status, ask a few queries, verify results
  4. (optional) drop the old collection to reclaim disk:
       docker compose exec bot python -c \
         "import chromadb; from chromadb.config import Settings; \
          c = chromadb.PersistentClient(path='/app/data/chroma', \
            settings=Settings(anonymized_telemetry=False)); \
          c.delete_collection('knowledge'); print('old collection deleted')"

Run:
  docker compose exec bot python -m src.scripts.migrate_embeddings
"""
import os

# Use every available vCPU for the encoder. Default torch behavior on
# CPU is sometimes single-threaded which crippled the migration to
# ~0.2 chunks/s on e2-standard-2. Picking up os.cpu_count() means the
# same script scales when the VM is temporarily upgraded to c3-highcpu
# class machines for the one-shot migration.
_CPU = str(os.cpu_count() or 2)
os.environ.setdefault("OMP_NUM_THREADS", _CPU)
os.environ.setdefault("MKL_NUM_THREADS", _CPU)
os.environ.setdefault("TOKENIZERS_PARALLELISM", "true")

import sys
import time

import chromadb
from chromadb.config import Settings

from .. import config


BATCH = 100
SRC_NAME = "knowledge"
DST_NAME = "knowledge_bge_m3"


def main() -> int:
    os.environ.setdefault("HF_HOME", "/app/data/hf_cache")
    client = chromadb.PersistentClient(
        path=str(config.DATA_DIR / "chroma"),
        settings=Settings(anonymized_telemetry=False),
    )
    try:
        src = client.get_collection(SRC_NAME)
    except Exception:
        print(f"ERROR: source collection '{SRC_NAME}' not found.")
        return 1
    dst = client.get_or_create_collection(
        DST_NAME, metadata={"hnsw:space": "cosine"},
    )

    total = src.count()
    already = dst.count()
    print(f"source '{SRC_NAME}': {total} chunks")
    print(f"target '{DST_NAME}': {already} chunks (resume from here)")
    if total == 0:
        print("nothing to migrate.")
        return 0
    if already >= total:
        print("target already at or beyond source count — nothing to do.")
        return 0

    # Load BGE-M3 once
    print("loading BAAI/bge-m3 ...")
    import torch
    n_cpu = os.cpu_count() or 2
    torch.set_num_threads(n_cpu)
    # interop threads should stay modest — too many causes contention.
    torch.set_num_interop_threads(min(n_cpu, 8))
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("BAAI/bge-m3")
    print(f"bge-m3 ready (dim={model.get_sentence_embedding_dimension()})")
    print(f"torch threads: intra={torch.get_num_threads()} inter={torch.get_num_interop_threads()}", flush=True)

    offset = 0
    done = 0
    start = time.time()
    print(f"starting loop: total={total}, BATCH={BATCH}", flush=True)
    while True:
        print(f"  fetching offset={offset} ...", flush=True)
        batch = src.get(
            limit=BATCH, offset=offset,
            include=["documents", "metadatas"],
        )
        ids = batch.get("ids") or []
        print(f"  got {len(ids)} ids from source", flush=True)
        if not ids:
            print("  no more ids — exiting loop", flush=True)
            break
        docs = batch.get("documents") or []
        metas = batch.get("metadatas") or []
        print(f"  encoding {len(docs)} docs ...", flush=True)
        vecs = model.encode(
            docs, normalize_embeddings=True, batch_size=32,
            show_progress_bar=False,
        )
        vec_list = [list(map(float, v)) for v in vecs]
        print(f"  upserting {len(ids)} vectors ...", flush=True)
        dst.upsert(
            ids=ids, embeddings=vec_list,
            documents=docs, metadatas=metas,
        )
        done += len(ids)
        offset += BATCH
        elapsed = time.time() - start
        rate = done / elapsed if elapsed > 0 else 0
        eta = (total - already - done) / rate if rate > 0 else 0
        print(
            f"  {already + done}/{total} "
            f"({(already + done) * 100 / total:.1f}%) · "
            f"{rate:.0f} chunks/s · ETA {eta:.0f}s"
        )

    final = dst.count()
    print(
        f"\nmigration complete: source={total}, target={final} "
        f"(target may exceed source slightly if chunks were added "
        f"during migration)"
    )
    print("\nnext steps:")
    print("  1. set EMBED_BACKEND=bge-m3 in .env")
    print("  2. docker compose up -d --build bot")
    print("  3. verify search quality with a few queries")
    print(f"  4. (later) delete old '{SRC_NAME}' collection to reclaim disk")
    return 0


if __name__ == "__main__":
    sys.exit(main())
