import asyncio
import base64
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)
from telegram.request import HTTPXRequest

from . import config
from .store import meta, vector, obsidian, cost, qna, pending as pending_store
from .store import pending_url_decisions
from .ingest import pipeline
from .agent import agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

_INGEST_SEM_CAPACITY = int(os.getenv("INGEST_SEM_CAPACITY", "8"))
_INGEST_SEM = asyncio.Semaphore(_INGEST_SEM_CAPACITY)
# How many queued retries to drain per tick + how often we tick. Tuned
# for c3-standard-4 / n2-standard-4 (4 vCPU, 16 GiB RAM) + 12 GiB bot
# mem_limit + BGE-M3 (local embed, no API wait). 4 items per 30 s =
# 8/min ≈ 480/hour of sustained drain, ~4× the e2-standard-2 baseline.
# Live ingests still get priority because the semaphore is shared.
# Override via env when the queue is huge and live messages are getting
# starved (e.g. RETRY_INGEST_INTERVAL_SEC=120 + RETRY_INGEST_BATCH=1
# slows the drain to give new ingests faster slot access).
# Hardcoded. The scheduler ticks every 10 s and spawns enough retry
# tasks to refill any free slots in _INGEST_SEM. Combined with a
# semaphore-aware spawn loop (below), this keeps ingest at full
# concurrency without depending on env tuning or scheduler batch
# sizing. The user's earlier .env throttle (1 per 120 s, set during a
# flood-control incident) is intentionally ignored.
_RETRY_INGEST_INTERVAL_SEC = 10
_RETRY_INGEST_BATCH = 8
# After a failed retry, hold the item for this many seconds before
# making it eligible again. Prevents one stuck item from monopolising
# the queue's drain rate (without this, a perpetually-overloaded
# upstream can spin the same item to the front of the queue every
# 30s). Default 1 hour. Other queue items (with elapsed not_before_ts
# or never-failed-yet) drain normally during the hold.
_RETRY_BACKOFF_SEC = int(os.getenv("RETRY_BACKOFF_SEC", "3600"))
_INGEST_RETRY_QUEUE: list[dict] = []
_INGEST_FAILED: list[dict] = []
# Live counters for /status — incremented on entry, decremented in
# finally so they stay accurate even when an exception unwinds.
_ACTIVE_AGENT_RUNS = 0
_LAST_REPLY_AT: datetime | None = None
_FAILED_MAX = 200
# Max times an item can land in /failed before getting dropped entirely.
# Each /failed_retry round trip counts as one cycle. Without this the
# user-triggered retry loop could ping-pong an unfixable item forever
# (paywall, removed URL, permanently 404'd). 3 = "tried hard enough".
_FAILED_MAX_CYCLES = 3

# Per-ingest tracking — populated while a semaphore slot is actually
# running work so /status can show filename + elapsed time. Earlier
# the status only knew "X/2 busy" with no visibility into WHICH file
# or how long. Critical when the user has just queued multiple 600+
# chunk PDFs and wants to know "is it still chewing or stuck?".
_ACTIVE_INGESTS: dict[str, dict] = {}
# Files confirmed as duplicates by retry-handler dedup (any layer:
# filename / file_hash / body_hash / title) get recorded here so the
# next orphan scan skips them entirely — without this, legacy docs
# missing file_hash kept resurfacing as orphans every restart, the
# user kept seeing '145개 미학습 파일 발견' notifications, and the
# retry queue cycled the same items endlessly even though every cycle
# was a dedup no-op.
_DEDUP_CONFIRMED_PATH = config.DATA_DIR / "orphan_dedup_confirmed.json"
_DEDUP_CONFIRMED: set[str] = set()


def _load_dedup_confirmed() -> None:
    global _DEDUP_CONFIRMED
    try:
        import json as _json
        if _DEDUP_CONFIRMED_PATH.exists():
            _DEDUP_CONFIRMED = set(_json.loads(
                _DEDUP_CONFIRMED_PATH.read_text(encoding="utf-8")
            ))
    except Exception:
        log.exception("dedup_confirmed load failed")
        _DEDUP_CONFIRMED = set()


def _record_dedup_confirmed(filename: str) -> None:
    """Mark a filename as 'already in our knowledge under some other
    source label'. Persisted so the orphan scan skips it across
    restarts."""
    if not filename or filename in _DEDUP_CONFIRMED:
        return
    _DEDUP_CONFIRMED.add(filename)
    try:
        _atomic_write_json(_DEDUP_CONFIRMED_PATH, sorted(_DEDUP_CONFIRMED))
    except Exception:
        log.exception("dedup_confirmed persist failed")


def _filename_from_source(source: str) -> str:
    """Pull the filename portion out of a meta.documents.source label.
    Recognised shapes:
      tg-doc:<file_unique_id>:<filename>
      tg-doc-caption:<msg_id>     → no filename
      local:<filename>
      http(s)://...                → "" (URLs have no on-disk filename)
    Returns "" when the source format doesn't carry a filename.

    Used by every doc-deletion entry point (/dedupe_confirm, /cleanup_confirm,
    /forget, /forget_search...) so the deleted filename gets added to
    _DEDUP_CONFIRMED. Without this, the orphan scanner picks up the
    file on disk and the retry queue re-ingests it — an infinite
    dedup/retry loop reported by the user on 2026-05-15."""
    if not source:
        return ""
    if source.startswith("tg-doc:"):
        parts = source.split(":", 2)
        if len(parts) == 3:
            return parts[2]
    if source.startswith("local:"):
        return source.split(":", 1)[1]
    return ""


# Items the user explicitly told the bot to stop retrying (via
# /failed_clear). Filenames and URLs collected from the failure log
# at clear-time so the orphan scan, URL ingest, and forward-listener
# all skip them silently — no re-attempts, no /failed re-entries.
_PERMANENTLY_IGNORED_PATH = config.DATA_DIR / "permanently_ignored.json"
_IGNORED_FILENAMES: set[str] = set()
_IGNORED_URLS: set[str] = set()


def _load_permanently_ignored() -> None:
    global _IGNORED_FILENAMES, _IGNORED_URLS
    try:
        import json as _json
        if _PERMANENTLY_IGNORED_PATH.exists():
            data = _json.loads(_PERMANENTLY_IGNORED_PATH.read_text(encoding="utf-8"))
            _IGNORED_FILENAMES = set(data.get("filenames") or [])
            _IGNORED_URLS = set(data.get("urls") or [])
    except Exception:
        log.exception("permanently_ignored load failed")
        _IGNORED_FILENAMES = set()
        _IGNORED_URLS = set()


def _persist_permanently_ignored() -> None:
    try:
        _atomic_write_json(_PERMANENTLY_IGNORED_PATH, {
            "filenames": sorted(_IGNORED_FILENAMES),
            "urls": sorted(_IGNORED_URLS),
        })
    except Exception:
        log.exception("permanently_ignored persist failed")


def _is_ignored_filename(filename: str) -> bool:
    return bool(filename) and filename in _IGNORED_FILENAMES


def _is_ignored_url(url: str) -> bool:
    return bool(url) and url in _IGNORED_URLS
# Live ⏳ status bubbles persisted to disk so a bot restart can clean
# them up — otherwise the bubble freezes at whatever elapsed time
# was last rendered ('처리 중 (2분 00초)' forever) because the updater
# task dies with the container. Each entry: {chat_id, msg_id, label,
# started_at}. Updated as bubbles are sent (see _track_bubble) and
# removed on completion (_untrack_bubble).
_ACTIVE_BUBBLES_PATH = config.DATA_DIR / "active_bubbles.json"
_ACTIVE_BUBBLES: list[dict] = []


def _atomic_write_json(path, data) -> None:
    """Crash-safe JSON write: serialise → write to temp → fsync → rename.
    POSIX rename is atomic, so reader processes never see a partial
    file even if the writer is killed (SIGTERM, OOM, container kill)
    mid-call. Without this, _persist_retry_queue + auto_deploy +
    heavy concurrent ingest can corrupt the file, and the next
    startup load throws JSONDecodeError → queue silently empties →
    every queued item disappears.

    Also keeps a .bak copy of the previous good file so corruption
    recovery on load has a fallback."""
    import json, os
    tmp = path.with_suffix(path.suffix + ".tmp")
    bak = path.with_suffix(path.suffix + ".bak")
    payload = json.dumps(data, ensure_ascii=False)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    if path.exists():
        try:
            os.replace(path, bak)
        except OSError:
            pass
    os.replace(tmp, path)


def _load_json_with_recovery(path):
    """Load JSON, falling back to the .bak snapshot if the primary file
    is corrupt (truncated mid-write by a previous crash). Returns None
    on total loss so the caller can continue with an empty list."""
    import json
    for candidate in (path, path.with_suffix(path.suffix + ".bak")):
        if not candidate.exists():
            continue
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.exception(
                "%s corrupt — trying next fallback", candidate.name,
            )
            continue
    return None


def _persist_active_bubbles() -> None:
    try:
        _atomic_write_json(_ACTIVE_BUBBLES_PATH, _ACTIVE_BUBBLES)
    except Exception:
        log.exception("active_bubbles persist failed")


def _track_bubble(chat_id: int, msg_id: int, label: str) -> None:
    _ACTIVE_BUBBLES.append({
        "chat_id": chat_id, "msg_id": msg_id,
        "label": label[:120], "started_at": time.time(),
    })
    _persist_active_bubbles()


def _untrack_bubble(chat_id: int, msg_id: int | None) -> None:
    if msg_id is None:
        return
    before = len(_ACTIVE_BUBBLES)
    _ACTIVE_BUBBLES[:] = [
        b for b in _ACTIVE_BUBBLES
        if not (b["chat_id"] == chat_id and b["msg_id"] == msg_id)
    ]
    if len(_ACTIVE_BUBBLES) != before:
        _persist_active_bubbles()


def _ingest_label_from_msg(msg) -> tuple[str, str]:
    """Best-effort (kind, label) extraction for an incoming Telegram
    message about to be ingested. Used purely for status display."""
    if getattr(msg, "document", None):
        return "doc", (msg.document.file_name or "(no name)")
    if getattr(msg, "photo", None):
        return "photo", "photo"
    if getattr(msg, "voice", None):
        return "voice", "voice"
    if getattr(msg, "audio", None):
        title = (getattr(msg.audio, "title", None) or
                 getattr(msg.audio, "file_name", None) or "audio")
        return "audio", title
    if getattr(msg, "video", None):
        return "video", "video"
    text = (getattr(msg, "text", None) or getattr(msg, "caption", None) or "").strip()
    if text:
        first_line = text.splitlines()[0][:80]
        return "text", first_line
    return "unknown", "(unknown)"


def _register_ingest(label: str, kind: str, chat_id: int) -> str:
    job_id = uuid.uuid4().hex
    _ACTIVE_INGESTS[job_id] = {
        "label": label,
        "kind": kind,
        "started_at": time.time(),
        "chat_id": chat_id,
    }
    return job_id


def _unregister_ingest(job_id: str) -> None:
    _ACTIVE_INGESTS.pop(job_id, None)


def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}초"
    if s < 3600:
        return f"{s // 60}분 {s % 60:02d}초"
    return f"{s // 3600}시간 {(s % 3600) // 60}분"

# Memory management — automatic gc + libc malloc_trim so long-running
# processes hand pages back to the OS instead of inflating until OOM.
# Container OOM kills were a chronic pain on the e2-small VM; even on
# the e2-medium upgrade the bot's RSS can climb past mem_limit during
# ingest bursts. _MEM_CLEANUP_THRESHOLD triggers immediate cleanup
# before agent.run, _MEM_REFUSE_THRESHOLD refuses the run outright so
# we surface a clear error instead of letting Docker kill the process.
_MEM_CLEANUP_THRESHOLD = 0.90
_MEM_REFUSE_THRESHOLD = 0.95
_LAST_CLEANUP_AT: datetime | None = None
_LAST_CLEANUP_FREED_MB: float = 0.0
_MEM_LIMIT_MB_CACHE: float | None = None


def _process_rss_mb() -> float:
    """Current process RSS in MB via psutil. Returns 0.0 if psutil
    isn't importable (shouldn't happen in container, but keeps the
    cleanup path safe in dev)."""
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:
        return 0.0


def _cgroup_mem_limit_mb() -> float:
    """Read the container's memory cap. cgroup v2 (memory.max) takes
    precedence; falls back to v1 (memory.limit_in_bytes). Result is
    cached because the limit never changes within a container's life."""
    global _MEM_LIMIT_MB_CACHE
    if _MEM_LIMIT_MB_CACHE is not None:
        return _MEM_LIMIT_MB_CACHE
    for path in ("/sys/fs/cgroup/memory.max",
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = Path(path).read_text().strip()
            if raw and raw != "max":
                _MEM_LIMIT_MB_CACHE = int(raw) / (1024 * 1024)
                return _MEM_LIMIT_MB_CACHE
        except Exception:
            continue
    _MEM_LIMIT_MB_CACHE = 0.0
    return 0.0


def _mem_pressure() -> float:
    """Fraction of the cgroup memory limit currently used (0.0-1.0+).
    Returns 0.0 if either reading fails so we never falsely refuse."""
    limit = _cgroup_mem_limit_mb()
    if limit <= 0:
        return 0.0
    return _process_rss_mb() / limit


def _run_memory_cleanup(reason: str = "scheduled") -> float:
    """Force a Python GC pass + libc heap trim. Returns MB freed
    (negative if RSS grew — possible if another worker allocated
    during the call). Safe to call any time; cheap (<50ms)."""
    global _LAST_CLEANUP_AT, _LAST_CLEANUP_FREED_MB
    import gc
    import ctypes
    before = _process_rss_mb()
    collected = gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass
    after = _process_rss_mb()
    freed = before - after
    _LAST_CLEANUP_AT = datetime.utcnow()
    _LAST_CLEANUP_FREED_MB = freed
    log.info(
        "memory cleanup (%s): %.0f → %.0f MB (freed %.1f MB, gc=%d objs)",
        reason, before, after, freed, collected,
    )
    return freed

# Persist these two so a hang/restart doesn't lose state.
_RETRY_QUEUE_PATH = config.DATA_DIR / "retry_queue.json"
_FAILED_LOG_PATH = config.DATA_DIR / "failed_log.json"
_HISTORY_PATH = config.DATA_DIR / "chat_history.json"
# Marker file: when present, _recover_orphan_files_at_startup skips
# the boot-time orphan rescan. /queue_cancel_all creates it so a
# cancel survives container restarts (auto_pull rebuilds, etc.).
# /recover_orphans clears it on manual re-enable.
_RECOVERY_SUPPRESS_PATH = config.DATA_DIR / "no_auto_recovery"

# Per-chat conversation memory: keyed by chat_id, holds the most
# recent user/model text turns so follow-up questions like
# "그 회사의 경쟁사는?" carry topic context. Tool-call payloads are
# NOT stored — replaying stale retrievals confuses the model and
# wastes tokens.
_HISTORY: dict[int, list[dict]] = {}
_HISTORY_MAX_TURNS = 7   # 7 user + 7 model = 14 messages
_HISTORY_USER_CAP = 400
_HISTORY_MODEL_CAP = 1200


def _load_persisted_state() -> None:
    """Restore retry queue + failed log from disk on startup.

    The retry queue is deduped while loading because the orphan
    recovery scanner used to re-enqueue files that were already in
    the queue (bug: it only checked meta.documents, not the queue
    itself). Older saved queues can be 3-4× bloated, so we collapse
    duplicates here as well as fix the scanner."""
    import json
    try:
        data = _load_json_with_recovery(_RETRY_QUEUE_PATH)
        if isinstance(data, list):
                seen: set[str] = set()
                deduped: list[dict] = []
                dropped_unsupported = 0
                dropped_already_learned = 0
                for item in data:
                    # Filter unsupported local_file entries (legacy
                    # orphan scan used to push .ppt/.ipynb/.png/etc).
                    if item.get("kind") == "local_file":
                        path = item.get("path") or ""
                        suffix = Path(path).suffix.lower()
                        if suffix and suffix not in _SUPPORTED_INGEST_SUFFIXES:
                            dropped_unsupported += 1
                            continue
                        # Same filename already learned (possibly via
                        # tg-doc: source) → don't re-cycle it.
                        fname = Path(path).name
                        if fname and meta.find_by_filename(fname):
                            dropped_already_learned += 1
                            continue
                    key = (
                        item.get("file_name")
                        or item.get("path")
                        or item.get("url")
                        or (item.get("text", "")[:60] if item.get("text") else None)
                        or item.get("file_unique_id")
                    )
                    if not key:
                        deduped.append(item)
                        continue
                    if key in seen:
                        continue
                    seen.add(key)
                    # Restart-time recovery: any item that was being
                    # processed when the bot stopped still has an
                    # in_flight_ts mark. Clear it so the next retry
                    # tick re-attempts the work — otherwise items in
                    # flight at restart would be invisible until the
                    # _IN_FLIGHT_TIMEOUT (12 min) elapsed.
                    item.pop("in_flight_ts", None)
                    deduped.append(item)
                _INGEST_RETRY_QUEUE.extend(deduped)
                dropped = len(data) - len(deduped)
                dups = dropped - dropped_unsupported - dropped_already_learned
                if dropped > 0:
                    log.info(
                        "restored %d retry items "
                        "(%d duplicates, %d unsupported, %d already learned dropped)",
                        len(deduped), dups, dropped_unsupported,
                        dropped_already_learned,
                    )
                else:
                    log.info("restored %d items to retry queue", len(deduped))
    except Exception:
        log.exception("retry queue load failed")
    try:
        data = _load_json_with_recovery(_FAILED_LOG_PATH)
        if isinstance(data, list):
            _INGEST_FAILED.extend(data[-_FAILED_MAX:])
            log.info("restored %d failed entries", len(_INGEST_FAILED))
    except Exception:
        log.exception("failed log load failed")
    try:
        data = _load_json_with_recovery(_HISTORY_PATH)
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, list):
                    _HISTORY[int(k)] = v
            log.info("restored chat history for %d chats", len(_HISTORY))
    except Exception:
        log.exception("chat history load failed")


def _scan_orphan_files() -> list[Path]:
    """Return files under data/files/ that aren't in meta.documents.

    Compares the directory listing against the filename tails of
    'tg-doc:<uniq>:<filename>' and 'local:<filename>' source labels.
    Filenames known under either prefix are considered learned."""
    files_dir = Path(config.DATA_DIR) / "files"
    if not files_dir.exists():
        return []
    try:
        # Only sweep formats the pipeline actually handles. Stray
        # .ppt / .ipynb / .zip / random binaries used to enter the
        # queue and cycle through max_attempts before landing in
        # /failed — pure waste. Files that don't match here just
        # stay on disk untouched.
        all_files = {
            p.name: p for p in files_dir.iterdir()
            if p.is_file() and p.suffix.lower() in _SUPPORTED_INGEST_SUFFIXES
        }
    except Exception:
        log.exception("orphan scan failed: dir list")
        return []
    known: set[str] = set()
    import sqlite3
    db_path = config.DATA_DIR / "meta.db"
    try:
        with sqlite3.connect(str(db_path)) as c:
            for (src,) in c.execute("SELECT source FROM documents"):
                if not src:
                    continue
                if src.startswith("tg-doc:"):
                    parts = src.split(":", 2)
                    if len(parts) == 3:
                        known.add(parts[2])
                elif src.startswith("local:"):
                    known.add(src.split(":", 1)[1])
    except Exception:
        log.exception("orphan scan failed: db query")
        return []
    # Files already queued for retry are NOT orphans yet — re-pushing
    # them produces N× duplicates on every restart (the bug that
    # bloated the queue 595 = 152 × ~4 deploys). Treat queued items
    # as 'known' so the scan only enqueues truly missing files.
    for item in _INGEST_RETRY_QUEUE:
        kind = item.get("kind", "")
        if kind == "local_file":
            p = item.get("path") or ""
            if p:
                known.add(Path(p).name)
        elif kind == "doc":
            fn = item.get("file_name") or ""
            if fn:
                known.add(fn)
    # Also exclude filenames that the retry handler already confirmed
    # as duplicates in a previous run (different source label /
    # body_hash / title match). Without this, legacy docs without
    # file_hash keep resurfacing every restart.
    known |= _DEDUP_CONFIRMED
    # And filenames the user explicitly told us to forget via
    # /failed_clear. Those files might still be on disk but we never
    # want to attempt them again.
    known |= _IGNORED_FILENAMES
    # Files currently in the /failed log are NOT orphans either —
    # they've been attempted and parked for manual retry. Without
    # this guard the periodic orphan scan re-enqueues every /failed
    # item every hour, creating an infinite loop with
    # _MAX_RETRY_ATTEMPTS=1 (enqueue → fail → /failed → re-enqueue).
    for entry in _INGEST_FAILED:
        payload = entry.get("retry") or {}
        name = payload.get("file_name")
        if not name and payload.get("path"):
            try:
                name = Path(payload["path"]).name
            except Exception:
                name = None
        if name:
            known.add(name)

    # First pass: filter by filename (cheap source-label match).
    candidates = [p for name, p in all_files.items() if name not in known]
    if not candidates:
        return []
    # Second pass: for each surviving candidate, check file_hash
    # against meta.documents.file_hash. Catches the case where the
    # file is learned under an unrelated source label (URL, paste,
    # different filename) — orphan scan would otherwise re-queue it
    # forever even though dedup short-circuits on every retry.
    import hashlib as _hashlib
    try:
        with sqlite3.connect(str(db_path)) as c:
            rows = c.execute(
                "SELECT file_hash FROM documents WHERE file_hash IS NOT NULL"
            ).fetchall()
        known_file_hashes = {h[0] for h in rows if h[0]}
    except Exception:
        log.exception("orphan scan: file_hash gather failed")
        known_file_hashes = set()

    def _quick_file_hash(path: Path) -> str:
        try:
            h = _hashlib.sha1()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            return h.hexdigest()[:16]
        except Exception:
            return ""

    survivors = []
    for p in candidates:
        if _quick_file_hash(p) in known_file_hashes:
            continue  # learned under a different source label
        survivors.append(p)
    return sorted(survivors, key=lambda p: p.name)


def _enqueue_orphan_recovery(orphans: list[Path], chat_id: int) -> int:
    """Push orphan files onto the retry queue as kind='local_file'.
    The existing _retry_pending_ingest tick (2 min interval, single
    file at a time, shares the semaphore) drains them safely. Returns
    the number enqueued."""
    if not orphans:
        return 0
    for p in orphans:
        _INGEST_RETRY_QUEUE.append({
            "kind": "local_file",
            "path": str(p),
            "file_name": p.name,
            "chat_id": chat_id,
            "attempts": 0,
        })
    _persist_retry_queue()
    log.info("orphan recovery: enqueued %d files", len(orphans))
    return len(orphans)


def _cleanup_stale_bubbles_at_startup(app) -> None:
    """Edit any ⏳ bubble that was in flight when the previous container
    instance shut down. Without this, restarted ingests leave their
    original status messages frozen at whatever elapsed time was last
    rendered (the '처리 중 (2분 00초)' UX bug).

    Loads the persisted active_bubbles list, schedules an edit_message
    job for each entry that flips it to '⏸ 학습 중단됨 — 자동 복구 큐
    에서 재처리', then clears the list. The actual edits run after the
    Application starts so the bot can post."""
    import json as _json
    if not _ACTIVE_BUBBLES_PATH.exists():
        return
    try:
        entries = _json.loads(_ACTIVE_BUBBLES_PATH.read_text(encoding="utf-8"))
    except Exception:
        log.exception("active_bubbles load failed")
        try:
            _ACTIVE_BUBBLES_PATH.unlink()
        except Exception:
            pass
        return
    if not entries:
        try:
            _ACTIVE_BUBBLES_PATH.unlink()
        except Exception:
            pass
        return

    async def _flip_bubbles(_ctx):
        for e in entries:
            try:
                await app.bot.edit_message_text(
                    chat_id=e["chat_id"], message_id=e["msg_id"],
                    text=(
                        f"⏸ 학습 중단됨 — {(e.get('label') or '')[:80]}\n"
                        "재시작으로 끊겼고, 디스크에 남아있으면 자동 복구 큐에서 재처리됩니다."
                    ),
                )
            except Exception as ex:
                log.info("stale bubble edit skipped (%s)",
                         type(ex).__name__)
        # Clear file regardless of edit success — never re-process the
        # same bubble list across multiple restarts.
        try:
            _ACTIVE_BUBBLES_PATH.unlink()
        except Exception:
            pass
        log.info("cleaned %d stale bubbles", len(entries))

    if app.job_queue:
        app.job_queue.run_once(_flip_bubbles, when=3, name="cleanup_stale_bubbles")


def _recover_orphan_files_at_startup(app) -> None:
    """Scan for orphan files on boot and enqueue them for recovery.
    Sends a one-time owner notification so the user knows recovery
    is in flight (otherwise the queue silently fills up). Skipped
    when DASHBOARD-only / config.TELEGRAM_OWNER_ID is unset.

    Honours the `_RECOVERY_SUPPRESS_PATH` marker — when the user has
    explicitly cancelled all work via /queue_cancel_all, we don't
    silently re-enqueue everything on the next deploy. Marker is
    cleared by /recover_orphans."""
    if _RECOVERY_SUPPRESS_PATH.exists():
        log.info(
            "orphan recovery: suppressed (marker %s present, "
            "use /recover_orphans to re-enable)",
            _RECOVERY_SUPPRESS_PATH,
        )
        return
    try:
        orphans = _scan_orphan_files()
        if not orphans:
            log.info("orphan recovery: no orphans")
            return
        count = _enqueue_orphan_recovery(orphans, config.TELEGRAM_OWNER_ID)
        if count <= 0:
            return
        # Estimate: retry tick = _RETRY_INGEST_INTERVAL_SEC, draining
        # _RETRY_INGEST_BATCH items per tick.
        per_min = max(1, _RETRY_INGEST_BATCH * 60 // _RETRY_INGEST_INTERVAL_SEC)
        eta_min = max(1, count // per_min)
        try:
            async def _notify(_ctx):
                try:
                    await app.bot.send_message(
                        config.TELEGRAM_OWNER_ID,
                        f"🔄 {count}개 미학습 파일 발견 — 자동 재학습 큐에 추가됨\n"
                        f"   {_RETRY_INGEST_INTERVAL_SEC}초 간격으로 최대 "
                        f"{_RETRY_INGEST_BATCH}개씩 처리 (~{eta_min}분 소요 예상)\n"
                        f"   /queue 로 진행 상황 확인 가능",
                    )
                except Exception:
                    log.exception("orphan recovery notify failed")
            if app.job_queue:
                app.job_queue.run_once(_notify, when=5)
        except Exception:
            log.exception("orphan recovery notify schedule failed")
    except Exception:
        log.exception("orphan recovery startup failed")


async def _drain_pending_url_decisions(ctx) -> None:
    """When the retry queue is idle, prompt the user about every URL
    whose body extraction failed but hasn't been asked about yet.
    Each entry gets its own bubble with [🔁 재시도] / [🚫 차단]
    buttons. Cap at 5 per tick to avoid flooding the chat on big
    backlogs; the next idle tick picks up the rest."""
    try:
        if _INGEST_RETRY_QUEUE:
            # Not idle yet — wait for next tick.
            return
        pending = pending_url_decisions.list_unprompted()
        if not pending:
            return
        if not config.TELEGRAM_OWNER_ID:
            return
        DRAIN_CAP = 5
        for entry in pending[:DRAIN_CAP]:
            url = entry.get("url") or ""
            if not url:
                continue
            title = (entry.get("title") or "")[:80]
            error = (entry.get("error") or "본문 비어있음")[:120]
            retry_n = int(entry.get("retry_count") or 0)
            retry_tag = f" (시도 #{retry_n + 1})" if retry_n else ""
            text = (
                f"⚠️ URL 본문 추출 실패{retry_tag}\n"
                f"{url}\n"
            )
            if title and title != url:
                text = f"⚠️ URL 본문 추출 실패{retry_tag}: {title}\n{url}\n"
            text += f"오류: {error}\n\n선택해 주세요:"
            # callback_data has a 64-byte limit; we key by URL hash
            # because long URLs blow past that. The hash is stable
            # per-URL so retry/block dispatch can fetch the entry by
            # iterating pending_url_decisions.list_all().
            import hashlib as _h
            key = _h.sha1(url.encode("utf-8")).hexdigest()[:16]
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🔁 재시도",
                    callback_data=f"urldec_retry:{key}",
                ),
                InlineKeyboardButton(
                    "🚫 차단",
                    callback_data=f"urldec_block:{key}",
                ),
            ]])
            try:
                await ctx.bot.send_message(
                    config.TELEGRAM_OWNER_ID, text,
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
                pending_url_decisions.mark_prompted(url)
            except Exception:
                log.exception("urldec prompt send failed for %s", url[:120])
    except Exception:
        log.exception("drain pending url decisions failed")


def _urldec_find_by_key(key: str) -> dict | None:
    """Map the sha1-prefix callback key back to an entry. Used by
    the retry/block callback handlers since the URL itself doesn't
    fit in callback_data."""
    import hashlib as _h
    for e in pending_url_decisions.list_all():
        url = e.get("url") or ""
        if _h.sha1(url.encode("utf-8")).hexdigest()[:16] == key:
            return e
    return None


async def _periodic_orphan_scan(ctx) -> None:
    """Hourly orphan scan to catch files dropped on disk without a
    matching meta.documents entry (e.g. forwarded files saved by the
    bot but ingest crashed before the doc row got written, manual
    copies, partial restores). Boot-time scan covers the immediate
    case; this catches drift during long uptimes.

    Quiet operation — no Telegram notification on every scan, just a
    log line. The boot-scan notification is kept as-is so the user
    still gets the visible recovery message after restarts. Respects
    the suppress marker so /queue_cancel_all stays effective."""
    if _RECOVERY_SUPPRESS_PATH.exists():
        return
    try:
        orphans = await asyncio.to_thread(_scan_orphan_files)
        if not orphans:
            return
        count = _enqueue_orphan_recovery(orphans, config.TELEGRAM_OWNER_ID)
        if count > 0:
            log.info("periodic orphan scan: enqueued %d files", count)
    except Exception:
        log.exception("periodic orphan scan failed")


def _persist_retry_queue() -> None:
    try:
        _atomic_write_json(_RETRY_QUEUE_PATH, _INGEST_RETRY_QUEUE)
    except Exception:
        log.exception("retry queue persist failed")


def _persist_chat_history() -> None:
    try:
        _atomic_write_json(
            _HISTORY_PATH,
            {str(k): v for k, v in _HISTORY.items()},
        )
    except Exception:
        log.exception("chat history persist failed")


def _record_turn(chat_id: int, role: str, text: str,
                 sources: list[str] | None = None,
                 tools: list[str] | None = None) -> None:
    """Append one turn to this chat's rolling history. Trims old turns
    so memory stays bounded; persists to disk so restarts don't drop
    context. Model turns can also carry the sources/tools that produced
    them so a follow-up that doesn't trigger a new search still has the
    previous turn's citations to show."""
    if not text:
        return
    cap = _HISTORY_USER_CAP if role == "user" else _HISTORY_MODEL_CAP
    entry: dict = {"role": role, "text": text[:cap]}
    if role == "model":
        if sources:
            entry["sources"] = list(sources)
        if tools:
            entry["tools"] = list(tools)
    h = _HISTORY.setdefault(chat_id, [])
    h.append(entry)
    max_msgs = _HISTORY_MAX_TURNS * 2
    if len(h) > max_msgs:
        del h[: len(h) - max_msgs]
    _persist_chat_history()


def _persist_failed_log() -> None:
    try:
        _atomic_write_json(_FAILED_LOG_PATH, _INGEST_FAILED)
    except Exception:
        log.exception("failed log persist failed")

URL_RE = re.compile(r"https?://[^\s\)\]<>\"']+")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s\)]+)\)")
_URL_TRAILING_RE = re.compile(r"[).,;:!?\]]+$")
_MD_BOLD_RE = re.compile(r"\*\*([^\*\n]{1,200}?)\*\*")
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)


