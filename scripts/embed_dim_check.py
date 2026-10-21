"""One-off, READ-ONLY check: does truncating the stored 3072-dim Gemini
embeddings to 768/1536 dims keep the same nearest neighbours?

Why this exists (2026-10-21): the Chroma HNSW index (~11GB on disk) is
what keeps the bot at 95%+ of its 11g mem_limit, and the host has no
RAM left to raise it. gemini-embedding-001 is Matryoshka-trained, so the
first N dims of a vector should be a usable N-dim embedding — this
script measures that on the LIVE corpus before any migration is built.

It never opens Chroma (a second PersistentClient would load the whole
11GB index into this process). Instead it memory-maps the HNSW
`data_level0.bin` and streams it in blocks, so RSS stays ~150-300MB.
Nothing is written anywhere.

Run (VM, separate capped container so it can't push the bot over):
  docker run --rm -m 700m -v ~/Thesis/data:/app/data:ro \
    -v ~/Thesis/scripts:/s:ro thesis-bot python -I /s/embed_dim_check.py
"""
from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

import numpy as np

# hnswlib header layout (saveIndex / chroma persistHeader; chroma's
# persistent index prefixes it with an int32 persistence version, so the
# file is 100 bytes there and 96 in plain hnswlib — both are tried):
# offsetLevel0 maxElements curElementCount sizeDataPerElement labelOffset
# offsetData (size_t ×6) · maxlevel (int) · enterpoint (uint) ·
# maxM maxM0 M (size_t ×3) · mult (double) · efConstruction (size_t)
_HDR = struct.Struct("<QQQQQQiIQQQdQ")


def _find_index(root: Path) -> Path:
    cands = [p.parent for p in root.glob("*/header.bin")
             if (p.parent / "data_level0.bin").exists()]
    if not cands:
        sys.exit(f"no HNSW segment (header.bin + data_level0.bin) under {root}")
    return max(cands, key=lambda d: (d / "data_level0.bin").stat().st_size)


def _read_header(seg: Path) -> dict:
    raw = (seg / "header.bin").read_bytes()
    for start in (4, 0):
        if len(raw) < start + _HDR.size:
            continue
        (off0, max_el, cur, spe, label_off, off_data, _lvl, _ep,
         _maxm, maxm0, m, _mult, _efc) = _HDR.unpack_from(raw, start)
        links0 = maxm0 * 4 + 4
        dim, rem = divmod(label_off - off_data, 4)
        if (off0 == 0 and off_data == links0 and spe == label_off + 8
                and rem == 0 and 0 < cur <= max_el and dim > 0):
            return {"cur": cur, "max": max_el, "spe": spe, "off": off_data,
                    "dim": dim, "links0": links0, "M": m}
    sys.exit(f"header.bin layout not recognised ({len(raw)} bytes) — "
             "aborting rather than guessing")


def _topk_merge(best_s, best_i, s, base, k):
    """Merge block scores s (rows=queries) into running top-k."""
    cat_s = np.concatenate([best_s, s], axis=1)
    cat_i = np.concatenate(
        [best_i, np.broadcast_to(np.arange(s.shape[1]) + base, s.shape)],
        axis=1)
    sel = np.argpartition(-cat_s, k - 1, axis=1)[:, :k]
    return (np.take_along_axis(cat_s, sel, 1),
            np.take_along_axis(cat_i, sel, 1))


def _unit(x):
    n = np.linalg.norm(x, axis=1, keepdims=True)
    return x / np.maximum(n, 1e-12)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/app/data/chroma")
    ap.add_argument("--queries", type=int, default=40)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--dims", default="768,1536")
    ap.add_argument("--block", type=int, default=8000)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    seg = _find_index(Path(a.root))
    h = _read_header(seg)
    n, spe, off, dim = h["cur"], h["spe"], h["off"], h["dim"]
    dims = [d for d in (int(x) for x in a.dims.split(",")) if d < dim]
    mm = np.memmap(seg / "data_level0.bin", dtype=np.uint8, mode="r",
                   shape=(n, spe))

    def vecs(rows):
        return np.ascontiguousarray(mm[rows, off:off + dim * 4]).view("<f4")

    t0 = time.time()
    deleted = 0
    for s in range(0, n, 50_000):
        deleted += int((mm[s:s + 50_000, 2] & 1).sum())

    rng = np.random.default_rng(a.seed)
    q_rows = []
    while len(q_rows) < a.queries:
        r = int(rng.integers(0, n))
        if not (mm[r, 2] & 1) and r not in q_rows:
            q_rows.append(r)
    q_rows = np.array(sorted(q_rows))
    Q = vecs(q_rows)
    norms = np.linalg.norm(Q, axis=1)
    Qs = {dim: _unit(Q), **{d: _unit(Q[:, :d]) for d in dims}}

    k1 = a.k + 1                      # +1: the query finds itself
    best = {d: (np.full((len(q_rows), k1), -np.inf, np.float32),
                np.zeros((len(q_rows), k1), np.int64)) for d in Qs}
    for s in range(0, n, a.block):
        e = min(s + a.block, n)
        V = vecs(slice(s, e))
        dead = (mm[s:e, 2] & 1).astype(bool)
        for d, Qd in Qs.items():
            Vd = V if d == dim else _unit(V[:, :d])
            sc = Qd @ Vd.T
            sc[:, dead] = -np.inf
            best[d] = _topk_merge(*best[d], sc, s, k1)

    def ranked(d):
        sc, ix = best[d]
        order = np.argsort(-sc, axis=1)
        ix = np.take_along_axis(ix, order, 1)
        return [[j for j in row if j != q][:a.k]
                for row, q in zip(ix.tolist(), q_rows.tolist())]

    ref = ranked(dim)
    print(f"segment        {seg.name}")
    print(f"vectors        {n:,} (deleted-marked {deleted:,}) · dim {dim} · "
          f"M {h['M']} · alloc {h['max']:,}")
    print(f"stored norm    min {norms.min():.4f} max {norms.max():.4f} "
          "(≈1.0 = cosine-normalised)")
    print(f"queries        {len(q_rows)} stored chunks as queries, top-{a.k}")
    print(f"index RAM est  {dim}d: {n * spe / 2**30:.2f} GiB")
    for d in dims:
        got = ranked(d)
        rec = np.mean([len(set(r) & set(g)) / a.k for r, g in zip(ref, got)])
        top1 = np.mean([r[:1] == g[:1] for r, g in zip(ref, got)])
        top3 = np.mean([len(set(r[:3]) & set(g[:a.k])) / 3
                        for r, g in zip(ref, got)])
        ram = n * (h["links0"] + d * 4 + 8) / 2**30
        print(f"{d:>5}d  recall@{a.k} {rec:.3f} · top1 same {top1:.3f} · "
              f"ref top3 kept in top{a.k} {top3:.3f} · index RAM est "
              f"{ram:.2f} GiB")
    print(f"elapsed        {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
