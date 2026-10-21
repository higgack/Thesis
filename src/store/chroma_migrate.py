"""One-time boot migration: 3072-dim Chroma index → 1536-dim (2026-10-21).

Why: the HNSW index keeps every vector in RAM. At 922k × 3072 dims it
was 10.68 GiB of the bot's 11g mem_limit (95%+ → Q&A/OCR refused), and
the host had no RAM left to raise the limit. gemini-embedding-001 is
Matryoshka-trained; `scripts/embed_dim_check.py` measured on the live
index that the first 1536 dims keep 97.5% of the top-10 neighbours and
100% of the top-3 inside the top-10. So the stored vectors are TRUNCATED
(no Gemini calls, ₩0) into a new store at `data/chroma_1536`, and new
embeddings are requested at 1536 dims (`embed.py`).

Runs from `bot.py` BEFORE `store.vector` is imported, so the old 11GB
index is never loaded: the old vectors are read straight from the HNSW
file with `pread`, ids/documents/metadata from the old chroma.sqlite3.

Fail-safe by design — the worst case is today's state, never worse:
- any error → the partial new store is removed and the bot keeps using
  the old 3072 index (`active_dim()` only flips on status "done");
- an attempt killed mid-way (watchdog restart) counts; after
  `_MAX_ATTEMPTS` the migration stops retrying;
- `.env VECTOR_INDEX=legacy` forces the old index and skips this.
The old `data/chroma` is NEVER modified or deleted here. Deleting it to
reclaim disk is the owner's call (irreversible).
"""
from __future__ import annotations

import json
import logging
import os
import pickle
import shutil
import sqlite3
import struct
import time
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

OLD_DIR = config.DATA_DIR / "chroma"
NEW_DIR = config.DATA_DIR / "chroma_1536"
STATE_PATH = config.DATA_DIR / "chroma_1536_state.json"
OLD_DIM = 3072
NEW_DIM = 1536
COLLECTION = "knowledge"
_MAX_ATTEMPTS = 2
_BATCH = 1000                      # chroma 1.x add() has ~1s fixed cost
# auto_pull.sh restarts the bot after 5 straight minutes at ≥90% CPU
# (docker stats over ~1s). One chroma add() call runs 1-2s at 100-200%,
# so sleeping between batches can't keep a 1s sample under 90%. Instead
# the work runs in a child process pinned to ONE core, which the parent
# SIGSTOP/SIGCONTs at 120ms/60ms (2/3 duty), and the other core stays
# free for the host's other bots. Measured at 100/80ms with real-size
# chunks: 1s-window CPU p95 59%, max 62%, 91 chunks/s; 2/3 duty scales
# that to a ~75% max — still clear of the 90% trip.
_RUN_SLICE = 0.12
_STOP_SLICE = 0.06
_DISK_RESERVE = 3 * 2**30          # abort if free space falls below this
_HEARTBEAT = config.DATA_DIR / "bot_heartbeat"

# hnswlib header (chroma prefixes an int32 persistence version → 100B)
_HDR = struct.Struct("<QQQQQQiIQQQdQ")


# ── state ────────────────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False))
    os.replace(tmp, STATE_PATH)


def _legacy_forced() -> bool:
    return (os.getenv("VECTOR_INDEX", "auto").strip().lower() == "legacy"
            or os.getenv("EMBED_BACKEND", "gemini").lower() != "gemini")


def active_dim() -> int:
    """Dimension of the store vector.py should open (and embed.py should
    request). Flips to 1536 only after a completed migration."""
    if _legacy_forced():
        return OLD_DIM
    if _load_state().get("status") == "done" and NEW_DIR.is_dir():
        return NEW_DIM
    return OLD_DIM


def active_dir() -> Path:
    return NEW_DIR if active_dim() == NEW_DIM else OLD_DIR


# ── helpers ──────────────────────────────────────────────────────────
def _notify(text: str) -> None:
    """Best-effort owner DM via the cloud Bot API (same path auto_pull.sh
    uses) — the bot's own Application isn't running yet at this point."""
    try:
        import urllib.parse
        import urllib.request
        data = urllib.parse.urlencode(
            {"chat_id": config.TELEGRAM_OWNER_ID, "text": text}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
            "/sendMessage", data=data, timeout=15).read()
    except Exception:
        log.warning("migration notify failed", exc_info=True)


def _stamp_heartbeat() -> None:
    # The event loop isn't up yet; keep auto_pull.sh's stale-heartbeat
    # watchdog from restarting a long, healthy migration.
    try:
        tmp = _HEARTBEAT.with_suffix(".tmp")
        tmp.write_text(str(int(time.time())))
        os.replace(tmp, _HEARTBEAT)
    except Exception:
        pass