def _extract_urls(text: str) -> tuple[list[str], str]:
    """Pull markdown-link and plain URLs out of `text`, return cleaned URLs
    plus the leftover text with URL syntax stripped. Trailing closing-paren
    and other punctuation that gets glued onto a URL are removed."""
    urls: list[str] = []

    def _take_md_link(m: "re.Match[str]") -> str:
        urls.append(_URL_TRAILING_RE.sub("", m.group(2)))
        return m.group(1)

    cleaned = _MD_LINK_RE.sub(_take_md_link, text)
    for raw in URL_RE.findall(cleaned):
        url = _URL_TRAILING_RE.sub("", raw)
        if url and url not in urls:
            urls.append(url)
    text_no_urls = URL_RE.sub("", cleaned).strip()
    return urls, text_no_urls


_INTERNAL_TG_RE = re.compile(r"^https?://t\.me/", re.IGNORECASE)
_MAX_URLS_PER_MSG = 5


def _collect_message_urls(msg) -> tuple[list[str], str]:
    """Plain text URLs + markdown links + Telegram text_link entities
    + inline-keyboard URL buttons. Drops t.me internal links, dedups,
    caps at _MAX_URLS_PER_MSG so spammy channels can't trigger runaway
    ingestion."""
    text = msg.text or msg.caption or ""
    urls, plain = _extract_urls(text)

    for ent_list in (msg.entities or [], msg.caption_entities or []):
        for ent in ent_list:
            u = getattr(ent, "url", None)
            if getattr(ent, "type", None) == "text_link" and u and u not in urls:
                urls.append(u)

    rm = getattr(msg, "reply_markup", None)
    if rm and getattr(rm, "inline_keyboard", None):
        for row in rm.inline_keyboard:
            for btn in row:
                u = getattr(btn, "url", None)
                if u and u not in urls:
                    urls.append(u)

    urls = [u for u in urls if not _INTERNAL_TG_RE.match(u)]

    is_forward = bool(
        getattr(msg, "forward_origin", None)
        or getattr(msg, "forward_from_chat", None)
        or getattr(msg, "forward_from", None)
    )
    # Forwarded automation digest (e.g. Noahsummary) tends to bundle 50+
    # URLs per message — let those ride as plain text, the URLs survive
    # inside the body string. Hand-curated forwards (broker reports
    # with 1~4 links) keep their URL ingest path.
    if is_forward and len(urls) >= _MAX_URLS_PER_MSG:
        urls = []
    else:
        urls = urls[:_MAX_URLS_PER_MSG]
    return urls, plain
_MERMAID_BLOCK_RE = re.compile(r"```mermaid\s*\n(.*?)\n```", re.DOTALL)
_NUMBERED_SECTION_RE = re.compile(
    r"^(\s*)(\d+)\.\s+(.{3,100}?)[:：]?\s*$", re.MULTILINE
)
_SECTION_EMOJIS = ["📌", "🔹", "🔸", "⚙️", "🧪", "💡", "📊", "🎯", "⚡", "🔧"]

# Per-tool emoji so the user can tell at a glance whether the answer
# came from their saved RAG store, an external academic DB, the web,
# etc. Anything not listed falls through to 🔧.
_TOOL_EMOJI = {
    "search_my_brain": "🧠",
    "compare_papers": "🧠",
    "recent_docs": "🧠",
    "search_papers": "📄",
    "web_search": "🌐",
    "ingest_url": "📥",
}


def _format_tool_calls(calls: list[str]) -> str:
    parts = [f"{_TOOL_EMOJI.get(c, '🔧')} {c}" for c in calls]
    return " → ".join(parts)
_SEP = "━" * 22


def _extract_mermaid(text: str) -> tuple[str, list[str]]:
    """Pull every ```mermaid``` block out so we can render it as a photo
    instead of leaking the raw code to the user."""
    blocks = [m.group(1).strip() for m in _MERMAID_BLOCK_RE.finditer(text)]
    cleaned = _MERMAID_BLOCK_RE.sub("", text).strip()
    return cleaned, blocks


async def _render_mermaid_png(code: str) -> bytes:
    """Render a Mermaid diagram to PNG bytes, trying kroki POST first
    (most lenient parser, no URL length limit) and falling back to
    mermaid.ink GET."""
    import httpx
    code = code.strip()

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.post(
                "https://kroki.io/mermaid/png",
                content=code.encode("utf-8"),
                headers={"Content-Type": "text/plain"},
            )
            if r.status_code == 200 and r.headers.get(
                "content-type", ""
            ).startswith("image/"):
                return r.content
            log.warning("kroki failed: %d %s",
                        r.status_code, r.text[:200])
    except Exception as e:
        log.warning("kroki render failed: %s", e)

    encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
    url = f"https://mermaid.ink/img/{encoded}?type=png&bgColor=white"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.content


def _enforce_format(text: str) -> str:
    """Strip stray markdown and beautify numbered sections with separators.
    Telegram is plain-text (no parse_mode), so we use unicode glyphs for the
    visual hierarchy the model is supposed to produce but sometimes skips."""
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_HEADING_RE.sub("", text)
    counter = [0]

    def replace(m: "re.Match[str]") -> str:
        indent, num, title = m.group(1), m.group(2), m.group(3).rstrip(":：").strip()
        if any(ch in title for ch in "•|") or "  " in title:
            return m.group(0)
        emoji = _SECTION_EMOJIS[counter[0] % len(_SECTION_EMOJIS)]
        counter[0] += 1
        return f"\n{indent}{_SEP}\n{indent}{emoji} {num}. {title}\n{indent}{_SEP}"

    return _NUMBERED_SECTION_RE.sub(replace, text)


def _strip_markdown(text: str) -> str:
    return _enforce_format(text)


# Lightweight stripper for command output (summary/title/detail fields).
# Telegram renders messages as plain text, so any markdown the source
# happens to use shows up as raw `**`, `##`, `*` characters and hurts
# readability. _enforce_format above is too heavy for these fields
# (it adds section separators), so we use a minimal pass instead.
_MD_BOLD2_RE_ = re.compile(r"\*\*([^\*\n]{1,300}?)\*\*")
_MD_ITALIC_RE_ = re.compile(r"(?<!\*)\*([^\*\n]+?)\*(?!\*)")
_MD_CODE_INLINE_RE_ = re.compile(r"`([^`\n]+?)`")
_MD_HEADER_RE_ = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_MD_LIST_STAR_RE_ = re.compile(r"^(\s*)\*\s+", re.MULTILINE)
_BLANK_LINES_RE_ = re.compile(r"\n{3,}")


def _clean_text(text: str) -> str:
    if not text:
        return text or ""
    text = _MD_BOLD2_RE_.sub(r"\1", text)
    text = _MD_ITALIC_RE_.sub(r"\1", text)
    text = _MD_CODE_INLINE_RE_.sub(r"\1", text)
    text = _MD_HEADER_RE_.sub("", text)
    text = _MD_LIST_STAR_RE_.sub(r"\1• ", text)
    text = _BLANK_LINES_RE_.sub("\n\n", text)
    return text.strip()


def _is_owner(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == config.TELEGRAM_OWNER_ID)


