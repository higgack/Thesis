"""Daily off-disk backup of the small, irreplaceable thesis state → GCS.

The full VM disk is snapshotted WEEKLY (cost-optimised). But the KB's
unique curation — ★/메모/알람 marks, Q&A archive, notes, KG, wiki, cost
history — lives ONLY on that disk and is KB~MB-scale, so it's backed up
DAILY off-disk (to a GCS bucket) at near-zero cost. That closes the 7-day
snapshot loss window for the data that actually matters.

NOT included: data/chroma (vectors — large, rebuildable by re-embedding)
and data/files (re-fetchable). Those rely on the weekly snapshot.

MEMORY-CRITICAL: this runs inside the bot's cgroup (mem_limit 5500m) on
top of the bot's ~4.6G baseline — only ~0.9G headroom. So it must stay
RAM-frugal or it OOM-kills the bot:
  • sqlite → online .backup to a temp FILE on disk (page-by-page, RAM-flat)
    — NOT serialize()/raw-cp (serialize holds the whole db in RAM; raw cp
    mid-WAL-write can corrupt).
  • upload via httpx (already loaded) + the metadata-server ADC token —
    NOT google-cloud-storage (its grpc/protobuf import alone is ~150MB+).
JSON/wiki files are atomically written elsewhere, so a plain copy is safe.

Inert until BACKUP_BUCKET is set (see docs/backup.md). Auths via the same
compute-SA ADC as Vertex (metadata server), no key file.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from .. import config

log = logging.getLogger(__name__)
_KST = timezone(timedelta(hours=9))

_META_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)

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
_JSON_GLOB = "*.json"   # wiki_*, ignored_*, notify_acks, dart_cap, …
_DIR_TREES = ["wiki"]   # data/wiki/*.md


def _access_token() -> str | None:
    """Short-lived OAuth token for the VM's compute SA (cloud-platform
    scope → GCS write). Same ADC path Vertex uses; needs the container's
    metadata.google.internal extra_host."""
    try:
        r = httpx.get(_META_TOKEN_URL,
                      headers={"Metadata-Flavor": "Google"}, timeout=10)
        r.raise_for_status()
        return r.json().get("access_token")
    except Exception:
        log.warning("metadata token fetch failed", exc_info=True)
        return None


def _sqlite_backup_to(src_path, dst_path) -> bool:
    """Online .backup into a temp db FILE (page-by-page → RAM-flat). Safe
    during WAL writes. True on success."""
    try:
        src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=30)
    except Exception:
        return False
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
            return True
        finally:
            dst.close()
    except Exception:
        log.warning("sqlite backup failed: %s",
                    getattr(src_path, "name", src_path), exc_info=True)
        return False
    finally:
        src.close()


def _build_archive(out_path: str, workdir: str) -> int:
    """Write the tar.gz of small KB state to out_path. Returns its size.
    DB images go through a disk temp file first (RAM-flat)."""
    d = config.DATA_DIR
    with tarfile.open(out_path, mode="w:gz") as tar:
        for name in _DB_FILES:
            p = d / name
            if not p.exists():
                continue
            tmp_db = os.path.join(workdir, name)
            if _sqlite_backup_to(p, tmp_db):
                try:
                    tar.add(tmp_db, arcname=f"db/{name}")
                finally:
                    try:
                        os.unlink(tmp_db)
                    except OSError:
                        pass
        for p in sorted(d.glob(_JSON_GLOB)):
            if p.is_file():
                try:
                    tar.add(p, arcname=f"json/{p.name}")
                except OSError:
                    log.warning("backup add json failed: %s", p.name)
        for tree in _DIR_TREES:
            t = d / tree
            if t.is_dir():
                try:
                    tar.add(t, arcname=tree)
                except OSError:
                    log.warning("backup add tree failed: %s", tree)
    return os.path.getsize(out_path)


def _upload(path: str, bucket: str, object_name: str, token: str) -> tuple[bool, str]:
    """Simple media upload to GCS via the JSON API + bearer token. Reads
    the (tens-of-MB) gzip into memory once for the POST — fine vs the
    ~150MB google-cloud-storage import it replaces."""
    url = (f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o"
           f"?uploadType=media&name={quote(object_name, safe='')}")
    try:
        with open(path, "rb") as f:
            body = f.read()
        r = httpx.post(url, content=body, timeout=180, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/gzip",
        })
        if r.status_code in (200, 201):
            return True, ""
        return False, f"HTTP {r.status_code}: {r.text[:160]}"
    except Exception as e:
        return False, str(e)[:160]


def run_backup() -> dict:
    """Build + upload today's backup. Returns {status, detail}.
    status ∈ {ok, skip, error}. Never raises."""
    bucket = (config.BACKUP_BUCKET or "").strip()
    if not bucket:
        return {"status": "skip", "detail": "BACKUP_BUCKET 미설정"}
    token = _access_token()
    if not token:
        return {"status": "error", "detail": "ADC 토큰 획득 실패(metadata)"}
    workdir = tempfile.mkdtemp(prefix="bk_")
    tar_path = os.path.join(workdir, "backup.tar.gz")
    try:
        try:
            size = _build_archive(tar_path, workdir)
        except Exception as e:
            log.exception("backup build failed")
            return {"status": "error", "detail": f"빌드 실패: {str(e)[:160]}"}
        name = "thesis/" + datetime.now(_KST).strftime("%Y-%m-%d") + ".tar.gz"
        ok, err = _upload(tar_path, bucket, name, token)
        if not ok:
            log.warning("backup upload failed: %s", err)
            return {"status": "error", "detail": f"업로드 실패: {err}"}
        return {"status": "ok",
                "detail": f"gs://{bucket}/{name} ({size // 1024}KB)"}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