def _free_bytes(p: Path) -> int:
    return shutil.disk_usage(p).free


def _find_hnsw(root: Path) -> Path | None:
    cands = [p.parent for p in root.glob("*/header.bin")
             if (p.parent / "data_level0.bin").exists()]
    if not cands:
        return None
    return max(cands, key=lambda d: (d / "data_level0.bin").stat().st_size)


def _read_header(seg: Path) -> dict:
    raw = (seg / "header.bin").read_bytes()
    for start in (4, 0):
        if len(raw) < start + _HDR.size:
            continue
        (off0, max_el, cur, spe, label_off, off_data, _lvl, _ep,
         _maxm, maxm0, _m, _mult, _efc) = _HDR.unpack_from(raw, start)
        dim, rem = divmod(label_off - off_data, 4)
        if (off0 == 0 and off_data == maxm0 * 4 + 4
                and spe == label_off + 8 and rem == 0
                and 0 < cur <= max_el and dim > 0):
            return {"cur": cur, "spe": spe, "off": off_data,
                    "label_off": label_off, "dim": dim}
    raise RuntimeError(f"unrecognised HNSW header ({len(raw)} bytes)")


def _id_to_label(seg: Path) -> dict:
    with open(seg / "index_metadata.pickle", "rb") as f:
        md = pickle.load(f)          # local file written by chroma itself
    d = md if isinstance(md, dict) else md.__dict__
    return d["id_to_label"]


def _label_rows(seg: Path, h: dict):
    """label → row index for every live (not delete-marked) row."""
    import numpy as np
    n, spe = h["cur"], h["spe"]
    labels = np.empty(n, np.uint64)
    live = np.empty(n, bool)
    step = 20_000
    with open(seg / "data_level0.bin", "rb") as f:
        fd = f.fileno()
        for s in range(0, n, step):
            e = min(s + step, n)
            buf = os.pread(fd, (e - s) * spe, s * spe)
            a = np.frombuffer(buf, np.uint8).reshape(e - s, spe)
            labels[s:e] = np.ascontiguousarray(
                a[:, h["label_off"]:h["label_off"] + 8]).view("<u8").ravel()
            live[s:e] = (a[:, 2] & 1) == 0
            try:
                os.posix_fadvise(fd, s * spe, (e - s) * spe,
                                 os.POSIX_FADV_DONTNEED)
            except Exception:
                pass
    rows = np.nonzero(live)[0]
    return dict(zip(labels[rows].tolist(), rows.tolist()))


def _meta_segment(con: sqlite3.Connection) -> str:
    coll = con.execute("SELECT id FROM collections WHERE name=?",
                       (COLLECTION,)).fetchone()
    if not coll:
        raise RuntimeError(f"collection {COLLECTION!r} not in old sqlite")
    seg = con.execute("SELECT id FROM segments WHERE collection=? AND "
                      "scope='METADATA'", (coll[0],)).fetchone()
    if not seg:
        raise RuntimeError("metadata segment not found")
    return seg[0]


def _queue_vectors(con: sqlite3.Connection, ids: list[str]):
    """Vectors for ids the persisted HNSW hasn't absorbed yet (still in
    chroma's write-ahead queue). Latest write wins."""
    import numpy as np
    out = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        q = ",".join("?" * len(part))
        for cid, blob, enc in con.execute(
                f"SELECT id, vector, encoding FROM embeddings_queue WHERE "
                f"id IN ({q}) AND vector IS NOT NULL ORDER BY seq_id",
                part):
            if (enc or "").upper() == "FLOAT32" and blob:
                v = np.frombuffer(blob, "<f4")
                if v.size == OLD_DIM:
                    out[cid] = v
    return out