async def _typing(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # send_chat_action is rate-limited harder than send/edit_message
    # (we hit 429 on a recover_missing burst that triggered _typing on
    # 1000+ items). Swallow 429s + transport errors so a flaky chat
    # action never propagates — the typing indicator is a nicety, not
    # essential.
    if update.effective_chat:
        try:
            await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        except Exception:
            pass


async def _sustained_typing(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Re-send the 'typing...' chat action every few seconds so the
    user sees continuous activity through long agent runs (Pro
    synthesis on a 50-doc compare can be ~30-60s). 2s cadence keeps
    the indicator visibly active even when the Telegram client
    refreshes lazily."""
    while True:
        try:
            await _typing(update, ctx)
        except Exception:
            pass
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            break


_HELP_TEXT = """<b>🧠 SECOND BRAIN 봇</b>

<b>【1. 명령어】</b>
조회: /find &lt;kw&gt; [N=50] · /find_all &lt;kw&gt;(최대 500) · /show &lt;id|kw&gt;(본문 전체 청크 dump) · /recent [N] · /recent_docs · /stats · /status · /usage · /cost
대화: /reset · /deep &lt;질문&gt;(Pro 강제)
장애: /failed(크기순·건별 [🔁]/[🗑] · drop=영구 무시) · /failed_retry · /failed_clear · /queue · /queue_to_failed(큐→실패로) · /queue_cancel_all · /audit · /blocked_hosts · /reset_blocked_hosts
Orphan: /orphans · /recover_orphans(크기순·건별 [📥]/[🗑])
보류(5분): /pending(OCR 크기순·건별 결정) · /pending_ocr &lt;N&gt; · /pending_pro &lt;N&gt; · /pending_approve_all · /pending_approve_all_confirm · /pending_cancel_all · /ocr_extend &lt;id|kw&gt;
삭제: /forget &lt;id&gt; · /forget_search · /forget_search_all · /forget_qna · /forget_qna_search · /dedupe · /dedupe_confirm · /cleanup · /cleanup_confirm · /forget_forwards · /forget_forwards_confirm
도구: /search_my_brain · /compare_papers · /search_papers · /web_search · /ingest_url
기타: /start /help

<b>【2. 핵심】</b> 채널/DM 자료→자동 수집·요약·임베딩·Obsidian / 자연어→에이전트 도구 자동 / 메모리 7턴(/reset) / 비용·Q&amp;A SQLite+대시보드 / 답변 끝 (자료 시점: YYYY.MM)

<b>【3. 도구】</b> 🧠 search_my_brain TOP_K 10 · 🧠 compare_papers 50건 통합(20+ Pro/Flash/취소) · 🧠 recent_docs · 📄 search_papers 15건/6소스 라우팅+PDF · 🌐 web_search (명시 시만) · 📥 ingest_url

<b>【4. 자연어 트리거】</b>
🧠 brain "삼성전기 MLCC" · 🧠 compare "정리/리뷰/비교/전체" · 📄 papers "찾아줘/논문" · 🌐 <b>web — "웹/구글/인터넷" 중 하나가 메시지에 있을 때만</b>(시간 표현은 트리거 X) · 📥 ingest "학습해줘 URL"·URL만

<b>【5. 자료 인입】</b> URL·PDF·PPTX·DOCX·XLSX·이미지·음성·YouTube·텍스트 전송
• PDF: 텍스트 자동 추출(PyMuPDF). sparse PDF는 <b>자동 OCR 0p</b>(OCR_AUTO_CAP=0), 학습 직후 3-버튼 prompt 발송 [📄 OCR 추가 / 📝 텍스트만 유지 / 🚫 학습 취소]. 버튼 만료 없음, pending_store에 영구 보관. 이미지-only PDF는 first 3p만 자동 OCR(OCR_IMAGE_ONLY_CAP) • 이미지 캡션≥80자면 OCR skip · 짧으면 OCR · [OCR] 강제 • 음성: Gemini STT · YouTube: 자막→Jina • <b>.txt/.md/.csv 첨부=학습 제외</b>
차단: LinkedIn/FB/IG, Reuters/Bloomberg/WSJ/FT/NYT/WaPo

<b>【6. 자동 포워딩】</b> .env LISTEN_CHANNELS·LISTEN_PLAIN_CHANNELS
[Noah 디지스트] 📋 TG 원문 fetch / 📰 Substack URL relay / 그 외 drop
[PLAIN] 본문 그대로(URL strip, 이미지 drop): daju_dart(DART 공시) · Fundeasyearnings(알파스캐너 drop) · aicorporateanalysisdeepdive(AI 리서치 딥다이브) · benineb9(블로그 글)
백필: tmux + python -m src.scripts.import_channel &lt;ch&gt; --resume

<b>【7. 메타데이터】</b> Flash-Lite 요약+메타 1콜 (이미지/음성/짧은 텍스트/캡션은 메타 skip) · 🏢회사 🏷태그 📅YYYY.MM

<b>【8. 대시보드】</b> http://34.50.23.221:8082/1e68e9fae4e6fb1f8298bdee768eb73b/index.html · Basic Auth(.env) · 60s 자동 갱신·다크 19~07

<b>【9. 답변 품질】</b> 자료 시점 필수·부족시 솔직히 표시 / 후속 질문도 brain 재검색 / web 결과 [도메인] / 인용=자료제목 본문 [N] 자동

<b>【10. 운영】</b> VM n2-standard-4(4vCPU/16GB) bot 12GB · Sem 8+batch 8 · concurrent_updates+HTTPX 32 · 영속(retry/failed/history/qna/cost/ocr_cache/chunk_cache/bubbles) · 메모리 5분(90%즉시 95%거부) · 60s call · 10분 ingest

<b>【10-1. 모델·단가】</b> 임베딩 Gemini embedding-001 3072d · 요약/메타 Flash-Lite(503→Flash) · 답변 Flash·Pro(/deep) · Vision Flash-Lite DPI 100 · 1M ₩ Pro 1,750/Flash 420/Lite 140/Embed 200 · 답변 1h 캐시

<b>【10-2. 비용 절감(자동)】</b>
✅ 6단 dedup ₩0: ①source ②URL canonical ③file_hash ④text_hash ⑤body_hash ⑥title 정규화
✅ 청크 1000 토큰·요약 단일콜 12k/partial 8k·Vision DPI 100·sparse 자동캡 0p(OCR_AUTO_CAP)·image-only 3p(OCR_IMAGE_ONLY_CAP)·트리거 800자/p
✅ <b>Progressive OCR</b>(probe 3p 텍스트 &lt;300자면 나머지 skip, 환경변수 OCR_PROBE_MIN_TEXT) · <b>청크 임베딩 캐시</b>(반복 disclaimer 청크 Gemini 호출 0) · <b>메타 추출 gating</b>(이미지/음성/짧은 텍스트 skip)
✅ 페이지 image hash dedup·빈/표지 skip·표 무료 임베딩·짧은 forward 요약 skip·차단 도메인·.txt/.md/.csv 제외·failed URL 즉시 skip·/failed_clear 영구

<b>【10-3. Retry/무손실 재개 (신규=재시도 동일)】</b> 5회 선형(1h→2h→3h→4h→/failed)·not_before_ts·silent retry · <b>모든 인입 시작 시 in_flight_ts 디스크 저장 → 배포·OOM·SIGKILL에도 자동 재개</b>(stop_grace 120s·부팅 시 stale 클리어 10s 픽업)·JSON persist tmp→fsync→rename+.bak 폴백·/audit 메모리/디스크/orphan 검증

<b>【11. 트러블슈팅】</b> 본문 비어있음→차단/paywall · 무응답→docker logs thesis-bot-1 · brain 에러→BM25 30s 후 · 토픽 어긋남→/reset · 비용 급등→audio/Pro/web · backend 전환 .env EMBED_BACKEND=gemini|bge-m3 · OCR_BACKEND=gemini|local|hybrid (local/hybrid은 docker compose --profile ocr-local up -d ocr-worker 필요)"""


# Telegram caps a single message at 4096 chars. _HELP_TEXT is hand-
# compacted to fit, but the section list keeps growing as commands /
# policies are added. Safety net: paragraph-aligned auto-split so a
# future edit that pushes past the limit still delivers the full help
# instead of silently failing.
_TG_MSG_LIMIT = 4096
_HELP_SOFT_LIMIT = 4000  # keep margin for parse_mode tags


def _split_for_telegram(text: str, limit: int = _HELP_SOFT_LIMIT) -> list[str]:
    """Split on blank-line paragraph boundaries; if a single paragraph
    is itself too long, fall back to line-level splits. Never breaks
    inside a line so HTML tags stay balanced."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        if len(para) > limit:
            # Paragraph alone exceeds limit — fall back to line splits.
            if buf:
                chunks.append(buf)
                buf = ""
            line_buf = ""
            for line in para.split("\n"):
                cand = (line_buf + "\n" + line) if line_buf else line
                if len(cand) > limit and line_buf:
                    chunks.append(line_buf)
                    line_buf = line
                else:
                    line_buf = cand
            if line_buf:
                buf = line_buf
            continue
        cand = (buf + "\n\n" + para) if buf else para
        if len(cand) > limit and buf:
            chunks.append(buf)
            buf = para
        else:
            buf = cand
    if buf:
        chunks.append(buf)
    return chunks


async def _send_pieces_with_throttle(
    send_fn, pieces, *, throttle: float = 0.15, max_retries: int = 3,
    **kw,
) -> int | None:
    """Send `pieces` one at a time with a small inter-message sleep
    and per-piece retry on Telegram FloodWait/RetryAfter. Returns
    the first sent message_id so callers can attach a reply-link
    footer (e.g. /show and /find's `⬆️ 처음으로`).

    Background: /show on a 39-chunk doc fanned out ~30 send_message
    calls; Telegram throttled around message 7 and the raw `for ...
    await reply_text(...)` loop crashed without catching the
    RetryAfter, so the user got a half-dumped doc. With this helper
    a RetryAfter just sleeps the mandated duration and re-sends the
    same piece; other errors log and move on (partial dump > total
    silence)."""
    try:
        from telegram.error import RetryAfter
    except Exception:  # pragma: no cover
        RetryAfter = None  # type: ignore
    first_id = None
    for i, piece in enumerate(pieces):
        for attempt in range(max_retries):
            try:
                sent = await send_fn(piece, **kw)
                if first_id is None and sent is not None:
                    first_id = getattr(sent, "message_id", None)
                break
            except Exception as e:
                if RetryAfter is not None and isinstance(e, RetryAfter):
                    wait = max(int(getattr(e, "retry_after", 1)), 1) + 1
                    log.info(
                        "send_pieces: RetryAfter %ds on piece %d/%d "
                        "(attempt %d), sleeping",
                        wait, i + 1, len(pieces), attempt + 1,
                    )
                    await asyncio.sleep(wait)
                    continue
                log.exception(
                    "send_pieces: send failed on piece %d/%d",
                    i + 1, len(pieces),
                )
                break
        if throttle > 0:
            await asyncio.sleep(throttle)
    return first_id


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    await _typing(update, ctx)
    for chunk in _split_for_telegram(_HELP_TEXT):
        await update.message.reply_text(
            chunk, parse_mode="HTML", disable_web_page_preview=True,
        )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    await _typing(update, ctx)
    await update.message.reply_text(
        f"문서 {meta.count()}개 / 청크 {vector.chunk_count()}개"
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Quick health snapshot — what is the bot doing right now?
    Reads in-memory counters so it returns even when the event loop
    is busy with ingest. Owner-only."""
    if not _is_owner(update):
        return
    await _typing(update, ctx)
    ingest_capacity = _INGEST_SEM_CAPACITY
    ingest_idle = _INGEST_SEM._value  # remaining slots
    ingest_busy = max(0, ingest_capacity - ingest_idle)
    last = _LAST_REPLY_AT
    last_str = "-"
    if last:
        age = int((datetime.utcnow() - last).total_seconds())
        if age < 60:
            last_str = f"{age}초 전"
        elif age < 3600:
            last_str = f"{age // 60}분 전"
        else:
            last_str = f"{age // 3600}시간 전"
    rss = _process_rss_mb()
    limit = _cgroup_mem_limit_mb()
    if limit > 0:
        mem_line = f"\n🧠 메모리: {rss:.0f} / {limit:.0f} MB ({rss/limit*100:.0f}%)"
    else:
        mem_line = f"\n🧠 메모리: {rss:.0f} MB"
    cleanup_at = _LAST_CLEANUP_AT
    if cleanup_at:
        age = int((datetime.utcnow() - cleanup_at).total_seconds())
        if age < 60:
            ago = f"{age}초 전"
        elif age < 3600:
            ago = f"{age // 60}분 전"
        else:
            ago = f"{age // 3600}시간 전"
        cleanup_line = (
            f"\n🧹 마지막 메모리 청소: {ago} "
            f"({_LAST_CLEANUP_FREED_MB:+.1f} MB 회수)"
        )
    else:
        cleanup_line = "\n🧹 마지막 메모리 청소: 아직 없음 (5분 주기 자동)"
    # Active ingest detail — file name + elapsed time per running
    # slot. Sorted by start time (oldest first) so the user sees
    # "what's been running longest, is it stuck?" at a glance.
    active = sorted(
        _ACTIVE_INGESTS.values(), key=lambda v: v.get("started_at", 0)
    )
    now = time.time()
    ingest_lines = []
    for i, info in enumerate(active):
        elapsed = now - info.get("started_at", now)
        label = (info.get("label") or "(unknown)")[:60]
        kind = info.get("kind", "")
        kind_tag = f" [{kind}]" if kind and kind not in ("doc", "text") else ""
        prefix = "   └─" if i == len(active) - 1 else "   ├─"
        ingest_lines.append(
            f"\n{prefix} {label}{kind_tag} ({_fmt_elapsed(elapsed)})"
        )
    ingest_detail = "".join(ingest_lines)

    out = (
        "🤖 봇 상태\n"
        f"\n💬 활성 agent: {_ACTIVE_AGENT_RUNS}건"
        f"\n📥 동시 학습: {ingest_busy}건 (슬롯 {ingest_busy}/{ingest_capacity}){ingest_detail}"
        f"\n🔁 인입 재시도 큐: {len(_INGEST_RETRY_QUEUE)}건"
        f"\n💤 agent 재시도 큐: {len(_RETRY_QUEUE)}건"
        f"\n❌ 영구 실패: {len(_INGEST_FAILED)}건"
        f"\n⏱ 마지막 응답: {last_str}"
        + mem_line + cleanup_line
    )
    await update.message.reply_text(out)


async def cmd_usage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Ingest velocity, type breakdown, rough cost band so the user
    can spot anomalies (sudden surge, drop) at a glance."""
    if not _is_owner(update):
        return
    await _typing(update, ctx)
    s = meta.usage_stats()
    chunks = vector.chunk_count()
    queue_len = len(_INGEST_RETRY_QUEUE)
    failed_len = len(_INGEST_FAILED)

    types_line = ", ".join(f"{t}:{c}" for t, c in s["types"][:8]) or "-"

    today = cost.today_krw()
    week = cost.period_krw(7)
    mtd = cost.month_to_date_krw()
    by_today = today["by_model"]
    cost_lines = []
    for m in ("gemini-2.5-pro", "gemini-2.5-flash",
              "gemini-2.5-flash-lite", "gemini-embedding-001"):
        if m in by_today:
            d = by_today[m]
            cost_lines.append(
                f"    {m.replace('gemini-2.5-', '').replace('gemini-', '')}"
                f"  ₩{d['cost']:,.1f}  ({d['calls']}콜)"
            )
    cost_breakdown = ("\n" + "\n".join(cost_lines)) if cost_lines else ""

    by_purpose = today.get("by_purpose", {})
    purpose_lines = []
    for tag, label in (("ingest", "📥 ingest"), ("query", "💬 query"),
                       ("unknown", "❓ unknown")):
        if tag in by_purpose:
            d = by_purpose[tag]
            purpose_lines.append(
                f"    {label}  ₩{d['cost']:,.1f}  ({d['calls']}콜)"
            )
    purpose_breakdown = ("\n" + "\n".join(purpose_lines)) if purpose_lines else ""

    # Tiny inline bar chart for the last 7 days so trends are visible
    # without leaving the /usage screen.
    daily = cost.daily_breakdown(7)
    max_cost = max((d["cost"] for d in daily), default=0.0)
    daily_lines = []
    for d in daily:
        bar_len = int(round((d["cost"] / max_cost) * 20)) if max_cost else 0
        bar = "█" * bar_len if bar_len else "·"
        daily_lines.append(
            f"  {d['date'][5:]}  {bar:<20}  ₩{d['cost']:,.0f}"
            + (f"  ({d['calls']}콜)" if d["calls"] else "")
        )
    daily_block = "\n".join(daily_lines)

    out = (
        "📊 봇 사용 현황\n"
        f"\n총 문서: {s['total']}개  /  청크: {chunks}개"
        f"\n\n📥 ingest 속도"
        f"\n  • 24h: {s['last_24h']}건"
        f"\n  • 7d:  {s['last_7d']}건"
        f"\n  • 30d: {s['last_30d']}건"
        f"\n\n📂 type별 분포"
        f"\n  {types_line}"
        f"\n\n💰 추정 비용 (Gemini API · KST)"
        f"\n  • 오늘: ₩{today['total_krw']:,.0f}  ({today['calls']}콜)"
        f"\n  • 7일:  ₩{week['total_krw']:,.0f}"
        f"\n  • 이번 달 ({mtd['year']}년 {mtd['month']}월): "
        f"₩{mtd['total_krw']:,.0f}  ({mtd['day']}일차)"
        f"{cost_breakdown}"
        f"{purpose_breakdown}"
        f"\n\n📅 최근 7일 (KST)\n{daily_block}"
        f"\n\n📖 모델 용도"
        f"\n  embedding   인입 chunk+summary / 질문 쿼리 임베딩"
        f"\n  flash-lite  인입 요약·메타·OCR·STT / 질문 확장·rerank·verify"
        f"\n  flash       질문 agent 추론·web_search"
        f"\n  pro         /deep 질문 전용"
        f"\n\n📚 가장 최근 학습"
        f"\n  {s['latest_title'][:80]}"
        f"\n  {s['latest_at'][:16].replace('T', ' ')}"
        f"\n\n🔁 retry 큐: {queue_len}건"
        f"\n❌ failed 누적: {failed_len}건"
    )
    await update.message.reply_text(out)


async def cmd_cost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cost-only view: today, week/month averages, projected monthly
    spend at the current pace, and the AS IS vs TO BE bands for sanity
    checking. /usage covers ingest + cost; this one strips the noise so
    you can answer "are we actually on the cheap path?" in two lines.

    The projection multiplies the trailing-7-day daily average by 30 — a
    rolling estimate that captures any expander activation or load
    spike better than the calendar month-to-date number does."""
    if not _is_owner(update):
        return
    await _typing(update, ctx)

    today = cost.today_krw()
    week = cost.period_krw(7)
    mtd = cost.month_to_date_krw()
    daily = cost.daily_breakdown(14)

    # Daily average over the last 7 days; project monthly at that rate.
    # Also derive a "remaining days × today's rate" forecast so a spiky
    # day surfaces immediately instead of after a week of averaging.
    avg_7d = week["total_krw"] / 7 if week["total_krw"] else 0.0
    projected_monthly = avg_7d * 30
    today_pace_monthly = today["total_krw"] * 30  # if today's pace held all month

    # Per-purpose split for the 7-day window so the user can see which
    # bucket (ingest / query / unknown) is driving the bill.
    by_purpose = (week.get("by_purpose") or {})
    purpose_lines = []
    for label, key in (("ingest 학습", "ingest"),
                       ("query 답변", "query"),
                       ("기타", "unknown")):
        info = by_purpose.get(key) or {}
        krw = info.get("cost") or 0.0
        calls = info.get("calls") or 0
        if krw or calls:
            purpose_lines.append(f"  {label}: ₩{krw:,.0f}  ({calls}콜)")
    purpose_block = "\n".join(purpose_lines) or "  (데이터 없음)"

    max_cost = max((d["cost"] for d in daily), default=0.0)
    daily_lines = []
    for d in daily:
        bar_len = int(round((d["cost"] / max_cost) * 24)) if max_cost else 0
        bar = "█" * bar_len if bar_len else "·"
        daily_lines.append(
            f"  {d['date'][5:]}  {bar:<24}  ₩{d['cost']:,.0f}"
            + (f"  ({d['calls']}콜)" if d["calls"] else "")
        )
    daily_block = "\n".join(daily_lines)

    out = (
        "💰 비용 현황 (KST · Gemini API)\n"
        f"\n• 오늘:    ₩{today['total_krw']:,.0f}  ({today['calls']}콜)"
        f"\n• 7일 합계: ₩{week['total_krw']:,.0f}"
        f"\n• 이번 달:  ₩{mtd['total_krw']:,.0f}  ({mtd['day']}일차)"
        f"\n\n📈 월말 예상치"
        f"\n  ₩{projected_monthly:,.0f}/월  (최근 7일 평균 × 30)"
        f"\n  ₩{today_pace_monthly:,.0f}/월  (오늘 페이스 × 30)"
        f"\n\n🧩 7일 용도별"
        f"\n{purpose_block}"
        f"\n\n🎯 가이드 (Gemini 임베딩 + Vision-Lite 캡 적용)"
        f"\n  일상 트래픽:  ~₩60,000 ~ 100,000/월"
        f"\n  heavy 업로드:  ~₩120,000 ~ 180,000/월"
        f"\n\n📅 최근 14일 (KST)\n{daily_block}"
        f"\n\n💡 세부 분석: /usage"
    )
    await update.message.reply_text(out)


async def cmd_recent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    await _typing(update, ctx)
    n = 10
    if ctx.args and ctx.args[0].isdigit():
        n = max(1, min(int(ctx.args[0]), 50))
    items = meta.recent(n)
    if not items:
        await update.message.reply_text("아직 비어있어요.")
        return
    lines = [f"📚 최근 {len(items)}개 학습"]
    for r in items:
        title = _clean_text(r.get("title") or "(제목 없음)")[:90]
        ingested = (r.get("ingested_at") or "")[:10]
        lines.append(f"\n[{r['type']}]  {title}\n  {ingested}  ·  id {r['id']}")
    await update.message.reply_text("\n".join(lines))


async def cmd_forget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    if not ctx.args:
        await update.message.reply_text("사용법: /forget <doc_id>")
        return
    doc_id = ctx.args[0]
    # Look up source BEFORE deletion so we can record the filename
    # to stop orphan-scan resurrection.
    src = ""
    try:
        existing = await asyncio.to_thread(meta.get_doc, doc_id)
        if existing:
            src = existing.get("source") or ""
    except Exception:
        log.exception("get_doc pre-forget failed")
    n = vector.delete_doc(doc_id)
    ok = meta.delete(doc_id)
    if ok:
        fname = _filename_from_source(src)
        if fname:
            _record_dedup_confirmed(fname)
    await update.message.reply_text(
        f"{'삭제됨' if ok else '메타 없음'} · 청크 {n}개 제거"
    )


async def cmd_cleanup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    noisy = meta.find_noise()
    if not noisy:
        await update.message.reply_text("정리할 노이즈 없음 ✨")
        return
    args = [a.lower() for a in (ctx.args or [])]
    if "confirm" in args:
        n_chunks = 0
        for r in noisy:
            n_chunks += vector.delete_doc(r["id"])
            meta.delete(r["id"])
            # Record filename so orphan scan doesn't re-queue this
            # file from disk (cleanup is normally text-only docs, but
            # belt-and-suspenders since find_noise() could expand).
            fname = _filename_from_source(r.get("source") or "")
            if fname:
                _record_dedup_confirmed(fname)
        await update.message.reply_text(
            f"✅ 노이즈 {len(noisy)}건 / 청크 {n_chunks}개 제거 완료"
        )
        return
    preview = "\n".join(
        f"  • {r['id']}  {_clean_text(r.get('title') or r.get('source') or '')[:55]}"
        for r in noisy[:15]
    )
    more = f"\n... 외 {len(noisy)-15}건" if len(noisy) > 15 else ""
    await update.message.reply_text(
        f"노이즈 후보 {len(noisy)}건 (text 타입, 본문 짧음):\n{preview}{more}\n\n"
        f"전부 삭제하려면: /cleanup_confirm"
    )


async def cmd_forget_forwards(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Wipe auto-forwarded channel digests (only docs whose title
    matches the digest emoji/header pattern AND whose source is
    tg-msg:). User-written long pastes share the same source prefix
    but don't match the title shape, so they survive. Two-step:
    /forget_forwards previews, /forget_forwards confirm executes."""
    if not _is_owner(update):
        return
    candidates = meta.find_forwarded_digests()
    if not candidates:
        await update.message.reply_text("📭 자동 포워딩 디지스트 없음 ✨")
        return
    args = [a.lower() for a in (ctx.args or [])]
    if "confirm" in args:
        n_chunks = 0
        for d in candidates:
            n_chunks += vector.delete_doc(d["id"])
            meta.delete(d["id"])
        await update.message.reply_text(
            f"✅ 자동 포워딩 자료 {len(candidates)}건 제거 "
            f"(청크 {n_chunks}개)"
        )
        return
    preview = "\n".join(
        f"  • {_clean_text(d.get('title') or '')[:70]}"
        for d in candidates[:15]
    )
    more = f"\n... 외 {len(candidates) - 15}건" if len(candidates) > 15 else ""
    await update.message.reply_text(
        f"📋 자동 포워딩 디지스트 후보 {len(candidates)}건:\n{preview}{more}\n\n"
        f"전부 삭제하려면: /forget_forwards_confirm"
    )


async def cmd_dedupe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    groups = meta.find_duplicates()
    if not groups:
        await update.message.reply_text("중복 없음 ✨")
        return
    args = [a.lower() for a in (ctx.args or [])]
    if "confirm" in args:
        deleted = 0
        chunks_removed = 0
        for g in groups:
            keeper = max(g, key=lambda d: len(d.get("summary") or ""))
            for d in g:
                if d["id"] == keeper["id"]:
                    continue
                chunks_removed += vector.delete_doc(d["id"])
                meta.delete(d["id"])
                deleted += 1
                # Stop the orphan-scan loop: file is still on disk
                # after meta delete; without this, _scan_orphan_files
                # finds the file missing from documents and re-queues
                # it on every restart → infinite dedup/retry cycle.
                fname = _filename_from_source(d.get("source") or "")
                if fname:
                    _record_dedup_confirmed(fname)
        await update.message.reply_text(
            f"✅ 중복 {deleted}건 / 청크 {chunks_removed}개 제거 완료\n"
            f"(각 그룹에서 본문이 가장 긴 것 1개만 유지)"
        )
        return
    lines: list[str] = []
    total_dups = 0
    for g in groups[:8]:
        total_dups += len(g) - 1
        title = _clean_text(g[0].get("title") or g[0].get("source") or "")[:55]
        lines.append(f"\n중복 {len(g)}건 — {title}")
        for d in g:
            chars = len(d.get("summary") or "")
            lines.append(f"  • {d['id']}  [{d['type']}]  {chars}자")
    more = f"\n\n... 외 {len(groups)-8}그룹" if len(groups) > 8 else ""
    total = sum(len(g) - 1 for g in groups)
    await update.message.reply_text(
        f"중복 {len(groups)}그룹 / 삭제 후보 {total}건\n"
        + "".join(lines) + more +
        f"\n\n각 그룹에서 본문 가장 긴 것 1개만 남기고 삭제: /dedupe_confirm"
    )


async def cmd_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Locate saved docs by title/source fragment. Compact per-item
    format + auto-split across multiple Telegram messages so every
    match is visible (no '나머지 N개 생략'). Snippet shortened to
    120 chars — enough to recognise the doc, not so much it buries
    later results."""
    if not _is_owner(update):
        return
    await _typing(update, ctx)
    # Trailing numeric arg overrides the default 50-cap. Lets the user
    # widen common-keyword searches (/find 배터리 200) without burying
    # narrow queries under 200 default results.
    args = list(ctx.args or [])
    limit = 50
    # Trailing numeric arg as limit only when there are other tokens
    # remaining — protects "/find 100" from being parsed as
    # "search for nothing with limit 100".
    if len(args) >= 2 and args[-1].isdigit():
        n = int(args[-1])
        if 10 <= n <= 500:
            limit = n
            args = args[:-1]
    query = " ".join(args).strip()
    if not query:
        await update.message.reply_text(
            "사용법: /find <제목 일부> [개수]\n"
            "예: /find 배터리          (기본 50)\n"
            "     /find 배터리 200    (최대 500)"
        )
        return
    matches = meta.search_broad(query, limit=limit)
    if not matches:
        await update.message.reply_text(f"매칭 없음: '{query}'")
        return

    header = f"🔍 '{query}' — {len(matches)}개 매칭"

    import json as _json
    import html as _html
    # Bulk chunk-count fetch so each match can show its size without
    # firing 50+ individual ChromaDB queries. Fails open (empty map →
    # size omitted) so /find never breaks just because vector store
    # blipped.
    try:
        doc_ids = [m.get("id") for m in matches if m.get("id")]
        chunk_counts = await asyncio.to_thread(vector.chunk_counts, doc_ids)
    except Exception:
        log.exception("find: chunk_counts bulk fetch failed")
        chunk_counts = {}
    blocks: list[str] = [header]
    for m in matches:
        title = _html.escape(_clean_text((m.get("title") or "(제목 없음)"))[:80])
        ingested = (m.get("ingested_at") or "")[:10]
        source = m.get("source") or ""
        # Full summary — line breaks preserved so bullet structure
        # stays readable. /find is free (SQLite only, ₩0) so we don't
        # truncate; _split_for_telegram fans the result across as many
        # messages as needed.
        summary_full = _html.escape(_clean_text(m.get("summary") or ""))

        loc = ""
        if source.startswith(("http://", "https://")):
            # Drop scheme + trailing query for readability.
            short_url = source.replace("https://", "").replace("http://", "")[:70]
            loc = f"📎 {short_url}"
        elif source.startswith("tg-"):
            # tg-doc:xxxx:filename / tg-photo:xx / tg-msg:N:hash —
            # show the prefix only, drops the hash noise.
            kind = source.split(":", 1)[0]
            loc = f"💬 {kind}"

        # report_date (본문 발행일 YYYY.MM) lifted into the header line
        # next to ingested date — user couldn't tell publish vs learn
        # date when both were buried in the same 🏷 tag line.
        meta_bits: list[str] = []
        published = ""
        meta_raw = m.get("metadata")
        if meta_raw:
            try:
                md = _json.loads(meta_raw)
            except Exception:
                md = {}
            if md.get("company"):
                meta_bits.append(_html.escape(md["company"]))
            if md.get("report_date"):
                published = _html.escape(md["report_date"])
            if md.get("tags"):
                meta_bits.append(_html.escape("·".join(md["tags"][:3])))
        meta_line = " · ".join(meta_bits)

        # One block per doc: title · dates · size · location · meta ·
        # full summary. doc_id rendered as `/show_<id>` — Telegram
        # auto-detects the /word pattern and makes it tap-to-send, so
        # one tap routes the user straight into cmd_show for that doc.
        # Pairs with a MessageHandler that catches `^/show_<hex>$` and
        # unwraps it back into `/show <hex>`.
        doc_id = m.get("id") or ""
        # Header second line: 학습 YYYY-MM-DD · 발행 YYYY.MM · N청크.
        # Each bit is optional — omitted when the data is missing so
        # docs without a Gemini-inferred publish date or with zero
        # chunks (shouldn't happen, defensive) don't show empty fields.
        info_bits: list[str] = []
        if ingested:
            info_bits.append(f"학습 {ingested}")
        if published:
            info_bits.append(f"발행 {published}")
        n_chunks = int(chunk_counts.get(doc_id, 0) or 0)
        if n_chunks:
            info_bits.append(f"{n_chunks}청크")
        item = f"\n\n📄 <b>{title}</b>"
        if info_bits:
            item += f"\n  <i>{' · '.join(info_bits)}</i>"
        if doc_id:
            item += f"\n  🆔 /show_{_html.escape(doc_id)}"
        if loc:
            item += f"\n  {loc}"
        if meta_line:
            item += f"\n  🏷 {meta_line}"
        if summary_full:
            # Blank line before the summary so the bullet structure
            # (• ...) reads as its own block rather than a runaway
            # continuation of the meta line.
            item += f"\n\n{summary_full}"
        blocks.append(item)

    out = "".join(blocks)
    # Reuse the help-splitter so any number of matches fits across
    # however many Telegram messages it takes. Paragraph (blank-line)
    # boundaries between items keep each chunk readable.
    pieces = _split_for_telegram(out)
    first_message_id = await _send_pieces_with_throttle(
        update.message.reply_text, pieces,
        parse_mode="HTML", disable_web_page_preview=True,
    )
    # "처음으로" footer — same pattern as /show. A common keyword like
    # HBM / CoWoS fans out across 30+ messages and the user has no
    # way back to the header without manually scrolling.
    if first_message_id is not None and len(pieces) > 1:
        try:
            await update.message.reply_text(
                "⬆️ 처음으로",
                reply_to_message_id=first_message_id,
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("find: top-link footer send failed")


_SHOW_ID_RE = re.compile(r"^/show_([a-f0-9]{6,32})\b")


async def cmd_show_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Tap-to-send handler for `/show_<id>` — the form /find renders
    under each match. Telegram auto-detects /word patterns and makes
    them tappable, so the user clicks the rendered ID and lands here
    instead of typing `/show <id>` by hand. We unwrap the suffix
    and delegate to cmd_show with the id pre-supplied."""
    if not _is_owner(update):
        return
    msg = (update.message and update.message.text) or ""
    m = _SHOW_ID_RE.match(msg.strip())
    if not m:
        return
    ctx.args = [m.group(1)]
    await cmd_show(update, ctx)


async def cmd_show(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Dump every chunk of one doc across multiple Telegram messages
    so the user can read the FULL original body, not just the
    LLM-compressed summary that /find shows.

    Usage: /show <doc_id 또는 제목 키워드>
    - doc_id: 16-char hex from /find's source/id area
      (or tap the `/show_<id>` link /find renders under each match)
    - 키워드: title substring match (first hit wins)

    Cost: ₩0 (SQLite + Chroma local lookups only). Output can be
    long for big PDFs — multi-message split handles up to ~50,000
    chars across ~15 Telegram messages."""
    if not _is_owner(update):
        return
    await _typing(update, ctx)
    if not ctx.args:
        await update.message.reply_text(
            "사용법: /show <doc_id 또는 제목 키워드>\n"
            "/find 로 찾은 doc의 본문 전체 청크를 차례대로 보여줍니다."
        )
        return
    query = " ".join(ctx.args).strip()

    # Lookup priority: exact id → keyword search. Picker now sorts by
    # recency primary (matches /find's order, so /show <kw> picks the
    # same doc /find lists first), with type rank and summary length
    # as tiebreakers when two docs share the same ingest time. Earlier
    # the picker preferred url/pdf over text regardless of date, which
    # surfaced week-old PDFs when the user wanted today's DART msg.
    doc = await asyncio.to_thread(meta.get_doc, query)
    multi_match_count = 0
    if not doc:
        matches = await asyncio.to_thread(meta.search_broad, query, 20)
        if not matches:
            await update.message.reply_text(f"매칭 없음: '{query[:60]}'")
            return
        _TYPE_RANK = {
            "url": 0, "pdf": 0, "pptx": 0, "docx": 0, "xlsx": 0,
            "audio": 1, "youtube": 0,
            "image": 2,
            "text": 3,
        }
        # Multi-pass stable sort: tertiary (summary length DESC) →
        # secondary (type rank ASC) → primary (ingested_at DESC).
        matches.sort(key=lambda d: -(len(d.get("summary") or "")))
        matches.sort(key=lambda d: _TYPE_RANK.get(
            (d.get("type") or "").lower(), 4))
        matches.sort(key=lambda d: d.get("ingested_at") or "", reverse=True)
        doc = matches[0]
        multi_match_count = len(matches)
        if len(matches) > 1:
            log.info(
                "show: %d matches for %r, picked id=%s type=%s "
                "ts=%s summary_len=%d (others: %s)",
                len(matches), query[:50], doc["id"],
                doc.get("type"), doc.get("ingested_at"),
                len(doc.get("summary") or ""),
                [(m["id"], m.get("type"), m.get("ingested_at"))
                 for m in matches[1:5]],
            )

    doc_id = doc["id"]
    title = doc.get("title") or "(제목 없음)"
    chunks = await asyncio.to_thread(vector.get_doc_chunks, doc_id)
    if not chunks:
        await update.message.reply_text(
            f"⚠️ '{title[:60]}' — Chroma 청크 없음. "
            f"(meta 만 있고 본문 청크가 삭제됐을 가능성)"
        )
        return

    import html as _html
    chunk_chunks = [c for c in chunks if c.get("kind") == "chunk"]
    summary_chunks = [c for c in chunks if c.get("kind") == "summary"]

    header_lines = [
        f"📄 <b>{_html.escape(title[:120])}</b>",
        f"🆔 <code>{_html.escape(doc_id)}</code>"
        f" · 청크 {len(chunk_chunks)}개"
        + (f" + 요약 1" if summary_chunks else ""),
    ]
    if multi_match_count > 1:
        header_lines.append(
            f"ℹ️ {multi_match_count}개 매칭 중 가장 최근 1개 표시. "
            f"다른 doc 보려면 /show &lt;id&gt;"
        )
    src = doc.get("source") or ""
    if src.startswith(("http://", "https://")):
        header_lines.append(f"📎 {_html.escape(src[:120])}")
    elif src.startswith("tg-"):
        header_lines.append(f"💬 {_html.escape(src.split(':', 1)[0])}")
    header = "\n".join(header_lines)

    # Compute body upfront so we can decide whether to attach the
    # translate button. Hangul ratio < 30% across the joined body
    # implies the doc is primarily foreign (English / CJK other) and
    # the user might want a Korean translation.
    raw_body = "\n".join(c.get("text") or "" for c in chunk_chunks)
    show_xlate_btn = _is_mostly_foreign(raw_body)
    header_kb = None
    if show_xlate_btn:
        header_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🌐 한국어 번역",
                callback_data=f"xlate:{doc_id}",
            ),
        ]])

    body_parts: list[str] = []
    for c in chunk_chunks:
        idx = c.get("idx", 0)
        text = _html.escape(c.get("text") or "")
        body_parts.append(f"\n\n<b>━━ chunk #{idx} ━━</b>\n{text}")
    body = "".join(body_parts)

    # Header is its own message so the translate button is attached
    # cleanly (one inline keyboard per message). Body chunks follow.
    first_sent = await update.message.reply_text(
        header, parse_mode="HTML", disable_web_page_preview=True,
        reply_markup=header_kb,
    )
    first_message_id = first_sent.message_id
    body_pieces = _split_for_telegram(body) if body.strip() else []
    await _send_pieces_with_throttle(
        update.message.reply_text, body_pieces,
        parse_mode="HTML", disable_web_page_preview=True,
    )
    # "처음으로" footer that replies-to the header message —
    # Telegram renders the reply quote as tappable, scrolling the
    # chat back to that message. /show output can be 50+ messages
    # long; without this the user has no way back to the top.
    if body_pieces:
        try:
            await update.message.reply_text(
                "⬆️ 처음으로",
                reply_to_message_id=first_message_id,
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("show: top-link footer send failed")


async def _handle_translate(ctx, chat_id: int, doc_id: str, q) -> None:
    """Translate a doc's full body to Korean. Hybrid strategy:
      • ≤ SINGLE_CHAR_CAP (30k): one Flash-Lite call, max_tokens 32k —
        fits a typical article + headroom, ~₩5.
      • >  SINGLE_CHAR_CAP: pack chunks into ~BATCH_CHAR_TARGET (10k)
        buckets along existing chunk boundaries, translate each in
        parallel (bounded), concat. Guarantees full-body translation
        for arbitrarily long docs without losing paragraph structure
        (we never split mid-chunk).
    Cost ≈ ₩5/short doc, ₩20-40/100k-char doc."""
    log.info("translate START doc=%s chat=%s", doc_id, chat_id)
    try:
        await q.answer("번역 중... (10~30초)")
    except Exception:
        pass
    # Immediate "working" message so the user has visual feedback
    # even if the LLM call takes 30+ seconds. Edited at the end into
    # either ✅ or ⚠️ to keep one row instead of two.
    try:
        status_msg = await ctx.bot.send_message(
            chat_id, "🌐 번역 중... (10~30초)"
        )
        status_id = status_msg.message_id
    except Exception:
        log.exception("translate status_msg send failed")
        status_id = None
    try:
        chunks = await asyncio.to_thread(vector.get_doc_chunks, doc_id)
    except Exception:
        log.exception("translate get_doc_chunks failed doc=%s", doc_id)
        await ctx.bot.send_message(
            chat_id, "⚠️ 번역 실패: 청크 로드 에러"
        )
        return
    chunk_chunks = [c for c in chunks if c.get("kind") == "chunk"]
    log.info("translate chunks doc=%s n=%d", doc_id, len(chunk_chunks))
    if not chunk_chunks:
        await ctx.bot.send_message(
            chat_id, "⚠️ 번역할 본문이 없음 (청크 0개)"
        )
        return

    SINGLE_CHAR_CAP = 30000
    BATCH_CHAR_TARGET = 10000
    PARALLEL_CAP = 5
    body_total = sum(len(c.get("text") or "") for c in chunk_chunks)
    log.info("translate body doc=%s total_chars=%d", doc_id, body_total)

    from .llm.gemini import complete
    _TRANSLATE_SYSTEM = (
        "You are a precise translator. Translate the user's "
        "text into natural Korean. Preserve structure: "
        "headers, bullet points, table layouts, line breaks. "
        "Keep technical terms (model names, company names, "
        "acronyms like LAB / TCNCP / HBM) in their original "
        "form. Output only the translation, no preamble."
    )

    batches_n = 0
    try:
        if body_total <= SINGLE_CHAR_CAP:
            # Single-call path. Output cap 32k tokens — generous so the
            # Korean translation (≈1.5× input token count) never gets
            # mid-sentence truncated.
            body = "\n\n".join(c.get("text") or "" for c in chunk_chunks)
            resp = await complete(
                model=config.SUMMARY_MODEL,
                system=_TRANSLATE_SYSTEM,
                user=body,
                max_tokens=32768,
                temperature=0.2,
                purpose="translate",
            )
            translated = (resp or "").strip()
        else:
            # Batch path. Pack chunks into ~BATCH_CHAR_TARGET buckets
            # without splitting individual chunks — paragraph structure
            # only stays intact when each batch sees whole chunks.
            batches: list[list[dict]] = []
            cur: list[dict] = []
            cur_chars = 0
            for c in chunk_chunks:
                t = c.get("text") or ""
                if cur_chars + len(t) > BATCH_CHAR_TARGET and cur:
                    batches.append(cur)
                    cur = []
                    cur_chars = 0
                cur.append(c)
                cur_chars += len(t)
            if cur:
                batches.append(cur)
            batches_n = len(batches)
            log.info("translate batched doc=%s batches=%d", doc_id, batches_n)

            sem = asyncio.Semaphore(PARALLEL_CAP)

            async def _translate_batch(batch: list[dict]) -> str:
                text = "\n\n".join(c.get("text") or "" for c in batch)
                async with sem:
                    r = await complete(
                        model=config.SUMMARY_MODEL,
                        system=_TRANSLATE_SYSTEM,
                        user=text,
                        max_tokens=32768,
                        temperature=0.2,
                        purpose="translate",
                    )
                return (r or "").strip()

            parts = await asyncio.gather(
                *(_translate_batch(b) for b in batches)
            )
            translated = "\n\n".join(p for p in parts if p).strip()
    except Exception as e:
        log.exception("translate gemini call failed doc=%s", doc_id)
        await ctx.bot.send_message(
            chat_id, f"⚠️ 번역 실패: {_explain_error(e)}"
        )
        return
    log.info("translate resp doc=%s resp_len=%d",
             doc_id, len(translated))
    if not translated:
        await ctx.bot.send_message(chat_id, "⚠️ 번역 결과 비어있음")
        return
    header = "🌐 <b>한국어 번역</b>"
    if batches_n > 1:
        header += f" <i>(전체 {body_total:,}자 · {batches_n}배치)</i>"
    import html as _html
    out = f"{header}\n\n{_html.escape(translated)}"
    pieces = _split_for_telegram(out)
    log.info("translate sending doc=%s pieces=%d", doc_id, len(pieces))

    async def _send(piece, **kw):
        return await ctx.bot.send_message(chat_id, piece, **kw)

    first_id = await _send_pieces_with_throttle(
        _send, pieces,
        parse_mode="HTML", disable_web_page_preview=True,
    )
    sent_n = len(pieces) if first_id is not None else 0
    log.info("translate DONE doc=%s sent_first=%s pieces=%d",
             doc_id, first_id, len(pieces))
    # Tidy the in-progress status row.
    if status_id:
        try:
            await ctx.bot.edit_message_text(
                f"✅ 번역 완료 ({sent_n}개 메시지)",
                chat_id=chat_id, message_id=status_id,
            )
        except Exception:
            pass
    # "처음으로" footer — same pattern as /show. A translated doc can
    # span 10+ messages; tap the reply-quote to jump back to the
    # translation's first message.
    if first_id is not None:
        try:
            await ctx.bot.send_message(
                chat_id, "⬆️ 처음으로",
                reply_to_message_id=first_id,
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("translate: top-link footer send failed")


def _is_mostly_foreign(text: str, threshold: float = 0.30) -> bool:
    """True when Hangul characters make up less than `threshold`
    of the (alphabetic + Hangul + CJK) character count. Used by
    /show to decide whether to surface the [🌐 한국어 번역] button —
    pure Korean docs don't get the offer, English / Chinese /
    Japanese do."""
    if not text:
        return False
    hangul = 0
    foreign = 0
    for ch in text:
        if "가" <= ch <= "힯":  # Hangul syllables
            hangul += 1
        elif ch.isalpha() or (
            "一" <= ch <= "鿿"      # CJK unified
            or "぀" <= ch <= "ヿ"   # Hiragana/Katakana
        ):
            foreign += 1
    total = hangul + foreign
    if total < 200:
        return False  # Too short to be confident — skip the button.
    return (hangul / total) < threshold


def _failed_recent_snapshot() -> list[dict]:
    """Same view that cmd_failed renders — last 50 entries sorted by
    file size ascending (smallest first) so the quick-to-triage items
    surface at the top instead of being buried under multi-MB PDFs and
    MP4 leftovers. Tiebreaker is recency (newest first). Centralised
    so /failed buttons and helper functions agree on the index space."""
    # Two-pass stable sort: secondary (ts DESC) → primary (size ASC).
    # Python's sort is stable so items keep their ts ordering within
    # the same size bucket.
    entries = list(_INGEST_FAILED[-50:])
    entries.sort(key=lambda r: r.get("ts") or "", reverse=True)
    entries.sort(key=lambda r: int(r.get("file_size") or 0))
    return entries


def _failed_pop_by_display_idx(idx: int) -> dict | None:
    """Remove and return the entry corresponding to the user-visible
    #N tag (0-based). Returns None when the index is out of range —
    can happen if the failure log changed between display and click."""
    if idx < 0 or idx >= len(_INGEST_FAILED):
        return None
    # recent[i] = _INGEST_FAILED[-(i+1)]; pop by the inverted index.
    real_idx = len(_INGEST_FAILED) - 1 - idx
    if real_idx < 0 or real_idx >= len(_INGEST_FAILED):
        return None
    return _INGEST_FAILED.pop(real_idx)


def _failed_retry_one(chat_id: int, idx: int) -> str:
    """Retry just one /failed entry by its #N tag (0-based). Pops the
    entry, requeues if it has a retry payload, and persists both
    queues to disk."""
    if idx < 0 or idx >= len(_INGEST_FAILED):
        return f"⚠️ #{idx + 1} 범위 초과 (현재 {len(_INGEST_FAILED)}건)"
    snapshot = _failed_recent_snapshot()
    if idx >= len(snapshot):
        return f"⚠️ #{idx + 1} 매칭 없음"
    target = snapshot[idx]
    payload = target.get("retry")
    if not payload:
        return (
            f"⚠️ #{idx + 1} retry 정보 없음 — 채널/원본에서 직접 다시 "
            "보내주세요"
        )
    entry = _failed_pop_by_display_idx(idx)
    if entry is None:
        return f"⚠️ #{idx + 1} 매칭 없음 (목록이 변경됐을 수 있음)"
    payload = dict(payload)
    payload["attempts"] = 0
    payload["chat_id"] = chat_id
    _INGEST_RETRY_QUEUE.append(payload)
    _persist_retry_queue()
    _persist_failed_log()
    title = (entry.get("title") or "(unknown)")[:60]
    return f"🔁 #{idx + 1} retry queue로 재등록: {title}"


def _ignore_from_entry(entry: dict) -> tuple[int, int]:
    """Add an entry's filename/URL to the permanent ignore sets.
    Returns (files_added, urls_added) — both zero when the entry
    has no recognizable identifier (rare; usually means it was a
    raw text paste with no source label)."""
    payload = entry.get("retry") or {}
    title = entry.get("title") or ""
    added_files = added_urls = 0
    url = payload.get("url") or (
        title if title.startswith(("http://", "https://")) else None
    )
    if url and url not in _IGNORED_URLS:
        _IGNORED_URLS.add(url)
        added_urls = 1
    fname = (
        payload.get("file_name")
        or (Path(payload["path"]).name if payload.get("path") else None)
    )
    # Source-label fallback: a /failed entry's `detail` (and sometimes
    # `title`) carries the raw source string when payload is missing.
    # `tg-doc:<msg_id>:<filename>` and `local:<filename>` both end in
    # the actual filename — extract it so the user's /failed_clear or
    # [🗑] reliably suppresses the file, even on rows that pre-dated
    # the retry_payload schema.
    if not fname:
        detail = entry.get("detail") or ""
        for src_str in (detail, title):
            if not src_str:
                continue
            if src_str.startswith("tg-doc:"):
                parts = src_str.split(":", 2)
                if len(parts) == 3 and parts[2]:
                    fname = parts[2]
                    break
            elif src_str.startswith("local:"):
                rest = src_str.split(":", 1)[1].strip()
                if rest:
                    fname = rest
                    break
    if not fname and title and not title.startswith(("http://", "https://")):
        if any(title.lower().endswith(ext) for ext in
               (".pdf", ".pptx", ".docx", ".xlsx", ".mp3", ".m4a",
                ".png", ".jpg", ".jpeg", ".mp4", ".gif")):
            fname = title
    if fname and fname not in _IGNORED_FILENAMES:
        _IGNORED_FILENAMES.add(fname)
        added_files = 1
    return added_files, added_urls


def _failed_drop_one(idx: int) -> str:
    """Delete a single /failed entry by #N tag (0-based) AND mark
    its filename/URL as permanently ignored — same semantics as the
    bulk /failed_clear. Without the ignore step the next orphan scan
    re-enqueues the file and the user gets to play whack-a-mole with
    the same row over and over."""
    entry = _failed_pop_by_display_idx(idx)
    if entry is None:
        return f"⚠️ #{idx + 1} 범위 초과 (현재 {len(_INGEST_FAILED)}건)"
    added_files, added_urls = _ignore_from_entry(entry)
    if added_files or added_urls:
        _persist_permanently_ignored()
    _persist_failed_log()
    title = (entry.get("title") or "(unknown)")[:60]
    ign_tag = ""
    if added_files:
        ign_tag = " · 🚫 영구 무시"
    elif added_urls:
        ign_tag = " · 🚫 URL 영구 무시"
    return f"🗑 #{idx + 1} 삭제{ign_tag}: {title}"


def _failed_retry_all(chat_id: int) -> str:
    """Move every failed entry that has a saved retry_payload back into
    the auto-retry queue. Returns the user-facing summary message."""
    retried = 0
    kept: list[dict] = []
    for entry in _INGEST_FAILED:
        payload = entry.get("retry")
        if payload:
            payload = dict(payload)
            payload["attempts"] = 0
            payload["chat_id"] = chat_id
            _INGEST_RETRY_QUEUE.append(payload)
            retried += 1
        else:
            kept.append(entry)
    _INGEST_FAILED.clear()
    _INGEST_FAILED.extend(kept)
    _persist_retry_queue()
    _persist_failed_log()
    msg = (
        f"🔁 retry queue로 {retried}건 재등록\n"
        f"{_RETRY_INGEST_INTERVAL_SEC}초 간격, 최대 "
        f"{_RETRY_INGEST_BATCH}건/회 자동 처리."
    )
    if kept:
        msg += f"\n\n♻️ retry 정보 없는 {len(kept)}건은 그대로 — 채널 스크롤로 직접 다시 보내주세요."
    return msg


def _failed_clear_all() -> str:
    """Clear the failure log AND mark every cleared item as
    permanently ignored. After this, orphan recovery / URL ingest /
    forward-listener all skip these items silently."""
    n = len(_INGEST_FAILED)
    added_files = 0
    added_urls = 0
    for entry in _INGEST_FAILED:
        f, u = _ignore_from_entry(entry)
        added_files += f
        added_urls += u
    if added_files or added_urls:
        _persist_permanently_ignored()
    _INGEST_FAILED.clear()
    _persist_failed_log()
    return (
        f"실패 목록 비웠음 ({n}건)\n"
        f"🚫 영구 무시 추가: 파일 {added_files}건, URL {added_urls}건\n"
        f"  → orphan 재학습 / URL 재시도 / forward-listener 모두 차단"
    )


async def cmd_failed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show recent ingest failures (errors + empty bodies). Persisted to
    disk so /failed survives bot restart. Inline buttons let the user
    retry / clear with one tap. /failed retry and /failed clear still
    work as text commands too."""
    if not _is_owner(update):
        return
    await _typing(update, ctx)
    if ctx.args and ctx.args[0] == "clear":
        await update.message.reply_text(_failed_clear_all())
        return
    if ctx.args and ctx.args[0] == "retry":
        await update.message.reply_text(
            _failed_retry_all(update.effective_chat.id)
        )
        return
    if not _INGEST_FAILED:
        await update.message.reply_text("실패 / 빈본문 없음 ✨")
        return
    out = f"❌ 실패/빈본문 누적 {len(_INGEST_FAILED)}건 (최근순)"
    LIMIT = 3800
    truncated = 0
    recent = _failed_recent_snapshot()
    # How many entries get their own per-item retry/drop buttons.
    # Telegram allows ~100 buttons per message and each entry adds 2
    # — capping at 20 keeps the keyboard responsive while covering
    # the typical "what failed today" working set.
    PER_ITEM_BUTTON_CAP = 20
    button_rows: list[list[InlineKeyboardButton]] = []
    for i, r in enumerate(recent):
        ts = (r.get("ts", "")[:16]).replace("T", " ")
        title = _clean_text(r.get("title", "(unknown)"))[:90]
        status = r.get("status", "error")
        detail = _clean_text(r.get("detail", ""))[:120]
        icon = "❌" if status == "error" else "⚠️"
        cycles = int(r.get("failed_cycles") or 1)
        cycle_tag = f" [{cycles}/{_FAILED_MAX_CYCLES}]" if cycles > 1 else ""
        size = int(r.get("file_size") or 0)
        if size >= 1024 * 1024:
            size_tag = f" · {size / 1024 / 1024:.1f}MB"
        elif size >= 1024:
            size_tag = f" · {size // 1024}KB"
        else:
            size_tag = ""
        item = f"\n\n{icon} #{i + 1} {ts}{cycle_tag}{size_tag}\n   {title}"
        if detail and detail != title:
            item += f"\n   {detail}"
        if len(out) + len(item) > LIMIT:
            truncated = len(recent) - i
            break
        out += item
        if i < PER_ITEM_BUTTON_CAP and r.get("retry"):
            button_rows.append([
                InlineKeyboardButton(
                    f"🔁 #{i + 1}", callback_data=f"failed_retry_one:{i}"
                ),
                InlineKeyboardButton(
                    f"🗑 #{i + 1}", callback_data=f"failed_drop_one:{i}"
                ),
            ])
        elif i < PER_ITEM_BUTTON_CAP:
            # No retry payload — only the drop button is useful here.
            button_rows.append([
                InlineKeyboardButton(
                    f"🗑 #{i + 1}", callback_data=f"failed_drop_one:{i}"
                ),
            ])
    if truncated:
        out += f"\n\n…(나머지 {truncated}개 생략)"
    button_rows.append([
        InlineKeyboardButton("🔁 일괄 재시도", callback_data="failed_retry"),
        InlineKeyboardButton("🗑 비우기", callback_data="failed_clear"),
    ])
    await update.message.reply_text(
        out, disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(button_rows),
    )


async def cmd_failed_retry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Single-token alias so the usage guide can render as a one-tap
    command (Telegram only treats `/word` as tappable; `/failed retry`
    needs the user to type 'retry' manually)."""
    if not _is_owner(update):
        return
    await update.message.reply_text(
        _failed_retry_all(update.effective_chat.id)
    )


async def cmd_failed_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """One-tap alias for /failed clear (see cmd_failed_retry)."""
    if not _is_owner(update):
        return
    await update.message.reply_text(_failed_clear_all())


async def on_callback_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle the /failed inline-button taps."""
    q = update.callback_query
    if not q:
        return
    user = q.from_user
    if not user or user.id != config.TELEGRAM_OWNER_ID:
        await q.answer("권한 없음", show_alert=False)
        return
    await q.answer()  # dismiss the loading spinner
    chat_id = q.message.chat.id if q.message else config.TELEGRAM_OWNER_ID
    if q.data == "failed_retry":
        await q.edit_message_text(_failed_retry_all(chat_id))
    elif q.data == "failed_clear":
        await q.edit_message_text(_failed_clear_all())
    elif q.data.startswith("failed_retry_one:"):
        try:
            idx = int(q.data.split(":", 1)[1])
        except (ValueError, IndexError):
            return
        await ctx.bot.send_message(
            chat_id, _failed_retry_one(chat_id, idx),
            disable_web_page_preview=True,
        )
    elif q.data.startswith("failed_drop_one:"):
        try:
            idx = int(q.data.split(":", 1)[1])
        except (ValueError, IndexError):
            return
        await ctx.bot.send_message(
            chat_id, _failed_drop_one(idx),
            disable_web_page_preview=True,
        )
    elif q.data.startswith("orphan_learn:") or q.data.startswith("orphan_ignore:"):
        try:
            idx = int(q.data.split(":", 1)[1])
        except (ValueError, IndexError):
            return
        orphans = await asyncio.to_thread(_scan_orphan_files_sorted)
        if idx < 0 or idx >= len(orphans):
            # Edit the bubble itself rather than dropping a new message —
            # the user shouldn't have to scroll to find the result.
            try:
                await q.edit_message_text(
                    f"⚠️ #{idx + 1} 범위 초과 (현재 {len(orphans)}건)"
                )
            except Exception:
                pass
            return
        p = orphans[idx]
        if q.data.startswith("orphan_learn:"):
            ok = _orphan_enqueue_one(p, chat_id)
            if ok:
                msg = f"📥 #{idx + 1} 학습 큐 등록\n{p.name[:80]}"
            else:
                msg = f"ℹ️ #{idx + 1} 이미 큐에 있음\n{p.name[:80]}"
        else:
            # orphan_ignore: add to _IGNORED_FILENAMES so the scan
            # won't surface it again, and try to remove the file.
            _IGNORED_FILENAMES.add(p.name)
            _persist_permanently_ignored()
            try:
                p.unlink()
                msg = f"🗑 #{idx + 1} 영구 무시 + 파일 삭제됨\n{p.name[:80]}"
            except Exception:
                msg = f"🗑 #{idx + 1} 영구 무시 (파일 삭제 실패)\n{p.name[:80]}"
        # Replace the bubble's own text + drop the buttons so the
        # action's result is unmistakable. send_message would put
        # the receipt below the keyboard which the user can miss.
        try:
            await q.edit_message_text(msg)
        except Exception:
            await ctx.bot.send_message(
                chat_id, msg, disable_web_page_preview=True,
            )
    elif q.data == "orphan_learn_all":
        orphans = await asyncio.to_thread(_scan_orphan_files_sorted)
        n = _enqueue_orphan_recovery(orphans, chat_id)
        try:
            await q.edit_message_text(f"📥 {n}건 모두 학습 큐 등록")
        except Exception:
            await ctx.bot.send_message(
                chat_id, f"📥 {n}건 모두 학습 큐 등록",
                disable_web_page_preview=True,
            )
    elif q.data == "orphan_ignore_all":
        orphans = await asyncio.to_thread(_scan_orphan_files_sorted)
        ignored_count = 0
        deleted_count = 0
        for p in orphans:
            if p.name not in _IGNORED_FILENAMES:
                _IGNORED_FILENAMES.add(p.name)
                ignored_count += 1
            try:
                p.unlink()
                deleted_count += 1
            except Exception:
                pass
        if ignored_count or deleted_count:
            _persist_permanently_ignored()
        result = (
            f"🗑 {len(orphans)}건 영구 무시 (파일 {deleted_count}개 삭제됨)"
        )
        try:
            await q.edit_message_text(result)
            return
        except Exception:
            pass
        await ctx.bot.send_message(
            chat_id, result,
            disable_web_page_preview=True,
        )
    elif q.data.startswith("urldec_retry:"):
        key = q.data.split(":", 1)[1]
        entry = _urldec_find_by_key(key)
        if not entry:
            try:
                await q.edit_message_text("⚠️ 해당 URL을 찾을 수 없음 (이미 처리됨)")
            except Exception:
                pass
            return
        url = entry["url"]
        try:
            await q.edit_message_text(
                f"🔁 재시도 중...\n{url[:120]}"
            )
        except Exception:
            pass
        try:
            r = await pipeline.ingest_url(url)
        except Exception as e:
            r = {"status": "error", "error": _explain_error(e)}
        if r.get("status") == "ok":
            pending_url_decisions.remove(url)
            try:
                await q.edit_message_text(
                    f"✅ 학습됨: {r.get('title', '')[:80]}\n"
                    f"청크 {r.get('chunks', 0)}개"
                )
            except Exception:
                pass
        elif r.get("status") in ("duplicate", "blocked"):
            pending_url_decisions.remove(url)
            label = "♻️ 이미 있음" if r.get("status") == "duplicate" else "🚫 차단됨"
            try:
                await q.edit_message_text(
                    f"{label}: {r.get('title', url)[:80]}"
                )
            except Exception:
                pass
        else:
            # Still empty / error → auto-block. The user already
            # said "재시도해보고 안 되면 차단" in the bubble; a
            # second failure means the URL is genuinely uningestable
            # so we don't keep bothering them. Adds to _IGNORED_URLS
            # (re-forwards silent-skipped) and clears the pending
            # entry. Replaces the prior "surface in /pending again"
            # behaviour per user request.
            if url not in _IGNORED_URLS:
                _IGNORED_URLS.add(url)
                _persist_permanently_ignored()
            pending_url_decisions.remove(url)
            err = (r.get("error") or r.get("detail") or "본문 비어있음")[:100]
            try:
                await q.edit_message_text(
                    f"🚫 재시도 실패 → 자동 차단됨\n"
                    f"{url[:120]}\n오류: {err}\n"
                    f"  → 재포워드해도 silent skip"
                )
            except Exception:
                pass
    elif q.data.startswith("xlate:"):
        doc_id = q.data.split(":", 1)[1]
        await _handle_translate(ctx, chat_id, doc_id, q)
        return
    elif q.data.startswith("urldec_block:"):
        key = q.data.split(":", 1)[1]
        entry = _urldec_find_by_key(key)
        if not entry:
            try:
                await q.edit_message_text("⚠️ 해당 URL을 찾을 수 없음 (이미 처리됨)")
            except Exception:
                pass
            return
        url = entry["url"]
        if url not in _IGNORED_URLS:
            _IGNORED_URLS.add(url)
            _persist_permanently_ignored()
        pending_url_decisions.remove(url)
        try:
            await q.edit_message_text(
                f"🚫 영구 차단됨\n{url[:120]}\n"
                f"  → 재포워드해도 silent skip"
            )
        except Exception:
            pass


async def cmd_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Items currently waiting in the auto-retry queue (503/timeout
    failures). They re-attempt every 2 minutes."""
    if not _is_owner(update):
        return
    await _typing(update, ctx)
    if not _INGEST_RETRY_QUEUE:
        await update.message.reply_text("재시도 큐 비어있음 ✨")
        return
    out = (
        f"🔁 재시도 큐 {len(_INGEST_RETRY_QUEUE)}건 "
        f"({_RETRY_INGEST_INTERVAL_SEC}초 간격, 최대 "
        f"{_RETRY_INGEST_BATCH}건/회 자동)"
    )
    for item in _INGEST_RETRY_QUEUE[:25]:
        kind = item.get("kind", "?")
        title = item.get("file_name") or item.get("url") or "(unknown)"
        attempts = item.get("attempts", 0)
        out += f"\n• [{kind}] {title[:80]} (시도 {attempts}회)"
    if len(_INGEST_RETRY_QUEUE) > 25:
        out += f"\n... 외 {len(_INGEST_RETRY_QUEUE) - 25}건"
    await update.message.reply_text(out, disable_web_page_preview=True)


async def cmd_blocked_hosts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """List hosts the URL blocklist is currently tracking, including
    the ones that have crossed the auto-block threshold. Free
    (₩0) — just a JSON read."""
    if not _is_owner(update):
        return
    from .store import url_blocklist
    entries = await asyncio.to_thread(url_blocklist.list_all)
    if not entries:
        await update.message.reply_text(
            "🟢 자동 차단된 host 없음.\n"
            "(URL 본문 추출이 2회 연속 실패하면 그 host는 자동 차단되어 "
            "다음번부터 즉시 skip)"
        )
        return
    lines = ["🚫 자동 차단 host 추적 현황"]
    for e in entries:
        flag = "🔴 차단됨" if e["is_blocked"] else "🟡 경고"
        lines.append(
            f"{flag} {e['host']}  ({e['count']}회 실패, "
            f"마지막 {e['last_at'][:16]})"
        )
        if e["last_url"]:
            lines.append(f"   ↳ {e['last_url'][:90]}")
    lines.append("")
    lines.append("재시도 허용: /reset_blocked_hosts (전체 초기화)")
    await update.message.reply_text(
        "\n".join(lines), disable_web_page_preview=True,
    )


async def cmd_reset_blocked_hosts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Wipe every host counter. Use after a paywall changes policy
    or the user explicitly wants to retry sites that previously
    failed."""
    if not _is_owner(update):
        return
    from .store import url_blocklist
    n = await asyncio.to_thread(url_blocklist.reset_all)
    await update.message.reply_text(
        f"✅ 자동 차단 host 초기화 — {n}건 정리됨.\n"
        f"이제 모든 host가 다시 추출 시도 가능."
    )


async def cmd_audit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """One-shot pending-work audit. Aggregates every place a not-yet-
    learned item could live so the user can verify nothing was lost
    across a deploy/restart:
      • Retry queue (in-memory + retry_queue.json on disk)
      • Failed log (failed_log.json)
      • Orphan files on disk (data/files not in meta.documents)
      • Pending OCR/Pro decisions (pending_store)
      • In-flight items (currently being processed, with stale check)
    Total = 학습 대기 자료 표면적. 같은 자료를 다시 올릴 필요 있는지
    이 한 줄로 판단."""
    if not _is_owner(update):
        return
    await _typing(update, ctx)

    queue_n = len(_INGEST_RETRY_QUEUE)
    in_flight = sum(1 for it in _INGEST_RETRY_QUEUE
                    if it.get("in_flight_ts"))
    waiting = queue_n - in_flight
    failed_n = len(_INGEST_FAILED)

    try:
        orphans = await asyncio.to_thread(_scan_orphan_files)
    except Exception:
        orphans = []
    orphan_n = len(orphans)

    try:
        pending_ocr_n = len(await asyncio.to_thread(pending_store.list_ocr))
        pending_pro_n = len(await asyncio.to_thread(pending_store.list_pro))
    except Exception:
        pending_ocr_n = pending_pro_n = 0

    # Disk truth: read the persisted files directly so the user sees
    # both in-memory and on-disk counts. Disk count >= memory means
    # we have everything; disk < memory shouldn't happen under the
    # atomic-write pattern but flag if it does.
    disk_queue_n = disk_failed_n = -1
    try:
        d = _load_json_with_recovery(_RETRY_QUEUE_PATH)
        if isinstance(d, list):
            disk_queue_n = len(d)
    except Exception:
        pass
    try:
        d = _load_json_with_recovery(_FAILED_LOG_PATH)
        if isinstance(d, list):
            disk_failed_n = len(d)
    except Exception:
        pass

    total_pending = queue_n + orphan_n + failed_n + pending_ocr_n + pending_pro_n

    lines = [
        "🔍 <b>학습 대기 감사</b>",
        f"• 재시도 큐: <b>{queue_n}건</b>"
        f" (처리중 {in_flight} · 대기 {waiting})",
        f"  └ 디스크: {disk_queue_n if disk_queue_n >= 0 else '?'}건"
        f"{' ⚠️ 메모리와 불일치' if disk_queue_n != queue_n and disk_queue_n >= 0 else ''}",
        f"• 실패 로그: <b>{failed_n}건</b>"
        f" (디스크 {disk_failed_n if disk_failed_n >= 0 else '?'})",
        f"• Orphan 파일: <b>{orphan_n}건</b>"
        " (디스크에는 있지만 미학습 — 다음 스캔/재시작 시 자동 큐 등록)",
        f"• Pending OCR: <b>{pending_ocr_n}건</b>"
        f" / Pending Pro: <b>{pending_pro_n}건</b>",
        f"━━━━━━━━━━━━━━━",
        f"📦 <b>합계 {total_pending}건</b> 학습 대기 (이 외엔 모두 처리 끝)",
        "",
        "💡 같은 자료를 또 올릴 필요 있는지 확인:",
        "  → 위 합계가 0이면 다 학습됐거나 영구 무시됨 (/failed 참고)",
        "  → 합계가 0보다 크면 자동 처리 대기중 — 더 보낼 필요 X",
    ]
    await update.message.reply_text(
        "\n".join(lines), parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def cmd_orphans(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Read-only orphan listing — files on disk under data/files/ that
    aren't yet in meta.documents. Doesn't enqueue them, doesn't touch
    the recovery suppress marker. Use /recover_orphans to actually
    push them onto the retry queue."""
    if not _is_owner(update):
        return
    await _typing(update, ctx)
    orphans = await asyncio.to_thread(_scan_orphan_files)
    if not orphans:
        await update.message.reply_text(
            "✨ 미학습 파일 없음 — 모든 디스크 파일이 meta에 기록됨."
        )
        return
    # By extension breakdown so the user sees "23 PDFs, 4 PPTs, ..." at
    # a glance. Sorted alphabetically inside each group for predictability.
    by_ext: dict[str, list[Path]] = {}
    for p in orphans:
        by_ext.setdefault(p.suffix.lower() or "(no ext)", []).append(p)
    summary_line = " · ".join(
        f"{ext} {len(files)}개"
        for ext, files in sorted(by_ext.items(), key=lambda kv: -len(kv[1]))
    )
    # Cap the visible list so a 200-file orphan set doesn't overflow
    # Telegram's 4096-char message limit. The full list is still
    # accessible via shell or future paging if needed.
    show = orphans[:30]
    listing = "\n".join(f"  • {p.name[:80]}" for p in show)
    more = f"\n... 외 {len(orphans) - len(show)}건" if len(orphans) > len(show) else ""
    await update.message.reply_text(
        f"📂 미학습 파일 {len(orphans)}건\n  ({summary_line})\n\n"
        f"{listing}{more}\n\n"
        f"학습 시작: /recover_orphans"
    )


def _scan_orphan_files_sorted() -> list[Path]:
    """_scan_orphan_files() result sorted by file size ASC so
    /recover_orphans surfaces small/quick files at the top — same
    triage UX as /failed."""
    orphans = _scan_orphan_files()
    try:
        return sorted(orphans, key=lambda p: p.stat().st_size)
    except Exception:
        return orphans


def _format_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f}MB"
    if size >= 1024:
        return f"{size // 1024}KB"
    return f"{size}B"


def _orphan_enqueue_one(orphan_path: Path, chat_id: int) -> bool:
    """Push a single orphan onto the retry queue. Returns True on
    enqueue, False on a duplicate (already queued under the same name)."""
    name = orphan_path.name
    for item in _INGEST_RETRY_QUEUE:
        if item.get("kind") == "local_file" and Path(
                item.get("path") or ""
        ).name == name:
            return False
    _INGEST_RETRY_QUEUE.append({
        "kind": "local_file",
        "path": str(orphan_path),
        "file_name": name,
        "chat_id": chat_id,
        "attempts": 0,
    })
    _persist_retry_queue()
    return True


async def cmd_recover_orphans(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Per-item orphan approval. Smallest files surface first so the
    user can clear quick wins without scrolling past multi-MB PDFs
    and unsupported MP4 leftovers. Each bubble shows filename + size
    with [📥 학습 #N] and [🗑 영구 무시 #N] buttons. Bulk buttons at
    the end fall back to the old all-or-nothing behavior.

    Side effect: clears the _RECOVERY_SUPPRESS_PATH marker so the
    hourly auto-scan resumes. Old /queue_cancel_all set that marker
    to silence background work."""
    if not _is_owner(update):
        return
    await _typing(update, ctx)
    marker_was_present = False
    try:
        if _RECOVERY_SUPPRESS_PATH.exists():
            _RECOVERY_SUPPRESS_PATH.unlink()
            marker_was_present = True
    except Exception:
        log.exception("failed to clear recovery suppress marker")
    orphans = await asyncio.to_thread(_scan_orphan_files_sorted)
    if not orphans:
        msg = "✨ 미학습 파일 없음 — 모든 디스크 파일이 meta에 기록됨."
        if marker_was_present:
            msg += "\n🔓 자동 복구 다시 활성화됨 (재시작 시 자동 스캔 재개)"
        await update.message.reply_text(msg)
        return
    header_lines = [
        f"📂 미학습 파일 {len(orphans)}건 (작은 것부터)",
    ]
    if marker_was_present:
        header_lines.append("🔓 자동 복구도 재활성화됨")
    header_lines.append(
        "건별 [📥 학습] / [🗑 영구 무시] · 또는 맨 아래 일괄 버튼"
    )
    await update.message.reply_text("\n".join(header_lines))

    # Per-item bubbles, cap at 10 to keep the keyboard responsive.
    # User can re-run /recover_orphans to see the next batch.
    ORPHAN_INLINE_CAP = 10
    for i, p in enumerate(orphans[:ORPHAN_INLINE_CAP]):
        try:
            size = p.stat().st_size
        except Exception:
            size = 0
        size_tag = _format_size(size)
        title = f"📄 #{i + 1} · {size_tag}\n{p.name[:100]}"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"📥 학습 #{i + 1}",
                callback_data=f"orphan_learn:{i}",
            ),
            InlineKeyboardButton(
                f"🗑 영구 무시 #{i + 1}",
                callback_data=f"orphan_ignore:{i}",
            ),
        ]])
        await update.message.reply_text(title, reply_markup=kb)
    if len(orphans) > ORPHAN_INLINE_CAP:
        await update.message.reply_text(
            f"…외 {len(orphans) - ORPHAN_INLINE_CAP}건 더 있음. "
            f"위 처리 후 /recover_orphans 다시 호출, 또는 일괄 버튼 사용."
        )

    # Bulk fallback for users who don't want to triage manually.
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📥 전체 학습", callback_data="orphan_learn_all"),
        InlineKeyboardButton(
            "🗑 전체 영구 무시", callback_data="orphan_ignore_all"
        ),
    ]])
    await update.message.reply_text("일괄 작업:", reply_markup=kb)


