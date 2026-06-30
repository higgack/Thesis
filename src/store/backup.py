"""Daily off-disk backup of the small, irreplaceable thesis state → GCS.

The full VM disk is snapshotted WEEKLY (cost-optimised). But the KB's
unique curation — ★/메모/알람 marks, Q&A archive, notes, KG, wiki, cost
history — lives ONLY on that disk and is KB~MB-scale, so it's backed up
DAILY off-disk (to a GCS bucket) at near-zero cost. That closes the 7-day
snapshot loss window for the data that actually matters.

NOT included: data/chroma (vector embeddings — large, and rebuildable by
re-embedding) and data/files (re-fetchable). Those rely on the weekly
snapshot.

Consistency: sqlite DBs are copied via the online backup API + serialize()
(NOT raw cp — a raw copy mid-WAL-write can be corrupt). JSON/wiki files are
written atomically elsewhere (CLAUDE.md invariant), so a plain read is safe.

RAM-friendly: the tar.gz is streamed to a temp FILE (not held in memory —
the VM is memory-tight), and each sqlite db is serialized one at a time.

Inert until BACKUP_BUCKET is set (see docs/backup.md), so shipping is safe.
Auths via the same ADC already used for Vertex (no key file).
"""
from __future__ import annotations

import io
import logging
import os
import sqlite3
import tarfile
import tempfile
import time
from datetime import datetime, timedelta, timezone

from .. import config

log = logging.getLogger(__name__)
_KST = timezone(timedelta(hours=9))

# Small, unique, irreplaceable. Resolved against DATA_DIR.
_DB_FILES = [
    "marks.db",        # ★/메모/알람 (수기 큐레이션 — 최우선)
    "qna.db",          # Q&A 아카이브
    "notes.db",        # 학습노트
    "kg.db",           # 지식그래프
    "kg_ignored.db",   # KG 영구무시
    "cost.db",         # 비용 이력
    "meta.db",         # 문서 메타
    "pending.db",      # 보류 결정
]
# Every state/curation JSON (wiki_*, ignored_*, notify_acks, dart_cap, …)
# and the wiki markdown pages tree.
_JSON_GLOB = "*.json"
_DIR_TREES = ["wiki"]


def _sqlite_image(path) -> bytes | None:
    """A consistent .db image via the online backup API → serialize()
    (py3.11+). Safe during WAL writes. None if not a usable sqlite db."""
    try:
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    except Exception:
        return None
    try:
        mem = sqlite3.connect(":memory:")
        try:
            src.backup(mem)
            return mem.serialize()
        finally:
            mem.close()
    except Exception:
        log.warning("sqlite image failed: %s", getattr(path, "name", path),
                    exc_info=True)
        return None
    finally:
        src.close()


def _build_archive(out_path: str) -> int:
    """Write the tar.gz of small KB state to out_path. Returns file size."""
    d = config.DATA_DIR
    now = int(time.time())
    with tarfile.open(out_path, mode="w:gz") as tar:
        # sqlite DBs → consistent image (one at a time, low RAM)
        for name in _DB_FILES:
            p = d / name
            if not p.exists():
                continue
            img = _sqlite_image(p)
            if img is None:
                continue
            info = tarfile.TarInfo(name=f"db/{name}")
            info.size = len(img)
            info.mtime = now
            tar.addfile(info, io.BytesIO(img))
        # state/curation JSON (atomic-written → plain copy is consistent)
        for p in sorted(d.glob(_JSON_GLOB)):
            if p.is_file():
                try:
                    tar.add(p, arcname=f"json/{p.name}")
                except OSError:
                    log.warning("backup add json failed: %s", p.name)
        # wiki markdown pages
        for tree in _DIR_TREES:
            t = d / tree
            if t.is_dir():
                try:
                    tar.add(t, arcname=tree)
                except OSError:
                    log.warning("backup add tree failed: %s", tree)
    return os.path.getsize(out_path)


def run_backup() -> dict:
    """Build + upload today's backup. Returns {status, detail}.
    status ∈ {ok, skip, error}. Never raises."""
    bucket = (config.BACKUP_BUCKET or "").strip()
    if not bucket:
        return {"status": "skip", "detail": "BACKUP_BUCKET 미설정"}
    fd, tmp = tempfile.mkstemp(suffix=".tar.gz")
    os.close(fd)
    try:
        try:
            size = _build_archive(tmp)
        except Exception as e:
            log.exception("backup build failed")
            return {"status": "error", "detail": f"빌드 실패: {str(e)[:160]}"}
        name = "thesis/" + datetime.now(_KST).strftime("%Y-%m-%d") + ".tar.gz"
        try:
            from google.cloud import storage
            client = storage.Client(project=config.VERTEX_PROJECT or None)
            blob = client.bucket(bucket).blob(name)
            blob.upload_from_filename(tmp, content_type="application/gzip")
        except Exception as e:
            log.exception("backup upload failed")
            return {"status": "error", "detail": f"업로드 실패: {str(e)[:160]}"}
        return {"status": "ok",
                "detail": f"gs://{bucket}/{name} ({size // 1024}KB)"}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