# ── migration ────────────────────────────────────────────────────────
def _migrate(st: dict) -> dict:
    import numpy as np
    import chromadb
    from chromadb.config import Settings

    seg = _find_hnsw(OLD_DIR)
    if seg is None:
        raise RuntimeError("old HNSW segment not found")
    h = _read_header(seg)
    if h["dim"] != OLD_DIM:
        raise RuntimeError(f"old index dim {h['dim']} != {OLD_DIM}")
    id2label = _id_to_label(seg)
    label2row = _label_rows(seg, h)

    old_sqlite = OLD_DIR / "chroma.sqlite3"
    need = (old_sqlite.stat().st_size
            + len(label2row) * (NEW_DIM * 4 + 200) + _DISK_RESERVE)
    free = _free_bytes(config.DATA_DIR)
    if free < need:
        raise RuntimeError(f"disk: need ~{need / 2**30:.1f}GiB free, have "
                           f"{free / 2**30:.1f}GiB")

    con = sqlite3.connect(f"file:{old_sqlite}?mode=ro", uri=True,
                          timeout=30)
    mseg = _meta_segment(con)
    total = con.execute("SELECT COUNT(*) FROM embeddings WHERE "
                        "segment_id=?", (mseg,)).fetchone()[0]

    client = chromadb.PersistentClient(
        path=str(NEW_DIR), settings=Settings(anonymized_telemetry=False))
    col = client.get_or_create_collection(
        name=COLLECTION, metadata={"hnsw:space": "cosine"})

    added = missing = 0
    samples: list[tuple[str, list[float]]] = []
    t0 = last_beat = last_log = time.time()
    fd = os.open(seg / "data_level0.bin", os.O_RDONLY)
    off, spe = h["off"], h["spe"]
    last_rowid = -1
    try:
        while True:
            rows = con.execute(
                "SELECT id, embedding_id FROM embeddings WHERE segment_id=? "
                "AND id>? ORDER BY id LIMIT ?",
                (mseg, last_rowid, _BATCH)).fetchall()
            if not rows:
                break
            last_rowid = rows[-1][0]
            rid_to_cid = dict(rows)
            meta: dict[int, dict] = {r: {} for r in rid_to_cid}
            docs: dict[int, str] = {}
            q = ",".join("?" * len(rows))
            for rid, key, sv, iv, fv, bv in con.execute(
                    "SELECT id, key, string_value, int_value, float_value, "
                    f"bool_value FROM embedding_metadata WHERE id IN ({q})",
                    list(rid_to_cid)):
                if key == "chroma:document":
                    docs[rid] = sv or ""
                elif not key.startswith("chroma:"):
                    meta[rid][key] = (sv if sv is not None else
                                      iv if iv is not None else
                                      fv if fv is not None else
                                      bool(bv) if bv is not None else None)

            vecs: dict[str, np.ndarray] = {}
            pending = []
            for cid in rid_to_cid.values():
                row = label2row.get(id2label.get(cid, -1))
                if row is None:
                    pending.append(cid)
                    continue
                b = os.pread(fd, OLD_DIM * 4, row * spe + off)
                vecs[cid] = np.frombuffer(b, "<f4")
            if pending:
                vecs.update(_queue_vectors(con, pending))

            ids, embs, dts, mds = [], [], [], []
            for rid, cid in rid_to_cid.items():
                v = vecs.get(cid)
                if v is None:
                    missing += 1
                    continue
                v = v[:NEW_DIM].astype(np.float32)
                v /= max(float(np.linalg.norm(v)), 1e-12)
                ids.append(cid)
                embs.append(v)
                dts.append(docs.get(rid, ""))
                mds.append({k: x for k, x in meta[rid].items()
                            if x is not None} or None)
            if ids:
                col.add(ids=ids, embeddings=np.stack(embs), documents=dts,
                        metadatas=mds)
                added += len(ids)
                if len(samples) < 20 and (not samples
                                          or added % 40_000 < len(ids)):
                    samples.append((ids[0], embs[0].tolist()))

            now = time.time()
            if now - last_beat > 30:
                last_beat = now
                if _free_bytes(config.DATA_DIR) < _DISK_RESERVE:
                    raise RuntimeError("disk free fell below reserve")
                try:
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                except Exception:
                    pass
            if now - last_log > 300:
                last_log = now
                st.update(progress=f"{added}/{total}")
                _save_state(st)
                log.info("chroma 1536 migration: %d/%d (%.0f%%) %.0fs, "
                         "missing %d", added, total,
                         100 * added / max(total, 1), now - t0, missing)
    finally:
        os.close(fd)
        con.close()

    if missing > max(100, total // 100):
        raise RuntimeError(f"{missing}/{total} chunks had no vector")
    got = col.count()
    if got != added:
        raise RuntimeError(f"count mismatch: added {added}, store has {got}")
    # Each sample must find itself — proves vectors landed intact. Top-5
    # or distance≈0, because the chunk-embedding cache gives identical
    # text in different docs the SAME vector, so a twin can rank first.
    hits = 0
    for cid, v in samples:
        r = col.query(query_embeddings=[v], n_results=5)
        got_ids = (r.get("ids") or [[]])[0]
        dist = (r.get("distances") or [[]])[0]
        hits += bool(cid in got_ids or (dist and dist[0] < 1e-4))
    if samples and hits < len(samples) * 0.9:
        raise RuntimeError(f"self-query check {hits}/{len(samples)}")
    return {"added": added, "missing": missing, "total": total,
            "self_check": f"{hits}/{len(samples)}",
            "secs": int(time.time() - t0)}


def _child(st: dict, result_path: str) -> None:
    try:
        os.sched_setaffinity(0, {sorted(os.sched_getaffinity(0))[0]})
    except Exception:
        pass
    try:
        res = {"ok": True, **_migrate(st)}
    except BaseException as e:            # report, never hang the parent
        log.exception("migration child failed")
        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    Path(result_path).write_text(json.dumps(res))
    os._exit(0)


def _migrate_throttled(st: dict) -> dict:
    import multiprocessing as mp
    import signal
    result = STATE_PATH.with_suffix(".result")
    result.unlink(missing_ok=True)
    proc = mp.get_context("fork").Process(target=_child,
                                          args=(st, str(result)))
    proc.start()
    _stamp_heartbeat()
    last_beat = time.time()
    try:
        while proc.is_alive():
            os.kill(proc.pid, signal.SIGCONT)
            time.sleep(_RUN_SLICE)
            if not proc.is_alive():
                break
            os.kill(proc.pid, signal.SIGSTOP)
            time.sleep(_STOP_SLICE)
            if time.time() - last_beat > 30:
                _stamp_heartbeat()
                last_beat = time.time()
    finally:
        try:
            os.kill(proc.pid, signal.SIGCONT)   # never leave it stopped
        except ProcessLookupError:
            pass
        if proc.is_alive():
            proc.terminate()
        proc.join(30)
    try:
        res = json.loads(result.read_text())
    except Exception:
        raise RuntimeError(f"migration child exited {proc.exitcode} "
                           "without a result")
    finally:
        result.unlink(missing_ok=True)
    if not res.pop("ok"):
        raise RuntimeError(res.get("error", "unknown"))
    return res


def run_at_boot() -> None:
    """Called once from bot.py before store.vector is imported. Never
    raises — every failure path leaves the old index in service."""
    # bot.py's logging.basicConfig runs after its imports, i.e. after
    # this; without a handler of our own the progress lines vanish.
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        _run_at_boot()
    finally:
        log.removeHandler(h)
        log.propagate = True
        log.setLevel(logging.NOTSET)


def _run_at_boot() -> None:
    try:
        if _legacy_forced():
            return
        st = _load_state()
        if st.get("status") == "done" and NEW_DIR.is_dir():
            return
        if st.get("status") == "gave_up":
            return
        if (not (OLD_DIR / "chroma.sqlite3").exists()
                and not NEW_DIR.exists()):
            # Fresh install: nothing to carry over, start at 1536.
            _save_state({"status": "done", "fresh": True})
            return
        attempts = int(st.get("attempts", 0))
        if attempts >= _MAX_ATTEMPTS:
            st["status"] = "gave_up"
            _save_state(st)
            _notify("⚠️ 벡터 인덱스 1536 전환을 "
                    f"{attempts}회 시도했지만 끝나지 않아 중단했어. 기존 "
                    "인덱스(3072)로 계속 동작 중. 마지막 사유: "
                    f"{st.get('error', '중간에 재시작됨')}")
            return
        if NEW_DIR.exists():
            shutil.rmtree(NEW_DIR)          # partial output of a killed run
        st = {"status": "running", "attempts": attempts + 1,
              "started": int(time.time()),
              "error": st.get("error", "")}
        _save_state(st)
        _notify("🔧 벡터 인덱스 1536 전환 시작 (메모리 절반으로). 3~5시간 "
                "동안 봇 응답 없음 — 끝나면 자동으로 다시 켜지고 알림 갈게. "
                f"시도 {st['attempts']}/{_MAX_ATTEMPTS}")
        log.warning("chroma 1536 migration starting (attempt %d)",
                    st["attempts"])
        try:
            res = _migrate_throttled(st)
        except Exception as e:
            log.exception("chroma 1536 migration failed")
            shutil.rmtree(NEW_DIR, ignore_errors=True)
            st.update(status="failed", error=str(e)[:300])
            _save_state(st)
            _notify("❌ 벡터 인덱스 1536 전환 실패 — 기존 인덱스(3072)로 "
                    f"정상 동작 중. 사유: {str(e)[:300]}")
            return
        st.update(status="done", finished=int(time.time()), **res)
        _save_state(st)
        log.warning("chroma 1536 migration done: %s", res)
        _notify(f"✅ 벡터 인덱스 1536 전환 완료 — {res['added']:,}청크, "
                f"{res['secs'] // 60}분, 벡터 없음 {res['missing']}, "
                f"자기검색 {res['self_check']}. 이제 봇 시작함. 기존 "
                "data/chroma(21GB)는 그대로 보존 중.")
    except Exception:
        log.exception("chroma migration bootstrap error — using old index")