async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """List pending OCR / Pro / URL decisions. Each section renders
    as individual inline-button bubbles so the user can decide per-doc
    by title with a single tap. URL decisions are entries from
    `pending_url_decisions` whose initial bubble has been on screen
    for ≥ OVERDUE_MIN minutes without a user response — they surface
    here so they never get lost in scroll."""
    if not _is_owner(update):
        return
    await _typing(update, ctx)
    ocr_items = await asyncio.to_thread(pending_store.list_ocr)
    pro_items = await asyncio.to_thread(pending_store.list_pro)
    url_items = await asyncio.to_thread(pending_url_decisions.list_overdue)
    if not ocr_items and not pro_items and not url_items:
        await update.message.reply_text(
            "📭 검토 대기 항목 없음 — 모든 확인 prompt가 처리됨."
        )
        return

    # Header summary
    total = len(ocr_items) + len(pro_items) + len(url_items)
    header = [f"📋 검토 대기 항목 ({total}개)"]
    if ocr_items:
        header.append(f"🔵 OCR 확장 가능 {len(ocr_items)}개 — "
                      f"아래 항목별 버튼으로 한 번에 결정")
    if pro_items:
        header.append(f"🟣 Pro 합성 가능 {len(pro_items)}개")
    if url_items:
        header.append(f"🟠 URL 추출 실패 {len(url_items)}개 — "
                      f"5분 이상 대기 중 (재시도/차단 선택 필요)")
    await update.message.reply_text("\n".join(header))

    # OCR items: per-item bubble with 3 inline buttons. Cap at 10
    # so a 30-item backlog doesn't spam 30 messages — user clears
    # the top batch, then re-runs /pending to see the rest. Sorted
    # by file size ASC so small PDFs surface first; the user gets
    # quick wins instead of bumping into a 200-page chart deck at
    # the top.
    def _ocr_size(it: dict) -> int:
        path = it.get("pdf_path")
        if not path:
            return 0
        try:
            return Path(path).stat().st_size
        except Exception:
            return 0
    ocr_items_sorted = sorted(ocr_items, key=_ocr_size)
    OCR_INLINE_CAP = 10
    for it in ocr_items_sorted[:OCR_INLINE_CAP]:
        remaining = max(0, it["total_pages"] - it["applied_pages"])
        est = max(5, remaining * 3)
        title = (it.get("title") or "(no title)")[:120]
        size_tag = _format_size(_ocr_size(it))
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"📄 OCR 추가 ({remaining}p, ~₩{est})",
                callback_data=f"ocr:{it['id']}:go",
            )],
            [InlineKeyboardButton(
                f"📝 텍스트만 유지" + (f" ({it['applied_pages']}p)"
                                       if it['applied_pages'] > 0 else ""),
                callback_data=f"ocr:{it['id']}:skip",
            )],
            [InlineKeyboardButton(
                "🚫 학습 취소 (문서 삭제)",
                callback_data=f"ocr:{it['id']}:forget",
            )],
        ])
        status = (f"{it['applied_pages']}/{it['total_pages']}p OCR 적용됨"
                  if it['applied_pages'] > 0
                  else f"총 {it['total_pages']}p · 텍스트만 추출됨")
        await update.message.reply_text(
            f"📊 {title} · {size_tag}\n{status}",
            reply_markup=kb,
        )
    if len(ocr_items) > OCR_INLINE_CAP:
        await update.message.reply_text(
            f"…외 {len(ocr_items) - OCR_INLINE_CAP}건 더 있음. "
            f"위 항목 처리 후 /pending 다시 호출."
        )

    # Pro items stay text-only (need full question replay)
    if pro_items:
        lines = ["🟣 Pro 합성 가능"]
        for it in pro_items[:30]:
            est = max(80, it["count"] * 3 + 30)
            q = (it.get("question") or "")[:70]
            lines.append(
                f"  [{it['id']}] \"{q}\"\n"
                f"        {it['count']}개 자료 · Pro ~₩{est}"
            )
        if len(pro_items) > 30:
            lines.append(f"  ... 외 {len(pro_items) - 30}건")
        lines.append("  → /pending_pro <번호> 로 Pro 답변 시작")
        await update.message.reply_text(
            "\n".join(lines), disable_web_page_preview=True,
        )

    # URL decisions: same per-item bubble pattern as OCR. These are
    # entries the drain prompted ≥ OVERDUE_MIN min ago that the user
    # never acted on, plus any retry-failed ones (mark_retry_failed
    # back-dates prompted_at to surface immediately). Cap at 10 to
    # keep the keyboard responsive — user reruns /pending for more.
    if url_items:
        import hashlib as _h
        URL_INLINE_CAP = 10
        for entry in url_items[:URL_INLINE_CAP]:
            url = entry.get("url") or ""
            if not url:
                continue
            title = (entry.get("title") or "")[:80]
            error = (entry.get("error") or "본문 비어있음")[:80]
            retry_n = int(entry.get("retry_count") or 0)
            retry_tag = f" (시도 #{retry_n + 1})" if retry_n else ""
            text = f"🟠 URL 추출 실패{retry_tag}\n"
            if title and title != url:
                text += f"{title}\n"
            text += f"{url}\n오류: {error}"
            key = _h.sha1(url.encode("utf-8")).hexdigest()[:16]
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🔁 재시도", callback_data=f"urldec_retry:{key}",
                ),
                InlineKeyboardButton(
                    "🚫 차단", callback_data=f"urldec_block:{key}",
                ),
            ]])
            await update.message.reply_text(
                text, reply_markup=kb, disable_web_page_preview=True,
            )
        if len(url_items) > URL_INLINE_CAP:
            await update.message.reply_text(
                f"…외 {len(url_items) - URL_INLINE_CAP}건 더 있음. "
                f"위 처리 후 /pending 다시 호출."
            )


async def cmd_ocr_extend(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Manual OCR extension for an already-ingested PDF.

    Same confirmation flow as the auto-trigger at ingest time:
      * compute page count + cost estimate
      * send inline buttons (✅ proceed · ❌ cancel)
      * prompt rows live in pending_store (SQLite) — never expire,
        survive bot restarts.

    Routes through the same on_ocr_extend_callback handler so a tap
    goes straight into pipeline.extend_pdf_ocr."""
    if not _is_owner(update):
        return
    if not ctx.args:
        await update.message.reply_text(
            "사용법: /ocr_extend <doc_id 또는 제목 키워드>"
        )
        return
    query = " ".join(ctx.args).strip()
    await _typing(update, ctx)
    # Look up the doc — try direct id first, then title substring.
    doc = await asyncio.to_thread(meta.get_doc, query)
    if not doc:
        matches = await asyncio.to_thread(meta.search_title, query, 1)
        if not matches:
            await update.message.reply_text(
                f"⚠️ 매칭 doc 없음: '{query[:60]}'"
            )
            return
        doc = matches[0]
    doc_id = doc["id"]
    title = doc.get("title") or query
    source = doc.get("source") or ""
    # Derive filename from the source label.
    if source.startswith("tg-doc:"):
        fname = source.split(":", 2)[-1]
    elif source.startswith("local:"):
        fname = source[len("local:"):]
    else:
        await update.message.reply_text(
            f"⚠️ 비-PDF source: {source[:60]} (OCR 확장은 디스크 PDF만 지원)"
        )
        return
    pdf_path = Path(config.DATA_DIR) / "files" / fname
    if not pdf_path.exists():
        await update.message.reply_text(
            f"⚠️ 디스크에 파일 없음: {fname[:60]}"
        )
        return
    # Count pages via PyMuPDF.
    try:
        def _count_pages(p: str) -> int:
            import fitz
            d = fitz.open(p)
            try:
                return d.page_count
            finally:
                d.close()
        total_pages = await asyncio.to_thread(_count_pages, str(pdf_path))
    except Exception as e:
        await update.message.reply_text(
            f"⚠️ 페이지 수 확인 실패: {_explain_error(e)}"
        )
        return
    if total_pages <= 0:
        await update.message.reply_text("⚠️ 페이지 0 — OCR 대상 없음")
        return
    # Cost estimate. ~₩3 per Vision-Lite page, text-dense pages
    # auto-skipped at extend time.
    est_cost = max(10, total_pages * 3)
    row_id = await asyncio.to_thread(
        pending_store.add_ocr,
        chat_id=update.effective_chat.id,
        doc_id=doc_id,
        title=title,
        pdf_path=str(pdf_path),
        applied_pages=0,  # extend from page 1
        total_pages=total_pages,
    )
    if row_id is None:
        await update.message.reply_text("⚠️ pending_store 등록 실패.")
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"📄 {total_pages}p OCR (~₩{est_cost})",
            callback_data=f"ocr:{row_id}:go",
        )],
        [InlineKeyboardButton(
            "❌ 취소", callback_data=f"ocr:{row_id}:skip",
        )],
    ])
    title_short = (title or fname)[:80]
    await update.message.reply_text(
        f"📊 OCR 확장 요청 — {title_short}\n"
        f"총 {total_pages}p (텍스트 충분한 페이지는 자동 skip)\n"
        f"예상 비용: ~₩{est_cost}\n\n"
        f"버튼은 만료 없음 — 언제든 선택 가능.",
        reply_markup=kb,
    )


async def cmd_pending_ocr(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Trigger OCR extension for a pending row id. Runs in background
    via the extend_pdf_ocr pipeline call. Removes the row when done."""
    if not _is_owner(update):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text(
            "사용법: /pending_ocr <번호>\n/pending 으로 번호 확인."
        )
        return
    row_id = int(ctx.args[0])
    item = await asyncio.to_thread(pending_store.get_ocr, row_id)
    if not item:
        await update.message.reply_text(f"⚠️ pending OCR #{row_id} 찾을 수 없음.")
        return
    pdf_path = Path(item["pdf_path"])
    if not pdf_path.exists():
        await update.message.reply_text(
            f"⚠️ 원본 PDF 파일 없음 ({pdf_path.name}). 자료 다시 보내야 함."
        )
        pending_store.delete_ocr(row_id)
        return
    await _typing(update, ctx)
    sent = await update.message.reply_text(
        f"⏳ OCR 확장 진행 중: {item['title'][:80]} "
        f"({item['applied_pages']+1}-{item['total_pages']}p)"
    )
    try:
        r = await pipeline.extend_pdf_ocr(
            pdf_path, item["doc_id"],
            int(item["applied_pages"]) + 1, int(item["total_pages"]),
        )
    except Exception as e:
        log.exception("pending OCR extend failed")
        await _edit_or_send(
            ctx, sent.chat.id, sent.message_id,
            f"⚠️ OCR 확장 실패: {_explain_error(e)}",
        )
        return
    pending_store.delete_ocr(row_id)
    if r.get("status") == "ok":
        skip_note = (f" · {r['pages_skipped']}p 텍스트 충분 스킵"
                     if r.get("pages_skipped") else "")
        await _edit_or_send(
            ctx, sent.chat.id, sent.message_id,
            f"✅ {item['title'][:80]}\n"
            f"   +{r['pages_ocrd']}p OCR{skip_note} · +{r['chunks_added']} 청크"
        )
    else:
        await _edit_or_send(
            ctx, sent.chat.id, sent.message_id,
            f"⚠️ OCR 결과 없음: {item['title'][:80]}",
        )


async def cmd_pending_pro(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Re-run a pending question as a Pro synthesis (deep=True)."""
    if not _is_owner(update):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text(
            "사용법: /pending_pro <번호>\n/pending 으로 번호 확인."
        )
        return
    row_id = int(ctx.args[0])
    item = await asyncio.to_thread(pending_store.get_pro, row_id)
    if not item:
        await update.message.reply_text(f"⚠️ pending Pro #{row_id} 찾을 수 없음.")
        return
    pending_store.delete_pro(row_id)
    # Route through the same _run_agent path as a normal /deep question
    # so memory/pressure guards and reply rendering apply. deep=True
    # bypasses the Pro confirmation gate (user explicitly asked).
    await _run_agent(update, ctx, item["question"], deep=True)


def _ocr_cost_est(applied: int, total: int) -> int:
    remaining = max(0, total - applied)
    return max(10, remaining * 3)


def _pro_cost_est(count: int) -> int:
    return max(80, count * 3 + 30)


async def cmd_pending_approve_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 1 of bulk approve — preview the totals + cost estimate.
    User must follow up with /pending_approve_all_confirm to actually
    execute. The two-step gate exists because a wide Pro approval can
    easily hit ~₩1k+; we don't want a slip-tap to spend it."""
    if not _is_owner(update):
        return
    await _typing(update, ctx)
    ocr_items = await asyncio.to_thread(pending_store.list_ocr)
    pro_items = await asyncio.to_thread(pending_store.list_pro)
    if not ocr_items and not pro_items:
        await update.message.reply_text("📭 일괄 승인 대상 없음 — /pending 비어있음.")
        return
    ocr_cost = sum(
        _ocr_cost_est(int(it["applied_pages"]), int(it["total_pages"]))
        for it in ocr_items
    )
    pro_cost = sum(_pro_cost_est(int(it["count"])) for it in pro_items)
    total = len(ocr_items) + len(pro_items)
    lines = [
        "📋 일괄 승인 대상\n",
        f"🔵 OCR {len(ocr_items)}건 (예상 ~₩{ocr_cost:,})",
        f"🟣 Pro {len(pro_items)}건 (예상 ~₩{pro_cost:,})",
        f"   합계 {total}건 · 약 ~₩{ocr_cost + pro_cost:,}\n",
        "진행하려면 /pending_approve_all_confirm",
    ]
    await update.message.reply_text("\n".join(lines))


async def cmd_pending_approve_all_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 2 — actually queue every pending item for processing.
    OCR rows ride the existing retry queue (kind='ocr_extend',
    drains 1/tick × 2min). Pro rows go to _PENDING_PRO_RUN_QUEUE
    (separate drain job, 1/tick × 90s)."""
    if not _is_owner(update):
        return
    await _typing(update, ctx)
    ocr_items = await asyncio.to_thread(pending_store.list_ocr)
    pro_items = await asyncio.to_thread(pending_store.list_pro)
    if not ocr_items and not pro_items:
        await update.message.reply_text("📭 일괄 승인 대상 없음.")
        return
    ocr_pushed = 0
    for it in ocr_items:
        if not it.get("pdf_path"):
            continue
        _INGEST_RETRY_QUEUE.append({
            "kind": "ocr_extend",
            "doc_id": it["doc_id"],
            "title": it["title"],
            "pdf_path": it["pdf_path"],
            "start_page": int(it["applied_pages"]) + 1,
            "end_page": int(it["total_pages"]),
            "chat_id": int(it["chat_id"]),
            "attempts": 0,
        })
        ocr_pushed += 1
    pro_pushed = 0
    for it in pro_items:
        if not it.get("question"):
            continue
        _PENDING_PRO_RUN_QUEUE.append({
            "chat_id": int(it["chat_id"]),
            "question": it["question"],
        })
        pro_pushed += 1
    pending_store.delete_all_ocr()
    pending_store.delete_all_pro()
    _persist_retry_queue()
    eta_ocr_min = (ocr_pushed * 2)
    eta_pro_min = (pro_pushed * 2)  # 90s tick ≈ 1.5min, round up
    await update.message.reply_text(
        f"✅ 일괄 승인 완료\n"
        f"🔵 OCR {ocr_pushed}건 인입 큐 추가 (~{eta_ocr_min}분 소요)\n"
        f"🟣 Pro {pro_pushed}건 답변 큐 추가 (~{eta_pro_min}분 소요)\n"
        f"진행 상황 → /queue · /status"
    )


async def cmd_pending_cancel_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Wipe every pending row without acting on it. Zero cost — just
    a DB delete on the two pending tables."""
    if not _is_owner(update):
        return
    await _typing(update, ctx)
    ocr_n = await asyncio.to_thread(pending_store.delete_all_ocr)
    pro_n = await asyncio.to_thread(pending_store.delete_all_pro)
    if ocr_n == 0 and pro_n == 0:
        await update.message.reply_text("📭 취소할 항목 없음.")
        return
    await update.message.reply_text(
        f"🗑 {ocr_n + pro_n}건 취소됨 (OCR {ocr_n} · Pro {pro_n})"
    )


def _retry_item_to_failed_entry(item: dict, reason: str) -> dict:
    """Convert a retry-queue item into the /failed log shape so it
    surfaces in /failed with the size tag + per-item retry button."""
    title = (
        item.get("file_name")
        or item.get("url")
        or item.get("title")
        or (Path(item["path"]).name if item.get("path") else None)
        or "(unknown)"
    )[:140]
    file_size = 0
    path = item.get("path") or item.get("file_path")
    if path:
        try:
            file_size = Path(path).stat().st_size
        except Exception:
            pass
    payload = {k: v for k, v in item.items() if k != "chat_id"}
    payload.setdefault("failed_cycles", 1)
    return {
        "status": "error",
        "title": title,
        "detail": reason[:200],
        "ts": datetime.utcnow().isoformat(),
        "failed_cycles": 1,
        "file_size": file_size,
        "retry": payload,
    }


async def cmd_queue_to_failed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Move everything in the ingest retry queue into the /failed log
    instead of discarding them like /queue_cancel_all does. In-flight
    items (currently being processed) can't be aborted mid-pipeline —
    they finish and land in /failed naturally on error or get learned
    on success. Use this when a flood of big/unsupported files clogs
    the queue: the entries land in /failed sorted smallest-first, with
    [🔁 #N]/[🗑 #N] buttons so the user can pick which ones deserve
    a manual retry instead of letting all of them cycle through 5
    auto-retries. Pending OCR/Pro buckets are left alone — those are
    awaiting user decisions, not failures."""
    if not _is_owner(update):
        return
    await _typing(update, ctx)
    moved_n = 0
    for item in list(_INGEST_RETRY_QUEUE):
        entry = _retry_item_to_failed_entry(
            item, "큐에서 /failed로 수동 이동"
        )
        _INGEST_FAILED.append(entry)
        moved_n += 1
    _INGEST_RETRY_QUEUE.clear()
    if len(_INGEST_FAILED) > _FAILED_MAX:
        del _INGEST_FAILED[: len(_INGEST_FAILED) - _FAILED_MAX]
    _persist_retry_queue()
    _persist_failed_log()
    if moved_n == 0:
        await update.message.reply_text(
            "📭 큐 비어있음 — 옮길 항목 없음"
        )
        return
    await update.message.reply_text(
        f"📋 retry queue → /failed 이동 완료 ({moved_n}건)\n"
        f"💡 /failed 에서 작은 파일부터 정렬돼 보임. "
        f"건별 [🔁 #N] 재시도 / [🗑 #N] 삭제 가능.\n"
        f"⚠️ 처리중인 항목은 끝까지 가서 자체 결과에 따라 정착됨."
    )


async def cmd_queue_cancel_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Stop everything in flight — wipe ingest retry queue, pending
    Pro run queue, agent overload retry queue, and both pending DB
    tables. Currently-running ingest finishes its file (no clean
    way to abort mid-pipeline) but nothing new starts. Zero cost.
    Use when the bot is overwhelmed and you want a fresh slate."""
    if not _is_owner(update):
        return
    await _typing(update, ctx)
    ingest_n = len(_INGEST_RETRY_QUEUE)
    pro_q_n = len(_PENDING_PRO_RUN_QUEUE)
    agent_q_n = len(_RETRY_QUEUE)
    _INGEST_RETRY_QUEUE.clear()
    _PENDING_PRO_RUN_QUEUE.clear()
    _RETRY_QUEUE.clear()
    _persist_retry_queue()
    ocr_n = await asyncio.to_thread(pending_store.delete_all_ocr)
    pro_n = await asyncio.to_thread(pending_store.delete_all_pro)
    # Drop a marker so the next container boot's orphan scan stays
    # quiet — previously a redeploy after cancel would re-enqueue
    # everything from disk and undo the user's intent.
    try:
        _RECOVERY_SUPPRESS_PATH.touch(exist_ok=True)
    except Exception:
        log.exception("failed to create recovery suppress marker")
    total = ingest_n + pro_q_n + agent_q_n + ocr_n + pro_n
    if total == 0:
        await update.message.reply_text(
            "📭 비울 항목 없음 — 모든 큐 비어있음.\n"
            "🚫 자동 복구도 영구 중단 (재시작해도 orphan 자동 학습 X)\n"
            "다시 학습하려면 /recover_orphans"
        )
        return
    await update.message.reply_text(
        f"🛑 전체 작업 취소 — 총 {total}건 정리\n"
        f"  • 인입 재시도 큐: {ingest_n}건\n"
        f"  • Pro 답변 큐: {pro_q_n}건\n"
        f"  • agent 과부하 재시도: {agent_q_n}건\n"
        f"  • 보류 OCR: {ocr_n}건\n"
        f"  • 보류 Pro: {pro_n}건\n\n"
        f"🚫 자동 복구도 영구 중단 (재시작해도 orphan 자동 학습 안 됨)\n"
        f"진행 중인 ingest 1-2건은 끝까지 처리되고 새 작업은 시작 안 됨.\n"
        f"다시 학습하려면 /recover_orphans 로 재시작 (suppress 마커 해제)"
    )


async def cmd_forget_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    query = " ".join(ctx.args).strip()
    if not query:
        await update.message.reply_text("사용법: /forget_search <제목 일부>")
        return
    matches = meta.search_title(query, limit=20)
    if not matches:
        await update.message.reply_text(f"매칭 없음: '{query}'")
        return
    if len(matches) > 5:
        preview = "\n".join(
            f"  • {m['id']}  [{m['type']}]  {_clean_text(m['title'])[:60]}"
            for m in matches[:8]
        )
        await update.message.reply_text(
            f"⚠️ {len(matches)}개 매칭 — 너무 많아서 자동 삭제 안 함.\n"
            f"더 구체적인 검색어로 다시 시도하거나 /forget <id>로 직접:\n{preview}"
        )
        return
    forgotten = []
    for m in matches:
        n = vector.delete_doc(m["id"])
        meta.delete(m["id"])
        fname = _filename_from_source(m.get("source") or "")
        if fname:
            _record_dedup_confirmed(fname)
        forgotten.append(f"  ✅ {_clean_text(m['title'])[:60]} ({n} chunks)")
    await update.message.reply_text(
        f"삭제 완료 · {len(forgotten)}건\n" + "\n".join(forgotten)
    )


async def cmd_deep(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    text = " ".join(ctx.args).strip()
    if not text:
        await update.message.reply_text("사용법: /deep <질문>")
        return
    await _run_agent(update, ctx, text, deep=True)


async def cmd_forget_qna(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Drop one Q&A row from the archive by id (visible on the
    dashboard's q-{id}.html detail page). Triggers an immediate
    dashboard regenerate so the card disappears in the next refresh."""
    if not _is_owner(update):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("사용법: /forget_qna <id>")
        return
    qid = int(ctx.args[0])
    n = qna.delete(qid)
    try:
        from .dashboard import regenerate as dashboard_regen
        dashboard_regen.regenerate()
    except Exception:
        log.exception("dashboard regen after qna delete failed")
    await update.message.reply_text(
        f"{'✅ 삭제됨' if n else '⚠️ 매칭 없음'} · qna #{qid}"
    )


async def cmd_forget_qna_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Bulk-drop Q&A rows whose question or answer contains the
    keyword. Same regen hook so the dashboard updates immediately."""
    if not _is_owner(update):
        return
    keyword = " ".join(ctx.args).strip()
    if not keyword:
        await update.message.reply_text("사용법: /forget_qna_search <키워드>")
        return
    n = qna.delete_search(keyword)
    try:
        from .dashboard import regenerate as dashboard_regen
        dashboard_regen.regenerate()
    except Exception:
        log.exception("dashboard regen after qna delete_search failed")
    await update.message.reply_text(f"✅ Q&A {n}건 삭제 · 키워드 '{keyword}'")


async def cmd_forget_search_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Bulk forget — bypass /forget_search's 5-match safety so junk
    cleanup (bot meta-output, accidental ingests) is one tap. Typing
    the long command name acts as the confirmation."""
    if not _is_owner(update):
        return
    query = " ".join(ctx.args).strip()
    if not query:
        await update.message.reply_text("사용법: /forget_search_all <키워드>")
        return
    matches = meta.search_title(query, limit=500)
    if not matches:
        await update.message.reply_text(f"매칭 없음: '{query}'")
        return
    forgotten = 0
    chunks_total = 0
    for m in matches:
        chunks_total += vector.delete_doc(m["id"])
        meta.delete(m["id"])
        forgotten += 1
        fname = _filename_from_source(m.get("source") or "")
        if fname:
            _record_dedup_confirmed(fname)
    await update.message.reply_text(
        f"✅ 일괄 삭제 · {forgotten}건 / 청크 {chunks_total}개 제거"
    )


# Single-token aliases so the usage guide can render destructive
# operations as one-tap; the original /dedupe and /cleanup still need
# 'confirm' typed manually as a guard, so these wrappers replicate
# that behavior with the arg pre-supplied.
async def cmd_find_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Alias for /find <kw> 500 — surface every match up to the cap
    on common keywords (배터리, AI, 반도체) without typing the
    number. No-op when invoked without args; cmd_find shows usage."""
    if not _is_owner(update):
        return
    args = list(ctx.args or [])
    if args:
        ctx.args = args + ["500"]
    await cmd_find(update, ctx)


async def cmd_dedupe_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    ctx.args = ["confirm"]
    await cmd_dedupe(update, ctx)


async def cmd_cleanup_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    ctx.args = ["confirm"]
    await cmd_cleanup(update, ctx)


async def cmd_forget_forwards_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    ctx.args = ["confirm"]
    await cmd_forget_forwards(update, ctx)


# Single-token slash aliases for the agent's tools so the usage guide
# can render them as one-tap commands. Each one rephrases the user's
# arg into a sentence the routing layer reliably maps to the intended
# tool — keeps the agent's rich formatting (sources, recency, emoji
# suffix) instead of bypassing it with a raw tool call.
async def cmd_search_my_brain(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("사용법: /search_my_brain <검색어>")
        return
    await _run_agent(update, ctx,
                     f"내 저장 자료에서 '{q}' 찾아서 정리해줘", deep=False)


async def cmd_compare_papers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("사용법: /compare_papers <주제>")
        return
    await _run_agent(update, ctx,
                     f"내 저장 자료에서 '{q}' 관련 다수 문서를 통합·비교해서 정리해줘",
                     deep=False)


async def cmd_search_papers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("사용법: /search_papers <검색어>")
        return
    await _run_agent(update, ctx,
                     f"외부 학술DB에서 '{q}' 관련 최신 논문 찾아줘", deep=False)


async def cmd_web_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("사용법: /web_search <검색어>")
        return
    await _run_agent(update, ctx,
                     f"'{q}' 웹에서 검색해줘", deep=False)


async def cmd_ingest_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    url = " ".join(ctx.args).strip()
    if not url:
        await update.message.reply_text("사용법: /ingest_url <URL>")
        return
    await _run_agent(update, ctx, f"이 URL 학습해줘: {url}", deep=False)


async def cmd_recent_docs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    n = " ".join(ctx.args).strip() or "10"
    await _run_agent(update, ctx,
                     f"최근 학습한 문서 {n}개 알려줘", deep=False)


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Wipe rolling chat memory for this chat. Useful when topic shifts
    and stale context is hurting answer quality."""
    if not _is_owner(update):
        return
    chat_id = update.effective_chat.id
    n = len(_HISTORY.get(chat_id, []))
    _HISTORY.pop(chat_id, None)
    _persist_chat_history()
    await update.message.reply_text(f"대화 메모리 초기화 ({n} 메시지 비움)")


_OVERLOAD_MARKERS = ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "high demand", "overloaded")
# Was 5 (1h→2h→3h→4h→5h backoff = up to 15h before /failed).
# Lowered to 1 so a single failure lands the item in /failed and the
# next queue item proceeds immediately. Big files (multi-GB OCR
# attempts, unsupported MP4/GIF) were clogging the queue with hours
# of pointless retries; if a transient error needs another shot the
# user can tap [🔁 #N] in /failed.
_MAX_RETRY_ATTEMPTS = 1
_RETRY_INTERVAL_SECONDS = 90
_RETRY_QUEUE: list[dict] = []

# Items popped from /pending Pro list via /pending_approve_all_confirm.
# Drained by _drain_pending_pro one item per 90s tick so a 20-row bulk
# approve doesn't fire 20 concurrent agent runs and blow the memory cap.
_PENDING_PRO_RUN_QUEUE: list[dict] = []


async def on_channel_post(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg:
        return
    if config.TELEGRAM_CHANNEL_ID and str(msg.chat.id) != config.TELEGRAM_CHANNEL_ID:
        return
    await _ingest_message(msg, ctx, notify_chat_id=config.TELEGRAM_OWNER_ID)


def _is_overload(exc: BaseException) -> bool:
    s = f"{type(exc).__name__} {exc}"
    return any(m in s for m in _OVERLOAD_MARKERS)


def _format_sources_with_url(
    titles: list[str], cap: int | None = None,
    source_urls: dict[str, str] | None = None,
) -> str:
    """Look up each cited doc title in meta and append the source URL
    if it is an http(s) link, so the user can click straight from the
    bot reply to the original article. By default lists every cited
    source — the chunked Telegram send handles oversized blocks.

    `source_urls` is a {label → URL} map harvested by the agent during
    this turn (e.g. search_papers gives every paper its direct-download
    PDF link). It takes precedence over meta lookup because the agent's
    map is the freshest source — papers from search_papers aren't yet
    in meta.documents."""
    formatted: list[str] = []
    items = titles if cap is None else titles[:cap]
    sm = source_urls or {}
    for title in items:
        url = sm.get(title) or ""
        if not url:
            try:
                matches = meta.search_title(title, limit=1)
            except Exception:
                matches = []
            if matches:
                src = matches[0].get("source") or ""
                if src.startswith(("http://", "https://")):
                    url = src
        if url:
            formatted.append(f"{title} → {url}")
        else:
            formatted.append(title)
    return "\n  • " + "\n  • ".join(formatted) if formatted else ""


_CITE_INNER_RE = re.compile(r"\[([^\[\]\n]{1,300})\]")


def _split_citation_inner(inner: str) -> list[str]:
    """Try to split '[A, B, C]' into ['A', 'B', 'C'] when it looks
    safe. Bail (return single label) when ambiguous — too many parts,
    very short fragments, or commas that look like prose punctuation
    inside one title."""
    if ", " not in inner:
        return [inner]
    parts = [p.strip() for p in inner.split(", ")]
    if len(parts) > 5 or any(len(p) < 3 for p in parts):
        return [inner]
    return parts


def _renumber_citations(
    text: str, harvested_sources: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Replace inline [label] citations with [N] numbered references.

    Each unique label gets the next sequential number; combined
    citations like '[A, B]' get rendered as '[1, 2]'. Returns the
    rewritten text plus the ordered label list (for the legend).

    Pure-digit citations (model wrote '[1]' instead of a real title)
    get resolved against the harvested source list — '[1]' →
    harvested_sources[0]. This fixes the case where the model
    decided to use academic numbered refs on its own: previously the
    legend ended up showing '[1] 1', '[2] 2', etc. (the digit itself
    treated as a label). When the index is out of range we fall
    back to leaving the bracket as-is so a stray '[2026]' year ref
    doesn't pollute the legend."""
    label_to_num: dict[str, int] = {}
    ordered: list[str] = []
    sources = harvested_sources or []

    def repl(m: re.Match) -> str:
        inner = m.group(1).strip()
        if not inner:
            return m.group(0)
        # Digit-only ref: try to map to the harvested source by index
        # so '[1]' becomes the real doc title in the legend.
        if inner.isdigit() and 1 <= len(inner) <= 3:
            idx = int(inner) - 1
            if 0 <= idx < len(sources):
                label = sources[idx]
                if label not in label_to_num:
                    ordered.append(label)
                    label_to_num[label] = len(ordered)
                return f"[{label_to_num[label]}]"
            # Out of range — leave the bracket untouched so a year
            # or unrelated number doesn't fabricate a legend entry.
            return m.group(0)
        parts = _split_citation_inner(inner)
        nums: list[str] = []
        for p in parts:
            if p not in label_to_num:
                ordered.append(p)
                label_to_num[p] = len(ordered)
            nums.append(str(label_to_num[p]))
        return "[" + ", ".join(nums) + "]"

    return _CITE_INNER_RE.sub(repl, text), ordered


def _format_numbered_sources(
    labels: list[str], source_urls: dict[str, str] | None = None,
) -> str:
    """Build the numbered legend rendered after the answer body.
    URLs come from `source_urls` (agent's harvested map for this turn,
    e.g. direct PDF download for /search papers results) when present,
    otherwise fall back to meta.search_title so previously-ingested
    docs still link to their source URL."""
    if not labels:
        return ""
    sm = source_urls or {}
    lines: list[str] = []
    for i, label in enumerate(labels, 1):
        url = sm.get(label) or ""
        if not url:
            try:
                matches = meta.search_title(label, limit=1)
            except Exception:
                matches = []
            if matches:
                src = matches[0].get("source") or ""
                if src.startswith(("http://", "https://")):
                    url = src
        if url:
            lines.append(f"  [{i}] {label} → {url}")
        else:
            lines.append(f"  [{i}] {label}")
    return "\n" + "\n".join(lines)


# Telegram caps a single message at 4096 chars. Long brain answers
# (compare_papers can produce 3000+ chars body + 2000 chars of sources)
# get silently dropped by the API if we pack everything into one send.
# Chunk on paragraph boundaries so each piece reads naturally.
_TG_CHUNK_LIMIT = 3900


def _chunk_for_telegram(text: str, limit: int = _TG_CHUNK_LIMIT) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        # Prefer paragraph boundary, then sentence, then last newline,
        # then a hard cut as a last resort.
        slice_at = remaining.rfind("\n\n", 0, limit)
        if slice_at < int(limit * 0.5):
            slice_at = remaining.rfind("\n", 0, limit)
        if slice_at < int(limit * 0.5):
            slice_at = remaining.rfind(". ", 0, limit)
        if slice_at < int(limit * 0.5):
            slice_at = limit
        parts.append(remaining[:slice_at].rstrip())
        remaining = remaining[slice_at:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


async def _send_chunked(send, text: str) -> None:
    for piece in _chunk_for_telegram(text):
        await send(piece)


_SECTION_HEADER_RE = re.compile(
    r"^(📌 [^\n]+)$", re.MULTILINE,
)


def _format_body_html(body: str) -> str:
    """Render the agent body for Telegram HTML mode. Escapes any
    raw HTML-unsafe chars first, then wraps '📌 N. xxx' section
    headers in <b>...</b> so they pop visually. Section markers in
    the agent's standard output (📌 N. Title) become bold; other
    body text stays plain."""
    import html
    escaped = html.escape(body)
    return _SECTION_HEADER_RE.sub(r"<b>\1</b>", escaped)


async def _send_chunked_html(send, text_html: str) -> None:
    """Same chunking as _send_chunked but with parse_mode=HTML so
    the <b> wrappers around section headers render. Falls back to
    plain text on parse failure (rare; html.escape covers user
    content already)."""
    for piece in _chunk_for_telegram(text_html):
        try:
            await send(piece, parse_mode="HTML")
        except Exception:
            log.warning("HTML send failed, retrying as plain")
            try:
                await send(piece)
            except Exception:
                log.exception("plain fallback send also failed")


_CITATION_LINE_RE = re.compile(r"\(사용 자료 시점:\s*([^)]+)\)")


def _annotate_learn_date(body: str, sources: list[str]) -> str:
    """Add '· 학습: YYYY.MM' to the agent's '(사용 자료 시점: …)' line.

    The agent's date range reflects publication dates pulled from
    source bodies — useful for brokerage reports where the writing
    time matters, but confusing when the user wonders why a recently
    ingested archive shows old dates. Append the latest ingest month
    among cited sources so both axes are visible at a glance."""
    if not sources or not _CITATION_LINE_RE.search(body):
        return body
    latest = ""
    for title in sources[:20]:
        try:
            matches = meta.search_title(title, limit=1)
        except Exception:
            matches = []
        if not matches:
            continue
        d = (matches[0].get("ingested_at") or "")[:7]  # YYYY-MM
        if d and d > latest:
            latest = d
    if not latest:
        return body
    learn_mark = latest.replace("-", ".")  # YYYY.MM
    return _CITATION_LINE_RE.sub(
        lambda m: f"(사용 자료 시점: {m.group(1)} · 학습: {learn_mark})",
        body, count=1,
    )


async def _send_agent_reply(send, result, inherited: bool = False):
    # `inherited` is retained for the historical call-site shape but
    # is now always False — the inheritance fallback was removed
    # because it masked routing failures (model skipped brain search,
    # made up citations, then we stamped unrelated old sources).
    raw, mermaid_blocks = _extract_mermaid(result["text"])
    body = _strip_markdown(raw)
    # Pass harvested sources so '[1]'-style digit refs resolve to the
    # real doc title (previously they ended up as '[1] 1' in the
    # legend because the digit became the label).
    body, ordered_labels = _renumber_citations(
        body, result.get("sources") or [],
    )
    body = _annotate_learn_date(body, result.get("sources") or [])
    body_html = _format_body_html(body)
    suffix_lines = []
    if result.get("warning"):
        suffix_lines.append(result["warning"])
    source_urls = result.get("source_urls") or {}
    if ordered_labels:
        # Use the numbered legend built from inline citations — what
        # the user sees in body [1][2] matches the [1] [2] entries
        # below.
        suffix_lines.append("📚 출처:" + _format_numbered_sources(
            ordered_labels, source_urls,
        ))
    elif result.get("sources"):
        # Tool was called but model didn't cite inline. Fall back to
        # the harvested source list so the user still gets a 출처
        # block.
        suffix_lines.append("📚 출처:" + _format_sources_with_url(
            result["sources"], source_urls=source_urls,
        ))
    if result.get("tool_calls"):
        suffix_lines.append(_format_tool_calls(result["tool_calls"]))
    await _send_chunked_html(send, body_html)
    if suffix_lines:
        await _send_chunked(send, "\n".join(suffix_lines))
    return body, mermaid_blocks


async def _retry_pending(ctx: ContextTypes.DEFAULT_TYPE):
    if not _RETRY_QUEUE:
        return
    item = _RETRY_QUEUE.pop(0)
    try:
        result = await agent.run(item["text"], deep=item["deep"])
    except Exception as e:
        item["attempts"] += 1
        if item["attempts"] >= _MAX_RETRY_ATTEMPTS or not _is_overload(e):
            await ctx.bot.send_message(
                item["chat_id"],
                f"⚠️ 재시도 포기 — {_explain_error(e)}\n원래 질문을 다시 보내주세요.",
            )
            return
        log.info("queued retry %d/%d for chat %s",
                 item["attempts"], _MAX_RETRY_ATTEMPTS, item["chat_id"])
        _RETRY_QUEUE.append(item)
        return
    chat_id = item["chat_id"]

    async def _send(text):
        await ctx.bot.send_message(chat_id, f"⏰ 재시도 성공\n\n{text}")

    _, mermaid_blocks = await _send_agent_reply(_send, result)
    for code in mermaid_blocks:
        try:
            png = await _render_mermaid_png(code)
            await ctx.bot.send_photo(chat_id, photo=png, caption="🧩 다이어그램")
        except Exception:
            pass




async def on_private(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    msg = update.message
    if not msg:
        return

    # Attachments — always ingest, never reach agent.
    if msg.document or msg.photo or msg.voice or msg.audio:
        await _ingest_message(msg, ctx, notify_chat_id=msg.chat.id)
        return

    text = (msg.text or msg.caption or "").strip()
    if not text:
        return

    # Treat anything with a URL or 200+ chars of body as a "save this"
    # signal — ingest first, no agent prompt asking '저장하시겠습니까?'.
    # Short queries fall through to the agent for a normal answer.
    urls, plain = _collect_message_urls(msg)
    if urls or len(plain) >= 200:
        await _ingest_message(msg, ctx, notify_chat_id=msg.chat.id)
        return

    await _run_agent(update, ctx, text, deep=False)


async def _run_agent(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                     text: str, deep: bool) -> None:
    global _ACTIVE_AGENT_RUNS, _LAST_REPLY_AT
    # Memory pressure gate. At ≥95% of mem_limit a new agent run
    # almost guarantees an OOM kill mid-answer (Pro synth + reranker +
    # BM25 cache all alloc fresh chunks); refusing with a clear
    # message is better than losing the whole container. At ≥90% try
    # a synchronous cleanup first — usually drops us back under the
    # bar and the run proceeds normally.
    pressure = _mem_pressure()
    if pressure >= _MEM_REFUSE_THRESHOLD:
        await update.message.reply_text(
            f"⚠️ 메모리 부족 — 현재 {pressure*100:.0f}% 사용 중. "
            "잠시 후 다시 시도해주세요. (자동 정리 5분 주기 · /status 확인)"
        )
        return
    if pressure >= _MEM_CLEANUP_THRESHOLD:
        try:
            _run_memory_cleanup(f"pre-agent {pressure*100:.0f}%")
        except Exception:
            log.exception("threshold cleanup failed")
    _ACTIVE_AGENT_RUNS += 1
    await _typing(update, ctx)
    chat_id = update.effective_chat.id
    history = list(_HISTORY.get(chat_id, []))
    # Sustained "typing..." indicator until agent returns. Background
    # task fires the chat_action every 4s so the user sees the bot is
    # still working through compare_papers/Pro synthesis.
    typing_task = asyncio.create_task(_sustained_typing(update, ctx))
    try:
        try:
            result = await agent.run(text, deep=deep, history=history)
        except Exception as e:
            if _is_overload(e):
                _RETRY_QUEUE.append({
                    "chat_id": update.effective_chat.id,
                    "text": text,
                    "deep": deep,
                    "attempts": 0,
                })
                await update.message.reply_text(
                    "⏳ Gemini 일시 과부하 — 자동으로 재시도 중입니다 (최대 약 7~8분).\n"
                    "별도로 다시 보내실 필요 없어요."
                )
                _ACTIVE_AGENT_RUNS = max(0, _ACTIVE_AGENT_RUNS - 1)
                _LAST_REPLY_AT = datetime.utcnow()
                return
            log.exception("agent failed")
            await update.message.reply_text(f"⚠️ {_explain_error(e)}")
            _ACTIVE_AGENT_RUNS = max(0, _ACTIVE_AGENT_RUNS - 1)
            _LAST_REPLY_AT = datetime.utcnow()
            return
    finally:
        typing_task.cancel()
    # Pro-confirmation gate: the agent paused mid-flight because a
    # large compare_papers result would trigger a ₩150-class Pro
    # synthesis. Show inline buttons so the user opts in instead of
    # getting silently billed for it. The agent run resumes in
    # on_pro_confirmation_callback when the user taps.
    if result.get("status") == "pending_pro_confirmation":
        try:
            await _send_pro_confirmation(update, result)
        except Exception:
            log.exception("pro confirmation send failed")
        _ACTIVE_AGENT_RUNS = max(0, _ACTIVE_AGENT_RUNS - 1)
        _LAST_REPLY_AT = datetime.utcnow()
        return
    try:
        await _finalize_agent_reply(
            update.message, ctx, chat_id, text, result,
        )
    finally:
        _ACTIVE_AGENT_RUNS = max(0, _ACTIVE_AGENT_RUNS - 1)
        _LAST_REPLY_AT = datetime.utcnow()


async def _finalize_agent_reply(message, ctx: ContextTypes.DEFAULT_TYPE,
                                chat_id: int, text: str, result: dict) -> None:
    """Shared reply tail used by both fresh runs and resumed runs.

    Wrapped so a render/network hiccup can't leave the user staring
    at a dead typing indicator with no error signal. If anything
    blows up we still ack with an explain string. `message` is any
    object with `reply_text`/`reply_photo` (Message or
    callback_query.message)."""
    try:
        # No inheritance fallback. Earlier we re-attached the previous
        # turn's sources when result["sources"] was empty so users
        # always saw a 출처 block — but combined with the rule that
        # every question MUST trigger a fresh brain search, that just
        # papers over routing failures (model answered from training
        # knowledge, made up citation labels in the body, then we
        # stamped unrelated old sources on top). Now an empty sources
        # set surfaces honestly as the verify '출처 없음' warning.
        body, mermaid_blocks = await _send_agent_reply(
            message.reply_text, result, inherited=False,
        )
        _record_turn(chat_id, "user", text)
        _record_turn(
            chat_id, "model", body,
            sources=result.get("sources") or [],
            tools=result.get("tool_calls") or [],
        )
        qna.record(
            chat_id=chat_id,
            question=text,
            answer=body,
            sources=result.get("sources") or [],
            tools=result.get("tool_calls") or [],
            model=result.get("model"),
            warning=result.get("warning"),
        )
        try:
            from .dashboard import regenerate as dashboard_regen
            dashboard_regen.regenerate()
        except Exception:
            log.exception("dashboard regen failed")
        for code in mermaid_blocks:
            try:
                png = await _render_mermaid_png(code)
                await message.reply_photo(photo=png, caption="🧩 다이어그램")
            except Exception as e:
                log.warning("mermaid render failed: %s", e)
                await message.reply_text(
                    f"(다이어그램 렌더 실패: {_explain_error(e)})\n\n{code[:500]}"
                )
    except Exception as e:
        log.exception("post-agent reply failed")
        try:
            await message.reply_text(
                f"⚠️ 응답 처리 중 오류 — {_explain_error(e)}\n"
                "질문은 처리됐지만 응답 전송에서 문제가 났습니다. "
                "/status 로 봇 상태 확인 후 다시 시도해주세요."
            )
        except Exception:
            log.exception("error notification also failed")


async def _send_pro_confirmation(update: Update, result: dict) -> None:
    """Inline keyboard asking whether to pay the Pro premium on a
    large compare_papers synthesis. Encodes the agent's state_id +
    the user's choice in callback_data — Telegram caps that at 64
    bytes so we keep it compact: 'pro:<state_id_first_24>:<choice>'.
    state_id is a uuid hex (32 chars), so we truncate to first 24
    which is still globally unique enough for our concurrent run
    count (~1 at a time)."""
    state_id = result["state_id"]
    count = result["count"]
    short = state_id[:24]
    # Estimate Pro cost: ~1500 chars/doc → ~1.5k input toks/doc.
    # Pro: ₩1,750/1M in + ₩14,000/1M out → ~₩3/doc input + ₩20 out.
    pro_est = max(80, int(count * 3 + 30))
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"💎 Pro 합성 (~₩{pro_est})", callback_data=f"pro:{short}:pro"
        )],
        [InlineKeyboardButton(
            "⚡ Flash 25개로 (~₩20)", callback_data=f"pro:{short}:flash"
        )],
        [InlineKeyboardButton(
            "❌ 취소", callback_data=f"pro:{short}:cancel"
        )],
    ])
    # Remember the full state_id since callback_data only carries 24
    # chars. Keyed by short id so we can look up the real one.
    _PENDING_SHORT_TO_FULL[short] = state_id
    await update.message.reply_text(
        f"📊 {count}개 문서가 검색됐어요.\n\n"
        f"💎 Pro: 전체 {count}개 깊이 통합 분석 (~₩{pro_est})\n"
        f"⚡ Flash: 상위 25개로 빠른 합성 (~₩20)\n"
        f"❌ 취소: 답변하지 않음\n\n"
        f"5분 안에 선택해주세요 (미응답 시 /pending 으로 자동 이동).",
        reply_markup=kb,
    )


# Short-id → full-state-id map for the Pro confirmation flow (the
# agent stores the full state under a uuid, but Telegram callback_data
# caps at 64 bytes so we send the first 24 hex chars and look up the
# rest here). OCR flow no longer uses this — it goes straight to
# pending_store by row id.
_PENDING_SHORT_TO_FULL: dict[str, str] = {}


async def _send_ocr_extend_prompts(ctx: ContextTypes.DEFAULT_TYPE,
                                   chat_id: int, results: list[dict]) -> None:
    """For each PDF result with capped Vision OCR, register a pending
    OCR row in the persistent store and send an inline-button prompt
    keyed on that row id. Three buttons:
      • 📄 OCR 추가 — extend OCR to the remaining pages (₩~3/p)
      • 📝 텍스트만 — accept current text-only learning, close prompt
      • 🚫 학습 취소 — forget the doc entirely (meta + vector delete)

    Prompts no longer expire — the pending_store row stays until the
    user taps a button or runs /pending_cancel_all. Buttons survive
    bot restarts because their state lives in SQLite, not memory."""
    for r in results:
        oc = r.get("ocr_meta")
        if not oc or not oc.get("capped"):
            continue
        applied = int(oc.get("applied_pages", 0))
        total = int(oc.get("total_pages", 0))
        remaining = max(0, total - applied)
        if remaining == 0:
            continue
        cost_est = max(5, remaining * 3)
        row_id = await asyncio.to_thread(
            pending_store.add_ocr,
            chat_id=chat_id,
            doc_id=str(r.get("doc_id") or ""),
            title=str(r.get("title") or "")[:200],
            pdf_path=str(oc.get("pdf_path") or ""),
            applied_pages=applied,
            total_pages=total,
        )
        if row_id is None:
            log.warning("pending_store.add_ocr returned None — skipping prompt")
            continue
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"📄 OCR 추가 ({remaining}p, ~₩{cost_est})",
                callback_data=f"ocr:{row_id}:go",
            )],
            [InlineKeyboardButton(
                f"📝 텍스트만 유지 ({applied}p)" if applied > 0
                else "📝 텍스트만 유지",
                callback_data=f"ocr:{row_id}:skip",
            )],
            [InlineKeyboardButton(
                "🚫 학습 취소 (문서 삭제)",
                callback_data=f"ocr:{row_id}:forget",
            )],
        ])
        title_short = (r.get("title") or "PDF")[:80]
        status_line = (
            f"총 {total}p 중 {applied}p OCR 완료 + PyMuPDF 텍스트"
            if applied > 0 else
            f"총 {total}p — 텍스트만 추출됨 (OCR 0p)"
        )
        await ctx.bot.send_message(
            chat_id,
            f"📊 {title_short}\n"
            f"{status_line}.\n"
            f"버튼은 만료 없음 — 언제든 선택 가능. /pending 에서도 확인.",
            reply_markup=kb,
        )


async def on_ocr_extend_callback(update: Update,
                                 ctx: ContextTypes.DEFAULT_TYPE):
    """Three-button OCR prompt handler — actions keyed on the
    persistent pending_store row id (callback_data 'ocr:<row_id>:<go|skip|forget>').
    Prompts never expire; rows survive bot restart.

      go     → extend Vision OCR to the rest of the doc, then delete
                the pending row
      skip   → close the prompt, keep the doc as-is (text-only learn)
      forget → delete the doc from meta + vector (cancel the learn)
    """
    q = update.callback_query
    if not q:
        return
    user = q.from_user
    if not user or user.id != config.TELEGRAM_OWNER_ID:
        await q.answer("권한 없음")
        return
    await q.answer()
    parts = q.data.split(":")
    if len(parts) != 3 or parts[0] != "ocr":
        return
    _, row_id_str, decision = parts
    try:
        row_id = int(row_id_str)
    except ValueError:
        return

    state = await asyncio.to_thread(pending_store.get_ocr, row_id)
    if not state:
        try:
            await q.edit_message_text(
                "⚠️ 보류 항목이 이미 처리됐거나 삭제됐습니다."
            )
        except Exception:
            pass
        return

    orig_chat_id = q.message.chat.id
    orig_msg_id = q.message.message_id
    orig_text = q.message.text or ""
    title_short = (state.get("title") or "PDF")[:80]

    if decision == "skip":
        await asyncio.to_thread(pending_store.delete_ocr, row_id)
        try:
            await q.edit_message_text(
                orig_text + "\n\n→ 📝 텍스트만 유지 (OCR 추가 안 함)"
            )
        except Exception:
            pass
        return

    if decision == "forget":
        # Full doc removal: meta row + every Chroma chunk. Same path
        # /forget uses internally, just one-tap from the prompt.
        doc_id = state.get("doc_id") or ""
        chunks_removed = 0
        try:
            chunks_removed = await asyncio.to_thread(vector.delete_doc, doc_id)
            await asyncio.to_thread(meta.delete, doc_id)
        except Exception:
            log.exception("ocr-prompt forget failed for doc_id=%s", doc_id)
        await asyncio.to_thread(pending_store.delete_ocr, row_id)
        try:
            await q.edit_message_text(
                f"{orig_text}\n\n"
                f"→ 🚫 학습 취소됨: {title_short} "
                f"(청크 {chunks_removed}개 제거)"
            )
        except Exception:
            pass
        return

    if decision != "go":
        return

    # 'go' path: extend OCR for remaining pages, then drop the row.
    pdf_path = state.get("pdf_path") or ""
    start_page = int(state.get("applied_pages") or 0) + 1
    end_page = int(state.get("total_pages") or 0)
    try:
        await q.edit_message_text(
            orig_text +
            f"\n\n→ 📄 {start_page}-{end_page}p OCR 진행 중..."
        )
    except Exception:
        pass
    pressure = _mem_pressure()
    if pressure >= _MEM_REFUSE_THRESHOLD:
        await q.message.reply_text(
            f"⚠️ 메모리 부족 — {pressure*100:.0f}% 사용 중. 잠시 후 다시 시도."
        )
        return
    if pressure >= _MEM_CLEANUP_THRESHOLD:
        try:
            _run_memory_cleanup(f"pre-ocr {pressure*100:.0f}%")
        except Exception:
            pass
    if not pdf_path or not Path(pdf_path).exists():
        await q.message.reply_text(
            "⚠️ 원본 PDF 파일을 찾을 수 없습니다 (자동 정리됐을 수 있음). "
            "동일 PDF를 다시 보내주세요."
        )
        await asyncio.to_thread(pending_store.delete_ocr, row_id)
        return
    typing_task = asyncio.create_task(_sustained_typing(update, ctx))
    try:
        try:
            r = await pipeline.extend_pdf_ocr(
                Path(pdf_path), state["doc_id"], start_page, end_page,
            )
        except Exception as e:
            log.exception("extend_pdf_ocr failed")
            await q.message.reply_text(f"⚠️ OCR 확장 실패: {_explain_error(e)}")
            return
    finally:
        typing_task.cancel()
    await asyncio.to_thread(pending_store.delete_ocr, row_id)
    if r.get("status") == "ok":
        skip_note = (f" · {r['pages_skipped']}p 텍스트 충분 스킵"
                     if r.get("pages_skipped") else "")
        final_text = (
            f"{orig_text}\n\n"
            f"→ ✅ {start_page}-{end_page}p OCR 완료 "
            f"(+{r['pages_ocrd']}p Vision{skip_note} · "
            f"+{r['chunks_added']} 청크)"
        )
    elif r.get("status") == "empty":
        skipped = r.get("pages_skipped", 0)
        if skipped:
            final_text = (
                f"{orig_text}\n\n"
                f"→ ✅ {skipped}p 모두 텍스트 충분 → OCR 스킵 "
                f"(추가 청크 없음, 비용 0)"
            )
        else:
            final_text = f"{orig_text}\n\n→ ⚠️ OCR 결과 없음: {title_short}"
    else:
        final_text = f"{orig_text}\n\n→ ⚠️ OCR 결과 없음: {title_short}"
    await _edit_or_send(ctx, orig_chat_id, orig_msg_id, final_text)


async def on_pro_confirmation_callback(update: Update,
                                       ctx: ContextTypes.DEFAULT_TYPE):
    """Resume the agent run when the user picks Pro/Flash/Cancel."""
    global _ACTIVE_AGENT_RUNS, _LAST_REPLY_AT
    q = update.callback_query
    if not q:
        return
    user = q.from_user
    if not user or user.id != config.TELEGRAM_OWNER_ID:
        await q.answer("권한 없음")
        return
    await q.answer()
    parts = q.data.split(":")
    if len(parts) != 3 or parts[0] != "pro":
        return
    _, short, decision = parts
    state_id = _PENDING_SHORT_TO_FULL.pop(short, None) or short
    if decision not in ("pro", "flash", "cancel"):
        return
    label = {"pro": "💎 Pro 합성", "flash": "⚡ Flash 25개",
             "cancel": "❌ 취소"}[decision]
    try:
        await q.edit_message_text(
            (q.message.text or "") + f"\n\n→ {label} 진행 중..."
        )
    except Exception:
        pass
    # Same memory + counter discipline as _run_agent.
    pressure = _mem_pressure()
    if pressure >= _MEM_REFUSE_THRESHOLD:
        await q.message.reply_text(
            f"⚠️ 메모리 부족 — 현재 {pressure*100:.0f}% 사용 중. "
            "잠시 후 다시 질문해주세요."
        )
        return
    if pressure >= _MEM_CLEANUP_THRESHOLD:
        try:
            _run_memory_cleanup(f"pre-resume {pressure*100:.0f}%")
        except Exception:
            log.exception("threshold cleanup failed")
    _ACTIVE_AGENT_RUNS += 1
    typing_task = asyncio.create_task(_sustained_typing(update, ctx))
    try:
        try:
            result = await agent.resume(state_id, decision)
        except Exception as e:
            log.exception("agent resume failed")
            await q.message.reply_text(f"⚠️ {_explain_error(e)}")
            return
    finally:
        typing_task.cancel()
    chat_id = q.message.chat.id
    text = result.get("query") or ""
    try:
        await _finalize_agent_reply(q.message, ctx, chat_id, text, result)
    finally:
        _ACTIVE_AGENT_RUNS = max(0, _ACTIVE_AGENT_RUNS - 1)
        _LAST_REPLY_AT = datetime.utcnow()


def _explain_error(e: BaseException, max_len: int = 280) -> str:
    """Pretty-print an exception with type + first line of message."""
    cause = e
    while getattr(cause, "__cause__", None):
        cause = cause.__cause__
    msg = str(cause).strip().splitlines()[0] if str(cause).strip() else "(no message)"
    return f"{type(cause).__name__}: {msg}"[:max_len]


_INGEST_TIMEOUT_SEC = 600  # 10 minutes per message — sized for the
# Gemini-embed era. Earlier 15min was for BGE-M3 CPU embedding which is
# no longer used; current pipeline (Vision OCR ≤7 pages × ~3s, Flash-Lite
# summary ~5s, Gemini embed ~3s) finishes well under 5 min, so 10 min
# is conservative enough for unusual edge cases without pinning slots
# on truly stuck items.


_LIVE_EDIT_INTERVAL = 30  # seconds between status edits. 30 s × 4 concurrent
# = 8 edits/min, comfortable under Telegram's per-bot floor. Earlier 10 s
# value triggered today's 22207 s flood ban when combined with success +
# duplicate spam.


async def _edit_or_send(ctx, chat_id: int, msg_id: int | None, text: str) -> None:
    """Edit msg_id in chat_id to `text`; fall back to a fresh send
    when no message id, or when the edit fails (rate limit, message
    too old, etc.). Belt-and-suspenders so a failed edit never leaves
    the user without a final status."""
    if msg_id:
        try:
            await ctx.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=text,
                disable_web_page_preview=True,
            )
            return
        except Exception:
            log.warning("edit failed; falling back to send")
    try:
        await ctx.bot.send_message(chat_id, text, disable_web_page_preview=True)
    except Exception:
        log.exception("fallback send failed")


async def _live_status_updater(ctx, chat_id: int, msg_id: int,
                               label: str, job_id: str) -> None:
    """Re-render the ⏳ status message every _LIVE_EDIT_INTERVAL
    seconds so the user sees elapsed time tick forward.

    Defensive vs the 22207s flood-ban incident:
    1. 30 s cadence (vs prior 10 s) — 4 concurrent ingests = ~8
       edits/min, well under the per-bot floor.
    2. Circuit breaker — on a RetryAfter exception we stop editing
       for this job entirely, so a flood-control event can't recur
       across the still-active updater tasks."""
    short_label = label[:80]
    while True:
        try:
            await asyncio.sleep(_LIVE_EDIT_INTERVAL)
        except asyncio.CancelledError:
            return
        info = _ACTIVE_INGESTS.get(job_id)
        if not info:
            return
        elapsed = time.time() - info.get("started_at", time.time())
        stage = info.get("stage") or "처리 중"
        try:
            await ctx.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=f"⏳ {short_label}\n   {stage} ({_fmt_elapsed(elapsed)})",
            )
        except asyncio.CancelledError:
            return
        except Exception as e:
            # RetryAfter (flood control) or any other Telegram error —
            # stop editing this bubble. The final result post still
            # tries once at completion; that single attempt is far less
            # likely to compound a ban than a long-running 30s loop.
            if "RetryAfter" in type(e).__name__ or "Flood" in str(e):
                log.warning("live status updater hit flood control, stopping for %s", label[:40])
                return
            # Other errors (message too old etc) — swallow and continue.


async def _ingest_message(msg, ctx: ContextTypes.DEFAULT_TYPE, notify_chat_id: int):
    """Cap concurrent ingests via semaphore + per-message timeout
    + per-message live status bubble.

    Up to 2 messages run in parallel via the semaphore; the rest
    wait. The 15-min timeout prevents one stuck PDF from hanging
    the whole bot. While work runs we keep editing a single status
    message instead of going silent, so the user sees the ingest
    is alive."""
    async with _INGEST_SEM:
        kind, label = _ingest_label_from_msg(msg)
        job_id = _register_ingest(label, kind, notify_chat_id)

        # Per-job stage callback — pipeline calls it at each pipeline
        # phase (load → OCR → summary+embed → save), bot updates the
        # _ACTIVE_INGESTS slot, and _live_status_updater shows the
        # current stage in the ⏳ bubble next refresh.
        def _stage_cb(name: str) -> None:
            slot = _ACTIVE_INGESTS.get(job_id)
            if slot is not None:
                slot["stage"] = name

        status_msg_id: int | None = None
        try:
            sent = await ctx.bot.send_message(
                notify_chat_id,
                f"⏳ 학습 시작: {label[:80]}",
            )
            status_msg_id = sent.message_id
            _ACTIVE_INGESTS[job_id]["status_msg_id"] = status_msg_id
            _track_bubble(notify_chat_id, status_msg_id, label)
        except Exception:
            log.exception("status start message failed")

        updater_task = None
        if status_msg_id:
            updater_task = asyncio.create_task(
                _live_status_updater(
                    ctx, notify_chat_id, status_msg_id, label, job_id,
                )
            )

        results: list[dict] | None = None
        timed_out = False
        try:
            results = await asyncio.wait_for(
                _ingest_message_locked(msg, ctx, notify_chat_id, on_stage=_stage_cb),
                timeout=_INGEST_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            timed_out = True
            log.warning("ingest timeout (%ds) for %s", _INGEST_TIMEOUT_SEC, label)
            _record_failure("timeout", label,
                            f"ingest exceeded {_INGEST_TIMEOUT_SEC}s")
        finally:
            if updater_task:
                updater_task.cancel()
                try:
                    await updater_task
                except asyncio.CancelledError:
                    pass
            _unregister_ingest(job_id)
            _untrack_bubble(notify_chat_id, status_msg_id)

        # Render the final state into the live message via edit so the
        # ⏳ bubble becomes the result bubble in place — no scroll-down
        # cleanup needed.
        if timed_out:
            final_text = (
                f"⚠️ ingest timeout (15분 초과): {label[:60]}\n"
                "자료가 너무 크거나 OCR 처리 지연. 같은 자료 다시 보내면 재시도됩니다."
            )
        elif results:
            final_text = _format_results(results)
        else:
            final_text = f"(빈 결과: {label[:60]})"

        sent_ok = False
        if status_msg_id:
            try:
                await ctx.bot.edit_message_text(
                    chat_id=notify_chat_id, message_id=status_msg_id,
                    text=final_text, disable_web_page_preview=True,
                )
                sent_ok = True
            except Exception:
                # Edit can fail (too old, network) — fall back to new send.
                log.warning("status final edit failed; sending fresh")
        if not sent_ok:
            try:
                await ctx.bot.send_message(notify_chat_id, final_text)
            except Exception:
                log.exception("ingest result notify failed")

        # OCR-extend prompts run after the final result is visible.
        if results:
            try:
                await _send_ocr_extend_prompts(ctx, notify_chat_id, results)
            except Exception:
                log.exception("ocr extend prompts failed")

        return results


async def _ingest_doc_attachment(msg, ctx: ContextTypes.DEFAULT_TYPE,
                                 on_stage=None) -> dict:
    if on_stage:
        on_stage("파일 다운로드")
    file = await ctx.bot.get_file(msg.document.file_id)
    dest = Path(config.DATA_DIR) / "files" / msg.document.file_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    await file.download_to_drive(custom_path=dest)
    label = f"tg-doc:{msg.document.file_unique_id}:{msg.document.file_name}"
    suffix = dest.suffix.lower()
    if suffix == ".pdf":
        return await pipeline.ingest_pdf(dest, label, on_stage=on_stage)
    if suffix == ".pptx":
        return await pipeline.ingest_pptx(dest, label, on_stage=on_stage)
    if suffix == ".docx":
        return await pipeline.ingest_docx(dest, label, on_stage=on_stage)
    if suffix == ".xlsx":
        return await pipeline.ingest_xlsx(dest, label, on_stage=on_stage)
    if suffix in _AUDIO_SUFFIX_MIME:
        return await pipeline.ingest_audio(
            dest.read_bytes(), label,
            caption=msg.caption or "",
            mime_type=_AUDIO_SUFFIX_MIME[suffix],
        )
    if suffix in {".ppt", ".doc", ".xls"}:
        fname = (msg.document.file_name or dest.name)
        return {
            "status": "error",
            "title": fname,
            "error": (
                f"{fname} — {suffix} 구버전 포맷 지원 안 됨. "
                f"{suffix}x로 변환해서 다시 보내주세요."
            ),
        }
    if suffix in {".txt", ".md", ".csv"}:
        fname = (msg.document.file_name or dest.name)
        return {
            "status": "skipped",
            "title": fname,
            "detail": (
                f"{fname} — {suffix} 첨부는 학습 대상에서 제외됩니다. "
                f"필요한 내용만 메시지로 직접 붙여넣어 주세요."
            ),
        }
    content = dest.read_text(encoding="utf-8", errors="ignore")
    return await pipeline.ingest_text(content, label)


_AUDIO_SUFFIX_MIME = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
}

_IMAGE_SUFFIX_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# Whitelist for orphan recovery + retry-queue dedup. Anything not in
# here just stays on disk and never enters the queue, so a stray
# .ppt / .ipynb / .zip can't burn cycles failing in a loop.
# .txt / .md / .csv removed (2026-05): plaintext attachments rarely
# carry the kind of structured analytical content the user wants
# searchable, and the few that do can be pasted as message text or
# learned via /ingest_url. Existing .txt files on disk simply stop
# matching the orphan scan and get ignored.
_SUPPORTED_INGEST_SUFFIXES: frozenset[str] = frozenset({
    ".pdf", ".pptx", ".docx", ".xlsx",
    *_AUDIO_SUFFIX_MIME.keys(),
    *_IMAGE_SUFFIX_MIME.keys(),
})


async def _ingest_voice_attachment(msg, ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    """Telegram voice note (msg.voice) — OGG/Opus."""
    import io
    voice = msg.voice
    file = await ctx.bot.get_file(voice.file_id)
    bio = io.BytesIO()
    await file.download_to_memory(out=bio)
    label = f"tg-voice:{voice.file_unique_id}"
    return await pipeline.ingest_audio(
        bio.getvalue(), label, caption=msg.caption or "",
        mime_type=voice.mime_type or "audio/ogg",
    )


async def _ingest_audio_attachment(msg, ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    """Telegram audio (msg.audio) — uploaded music/audio file."""
    import io
    audio = msg.audio
    file = await ctx.bot.get_file(audio.file_id)
    bio = io.BytesIO()
    await file.download_to_memory(out=bio)
    label = f"tg-audio:{audio.file_unique_id}"
    title_hint = (audio.title or audio.file_name or "").strip()
    caption_part = (msg.caption or "").strip()
    full_caption = "\n".join(p for p in [title_hint, caption_part] if p)
    return await pipeline.ingest_audio(
        bio.getvalue(), label, caption=full_caption,
        mime_type=audio.mime_type or "audio/mpeg",
    )


async def _ingest_photo_attachment(msg, ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    """Standalone photo (screenshot, table capture). Caption ≥80 chars
    skips OCR; otherwise Gemini Vision extracts text."""
    import io
    photo = msg.photo[-1]  # largest size
    file = await ctx.bot.get_file(photo.file_id)
    bio = io.BytesIO()
    await file.download_to_memory(out=bio)
    label = f"tg-photo:{photo.file_unique_id}"
    return await pipeline.ingest_image(
        bio.getvalue(), label, caption=msg.caption or "",
        mime_type="image/jpeg",
    )


def _is_retryable(e: BaseException) -> bool:
    s = f"{type(e).__name__} {e}"
    return any(m in s for m in (
        *_OVERLOAD_MARKERS,
        "Timeout", "ReadError", "ConnectError", "RemoteProtocolError",
    ))


async def _ingest_message_locked(msg, ctx: ContextTypes.DEFAULT_TYPE,
                                 notify_chat_id: int, on_stage=None):
    text = msg.text or msg.caption or ""
    results = []

    if msg.document:
        doc_item = _enqueue_with_inflight({
            "kind": "doc",
            "file_id": msg.document.file_id,
            "file_unique_id": msg.document.file_unique_id,
            "file_name": msg.document.file_name,
            "chat_id": notify_chat_id,
        })
        try:
            results.append(await _ingest_doc_attachment(msg, ctx, on_stage=on_stage))
            _finish_inflight(doc_item, "done")
        except Exception as e:
            log.exception("file ingest failed")
            if _is_retryable(e):
                _finish_inflight(doc_item, "retry")
                results.append({"status": "queued",
                                "title": msg.document.file_name})
            else:
                _finish_inflight(doc_item, "done")
                results.append({"status": "error", "error": _explain_error(e)})

        # Korean analyst commentary often ships as a long caption above
        # the attached IR PDF/XLSX. Keep it as a separate doc so search
        # can match the analysis even when the PDF body is in English.
        cap = (msg.caption or "").strip()
        if len(cap) >= 200:
            cap_retry = {
                "kind": "text",
                "text": cap,
                "label": f"tg-doc-caption:{msg.message_id}",
            }
            cap_item = _enqueue_with_inflight(
                {**cap_retry, "chat_id": notify_chat_id}
            )
            try:
                rcap = await pipeline.ingest_text(
                    cap, f"tg-doc-caption:{msg.message_id}",
                )
                if rcap.get("status") in ("empty", "error"):
                    rcap["retry_payload"] = cap_retry
                results.append(rcap)
                _finish_inflight(cap_item, "done")
            except Exception as e:
                log.exception("doc caption ingest failed")
                if _is_retryable(e):
                    _finish_inflight(cap_item, "retry")
                    results.append({"status": "queued", "title": cap[:60]})
                else:
                    _finish_inflight(cap_item, "done")
                    results.append({"status": "error",
                                    "error": _explain_error(e),
                                    "retry_payload": cap_retry})

    if msg.photo:
        photo = msg.photo[-1]
        photo_retry = {
            "kind": "photo",
            "file_id": photo.file_id,
            "file_unique_id": photo.file_unique_id,
            "caption": msg.caption or "",
        }
        photo_item = _enqueue_with_inflight(
            {**photo_retry, "chat_id": notify_chat_id}
        )
        try:
            r = await _ingest_photo_attachment(msg, ctx)
            if r.get("status") in ("empty", "error"):
                r["retry_payload"] = photo_retry
            results.append(r)
            _finish_inflight(photo_item, "done")
        except Exception as e:
            log.exception("photo ingest failed")
            if _is_retryable(e):
                _finish_inflight(photo_item, "retry")
                results.append({"status": "queued", "title": "photo"})
            else:
                _finish_inflight(photo_item, "done")
                results.append({"status": "error",
                                "error": _explain_error(e),
                                "retry_payload": photo_retry})

    if msg.voice:
        voice = msg.voice
        voice_retry = {
            "kind": "voice",
            "file_id": voice.file_id,
            "file_unique_id": voice.file_unique_id,
            "mime_type": voice.mime_type or "audio/ogg",
            "caption": msg.caption or "",
        }
        voice_item = _enqueue_with_inflight(
            {**voice_retry, "chat_id": notify_chat_id}
        )
        try:
            r = await _ingest_voice_attachment(msg, ctx)
            if r.get("status") in ("empty", "error"):
                r["retry_payload"] = voice_retry
            results.append(r)
            _finish_inflight(voice_item, "done")
        except Exception as e:
            log.exception("voice ingest failed")
            if _is_retryable(e):
                _finish_inflight(voice_item, "retry")
                results.append({"status": "queued", "title": "voice"})
            else:
                _finish_inflight(voice_item, "done")
                results.append({"status": "error",
                                "error": _explain_error(e),
                                "retry_payload": voice_retry})

    if msg.audio:
        audio = msg.audio
        audio_retry = {
            "kind": "audio",
            "file_id": audio.file_id,
            "file_unique_id": audio.file_unique_id,
            "file_name": audio.file_name or "",
            "title": audio.title or "",
            "mime_type": audio.mime_type or "audio/mpeg",
            "caption": msg.caption or "",
        }
        audio_item = _enqueue_with_inflight(
            {**audio_retry, "chat_id": notify_chat_id}
        )
        try:
            r = await _ingest_audio_attachment(msg, ctx)
            if r.get("status") in ("empty", "error"):
                r["retry_payload"] = audio_retry
            results.append(r)
            _finish_inflight(audio_item, "done")
        except Exception as e:
            log.exception("audio ingest failed")
            if _is_retryable(e):
                _finish_inflight(audio_item, "retry")
                results.append({"status": "queued",
                                "title": audio.file_name or "audio"})
            else:
                _finish_inflight(audio_item, "done")
                results.append({"status": "error",
                                "error": _explain_error(e),
                                "retry_payload": audio_retry})

    urls, plain = _collect_message_urls(msg)
    for url in urls:
        # Skip URLs the user explicitly cleared via /failed_clear
        # (permanently ignored) OR URLs that have already burned
        # through their retry budget — paywalled domains stay broken,
        # no point spending another 5 attempts.
        if _is_ignored_url(url):
            log.info("url skip — permanently ignored: %s", url[:120])
            results.append({"status": "skipped", "title": url,
                            "source": url,
                            "detail": "영구 무시 URL (/failed_clear)"})
            continue
        if _url_in_failed_log(url):
            log.info("url skip — previously failed: %s", url[:120])
            results.append({"status": "skipped", "title": url,
                            "source": url,
                            "detail": "이전에 실패한 URL — 자동 skip"})
            continue
        url_retry = {"kind": "url", "url": url}
        url_item = _enqueue_with_inflight(
            {**url_retry, "chat_id": notify_chat_id}
        )
        try:
            r = await pipeline.ingest_url(url)
            r.setdefault("source", url)
            r.setdefault("title", url)
            if r.get("status") in ("empty", "error"):
                r["retry_payload"] = url_retry
            results.append(r)
            _finish_inflight(url_item, "done")
        except Exception as e:
            log.exception("url ingest failed: %s", url)
            if _is_retryable(e):
                _finish_inflight(url_item, "retry")
                results.append({"status": "queued", "title": url})
            else:
                _finish_inflight(url_item, "done")
                results.append({"status": "error",
                                "error": _explain_error(e),
                                "source": url, "retry_payload": url_retry})

    if (plain and not msg.document and not msg.photo
            and not msg.voice and not msg.audio and len(plain) >= 80):
        text_retry = {
            "kind": "text",
            "text": plain,
            "label": f"tg-msg:{msg.message_id}",
        }
        text_item = _enqueue_with_inflight(
            {**text_retry, "chat_id": notify_chat_id}
        )
        try:
            r = await pipeline.ingest_text(plain, f"tg-msg:{msg.message_id}")
            if r.get("status") in ("empty", "error"):
                r["retry_payload"] = text_retry
            results.append(r)
            _finish_inflight(text_item, "done")
        except Exception as e:
            log.exception("text ingest failed")
            if _is_retryable(e):
                _finish_inflight(text_item, "retry")
                results.append({"status": "queued", "title": plain[:60]})
            else:
                _finish_inflight(text_item, "done")
                results.append({"status": "error",
                                "error": _explain_error(e),
                                "retry_payload": text_retry})

    # Caller (_ingest_message) owns the live-edit status message and
    # the OCR-extend prompts now — return results so the orchestrator
    # can render them in the single edited bubble instead of as a
    # second message.
    return results


def _record_failure(status: str, title: str, detail: str = "",
                    retry_payload: dict | None = None) -> None:
    """Append to in-memory failure log + persist so /failed survives
    restart. retry_payload (kind/file_id/url/text/...) lets
    /failed retry re-enqueue the item automatically.

    Tracks `failed_cycles` per item across /failed_retry round trips:
    cycle 1 = first time it landed here, cycle 2 = it came back after
    a manual retry, cycle 3 = same. After cycle 3 we drop the entry
    entirely so the user doesn't have to /failed_clear paywalled or
    permanently-404'd URLs by hand."""
    prior_cycles = 0
    if retry_payload:
        try:
            prior_cycles = int(retry_payload.get("failed_cycles") or 0)
        except (TypeError, ValueError):
            prior_cycles = 0
    new_cycles = prior_cycles + 1
    if new_cycles > _FAILED_MAX_CYCLES:
        log.info(
            "failed-log drop after %d cycles: %s (%s)",
            prior_cycles, (title or "")[:60], (detail or "")[:60],
        )
        return
    entry: dict = {
        "status": status,
        "title": (title or "(unknown)")[:140],
        "detail": (detail or "")[:200],
        "ts": datetime.utcnow().isoformat(),
        "failed_cycles": new_cycles,
    }
    # Record file size if available so /failed can sort smallest-first.
    # Big files (multi-MB OCR-heavy PDFs, MP4 videos) tend to bury the
    # quick-to-process items the user actually wants to triage first.
    file_size = 0
    if retry_payload:
        path = retry_payload.get("path") or retry_payload.get("file_path")
        if path:
            try:
                file_size = Path(path).stat().st_size
            except Exception:
                file_size = 0
    entry["file_size"] = file_size
    if retry_payload:
        # Stash the latest count inside the payload so /failed_retry
        # round-trips it back to us next time.
        retry_payload = dict(retry_payload)
        retry_payload["failed_cycles"] = new_cycles
        entry["retry"] = retry_payload
    _INGEST_FAILED.append(entry)
    if len(_INGEST_FAILED) > _FAILED_MAX:
        del _INGEST_FAILED[0]
    _persist_failed_log()


def _url_in_failed_log(url: str) -> bool:
    """Has this exact URL already landed in /failed? Used to short-circuit
    repeated tries on the same paywalled / blocked link — digests often
    cite the same Reuters/Bloomberg article from many sections, and each
    citation otherwise spawns its own 5-attempt cycle to /failed."""
    if not url:
        return False
    for e in _INGEST_FAILED:
        payload = e.get("retry") or {}
        if payload.get("kind") == "url" and payload.get("url") == url:
            return True
        # Pre-retry-payload entries stored the URL only in title.
        if e.get("title") == url:
            return True
    return False


def _empty_url_guidance(source: str) -> str:
    """Suggest a manual recovery path for URLs the bot can't crawl
    (auth wall, IP block, JS-only, shortener that points at a
    restricted PDF). Shown right under the ⚠️ 본문 비어있음 line."""
    s = (source or "").lower()
    if any(d in s for d in (
        "linkedin.com", "facebook.com", "story.kakao.com", "instagram.com",
    )):
        return (
            "🔒 인증 차단 사이트입니다.\n"
            "  • 글 열어 본문 복사 → 봇 DM에 붙여넣기 (200자+ 자동 학습)\n"
            "  • 또는 스크린샷 → 봇에 이미지로 (caption 없이) 보내기"
        )
    if any(d in s for d in (
        "reuters.com", "bloomberg.com", "ft.com", "wsj.com",
        "economist.com", "nytimes.com", "washingtonpost.com", "barrons.com",
    )):
        return (
            "🔒 영문 paywall 사이트입니다.\n"
            "  • 본문 복사 → 봇 DM에 붙여넣기 (구독 중이면 가능)\n"
            "  • 또는 같은 뉴스 다루는 한국 매체 URL을 대신 전송\n"
            "  • 또는 스크린샷 → 이미지로 보내기 (OCR 자동)"
        )
    if "x.com" in s or "twitter.com" in s:
        return (
            "🔒 X(Twitter) 본문 추출 어려움.\n"
            "  • 트윗 본문 복사 → 봇 DM에 붙여넣기\n"
            "  • 또는 스크린샷 → 이미지로 보내기 (OCR 자동)"
        )
    if any(d in s for d in (
        "buly.kr", "vo.la", "zrr.kr", "bit.ly", "shinhansec.com",
        "kiwoom.com", "nh-securities.com",
    )):
        return (
            "🔒 단축 URL 또는 인증 필요한 호스트.\n"
            "  • 브라우저에서 URL 열어 PDF 다운로드 → 봇에 첨부\n"
            "  • 또는 본문 텍스트 복사 → 봇 DM에 붙여넣기"
        )
    return (
        "🔒 본문 추출 실패.\n"
        "  • URL 열어 본문 복사 → 봇 DM에 붙여넣기 (200자+)\n"
        "  • 또는 스크린샷 → 이미지로 보내기 (OCR 자동)"
    )


def _format_results(results: list[dict]) -> str:
    lines = []
    for r in results:
        s = r.get("status")
        if s == "ok":
            lines.append(f"✅ {r['title']}  ({r['type']}, {r['chunks']} chunks)")
        elif s == "duplicate":
            lines.append(f"♻️ 이미 있음: {r['title']}")
        elif s in ("blocked", "skipped"):
            # Blocked host (paywall / X / shortener / forum board) or
            # drop-pattern match (알파 스캐너 등 narrative-free auto-
            # generated formats). These are KNOWN-uningestable, not
            # transient failures, so don't pile them up in /failed —
            # just log the skip and move on.
            log.info("ingest %s: %s — %s",
                     s, r.get("title", "")[:80], r.get("detail", ""))
            continue
        elif s == "empty":
            title = r.get("title", "")
            src = r.get("source", "") or title
            # URL이 source에 있으면 그걸 우선 노출 (사용자가 어떤 자료인지
            # 즉시 알 수 있게). title이 'XX증권' 같은 짧은 페이지 제목이면
            # 정보가 부족하므로 URL을 같이 보여줌.
            if src.startswith(("http://", "https://")):
                shown = src
                if title and title != src and title not in src:
                    shown = f"{title}\n   {src}"
                # URL extraction failures park in pending_url_decisions
                # instead of /failed. The drain scheduler asks the
                # user — when the queue is idle — to retry or block.
                # Replaces the auto-burial that made the user feel
                # like URLs disappeared without input.
                pending_url_decisions.add(
                    src, title or "", r.get("detail") or "본문 비어있음"
                )
            else:
                shown = title or src
                _record_failure("empty", title or src, src,
                                retry_payload=r.get("retry_payload"))
            line = f"⚠️ 본문 비어있음: {shown}"
            guidance = _empty_url_guidance(src)
            if guidance:
                line += "\n" + guidance
            lines.append(line)
        elif s == "queued":
            lines.append(f"⏳ 재시도 대기 (자동): {r.get('title', '')}")
        else:
            label = r.get("title") or r.get("source", "")
            _record_failure("error", label, r.get("error", ""),
                            retry_payload=r.get("retry_payload"))
            lines.append(f"❌ {r.get('error', 'error')}")
    return "\n".join(lines)


async def _promote_expired_pending(ctx: ContextTypes.DEFAULT_TYPE):
    """OCR side: nothing to promote anymore — prompts now write
    directly into pending_store (never expire). Only Pro
    confirmation prompts still use a TTL'd in-memory dict, so they
    still get demoted here."""
    from .agent import agent as agent_mod
    try:
        pro_expired = agent_mod.gc_expired_pending()
        for state in pro_expired:
            try:
                pending_store.add_pro(
                    chat_id=config.TELEGRAM_OWNER_ID,
                    question=str(state.get("message") or ""),
                    count=int(state.get("compare_papers_count", 0) or 0),
                )
            except Exception:
                log.exception("promote pro to pending failed")
        if pro_expired:
            log.info("promoted %d Pro prompts to /pending", len(pro_expired))
    except Exception:
        log.exception("Pro pending promotion failed")


async def _periodic_memory_cleanup(ctx: ContextTypes.DEFAULT_TYPE):
    """Idle-only gc + malloc_trim every 5min. Skip when agent runs
    are active so we don't steal CPU from in-flight answers; the
    pre-run threshold guard handles the busy-but-pressured case
    separately."""
    if _ACTIVE_AGENT_RUNS > 0:
        return
    try:
        _run_memory_cleanup("periodic")
    except Exception:
        log.exception("periodic memory cleanup failed")


async def _refresh_dashboard(ctx: ContextTypes.DEFAULT_TYPE):
    """Regenerate the static dashboard HTML on a tick so ingest-only
    activity (no Q&As happening) still shows up in the totals."""
    try:
        from .dashboard import regenerate as dashboard_regen
        dashboard_regen.regenerate()
    except Exception:
        log.exception("scheduled dashboard refresh failed")


_PADDLE_RELEASE_PATH = config.DATA_DIR / ".paddle_last_seen"
_PADDLE_BASELINE = "v3.3.1"  # the broken version we shipped against


def _parse_semver(tag: str) -> tuple[int, ...]:
    """Parse 'v3.3.1' / '3.3.1' / 'v3.3.1-rc1' into (3, 3, 1).
    Pre-release suffixes (rc/beta/alpha) are stripped so the
    numeric comparison still works. Returns (0,) on unparseable
    input so a weird tag is treated as 'older than anything' —
    safer than firing a false positive."""
    if not tag:
        return (0,)
    s = tag.strip().lstrip("vV")
    # cut anything past first non-digit-or-dot (e.g. '-rc1', 'b0')
    cleaned = []
    for ch in s:
        if ch.isdigit() or ch == ".":
            cleaned.append(ch)
        else:
            break
    s = "".join(cleaned).strip(".")
    if not s:
        return (0,)
    parts = []
    for p in s.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts) if parts else (0,)


async def _send_actionable_alert(ctx, notify_id: str, message: str,
                                  parse_mode: str | None = "HTML") -> None:
    """Send a Telegram message with a [✅ 확인 / 알람 정지] inline
    button and record it for daily resend until the user taps it.
    Standard pattern for any 'user must take action' notification —
    avoids silent miss when the user is away from Telegram on the
    day a one-shot alert fires."""
    from .store import notify_acks
    inserted = await asyncio.to_thread(
        notify_acks.record_pending, notify_id, message, parse_mode,
    )
    if not inserted:
        # Already pending from a previous check; resend loop will
        # handle it. Don't send again here or we double-fire.
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 확인 / 알람 정지",
                             callback_data=f"ack:{notify_id}"),
    ]])
    try:
        await ctx.bot.send_message(
            chat_id=config.TELEGRAM_OWNER_ID,
            text=message,
            parse_mode=parse_mode,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("actionable alert send failed (id=%s)", notify_id)


async def on_ack_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handler for the [✅ 확인 / 알람 정지] button. Flips the alert's
    acked state so the daily resend loop stops, and edits the
    message in-place to confirm."""
    q = update.callback_query
    if not q:
        return
    user = q.from_user
    if not user or user.id != config.TELEGRAM_OWNER_ID:
        await q.answer("권한 없음")
        return
    await q.answer()
    parts = (q.data or "").split(":", 1)
    if len(parts) != 2 or parts[0] != "ack":
        return
    notify_id = parts[1]
    from .store import notify_acks
    flipped = await asyncio.to_thread(notify_acks.mark_acked, notify_id)
    suffix = "\n\n→ ✅ 확인됨, 알람 중단" if flipped else "\n\n→ (이미 확인됨)"
    try:
        await q.edit_message_text(
            (q.message.text or "") + suffix,
            parse_mode=None,        # plain text after the user message
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("ack callback edit failed")


async def _resend_unacked_alerts(ctx: ContextTypes.DEFAULT_TYPE):
    """Hourly: any actionable alert that hasn't been acked within
    24h gets re-sent with the same [✅ 확인] button. Telegram dedups
    visually by the new send_message id so the user sees a fresh
    bubble each day until they tap."""
    from .store import notify_acks
    try:
        due = await asyncio.to_thread(notify_acks.list_due)
    except Exception:
        log.exception("notify_acks list_due failed")
        return
    for notify_id, rec in due:
        try:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ 확인 / 알람 정지",
                                     callback_data=f"ack:{notify_id}"),
            ]])
            await ctx.bot.send_message(
                chat_id=config.TELEGRAM_OWNER_ID,
                text=rec.get("message") or "(empty alert)",
                parse_mode=rec.get("parse_mode"),
                reply_markup=kb,
                disable_web_page_preview=True,
            )
            # Bump last_sent_at AFTER the successful send so a network
            # error keeps the alert eligible for retry next hour.
            await asyncio.to_thread(notify_acks.update_last_sent, notify_id)
        except Exception:
            log.exception("alert resend failed (id=%s)", notify_id)


_YT_DLP_RE_ARM_DAYS = int(os.getenv("YTDLP_HEALTH_REARM_DAYS", "7"))


async def _check_yt_dlp_health(ctx: ContextTypes.DEFAULT_TYPE):
    """Hourly: if yt-dlp failure rate in the last 24h crosses the
    threshold, fire an actionable alert telling the user to force-
    rebuild the bot image (which pulls a fresher yt-dlp from PyPI).

    Re-arm semantics: when the user acks the alert, we suppress
    further alerts using the same notify_id. After
    _YT_DLP_RE_ARM_DAYS days have passed since the ack AND the
    condition is still bad, we DELETE the acked record so the next
    _send_actionable_alert call inserts fresh and fires again.
    This handles the realistic timeline where YouTube breaks
    yt-dlp again months later — the user gets a fresh alert
    instead of being stuck with the old acked one suppressing
    everything forever."""
    from .store import yt_dlp_health, notify_acks
    try:
        if not await asyncio.to_thread(yt_dlp_health.is_unhealthy):
            return
        # Check existing alert state for the stable id.
        stable_id = "yt_dlp_health"
        existing = await asyncio.to_thread(notify_acks.get, stable_id)
        if existing and existing.get("acked"):
            ack_at = existing.get("ack_at") or ""
            try:
                ack_dt = datetime.fromisoformat(ack_at)
            except Exception:
                ack_dt = None
            if ack_dt and (datetime.utcnow() - ack_dt
                           < timedelta(days=_YT_DLP_RE_ARM_DAYS)):
                # Acked recently — suppress.
                return
            # Acked long ago and condition still bad → re-arm.
            await asyncio.to_thread(notify_acks.delete, stable_id)
            log.info("yt_dlp_health: re-arming alert (acked %s ago)",
                     ack_at)

        summary = await asyncio.to_thread(yt_dlp_health.status_summary)
        rate_pct = int(summary["rate"] * 100)
        msg = (
            f"⚠️ <b>yt-dlp 작동 이상</b>\n"
            f"최근 24시간 yt-dlp 실패율: <b>{rate_pct}%</b> "
            f"({summary['total']}회 시도 중)\n\n"
            f"YouTube가 내부 API를 바꾸면서 현재 핀된 yt-dlp 버전이 "
            f"못 따라가는 중. yt-dlp 팀이 보통 1-3일 내 fix release함.\n\n"
            f"해결 (둘 중 하나):\n"
            f"1. <code>docker compose up -d --build --force-recreate bot</code>\n"
            f"   → 매 빌드 시 pip가 PyPI에서 최신 yt-dlp 자동으로 받아옴\n"
            f"2. pyproject.toml 의 <code>yt-dlp&gt;=YYYY.MM.DD</code> 핀을 "
            f"새 버전으로 bump 후 push → auto_pull 이 알아서 rebuild\n\n"
            f"며칠 더 기다리고 1번 시도해도 됨. 그 사이 YouTube 학습은 "
            f"transcript_api 가 한 번씩 성공할 때만 들어가고 나머지는 "
            f"stub 으로 떨어져."
        )
        await _send_actionable_alert(
            ctx, stable_id, msg, parse_mode="HTML",
        )
    except Exception:
        log.exception("yt_dlp_health check failed")


async def _check_paddle_release(ctx: ContextTypes.DEFAULT_TYPE):
    """Weekly check for a new paddlepaddle release. We're stuck on
    v3.3.1 because of the PIR+oneDNN ConvertPirAttribute crash; when
    a newer release ships we want to know so the hybrid OCR worker
    can be re-attempted. Notifies via _send_actionable_alert so the
    message keeps re-sending daily until the user taps ✅. State
    files: .paddle_last_seen (dedup the github check) and
    notify_acks.json (dedup the user-facing alert)."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(
                "https://api.github.com/repos/PaddlePaddle/Paddle/releases/latest",
                headers={"User-Agent": "thesis-bot"},
            )
            r.raise_for_status()
            data = r.json()
        latest = (data.get("tag_name") or "").strip()
        if not latest:
            return

        last_seen = _PADDLE_BASELINE
        try:
            if _PADDLE_RELEASE_PATH.exists():
                last_seen = _PADDLE_RELEASE_PATH.read_text(
                    encoding="utf-8"
                ).strip() or _PADDLE_BASELINE
        except Exception:
            pass

        # github /releases/latest returns the MOST RECENTLY CREATED
        # release, which can be a back-port patch on an older branch
        # (e.g. v3.3.0 published AFTER v3.3.1). Only notify if the
        # new tag is strictly higher than BOTH the baseline AND the
        # last_seen — otherwise it's noise (and last_seen mustn't
        # regress either, or the next legit upgrade gets compared
        # against the wrong floor).
        latest_v = _parse_semver(latest)
        baseline_v = _parse_semver(_PADDLE_BASELINE)
        last_seen_v = _parse_semver(last_seen)
        target_v = max(baseline_v, last_seen_v)
        if latest_v <= target_v:
            log.info(
                "paddle release check: %s ≤ %s (baseline %s, last_seen %s) "
                "— skip notify",
                latest, ".".join(str(p) for p in target_v),
                _PADDLE_BASELINE, last_seen,
            )
            return

        # Record FIRST so a duplicate run doesn't double-fire the
        # actionable alert (notify_acks also dedups by id, belt+
        # suspenders).
        try:
            tmp = _PADDLE_RELEASE_PATH.with_suffix(
                _PADDLE_RELEASE_PATH.suffix + ".tmp"
            )
            tmp.write_text(latest, encoding="utf-8")
            tmp.replace(_PADDLE_RELEASE_PATH)
        except Exception:
            log.exception("paddle release state write failed")

        notes_url = data.get("html_url") or "https://github.com/PaddlePaddle/Paddle/releases"
        msg = (
            f"🔔 paddle 새 버전 감지: <b>{latest}</b>\n"
            f"이전 기록: {last_seen}\n\n"
            f"PIR+oneDNN 버그(ConvertPirAttribute2RuntimeAttribute)가 "
            f"fix됐는지 release notes 확인:\n{notes_url}\n\n"
            f"fix 됐으면 ocr-worker/Dockerfile FROM 태그를 "
            f"<code>paddlepaddle/paddle:{latest.lstrip('v')}</code> 로 bump "
            f"후 hybrid 재시도 가능."
        )
        await _send_actionable_alert(
            ctx,
            notify_id=f"paddle_{latest}",
            message=msg,
            parse_mode="HTML",
        )
    except Exception:
        log.exception("paddle release check failed")


async def _drain_pending_pro(ctx: ContextTypes.DEFAULT_TYPE):
    """One pending-Pro question per tick. Sends a ⏳ status, runs
    agent.run(deep=True), then ships the answer through the same
    chunked / qna-record / dashboard-regen pipeline as a live /deep.
    Re-queues if memory pressure refuses the run."""
    if not _PENDING_PRO_RUN_QUEUE:
        return
    global _ACTIVE_AGENT_RUNS, _LAST_REPLY_AT
    pressure = _mem_pressure()
    if pressure >= _MEM_REFUSE_THRESHOLD:
        log.info("pending pro: memory %.0f%%, deferring", pressure * 100)
        return
    if pressure >= _MEM_CLEANUP_THRESHOLD:
        try:
            _run_memory_cleanup(f"pre-pending-pro {pressure*100:.0f}%")
        except Exception:
            pass
    item = _PENDING_PRO_RUN_QUEUE.pop(0)
    chat_id = int(item["chat_id"])
    question = item["question"]
    _ACTIVE_AGENT_RUNS += 1
    try:
        try:
            sent = await ctx.bot.send_message(
                chat_id, f"⏳ [보류 승인] Pro 답변 시작: {question[:80]}",
            )
        except Exception:
            sent = None
        try:
            result = await agent.run(question, deep=True, history=[])
        except Exception as e:
            log.exception("pending pro agent.run failed")
            await _edit_or_send(
                ctx, chat_id, getattr(sent, "message_id", None),
                f"⚠️ Pro 답변 실패: {question[:60]}\n{_explain_error(e)}",
            )
            return
        async def _send(text):
            await ctx.bot.send_message(chat_id, text, disable_web_page_preview=True)
        try:
            body, mermaid_blocks = await _send_agent_reply(_send, result)
        except Exception:
            log.exception("pending pro reply render failed")
            return
        try:
            _record_turn(chat_id, "user", question)
            _record_turn(
                chat_id, "model", body,
                sources=result.get("sources") or [],
                tools=result.get("tool_calls") or [],
            )
            qna.record(
                chat_id=chat_id, question=question, answer=body,
                sources=result.get("sources") or [],
                tools=result.get("tool_calls") or [],
                model=result.get("model"),
                warning=result.get("warning"),
            )
            from .dashboard import regenerate as dashboard_regen
            dashboard_regen.regenerate()
        except Exception:
            log.exception("pending pro qna/dashboard record failed")
        for code in mermaid_blocks:
            try:
                png = await _render_mermaid_png(code)
                await ctx.bot.send_photo(chat_id, photo=png, caption="🧩 다이어그램")
            except Exception:
                log.warning("pending pro mermaid render failed")
    finally:
        _ACTIVE_AGENT_RUNS = max(0, _ACTIVE_AGENT_RUNS - 1)
        _LAST_REPLY_AT = datetime.utcnow()


async def _retry_pending_ingest_batch(ctx: ContextTypes.DEFAULT_TYPE):
    """Refill any free _INGEST_SEM slots from the retry queue and
    return immediately so the next tick (10 s later) can do the same.

    Older version awaited gather() of all spawned tasks per tick,
    which let APScheduler's max_instances=1 wedge the whole drain
    behind one slow PDF — even though 3 of 4 slots were idle. Now we
    spawn one fire-and-forget task per free slot (capped by
    _RETRY_INGEST_BATCH) and finish, so subsequent ticks always see
    'how many slots are open right now?' and top them up.

    Per-item not_before_ts (linear backoff on failure) is enforced
    inside _pop_eligible_retry_item, so stuck items hold while
    healthy ones drain around them."""
    if not _INGEST_RETRY_QUEUE:
        return
    free_slots = _INGEST_SEM._value
    n = min(_RETRY_INGEST_BATCH, free_slots, len(_INGEST_RETRY_QUEUE))
    if n <= 0:
        return
    for _ in range(n):
        # Detach: each task acquires the semaphore inside
        # _retry_pending_ingest. Returning immediately lets APScheduler
        # consider the tick 'done' so the next one runs on schedule.
        asyncio.create_task(_retry_pending_ingest(ctx))


# Mid-processing items stay in the queue with an in_flight_ts mark
# instead of being popped. If the bot crashes/restarts while an item
# is being processed, the persisted queue still contains it and it
# becomes eligible again after _IN_FLIGHT_TIMEOUT — previously the
# pop-then-persist sequence meant in-flight items vanished from both
# the queue and the failed log on restart.
_IN_FLIGHT_TIMEOUT = _INGEST_TIMEOUT_SEC + 120  # 12 min


def _pop_eligible_retry_item() -> dict | None:
    """Walk the retry queue from the front and mark the first item whose
    not_before_ts has elapsed AND is not currently in flight. The item
    stays in the queue with an in_flight_ts stamp so a mid-processing
    restart doesn't lose it. Use _retry_item_done / _retry_item_soft_fail
    to release the slot."""
    now = time.time()
    for candidate in _INGEST_RETRY_QUEUE:
        in_flight = candidate.get("in_flight_ts")
        if in_flight and now - in_flight < _IN_FLIGHT_TIMEOUT:
            continue
        nb = candidate.get("not_before_ts")
        if nb is None or nb <= now:
            candidate["in_flight_ts"] = now
            _persist_retry_queue()
            return candidate
    return None


def _retry_item_done(item: dict) -> None:
    """Item finished (success / duplicate / permanent failure / file
    gone). Remove from queue and persist."""
    try:
        _INGEST_RETRY_QUEUE.remove(item)
    except ValueError:
        pass
    _persist_retry_queue()


def _retry_item_soft_fail(item: dict, hold_sec: int) -> None:
    """Item failed transiently. Clear in_flight, set next-attempt time;
    item stays in queue at its existing position."""
    item.pop("in_flight_ts", None)
    item["not_before_ts"] = time.time() + hold_sec
    _persist_retry_queue()


def _enqueue_with_inflight(item: dict) -> dict:
    """Persist a live-path work item to the retry queue with an
    in_flight_ts mark BEFORE the pipeline call. If the bot dies mid-
    process (SIGTERM from auto_deploy, OOM kill, panic), the item
    survives in retry_queue.json; startup load strips its stale
    in_flight_ts and the next retry tick resumes the work. Without
    this, in-flight URL/text/photo/voice/audio ingests vanished
    silently on every restart.

    File-bearing items (doc/photo/voice/audio) also have orphan-scan
    coverage as a second safety net, but plain URL/text would
    otherwise be unrecoverable."""
    item.setdefault("attempts", 0)
    item["in_flight_ts"] = time.time()
    _INGEST_RETRY_QUEUE.append(item)
    _persist_retry_queue()
    return item


def _finish_inflight(item: dict, outcome: str) -> None:
    """Release a live-path item.

    outcome:
      'done'  — success / duplicate / permanent failure / skipped.
                Item removed from queue.
      'retry' — retryable failure raised. Item stays in queue with
                in_flight_ts cleared so the next tick picks it up.
                Live caller has already shown a 'queued' status."""
    if outcome == "retry":
        _retry_item_soft_fail(item, 0)
    else:
        _retry_item_done(item)


async def _retry_pending_ingest(ctx: ContextTypes.DEFAULT_TYPE):
    """Drain one queued ingest, sharing the same semaphore as live
    ingests so total concurrent ingests stays bounded."""
    if not _INGEST_RETRY_QUEUE:
        return
    item = _pop_eligible_retry_item()
    if item is None:
        return  # everything currently in backoff or in flight
    chat_id = item["chat_id"]
    title = (
        item.get("file_name")
        or item.get("url")
        or (Path(item["path"]).name if item.get("path") else None)
        or item.get("text", "")[:60]
        or "(unknown)"
    )

    # Filename-level dedup: same file may have been learned earlier
    # under a different source label (tg-doc: ↔ local:). Skip silently
    # so the user doesn't see a stream of '♻️ 이미 있음' messages and
    # the bot doesn't spend the slot.
    if item.get("kind") == "local_file":
        fname = Path(item.get("path") or "").name
        if fname:
            try:
                already = await asyncio.to_thread(meta.find_by_filename, fname)
            except Exception:
                already = None
            if already:
                log.info("retry skip — filename already learned: %s", fname)
                _record_dedup_confirmed(fname)
                _retry_item_done(item)
                return
            # User permanently ignored this filename — drop the retry
            # request without touching the pipeline.
            if _is_ignored_filename(fname):
                log.info("retry skip — permanently ignored: %s", fname)
                _retry_item_done(item)
                return
    async with _INGEST_SEM:
        retry_job_id = _register_ingest(
            f"[재시도] {title}", item.get("kind", "retry"), chat_id,
        )

        # Per-job stage callback (same shape as live-upload path) so the
        # retry ⏳ bubble shows '요약 + 임베딩' / 'PDF 로드' instead of
        # an opaque '처리 중'. _ACTIVE_INGESTS slot is keyed on
        # retry_job_id so the live updater picks up the stage on its
        # next refresh.
        def _retry_stage_cb(name: str) -> None:
            slot = _ACTIVE_INGESTS.get(retry_job_id)
            if slot is not None:
                slot["stage"] = name

        # Live ⏳ bubble + status updater.
        status_msg_id: int | None = None
        try:
            sent = await ctx.bot.send_message(
                chat_id, f"⏳ [재시도] {title[:80]}",
            )
            status_msg_id = sent.message_id
            _ACTIVE_INGESTS[retry_job_id]["status_msg_id"] = status_msg_id
            _track_bubble(chat_id, status_msg_id, f"[재시도] {title}")
        except Exception:
            log.exception("retry status start send failed")

        updater_task = None
        if status_msg_id:
            updater_task = asyncio.create_task(
                _live_status_updater(
                    ctx, chat_id, status_msg_id,
                    f"[재시도] {title}", retry_job_id,
                )
            )

        r = None
        try:
            kind = item["kind"]
            if kind == "doc":
                file = await ctx.bot.get_file(item["file_id"])
                dest = Path(config.DATA_DIR) / "files" / item["file_name"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                await file.download_to_drive(custom_path=dest)
                label = f"tg-doc:{item['file_unique_id']}:{item['file_name']}"
                suffix = dest.suffix.lower()
                if suffix == ".pdf":
                    r = await asyncio.wait_for(
                        pipeline.ingest_pdf(dest, label, on_stage=_retry_stage_cb),
                        timeout=_INGEST_TIMEOUT_SEC,
                    )
                elif suffix == ".pptx":
                    r = await asyncio.wait_for(
                        pipeline.ingest_pptx(dest, label, on_stage=_retry_stage_cb),
                        timeout=_INGEST_TIMEOUT_SEC,
                    )
                elif suffix == ".docx":
                    r = await asyncio.wait_for(
                        pipeline.ingest_docx(dest, label, on_stage=_retry_stage_cb),
                        timeout=_INGEST_TIMEOUT_SEC,
                    )
                elif suffix == ".xlsx":
                    r = await asyncio.wait_for(
                        pipeline.ingest_xlsx(dest, label, on_stage=_retry_stage_cb),
                        timeout=_INGEST_TIMEOUT_SEC,
                    )
                elif suffix in _AUDIO_SUFFIX_MIME:
                    r = await asyncio.wait_for(
                        pipeline.ingest_audio(
                            dest.read_bytes(), label,
                            mime_type=_AUDIO_SUFFIX_MIME[suffix],
                        ),
                        timeout=_INGEST_TIMEOUT_SEC,
                    )
                else:
                    content = dest.read_text(encoding="utf-8", errors="ignore")
                    r = await asyncio.wait_for(
                        pipeline.ingest_text(content, label),
                        timeout=_INGEST_TIMEOUT_SEC,
                    )
            elif kind == "url":
                r = await asyncio.wait_for(
                    pipeline.ingest_url(item["url"]),
                    timeout=_INGEST_TIMEOUT_SEC,
                )
            elif kind == "photo":
                import io as _io
                file = await ctx.bot.get_file(item["file_id"])
                bio = _io.BytesIO()
                await file.download_to_memory(out=bio)
                label = f"tg-photo:{item['file_unique_id']}"
                r = await pipeline.ingest_image(
                    bio.getvalue(), label,
                    caption=item.get("caption", ""),
                    mime_type="image/jpeg",
                )
            elif kind in ("voice", "audio"):
                import io as _io
                file = await ctx.bot.get_file(item["file_id"])
                bio = _io.BytesIO()
                await file.download_to_memory(out=bio)
                if kind == "voice":
                    label = f"tg-voice:{item['file_unique_id']}"
                    cap = item.get("caption", "")
                else:
                    label = f"tg-audio:{item['file_unique_id']}"
                    parts = [item.get("title", ""), item.get("caption", "")]
                    cap = "\n".join(p for p in parts if p)
                r = await pipeline.ingest_audio(
                    bio.getvalue(), label, caption=cap,
                    mime_type=item.get("mime_type", "audio/ogg"),
                )
            elif kind == "text":
                r = await pipeline.ingest_text(
                    item["text"], item.get("label", "tg-msg-retry"),
                )
            elif kind == "ocr_extend":
                # Bulk-approved OCR extension from /pending_approve_all.
                # Same pipeline.extend_pdf_ocr call as the single-row
                # /pending_ocr <N> path; wrapped here so it inherits
                # the live-status bubble + soft-retry mechanics.
                pdf_path = Path(item.get("pdf_path") or "")
                if not pdf_path.exists():
                    log.info("pending ocr_extend: file gone, skipping %s",
                             pdf_path.name)
                    _retry_item_done(item)
                    return
                r = await pipeline.extend_pdf_ocr(
                    pdf_path, item["doc_id"],
                    int(item["start_page"]), int(item["end_page"]),
                )
                # Normalise to the same {status, title, type, chunks}
                # shape _format_results expects for the success line.
                if r.get("status") == "ok":
                    r = {
                        "status": "ok",
                        "title": (item.get("title") or pdf_path.name)[:200],
                        "type": "pdf",
                        "chunks": r.get("chunks_added", 0),
                    }
                else:
                    r = {
                        "status": "empty",
                        "title": (item.get("title") or pdf_path.name)[:200],
                    }
            elif kind == "local_file":
                # Orphan-file recovery path — file already on disk
                # (preserved across container restarts) so we skip the
                # Telegram download and ingest straight from the path.
                # Label is filename-based so re-runs dedupe via
                # meta.find_by_source.
                dest = Path(item["path"])
                if not dest.exists():
                    log.info("orphan recovery: file gone, skipping %s", dest.name)
                    _retry_item_done(item)
                    return
                label = f"local:{dest.name}"
                suffix = dest.suffix.lower()
                if suffix == ".pdf":
                    r = await asyncio.wait_for(
                        pipeline.ingest_pdf(dest, label, on_stage=_retry_stage_cb),
                        timeout=_INGEST_TIMEOUT_SEC,
                    )
                elif suffix == ".pptx":
                    r = await asyncio.wait_for(
                        pipeline.ingest_pptx(dest, label, on_stage=_retry_stage_cb),
                        timeout=_INGEST_TIMEOUT_SEC,
                    )
                elif suffix == ".docx":
                    r = await asyncio.wait_for(
                        pipeline.ingest_docx(dest, label, on_stage=_retry_stage_cb),
                        timeout=_INGEST_TIMEOUT_SEC,
                    )
                elif suffix == ".xlsx":
                    r = await asyncio.wait_for(
                        pipeline.ingest_xlsx(dest, label, on_stage=_retry_stage_cb),
                        timeout=_INGEST_TIMEOUT_SEC,
                    )
                elif suffix in _AUDIO_SUFFIX_MIME:
                    r = await asyncio.wait_for(
                        pipeline.ingest_audio(
                            dest.read_bytes(), label,
                            mime_type=_AUDIO_SUFFIX_MIME[suffix],
                        ),
                        timeout=_INGEST_TIMEOUT_SEC,
                    )
                elif suffix in _IMAGE_SUFFIX_MIME:
                    # Pictures dropped into /app/data/files (e.g.
                    # 'send as file' attachments) — feed them through
                    # the same Vision OCR path the live photo handler
                    # uses. No caption available offline so it's pure
                    # OCR text.
                    r = await pipeline.ingest_image(
                        dest.read_bytes(), label, caption="",
                        mime_type=_IMAGE_SUFFIX_MIME[suffix],
                    )
                else:
                    try:
                        content = dest.read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        _retry_item_done(item)
                        return
                    r = await pipeline.ingest_text(content, label)
            else:
                _retry_item_done(item)
                return
        except Exception as e:
            item["attempts"] += 1
            if item["attempts"] >= _MAX_RETRY_ATTEMPTS or not _is_retryable(e):
                # Persist to /failed so the user can /failed retry later.
                payload = {k: v for k, v in item.items() if k != "chat_id"}
                payload.pop("in_flight_ts", None)
                _record_failure(
                    "error", title[:140], _explain_error(e),
                    retry_payload=payload,
                )
                _retry_item_done(item)
                final_fail = (
                    f"⚠️ ingest 재시도 포기 — {title[:80]}\n{_explain_error(e)}\n"
                    "/failed_retry 로 다시 시도할 수 있습니다."
                )
                await _edit_or_send(
                    ctx, chat_id, status_msg_id, final_fail,
                )
                return
            log.info("ingest retry %d/%d: %s",
                     item["attempts"], _MAX_RETRY_ATTEMPTS, title[:80])
            # Linear backoff per attempt: 1×_RETRY_BACKOFF_SEC on 1st
            # failure, 2× on 2nd, ... so a chronically-stuck item never
            # monopolises the queue. With default 3600s and
            # _MAX_RETRY_ATTEMPTS=5 the final wait is up to 5 h before
            # /failed pickup.
            hold = _RETRY_BACKOFF_SEC * item["attempts"]
            _retry_item_soft_fail(item, hold)
            wait_min = max(1, hold // 60)
            await _edit_or_send(
                ctx, chat_id, status_msg_id,
                f"🔁 일시 오류 — {wait_min}분 후 자동 재시도 "
                f"({item['attempts']}/{_MAX_RETRY_ATTEMPTS}): {title[:80]}\n"
                f"{_explain_error(e)}",
            )
            log.info("retry soft-fail %d/%d (hold %dm): %s",
                     item["attempts"], _MAX_RETRY_ATTEMPTS, wait_min, title[:80])
            return
        finally:
            if updater_task:
                updater_task.cancel()
                try:
                    await updater_task
                except asyncio.CancelledError:
                    pass
            _unregister_ingest(retry_job_id)
            _untrack_bubble(chat_id, status_msg_id)
    # Visibility policy for drained retry items:
    #   - ok (newly learned)     → send '✅ title (chunks)' so the user
    #                              sees real progress through the queue.
    #   - duplicate              → silent (forwarded digests re-cite
    #                              the same URLs hundreds of times).
    #   - empty / error / other  → send so the user can see what's
    #                              stuck even after the silent backoff
    #                              messages above suppress the soft
    #                              fails.
    # Record local_file dedup hits so the orphan scan stops re-queuing
    # them. Caught by any pipeline layer (file_hash / body_hash /
    # title) — they all surface as status='duplicate'.
    is_local_orphan = item.get("kind") == "local_file"
    if r and r.get("status") == "duplicate" and is_local_orphan:
        fname = Path(item.get("path") or "").name
        if fname:
            _record_dedup_confirmed(fname)
    # Orphan recovery is the only retry kind that legitimately fires
    # 100+ duplicates in a single drain (one per file the scan picked
    # up that's already learned under a different source label).
    # Surfacing each as a Telegram message floods the chat without
    # adding signal. Suppress duplicate messages for the local_file
    # path specifically; user-initiated retries (failed-retry, etc.)
    # still show the ♻️ line because the user actively asked to retry.
    if (is_local_orphan and r and r.get("status") == "duplicate"
            and status_msg_id):
        try:
            await ctx.bot.delete_message(chat_id=chat_id, message_id=status_msg_id)
        except Exception:
            pass
    else:
        summary = _format_results([r]) if r else f"(빈 결과: {title[:60]})"
        if summary.strip():
            await _edit_or_send(
                ctx, chat_id, status_msg_id, f"⏰ ingest 재시도\n{summary}",
            )
    # Item reached a terminal state (ok / duplicate / empty / other).
    # Soft-fail returns earlier via _retry_item_soft_fail, so anything
    # reaching here is done.
    _retry_item_done(item)
    log.info("retry done [%s]: %s",
             (r or {}).get("status", "unknown"), title[:80])


def main():
    if len(_HELP_TEXT) > _TG_MSG_LIMIT:
        log.warning(
            "/help text %d chars > Telegram limit %d — auto-splitting "
            "on paragraph boundaries. Compact _HELP_TEXT to restore "
            "single-message delivery.",
            len(_HELP_TEXT), _TG_MSG_LIMIT,
        )
    meta.init()
    obsidian.init()
    pending_store.init()
    # Larger pool + parallel update processing so /status, /queue, etc.
    # never queue behind a slow ingest's ⏳ edit. concurrent_updates=True
    # tells python-telegram-bot to process incoming updates in parallel
    # tasks instead of strictly sequential — command handlers run on
    # their own task and don't wait for an in-flight ingest reply to
    # finish flushing through the HTTPX pool.
    request = HTTPXRequest(
        connect_timeout=15.0,
        read_timeout=180.0,
        write_timeout=180.0,
        pool_timeout=15.0,
        connection_pool_size=32,
    )
    builder = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .request(request)
        .get_updates_request(request)
        .concurrent_updates(True)
    )
    if config.TELEGRAM_BASE_URL:
        base = config.TELEGRAM_BASE_URL.rstrip("/")
        # base ends with "/bot"; file URL is "/file/bot" on the same host
        file_base = base.replace("/bot", "/file/bot")
        builder = (
            builder.base_url(base)
            .base_file_url(file_base)
            .local_mode(True)
        )
        log.info("Telegram local mode at %s", base)
    app = builder.build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("recent", cmd_recent))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("forget_search", cmd_forget_search))
    app.add_handler(CommandHandler("forget_search_all", cmd_forget_search_all))
    app.add_handler(CommandHandler("forget_qna", cmd_forget_qna))
    app.add_handler(CommandHandler("forget_qna_search", cmd_forget_qna_search))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("find_all", cmd_find_all))
    app.add_handler(CommandHandler("show", cmd_show))
    # Tap-to-show: /find renders each doc's id as `/show_<id>` so a
    # single tap fires cmd_show without the user typing it.
    app.add_handler(MessageHandler(
        filters.Regex(r"^/show_[a-f0-9]{6,32}\b") & filters.ChatType.PRIVATE,
        cmd_show_id,
    ))
    app.add_handler(CommandHandler("failed", cmd_failed))
    app.add_handler(CommandHandler("failed_retry", cmd_failed_retry))
    app.add_handler(CommandHandler("failed_clear", cmd_failed_clear))
    # on_callback_query dispatches several prefixes — the pattern must
    # admit all of them or the callback silently drops. Today's
    # additions (xlate:, urldec_*, failed_*_one, orphan_*) all rely
    # on this routing. Anchored start, no trailing $ so prefix
    # variants like `failed_retry_one:5` route through too.
    app.add_handler(CallbackQueryHandler(
        on_callback_query,
        pattern=(
            r"^("
            r"failed_(retry|clear)$"
            r"|failed_(retry|drop)_one:\d+"
            r"|orphan_(learn|ignore)(:\d+|_all$)"
            r"|xlate:[A-Za-z0-9]+"
            r"|urldec_(retry|block):[A-Fa-f0-9]+"
            r")"
        ),
    ))
    app.add_handler(CallbackQueryHandler(
        on_pro_confirmation_callback, pattern=r"^pro:"
    ))
    app.add_handler(CallbackQueryHandler(
        on_ocr_extend_callback, pattern=r"^ocr:"
    ))
    app.add_handler(CallbackQueryHandler(
        on_ack_callback, pattern=r"^ack:"
    ))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("audit", cmd_audit))
    app.add_handler(CommandHandler("blocked_hosts", cmd_blocked_hosts))
    app.add_handler(CommandHandler("reset_blocked_hosts", cmd_reset_blocked_hosts))
    app.add_handler(CommandHandler("orphans", cmd_orphans))
    app.add_handler(CommandHandler("recover_orphans", cmd_recover_orphans))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("pending_ocr", cmd_pending_ocr))
    app.add_handler(CommandHandler("pending_pro", cmd_pending_pro))
    app.add_handler(CommandHandler("ocr_extend", cmd_ocr_extend))
    app.add_handler(CommandHandler("pending_approve_all", cmd_pending_approve_all))
    app.add_handler(CommandHandler(
        "pending_approve_all_confirm", cmd_pending_approve_all_confirm,
    ))
    app.add_handler(CommandHandler("pending_cancel_all", cmd_pending_cancel_all))
    app.add_handler(CommandHandler("queue_cancel_all", cmd_queue_cancel_all))
    app.add_handler(CommandHandler("queue_to_failed", cmd_queue_to_failed))
    app.add_handler(CommandHandler("cleanup", cmd_cleanup))
    app.add_handler(CommandHandler("cleanup_confirm", cmd_cleanup_confirm))
    app.add_handler(CommandHandler("dedupe", cmd_dedupe))
    app.add_handler(CommandHandler("dedupe_confirm", cmd_dedupe_confirm))
    app.add_handler(CommandHandler("forget_forwards", cmd_forget_forwards))
    app.add_handler(CommandHandler(
        "forget_forwards_confirm", cmd_forget_forwards_confirm,
    ))
    app.add_handler(CommandHandler("deep", cmd_deep))
    app.add_handler(CommandHandler("search_my_brain", cmd_search_my_brain))
    app.add_handler(CommandHandler("compare_papers", cmd_compare_papers))
    app.add_handler(CommandHandler("search_papers", cmd_search_papers))
    app.add_handler(CommandHandler("web_search", cmd_web_search))
    app.add_handler(CommandHandler("ingest_url", cmd_ingest_url))
    app.add_handler(CommandHandler("recent_docs", cmd_recent_docs))
    app.add_handler(CommandHandler("reset", cmd_reset))

    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_channel_post))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND, on_private
    ))

    if app.job_queue:
        app.job_queue.run_repeating(
            _retry_pending,
            interval=_RETRY_INTERVAL_SECONDS,
            first=30,
            name="retry_pending",
        )
        app.job_queue.run_repeating(
            _retry_pending_ingest_batch,
            interval=_RETRY_INGEST_INTERVAL_SEC,
            first=60,
            name="retry_pending_ingest",
        )
        app.job_queue.run_repeating(
            _refresh_dashboard,
            interval=60,
            first=20,
            name="refresh_dashboard",
        )
        app.job_queue.run_repeating(
            _promote_expired_pending,
            interval=60,
            first=90,
            name="promote_expired_pending",
        )
        app.job_queue.run_repeating(
            _drain_pending_pro,
            interval=90,
            first=120,
            name="drain_pending_pro",
        )
        app.job_queue.run_repeating(
            _periodic_memory_cleanup,
            interval=300,
            first=180,
            name="periodic_memory_cleanup",
        )
        # Weekly paddle release check — fires once per 7 days,
        # 5 min after bot start. Notifies Telegram on a new
        # release so the user can decide whether to bump the
        # ocr-worker base image and retry hybrid mode. State
        # persisted in data/.paddle_last_seen to avoid re-notify
        # on the same tag across bot restarts.
        app.job_queue.run_repeating(
            _check_paddle_release,
            interval=7 * 24 * 3600,
            first=300,
            name="check_paddle_release",
        )
        # Hourly: any actionable alert (e.g. paddle release notice)
        # whose ack button hasn't been tapped within 24h gets re-
        # sent. The 24h gate lives inside notify_acks.list_due(),
        # so a noop hourly tick is cheap. State in notify_acks.json
        # survives bot restarts so a tap days later still works.
        app.job_queue.run_repeating(
            _resend_unacked_alerts,
            interval=3600,
            first=600,
            name="resend_unacked_alerts",
        )
        # Hourly: check yt-dlp health (24h rolling failure rate). If
        # rate ≥ 70% over ≥ 5 attempts, fire an actionable alert.
        # Stable notify_id "yt_dlp_health" — re-arms after 7 days of
        # being acked while condition stays bad.
        app.job_queue.run_repeating(
            _check_yt_dlp_health,
            interval=3600,
            first=900,
            name="check_yt_dlp_health",
        )
        # Hourly: rescan disk for orphan files. Boot-time scan covers
        # the post-deploy case; this catches drift during long uptimes
        # (forward-listener wrote a file but bot crashed before the
        # doc row got persisted, etc). Quiet — no Telegram ping per
        # scan; only the log records new finds.
        app.job_queue.run_repeating(
            _periodic_orphan_scan,
            interval=3600,
            first=1800,
            name="periodic_orphan_scan",
        )
        # Every 60s: if the retry queue is empty (= bot is idle),
        # ask the user about every URL whose body extraction failed.
        # Each gets a [🔁 재시도] / [🚫 차단] prompt instead of being
        # silently buried in /failed.
        app.job_queue.run_repeating(
            _drain_pending_url_decisions,
            interval=60,
            first=120,
            name="drain_pending_url_decisions",
        )
        # One-shot Telegram flood-ban release notification. Today's
        # 22207s ban (logged at 2026-05-14 01:28:56 UTC) lifts at
        # 2026-05-14 07:39:03 UTC. Schedule a single send_message at
        # 07:40 UTC + 60s grace; if the ban is lifted the user gets a
        # phone notification, otherwise the call fails silently and
        # the user just doesn't get the alarm. Safe to leave in code
        # — the run_date check below auto-skips when already past.
        from datetime import datetime, timezone, timedelta
        ban_release_at = datetime(2026, 5, 14, 7, 40, 0, tzinfo=timezone.utc)
        if ban_release_at > datetime.now(timezone.utc):
            async def _ban_release_notify(_ctx):
                try:
                    await app.bot.send_message(
                        config.TELEGRAM_OWNER_ID,
                        "🔓 텔레그램 flood ban 해제됨.\n"
                        "/status 로 봇 정상 동작 확인하세요.",
                    )
                except Exception as e:
                    log.warning("ban release notify failed: %s", e)
            app.job_queue.run_once(
                _ban_release_notify,
                when=ban_release_at,
                name="ban_release_notify",
            )

    _load_persisted_state()
    _load_dedup_confirmed()
    _load_permanently_ignored()
    _cleanup_stale_bubbles_at_startup(app)
    _recover_orphan_files_at_startup(app)
    vector.warm_bm25_cache()  # background scan; first query stays fast
    log.info("bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
