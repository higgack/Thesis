import asyncio
import base64
import html
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

_KST = timezone(timedelta(hours=9))
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)
from telegram.request import HTTPXRequest

from . import config
from .store import meta, vector, obsidian, cost, qna, wiki, pending as pending_store
from .store import pending_url_decisions
from .store import dash_queries
from .ingest import pipeline
from .agent import agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

# Ingest concurrency cap. Sized for the live VM = e2-standard-2 (2 vCPU,
# 8 GB) + 5500m bot mem_limit. 4 parallel ingests keep the chunk +
# reranker working set under the cap, so a multi-file burst refuses
# gracefully (retry queue) instead of OOM-killing the co-tenant `stock`
# project that shares this host. Was 8 — tuned for a 16 GB VM that never
# ran in production. Override via env if the VM is later upsized.
_INGEST_SEM_CAPACITY = int(os.getenv("INGEST_SEM_CAPACITY", "4"))
_INGEST_SEM = asyncio.Semaphore(_INGEST_SEM_CAPACITY)
# Interactive-priority gate. A user's command (_SustainedTyping bumps
# _INTERACTIVE_INFLIGHT) or Q&A (_run_agent bumps _ACTIVE_AGENT_RUNS)
# should win event-loop + Gemini concurrency over background learning.
# Ingest waits on _await_interactive_idle() before grabbing an
# _INGEST_SEM slot, and the retry-drain tick is skipped entirely while
# interactive work is in flight. _ACTIVE_AGENT_RUNS stays high through
# _finalize_agent_reply (the full answer render+send), so a live forward
# can't cut into a long multi-part answer (charts/diagrams). 300s cap so
# a stuck query can't starve ingest forever — forwards aren't urgent and
# the retry queue loses nothing. Ingest paths touch NEITHER counter, so
# there's no self-wait / deadlock.
_INTERACTIVE_INFLIGHT = 0
_INTERACTIVE_MAX_DEFER_SEC = float(os.getenv("INGEST_DEFER_SEC", "300"))
# How many queued retries to drain per tick + how often we tick. Batch
# is an upper bound on tasks spawned per tick; actual concurrency is
# bounded by _INGEST_SEM (4 on the live e2-standard-2 / 8 GB VM), shared
# with live ingests. Extras just block on the semaphore acquire, so a
# larger batch only speeds slot refill — it never overloads the box.
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
# Bound the retry tick's deference to interactive activity. The tick
# normally skips entirely when _interactive_busy() so a chat / command
# gets full Gemini headroom. But with no cap that means a steadily-
# chatting user can starve the queue indefinitely (the live-ingest path
# has the same yield via _await_interactive_idle, but it's bounded to
# 300 s; the retry tick had no equivalent). After this many consecutive
# busy skips (≈60 s) the next tick force-drains a single slot so the
# queue makes forward progress regardless. One slot is small enough
# that the user's in-flight command barely notices the API contention.
_RETRY_BUSY_SKIP_GRACE = 6
_RETRY_BUSY_SKIP_COUNT = 0
# After a failed retry, hold the item for this many seconds before
# making it eligible again. Prevents one stuck item from monopolising
# the queue's drain rate (without this, a perpetually-overloaded
# upstream can spin the same item to the front of the queue every
# 30s). Default 1 hour. Other queue items (with elapsed not_before_ts
# or never-failed-yet) drain normally during the hold.
_RETRY_BACKOFF_SEC = int(os.getenv("RETRY_BACKOFF_SEC", "3600"))
_INGEST_RETRY_QUEUE: list[dict] = []
_INGEST_FAILED: list[dict] = []
# /failed_retry progress tracker (one active batch at a time — personal
# bot). _failed_retry_all stamps each re-queued item with `_batch=id`
# and seeds this; _refresh_retry_progress (called from the drain tick)
# edits a single "🔄 재시도 N/M 완료" message as batch items leave the
# queue, so the user can see progress instead of only per-item results.
# In-memory only: a restart drops the live message (items still drain).
_RETRY_PROGRESS: dict = {
    "chat_id": None, "msg_id": None, "batch_id": None,
    "total": 0, "last_done": -1,
}
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


def _stamp_heartbeat() -> None:
    """Write the current epoch to _HEARTBEAT_PATH (atomic). Plain integer
    text for trivial shell parsing; errors swallowed so a transient FS
    issue never kills the caller. Called once at startup (so a fresh
    container's file is current the instant it boots, not only after the
    first 60s job tick — closes the post-deploy false-restart window)
    and every 60s thereafter via _write_heartbeat."""
    try:
        import os
        import time as _t
        tmp = _HEARTBEAT_PATH.with_suffix(".tmp")
        tmp.write_text(str(int(_t.time())))
        os.replace(tmp, _HEARTBEAT_PATH)
    except Exception:
        log.exception("heartbeat write failed")


async def _write_heartbeat(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue tick → stamp _HEARTBEAT_PATH. Runs on the asyncio loop so
    a wedged loop freezes the stamp, which the host watchdog
    (auto_pull.sh) uses to detect + restart a hung bot."""
    _stamp_heartbeat()


async def _startup_smoke(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Run the post-deploy smoke test (src/smoke.py) and DM the owner the
    result. Pass and fail both reported — after every deploy the user
    gets one line confirming the live query+embed path actually works,
    not just that the container booted."""
    try:
        from . import smoke
        ok, msg = await smoke.run_smoke()
    except Exception as e:
        log.exception("startup smoke crashed")
        ok, msg = False, f"❌ 스모크 실행 자체 실패: {type(e).__name__}: {e}"
    if not config.TELEGRAM_OWNER_ID:
        return
    try:
        await ctx.bot.send_message(config.TELEGRAM_OWNER_ID, msg)
    except Exception:
        log.exception("smoke result send failed")


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
# Liveness heartbeat: a JobQueue tick stamps this file with the current
# epoch every 60 s. It runs on the asyncio loop, so a wedged loop (hung
# bot that Docker's restart-on-exit can't catch) freezes the stamp. The
# host watchdog in scripts/auto_pull.sh reads it every minute and
# force-recreates the bot container + alerts when it goes stale.
_HEARTBEAT_PATH = config.DATA_DIR / "bot_heartbeat"
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
                # One scan instead of N per-item find_by_filename() LIKE
                # queries — the queue can hold hundreds of local_file
                # entries and each lookup opened its own connection,
                # adding hundreds of ms to boot before run_polling.
                _already_learned = meta.ingested_filename_set()
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
                        if fname and fname in _already_learned:
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
        with sqlite3.connect(str(db_path), timeout=30) as c:
            c.execute("PRAGMA journal_mode=WAL")
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
        with sqlite3.connect(str(db_path), timeout=30) as c:
            c.execute("PRAGMA journal_mode=WAL")
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
# Per-message URL cap. Raised 5→30 so hand-curated broker dailies (한투
# 로보틱스 데일리 등 — typically 4~7 vo.la links) get every original
# article ingested instead of tripping the forwarded-digest URL-drop
# below. Genuine spam digests (Noah's auto-aggregator bundles 50+ URLs)
# still exceed 30 and fall back to body-only.
_MAX_URLS_PER_MSG = 30


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
    # inside the body string. Hand-curated forwards (broker dailies with
    # ~4~7 links) stay under the 30 cap and keep their URL ingest path.
    if is_forward and len(urls) >= _MAX_URLS_PER_MSG:
        urls = []
    else:
        urls = urls[:_MAX_URLS_PER_MSG]
    return urls, plain
_MERMAID_BLOCK_RE = re.compile(
    # Tolerate leading indentation on both the opening and closing
    # fences — Gemini sometimes wraps mermaid blocks inside a bulleted
    # / numbered section and indents the whole code fence. When the
    # closing ``` is indented the original `\n```` pattern doesn't
    # match, leaving the raw mermaid content in the body where
    # `_renumber_citations` then mangles every `[2026E]` / `[342.6]`
    # array literal into a fake citation number. Optional ` ` / `\t`
    # runs before each fence fixes both extraction and the citation
    # collision.
    r"^[ \t]*```mermaid[^\n]*\n(.*?)\n[ \t]*```",
    re.DOTALL | re.MULTILINE,
)
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
    "search_patents": "⚖️",
    "search_company_patents": "🇰🇷",
    "search_kr_papers": "🇰🇷📄",
    "get_kr_paper_detail": "🇰🇷📄",
    "search_kr_patents_kisti": "🇰🇷⚖️",
    "get_kr_patent_detail": "🇰🇷⚖️",
    "get_kr_patent_citations": "🇰🇷⚖️",
    "search_kr_reports": "🇰🇷📑",
    "get_kr_report_detail": "🇰🇷📑",
    "search_kr_trends": "🇰🇷🌐",
    "search_kr_researchers": "🇰🇷👤",
    "search_kr_organs": "🇰🇷🏛️",
    "search_kr_science_trends": "🇰🇷📈",
    "search_kr_rnd_projects": "🇰🇷🔬",

    "get_kr_related_content": "🇰🇷🔗",
    "search_kr_rnd_outcomes": "🇰🇷🎯",
    "search_kr_govt_reports": "🇰🇷📑",
    "search_kr_agency_rnd": "🇰🇷🏛️",
    "search_kr_rnd_issues": "🇰🇷📈",
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


async def _send_body_with_mermaid(update, ctx, body: str,
                                  status_msg=None) -> None:
    """Send a long text body that may contain ```mermaid``` blocks,
    rendering each block to a PNG via _render_mermaid_png. Text parts
    are split at the 4000-char cap. The first text chunk is edited
    into status_msg if provided (saves a notification), the rest are
    fresh reply_text calls. PNGs go via ctx.bot.send_photo.

    Order is preserved — text-A → mermaid₁ → text-B → mermaid₂ → ...
    so legend lines stay next to the chart they describe. Used by
    /patent_stats and /paper_stats trend view; also generally usable
    for any future command that wants inline diagrams."""
    if not body:
        return
    # Split on the placeholder so we keep ordering.
    blocks: list[str] = []

    def _stash(m: "re.Match[str]") -> str:
        idx = len(blocks)
        blocks.append(m.group(1).strip())
        return f"__MERMAID_BLOCK_{idx}__"

    body_stashed = _MERMAID_BLOCK_RE.sub(_stash, body)
    if not blocks:
        # No diagrams — just chunk + send as before.
        pieces = _split_for_telegram(body_stashed)
        first_done = False
        for piece in pieces:
            if not first_done and status_msg is not None:
                try:
                    await ctx.bot.edit_message_text(
                        chat_id=status_msg.chat.id,
                        message_id=status_msg.message_id,
                        text=piece, parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    first_done = True
                    continue
                except Exception:
                    pass
            try:
                await update.message.reply_text(
                    piece, parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                first_done = True
            except Exception:
                log.exception("send_body_with_mermaid chunked send failed")
        return
    # Has diagrams — walk text/idx parts in order.
    parts = re.split(r"__MERMAID_BLOCK_(\d+)__", body_stashed)
    first_done = False
    for i, part in enumerate(parts):
        if i % 2 == 0:
            text = part.strip()
            if not text:
                continue
            for piece in _split_for_telegram(text):
                if not first_done and status_msg is not None:
                    try:
                        await ctx.bot.edit_message_text(
                            chat_id=status_msg.chat.id,
                            message_id=status_msg.message_id,
                            text=piece, parse_mode="HTML",
                            disable_web_page_preview=True,
                        )
                        first_done = True
                        continue
                    except Exception:
                        pass
                try:
                    await update.message.reply_text(
                        piece, parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    first_done = True
                except Exception:
                    log.exception("send_body_with_mermaid text part failed")
        else:
            try:
                block_idx = int(part)
            except ValueError:
                continue
            if not (0 <= block_idx < len(blocks)):
                continue
            code = blocks[block_idx]
            try:
                png = await _render_mermaid_png(code)
                await ctx.bot.send_photo(
                    chat_id=update.effective_chat.id, photo=png,
                )
            except Exception as e:
                log.warning("inline mermaid render failed: %s", e)
                # Fallback: emit the code as a code-block so the user
                # at least sees something.
                try:
                    await update.message.reply_text(
                        f"(다이어그램 렌더 실패) <pre>{html.escape(code)[:1500]}</pre>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass


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
    synthesis on a 50-doc compare can be ~30-60s). 3s cadence keeps
    the indicator visibly active even when the Telegram client
    refreshes lazily."""
    while True:
        try:
            await _typing(update, ctx)
        except Exception:
            pass
        try:
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            break


class _SustainedTyping:
    """Async context manager that keeps the 'typing...' indicator
    alive throughout a long-running block. Telegram's chat_action
    auto-expires ~5s after each sendChatAction call; the background
    task refreshes every 2s so the indicator never disappears mid-
    operation. Cancels cleanly on exit even when the body raises.

        async with _SustainedTyping(update, ctx):
            rows = await long_running_api_call()

    Used by the 4 shared search helpers (_kipris_search_command,
    _kipris_lookup_command, _kisti_search_command,
    _ntis_simple_search_command) so all 21+ KR backend commands
    show continuous activity for the full 30-60s of API call +
    translation + send."""

    def __init__(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self._update = update
        self._ctx = ctx
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "_SustainedTyping":
        global _INTERACTIVE_INFLIGHT
        _INTERACTIVE_INFLIGHT += 1
        self._task = asyncio.create_task(
            _sustained_typing(self._update, self._ctx)
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        global _INTERACTIVE_INFLIGHT
        _INTERACTIVE_INFLIGHT = max(0, _INTERACTIVE_INFLIGHT - 1)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass


def _interactive_busy() -> bool:
    """True while any user-facing command (_SustainedTyping) or Q&A
    (_run_agent → _ACTIVE_AGENT_RUNS) is running. Ingest consults this
    to yield to interactive work."""
    return _INTERACTIVE_INFLIGHT > 0 or _ACTIVE_AGENT_RUNS > 0


async def _await_interactive_idle(
    max_wait: float = _INTERACTIVE_MAX_DEFER_SEC,
) -> None:
    """Hold the calling ingest task while a command / Q&A is in flight,
    so interactive requests get loop + Gemini headroom first. Bounded by
    max_wait so continuous chatting can't starve ingest indefinitely."""
    if not _interactive_busy():
        return
    import time as _t
    deadline = _t.monotonic() + max_wait
    while _interactive_busy() and _t.monotonic() < deadline:
        await asyncio.sleep(0.5)


_HELP_TEXT = """<b>🧠 SECOND BRAIN 봇</b>

<b>【1. 대시보드】</b> Basic Auth · 60s 갱신·다크 19~07
📊 Q&amp;A http://34.50.23.221:8082/1e68e9fae4e6fb1f8298bdee768eb73b/index.html
📚 Wiki http://34.50.23.221:8082/1e68e9fae4e6fb1f8298bdee768eb73b/wiki/index.html
📒 학습 http://34.50.23.221:8082/1e68e9fae4e6fb1f8298bdee768eb73b/notes/index.html

<b>【2. 명령어】</b>

📋 <b>조회</b>
  /find &lt;kw&gt; [N=50](헤더 학습/발행/청크수) · /find_all &lt;kw&gt;(최대 500)
  /show &lt;id|kw&gt;(본문 + [🌐 한국어 번역]) · /recent [N]
  /stats · /status · /usage · /cost · /eval · /eval_seed

💬 <b>대화</b>
  /reset · /deep &lt;질문&gt;(Pro 강제)

🚨 <b>장애 / 큐</b>
  /failed(건별 [🔁]/[🗑]·drop=영구) · /failed_retry · /failed_clear · /unignore
  /queue · /queue_to_failed · /queue_panic · /queue_cancel_all
  /audit · /blocked_hosts · /reset_blocked_hosts

🔍 <b>Orphan</b>: /orphans · /recover_orphans(건별 [📥]/[🗑])

⏸️ <b>보류 (5분)</b>
  /pending · /pending_ocr &lt;N&gt; · /pending_pro &lt;N&gt; · /pending_links · /ocr_extend &lt;id|kw&gt;
  /pending_approve_all · /pending_approve_all_confirm · /pending_cancel_all

🗑️ <b>삭제</b>
  /forget &lt;id&gt; · /forget_search · /forget_search_all
  /forget_qna · /forget_qna_search
  /dedupe · /dedupe_confirm · /cleanup · /cleanup_confirm
  /forget_forwards · /forget_forwards_confirm
  /youtube_restub_rescan · /fix_placeholder_titles

🛠️ <b>도구</b>
  /search_my_brain · /compare_papers · /web_search · /ingest_url
  /search_papers (+adv·stats) · /search_patents (+adv·stats)
  🕸 KG(시범): /kg_extract · /kg

📚 <b>위키</b>(매시 정시 학습·요약알림 1일1회): /wiki · /wiki_today · /wiki_recent · /wiki_new · /wiki_lint · /wiki_status · /wiki_cost · /wiki_run · /wiki_drain · /wiki_split · /wiki_dedup · /wiki_rename · /wiki_delete · /wiki_backfill · /wiki_pending · /wiki_failed · /wiki_off · /wiki_on · 상세: /wiki_guide

📒 <b>학습 노트</b>: /notes · 상세: /notes_guide

🇰🇷 <b>한국</b>
  KIPRIS: /company_patents · /patent_detail · /citing_patents
          /kipris_{search,pub,reg,inventor,status,family,claims,priority}
  ScienceON: /kr_papers · /kr_patents · /kr_reports · /kr_trends · /kr_researcher · /kr_organ · /kr_science_trend
  NTIS: /kr_rnd_projects · /kr_related
        /kr_outcomes · /kr_govt_reports · /kr_agency_rnd · /kr_rnd_issues

ℹ️ <b>기타</b>: /start · /help · 상세: /guide_lookup · /wiki_guide

<b>【3. 핵심】</b> 채널/DM 자료→자동 수집·요약·임베딩·Obsidian / 자연어→에이전트 도구 자동 / 메모리 7턴(/reset) / 비용·Q&amp;A SQLite+대시보드 / 답변 끝 (자료 시점: YYYY.MM)

<b>【4. 도구】</b> 🧠 brain·compare · 📄 papers 6소스 · 🇰🇷 KIPRIS·ScienceON·NTIS · ⚖️ patents EPO · 🌐 web · 📥 ingest · 한국어번역

<b>【4-1. 회사 분석】</b> "회사명+실적/매출" → 본문 + 신사업 + 📌 실적 표(A./F.·YoY·QoQ) + 가이던스 + brain/web 분리. 숫자 audit.

<b>【5. 자연어 트리거】</b>
🧠 brain "삼성전기 MLCC" · 🧠 compare "정리/리뷰" · 📄 papers "논문" · ⚖️ patents "특허" (글로벌) · 🇰🇷 company_patents "[KR회사] 특허" · 🌐 <b>web "웹/구글/인터넷"만</b> · 📥 ingest "URL"

<b>【6. 자료 인입】</b> URL·PDF·PPTX·DOCX·XLSX·이미지·음성·YouTube·텍스트 전송
• PDF 텍스트 자동(PyMuPDF). sparse PDF는 <b>자동 OCR 0p</b> + 학습 직후 3-버튼 [📄 OCR / 📝 텍스트만 / 🚫]. image-only 3p · 캡션≥80자 OCR skip · [OCR] 강제 · 음성=Gemini STT · YouTube=자막→Jina · <b>.txt/.md/.csv 학습 제외</b>
차단: LinkedIn/FB/IG · Reuters/Bloomberg/WSJ/FT/NYT/WaPo

<b>【7. 자동 포워딩】</b> .env LISTEN_CHANNELS·LISTEN_PLAIN_CHANNELS
[Noah 디지스트] 📋 TG 원문 fetch / 📰 Substack URL relay / 그 외 drop
[PLAIN] 텍스트만: Fundeasyearnings / 전체: aicorporateanalysisdeepdive
[URL전용] benineb9·getfeed: naver/youtube/t.me 자동, 그외=알림
[제목필터] insidertracking: "미국 레딧 게시물 분석"만 forward
백필: tmux + python -m src.scripts.import_channel &lt;ch&gt; --resume

<b>【8. 메타데이터】</b> Flash-Lite 요약+메타 1콜 · 🏢회사 🏷태그 📅YYYY.MM · 📊브로커리지·애널리스트(리포트 자동 추출, 회사 분석에 활용)

<b>【9. 답변 품질】</b> 시점 필수 · brain 재검색 · web [도메인]·인용 [N] · 숫자 audit

<b>【10. 운영】</b> 영속(atomic+.bak) · 메모리 5분 · 질문＞학습

<b>【10-1. 모델】</b> Embed gemini-embedding-001 · Lite flash-lite · 답변 flash · /deep pro · 캐시 1h

<b>【10-2. 비용】</b> 6단 dedup · 청크 1000 · DPI 100 · OCR 0p · 캐시 · 메타 gating

<b>【10-3. Retry】</b> 자동1회→/failed(🔁3회후폐기) · in_flight→자동재개

<b>【11.】</b> 본문비어→차단 · 막힘→/queue_panic · /reset

<b>【12. 백엔드】</b> ✅ EPO·ScienceON·NTIS · ⏳ KIPRIS 14건+NTIS 5건 승인대기"""


# Detailed multi-section guide for the patent suite. Kept separate
# from _HELP_TEXT so the main help stays under Telegram's 4096-char
# limit. Surfaced as its own /patents_guide command — the help text
# above just points the user at it.
_LOOKUP_GUIDE_TEXT = """<b>📘 전체 명령어 상세 가이드 (/guide_lookup)</b>

봇의 <b>모든 명령어</b> 한 자리. /help 본문은 한 줄짜리 요약,
여기는 각 명령어의 사용법 · 인자 · 동작 · 비용 · 팁까지 다 적음.
파일 길이는 제한 없음 — Telegram 4000자 cap 에 걸리면 자동 분할
전송됨.

═══════════════════════════════════════
<b>📋 1. 조회</b>
═══════════════════════════════════════

<b>/find &lt;keyword&gt; [N=50]</b>
타이틀 + 첫 청크 텍스트에 키워드 매치. 결과 최대 N개 (기본 50).
각 행에 학습일 · 발행일 · 청크 수 · doc_id 6자.
예: <code>/find HBM</code> · <code>/find 삼성전기 20</code>

<b>/find_all &lt;keyword&gt;</b>
/find 와 같지만 N=500 까지 확장. 광범위 검색용.

<b>/show &lt;id|keyword&gt;</b>
doc_id 4자 이상 또는 키워드로 매치되는 문서의 본문 dump.
[🌐 한국어 번역] 버튼 자동 부착 — 영문 자료 즉시 번역.
<b>/find 결과에서 들어왔을 때:</b> 헤더 + 푸터에 [⏩ find 다음
#N+1~#N+5 / total] 버튼 자동 부착. 클릭하면 다음 5개 항목을
새 메시지로 띄움 (find 전체를 위로 스크롤할 필요 X). 미리보기는
/find 와 동일한 풍부한 per-item 렌더 — 제목·학습일·발행일·청크수
·소스·태그·전체 summary 모두 포함, 다음 /show 결정 가능.
/find 후 1시간 이내, 이 chat 안에서만 유효.
예: <code>/show abc1</code> · <code>/show 대덕전자 1Q26</code>

<b>/recent [N]</b>
최근 N개 (기본 10) 학습된 자료 카드뷰. 시간순.
예: <code>/recent</code> · <code>/recent 30</code>

<b>/stats</b>
문서 총개수 + 청크 총개수.

<b>/status</b>
봇 실시간 상태 (in-flight · 큐 길이 · retry · pending · orphan).

<b>/usage</b>
누적 Q&amp;A 횟수 + 비용 추정.

<b>/cost</b>
일별/월별 비용 (.usage_log 기반) — 모델별 분해.

<b>/eval</b>
답변 품질 회귀 테스트. <code>data/eval_golden.json</code>에 정의된 골든셋을
에이전트에 돌려 출처 적중률·사실 포함 여부를 채점.
• 처음 실행하면 골든셋이 비어있음 → 편집 방법 안내 출력.
• 골든셋 형식: <code>{"items":[{"id":"q001","query":"질문",
  "expected_sources":["출처 키워드"],"expected_facts":["핵심 사실"]}]}</code>
• expected_sources (OR): 반환 출처 제목에 키워드 하나라도 있으면 통과.
• expected_facts (AND): 답변 본문에 모두 포함돼야 통과.
• 빈 배열 [] → 해당 체크 스킵.
언제: 프롬프트·청크·TOP_K·리랭커 변경 후 품질 회귀 확인.

<b>/eval_seed [N]</b>
과거 Q&A(qna.db)에서 골든셋 초안 자동 생성 — 신규 질문 N개(기본 20, 최대 50)
append. query·expected_sources 자동, expected_facts는 비워둠(네가 채움).
기존 항목 안 지움. 빈 골든셋부터 시작할 때 한 번 돌려 시드 확보.

═══════════════════════════════════════
<b>💬 2. 대화</b>
═══════════════════════════════════════

<b>/reset</b>
대화 메모리 초기화 (7턴 유지). 토픽 어긋났을 때 / 새 주제로 넘어갈 때.

<b>/deep &lt;질문&gt;</b>
Gemini 2.5 Pro 강제 사용. 기본 답변은 Flash, /deep 만 Pro (비용 ~4배,
정확도/추론 강함).
언제: 복잡한 다단계 추론 · 다수 자료 종합 비판 검토 · 수치 audit 필요.

<b>자연어 트리거 (대화창 직접):</b>
• 🧠 brain "삼성전기 MLCC 어때?" — 저장 자료 검색
• 🧠 compare "리뷰/정리/통합" — 여러 자료 종합
• 📄 papers "논문 찾아줘" — 외부 학술 6소스
• ⚖️ patents "특허 알려줘" — EPO 글로벌
• 🇰🇷 company_patents "삼성전기 특허" — KIPRIS
• 🌐 web "웹/구글/인터넷" — Gemini Grounding
• 📥 ingest "이 URL 저장" — 영구 보관

═══════════════════════════════════════
<b>🚨 3. 장애 / 큐 관리</b>
═══════════════════════════════════════

<b>/failed</b>
실패 큐 카드뷰. 각 행 [🔁 재시도] / [🗑 영구 무시] 버튼.
[🔁] 즉시 재시도 · [🗑] URL/filename 을 ignored 등록 (재인입 차단).
크기순 정렬.

<b>/failed_retry</b>
실패 큐 전체 한번에 재시도.

<b>/failed_clear</b>
실패 큐 전체 영구 무시 — [🗑] 일괄. 복구 불가.

<b>/queue</b>
자동 재처리 대기/진행 중인 인입 항목. 처리 실패 시 자동 1회 후 /failed로 이동(거기서 🔁 수동 재시도, 3 cycle 초과 시 자동 폐기). 항목별 종류·제목·시도 횟수 표시.

<b>/queue_to_failed</b>
retry 큐 모두 실패 큐로 강제 이동. 진행중인 in-flight 작업은 그대로 끝까지 돔.

<b>/queue_panic</b>
패닉 정리: 큐 → /failed (retry payload 유지) + Pro/Agent/Pending OCR·Pro 모두 비움 + orphan 복구 suppress 마커 + 봇 프로세스 종료(Docker 자동 재시작). in-flight 태스크가 세마포어·_CHROMA_LOCK 잡고 안 풀려서 새 학습까지 느릴 때 사용. 복구는 /failed 의 🔁 #N로 하나씩.

<b>/queue_cancel_all</b>
retry 큐 전체 취소 — 재시도 안 함, /failed 로도 안 감.

<b>/audit</b>
무결성 검증 — 메모리 ↔ 디스크 ↔ vector store ↔ obsidian orphan 비교.

<b>/blocked_hosts</b>
차단된 호스트 목록 (HTTP 4xx/timeout 누적 → 자동 차단).

<b>/reset_blocked_hosts</b>
차단 호스트 모두 해제.

═══════════════════════════════════════
<b>🔍 4. Orphan</b>
═══════════════════════════════════════

<b>/orphans</b>
data/files/ 에 있지만 meta.db 미등록 파일 목록. 크기순 정렬.

<b>/recover_orphans</b>
각 항목에 [📥 학습] / [🗑 영구 무시] 버튼.
• [📥] 다시 ingest pipeline 통과 → 정상 학습
• [🗑] 파일 삭제 + filename ignored 등록 (재인입 차단)

═══════════════════════════════════════
<b>⏸️ 5. 보류 (OCR 5분 윈도)</b>
═══════════════════════════════════════

<b>/pending</b>
보류 항목 카드뷰. 학습 직후 자동 3-버튼:
• [📄 OCR 추가] · [📝 텍스트만] · [🚫 취소]
크기순 정렬. 만료 임박 항목 위로.

<b>/pending_ocr &lt;N&gt;</b>
N번째 항목에 OCR 추가 결정.

<b>/pending_pro &lt;N&gt;</b>
N번째 항목을 Pro 모델 (Gemini 2.5 Pro) 처리. 비용 4배, 정확도 향상.

<b>/pending_links</b>
학습한 URL 본문에서 찾은 링크 묶음을 다시 표시 (제목·요약 미리보기 +
선택 버튼). 학습 직후 못 골랐거나 봇이 재시작돼도 여기서 확인·선택.
선택한 링크만 추가 학습 (글 속 링크까지 — 1단계).

<b>/pending_approve_all</b>
모든 보류 일괄 기본값 (텍스트만) 승인 미리보기.

<b>/pending_approve_all_confirm</b>
일괄 승인 실행.

<b>/pending_cancel_all</b>
모든 보류 학습 취소.

<b>/ocr_extend &lt;id|keyword&gt;</b>
OCR 5분 만료 연장 (+5분). id 4자 또는 키워드 매치.

OCR 정책 (자동):
• OCR_AUTO_CAP=0 → sparse PDF 도 OCR skip (사용자 결정 필요)
• image-only PDF first 3p 만 자동 OCR
• 이미지 캡션 ≥ 80자 → OCR skip
• Progressive OCR: probe 3p &lt; 300자 → 전체 OCR skip

═══════════════════════════════════════
<b>🗑️ 6. 삭제</b>
═══════════════════════════════════════

<b>/forget &lt;doc_id&gt;</b>
특정 doc_id 자료 삭제. id 4자 이상.
예: <code>/forget abc1ef2g</code>

<b>/forget_search &lt;keyword&gt;</b>
키워드 매치 자료 미리보기 (실제 삭제 X).

<b>/forget_search_all &lt;keyword&gt;</b>
미리보기 결과 전체 삭제 실행.

<b>/forget_qna &lt;keyword&gt;</b>
Q&amp;A 카드 미리보기.

<b>/forget_qna_search &lt;keyword&gt;</b>
Q&amp;A 검색 결과 전체 삭제.

<b>/dedupe</b>
중복 감지 미리보기 — 같은 file_hash / text_hash / title.

<b>/dedupe_confirm</b>
중복 삭제 실행.

<b>/cleanup</b>
정리 후보 미리보기 — 본문 비어있음 / 청크 0개 / 메타 누락.

<b>/cleanup_confirm</b>
정리 삭제 실행.

<b>/forget_forwards</b>
포워드 학습 자료 미리보기.

<b>/forget_forwards_confirm</b>
포워드 전체 삭제 실행.

<b>/youtube_restub_rescan</b>
yt-dlp 가 깨져있던 동안 stub 으로 학습된 YouTube 자료를 찾아
삭제 + 원 URL 을 retry 큐에 재투입. 자막 fetch 실패 안내문이
본문에 그대로 들어간 케이스를 정확히 식별 (실제 자막에는 없는
마커 매칭). bare = 미리보기, <code>confirm</code> = 실행.
실행 후 다음 retry tick(≈60s)부터 nightly yt-dlp + Deno 로 재추출.

⚠️ 모든 삭제는 2단계 confirm — 첫 명령은 항상 미리보기.
영구 무시 (/failed [🗑]) 와 다름: /forget 은 자료만 지움, ignored 등록 X.

═══════════════════════════════════════
<b>🛠️ 7. 도구 (외부 자료 검색)</b>
═══════════════════════════════════════

<b>/search_my_brain &lt;keyword&gt;</b>
저장된 RAG 자료에서 hybrid 검색 (semantic + BM25). agent 가 변형
쿼리 2개 자동 생성 → 병렬 호출 → dedupe.

<b>/compare_papers &lt;주제&gt;</b>
여러 자료의 summary 한번에 비교/종합 (최대 50건). 자동 필터:
• semantic floor (cosine ≤ 0.55)
• 최소 summary 100자
• digest 패널티 0.7 + 30일 recency + 5건 quota

<b>/search_papers &lt;키워드&gt;</b>
6소스 라우팅 (S2 / arXiv / OpenAlex / CrossRef / IEEE / PubMed). 도메인
키워드 (semiconductor / LLM / cancer) 에 따라 2-3 백엔드 병렬 + dedupe.
결과에 한국어 번역 + OA 배지 + 🏛️ 소속 + 🏷️ concepts + 인용 N회.

<b>/search_patents &lt;키워드&gt;</b>
EPO OPS, DOCDB 글로벌 (EP/WO/US/KR/JP/DE/CN). 한국어 번역 +
출원/공개/우선권 날짜 + family ID + IPC + Google Patents URL.

<b>/web_search &lt;query&gt;</b>
Gemini Grounding 라이브 구글 검색. 한국어 3-7 bullet + [도메인] 출처.
자연어 트리거 매우 좁음 — "웹/구글/인터넷" 단어 포함시만.

<b>/ingest_url &lt;URL&gt;</b>
URL 영구 학습 — fetch → 청크 → 요약 → 임베딩 → Obsidian. 일반 웹 ·
arXiv · YouTube 자막 · Jina readability. 차단: LinkedIn/FB/IG/주요 paywall.
네이버 블로그는 PostView 엔드포인트로 본문 추출(차단 면제).
글 본문에 다른 링크가 있으면 학습 직후 각 링크의 제목·요약 미리보기와
함께 버튼으로 떠서, 원하는 것만 골라 추가 학습 가능 (글 속 링크까지만 —
1단계, 링크의 링크는 안 따라감). 바로 못 고르면 /pending(또는
/pending_links)에 묶음으로 남아 나중에 확인·선택 가능 (봇 재시작에도 유지).

━━━━━━━━━━━━━━━━━━━━━━

<b>특허/논문 advanced + stats 는 별도:</b>
• <b>/patents_guide</b> — /search_patents_advanced · /patent_stats (5 view)
• <b>/papers_guide</b> — /search_papers_advanced · /paper_stats (6 view: overview/trend/newcomers/network/keywords/top)

═══════════════════════════════════════
<b>🇰🇷 8. 한국 (KIPRIS / ScienceON / NTIS)</b>
═══════════════════════════════════════

<b>/company_patents &lt;회사명&gt;</b>
KIPRIS Plus 출원인명 검색. 키워드 X, 회사명 정확히.
• <code>/company_patents 삼성전기</code>
• <code>/company_patents SK하이닉스</code>
• <code>/company_patents 한양대학교 산학협력단</code>
한국 특허 전용. 등록번호 우선, 없으면 공개번호, 없으면 출원번호.

<b>/patent_detail &lt;출원번호&gt;</b>
KIPRIS 단건 상세 + abstract + IPC.
예: <code>/patent_detail 1020220012345</code>

<b>/citing_patents &lt;출원번호&gt;</b>
KIPRIS 인용 네트워크 — 이 특허를 인용한 후속.

<b>🔎 KIPRIS Plus 확장 (8개 신규)</b>

<b>/kipris_search &lt;키워드&gt;</b>
KIPRIS 통합 free-text 검색 (freeSearchInfo). 제목·초록·청구항·청구
범위 전체 매칭. 50건 + 출원일 내림차순 + abstract 포함.

<b>/kipris_pub &lt;키워드&gt;</b>
KIPRIS 공개공보만 검색 (lastvalue=A 필터). 등록 전 단계만.

<b>/kipris_reg &lt;키워드&gt;</b>
KIPRIS 등록공보만 검색 (lastvalue=R 필터). 권리 확정된 특허만.

<b>/kipris_inventor &lt;발명자명&gt;</b>
발명자 이름으로 특허 검색. 핵심 엔지니어 추적 / 이직자 IP 분석용.
예: <code>/kipris_inventor 김기남</code>

<b>/kipris_status &lt;KR 출원번호&gt;</b>
행정상태 정보 — 출원→공개→심사→등록/거절 진행 이력 lookup.

<b>/kipris_family &lt;KR 출원번호&gt;</b>
DOCDB 패밀리 — 같은 발명의 해외 출원 (US/JP/EP/WO 등). EPO 의
DOCDB family_id 와 보완.

<b>/kipris_claims &lt;KR 출원번호&gt;</b>
청구항 텍스트 — 독립항 + 종속항 전체. 권리 범위 검토용.

<b>/kipris_priority &lt;KR 출원번호&gt;</b>
우선권 주장 정보 — 해외 선출원 추적.

<b>📄 ScienceON 핵심 (ARTI/PATENT/REPORT)</b>

<b>/kr_papers &lt;키워드&gt;</b>
KISTI ScienceON 한국 논문 (SCIE/SCOPUS/KSCI 99%+ 커버, 한글 메타).

<b>/kr_patents &lt;키워드&gt;</b>
KISTI ScienceON 특허 (국제+KR, 한글 abstract).

<b>/kr_reports &lt;키워드&gt;</b>
정부 R&amp;D 보고서 (TRKO/KOSEN 풀텍스트).

<b>🌐 ScienceON 확장 (6개 콘텐츠 추가)</b>

<b>/kr_trends &lt;키워드&gt;</b>
해외과학기술동향 (ATT). 큐레이션된 외국 기술 발전 리뷰 (연구급).

<b>/kr_researcher &lt;이름|분야&gt;</b>
국내 식별 연구자 인덱스 (RESEARCHER). 연구자 프로필 + 그의 논문/
보고서/특허 목록. 특정 사람 추적용 ("김XX 연구자 논문").

<b>/kr_organ &lt;기관명&gt;</b>
국내 식별 연구기관 인덱스 (ORGAN). 기관 프로필 + publications.
KIPRIS 출원인 검색과 조합하면 회사 프로필 더 풍부.

<b>/kr_science_trend &lt;키워드&gt;</b>
ScienceON Trend (TREND). 큐레이션 토픽 트렌드 리포트 (논문/특허
통계 + 전문가 해설). ATT 와 달리 한국+국제 메타분석.

<b>🔬 NTIS (국가R&amp;D)</b>

<b>/kr_rnd_projects &lt;키워드&gt;</b>
NTIS 국가R&amp;D 과제. 각 행에:
• 과제번호 · 관리기관 · 책임자 · 연구원 수 (남/여) · 연도
• 🏷️ 키워드 (한국어)
• 🎯 목표 (Goal) · 📝 초록 (Abstract) · 💡 기대효과 (Effect)
✅ NTIS 활성 — 3개 서비스 (과제검색·분류추천·연관콘텐츠) 동일 키 공유.


<b>/kr_related &lt;pjt_id&gt; [paper|patent|researchreport|project]</b>
NTIS 연관 콘텐츠 (ConnectionContent). 과제번호로 관련 논문/특허/
보고서/연관과제 검색. type 기본 researchreport.

<b>/kr_outcomes [paper|patent|equipment] &lt;검색어&gt;</b>
NTIS 성과검색 — 정부R&amp;D 논문/특허/시설장비. ⏳ 승인 대기.

<b>/kr_govt_reports &lt;검색어&gt;</b>
NTIS 정부R&amp;D 연구보고서 — 행정 메타 정밀. ⏳ 승인 대기.

<b>/kr_agency_rnd &lt;기관명&gt;</b>
NTIS 수행기관 R&amp;D현황 — 기관별 과제·예산·논문 통계. ⏳ 승인 대기.

<b>/kr_rnd_issues &lt;토픽&gt;</b>
NTIS 이슈로보는R&amp;D — 정부R&amp;D 한정 트렌드. ⏳ 승인 대기.

승인 상태 (2026-05 기준):
✅ EPO OPS — 활성 (글로벌 특허)
⏳ KIPRIS Plus — 14건 활용신청 승인 대기 (영업일 1-3일)
   메인 (#1 특허·실용 공개·등록공보) + 인용/피인용 (#24·25) +
   행정처리/분류/패밀리/명칭변동 등 11개 향후 기능
   승인 전: /company_patents · /patent_detail · /citing_patents
   는 결과 없음 반환 (resultCode 30)
✅ KISTI ScienceON — 활성 (7개 콘텐츠 사용 가능: ARTI/PATENT/REPORT/
   ATT/RESEARCHER/ORGAN/TREND. SCENT/SNEWS 는 searchField 코드 미공개로 보류)
✅ KISTI NTIS — 3건 활성 (public_project · ConnectionContent) +
   ⏳ 6건 신청중 (public_paper · public_patent ·
   public_equipment · public_report · public_organization · public_issue)

═══════════════════════════════════════
<b>📘 9. 가이드 / 기타</b>
═══════════════════════════════════════

<b>/start · /help</b>
요약 도움말 (한 화면).

<b>/guide_lookup</b>
이 화면 (전체 명령어 상세 가이드).

═══════════════════════════════════════
<b>📊 대시보드 기록 정책</b>
═══════════════════════════════════════

자동 기록 (⚖️/📄/💾 칩으로 필터):
✅ 자연어 질문 (agent 경로)
✅ /search_patents · _advanced · _stats
✅ /search_papers · _advanced · _stats
✅ /company_patents · /patent_detail · /citing_patents
✅ /kr_papers · /kr_patents · /kr_reports
✅ /kr_rnd_projects · /kr_outcomes · /kr_govt_reports · /kr_agency_rnd · /kr_rnd_issues

기록 안 함 (운영/조회 명령어):
❌ /find · /show · /recent · /stats · /status · /usage · /cost
❌ /reset · /failed* · /queue* · /audit · /blocked_hosts
❌ /orphans · /recover_orphans
❌ /pending* · /ocr_extend
❌ /forget* · /dedupe · /cleanup
❌ /search_my_brain · /compare_papers · /web_search · /ingest_url
❌ /help · /guide_lookup · /patents_guide · /papers_guide · /wiki_guide · /wiki_recent · /wiki_new · /wiki_cost

═══════════════════════════════════════
<b>💰 비용 모델 요약</b>
═══════════════════════════════════════

• 임베딩: gemini-embedding-001 (₩200/1M tokens, 청크 캐시)
• 요약/메타/번역: gemini-2.5-flash-lite (₩140/1M in, ₩420/1M out)
  - 503 에러시 flash 로 자동 fallback
• 답변: gemini-2.5-flash · /deep 만 gemini-2.5-pro (₩1,750/1M)
• Vision: flash-lite, DPI 100, OCR_AUTO_CAP=0
• 답변 1h 캐시 (200건 LRU) · 번역 30k+ 자동 배치
• 자동 dedup 6단 (source/URL/file_hash/text_hash/body_hash/title 정규화) → ₩0

═══════════════════════════════════════
<b>📚 13. LLM Wiki (지식 누적)</b>
═══════════════════════════════════════

RAG는 질문마다 처음부터 검색·재조립 → 축적이 없음. LLM Wiki는 수집한
자료를 <b>마크다운 위키 페이지로 통합·누적</b>해서, 같은 주제 질문에 어제
정리한 내용 위에서 답하게 함. <b>RAG 대체가 아니라 그 위에 얹는 층</b>이고
<b>기본 OFF</b>(WIKI_ENABLED=0) — 켜기 전엔 기존 동작 그대로.

<b>⛔ 일일 비용 상한(가장 중요)</b>: WIKI_DAILY_BUDGET_KRW(기본 ₩1000, KST).
오늘 위키 비용이 도달하면 <b>그날 머지 즉시 중단 + ack 알람</b>, 자료는 큐
보존·다음날 0시 자동 재개. 0=무제한.

<b>동작 (append-only + periodic consolidation)</b>
• 수집 때: 큐에 적재만(LLM 0, 비용 0). 요약 600자(기본) 미만은 위키 제외.
• 매시 정시(KST) 배치: 큐를 토픽별로 묶어 위키 페이지 갱신(일일 예산 캡 내).
• 학습 요약 알림: <b>1일 1회</b> — 그날(KST) 첫 갱신이 나온 정시 직후(시각 유동).
  예산 초과·모순 알람은 별도로 즉시 발송(디듀프됨).
• <b>핵심 — 2단계 머지 전략:</b>
  ① 페이지 &lt; 30K자 → <b>append</b> (날짜별 섹션 이어붙이기, LLM 0, ₩0)
  ② 페이지 ≥ 30K자 → <b>LLM consolidation</b> (테마별 재구성, ~₩55)
  → 정보 손실 없이 페이지가 자유롭게 성장, 주기적으로 정리·압축.
• 토픽 = 수집 때 추출된 회사/태그(무료). 모순은 <b>## ⚠️ 검토 필요</b> + 알람.
• 상세 메커니즘 설명: <b>/wiki_guide</b>

<b>명령어</b>
• <b>/wiki</b> 목록(🆕=7일내 업데이트) · <b>/wiki &lt;토픽&gt;</b> 열람 · <b>/wiki_today</b> 마지막 배치
• <b>/wiki_recent [일수]</b> 최근 N일(기본7) 업데이트 토픽만 시간순
• <b>/wiki_new [일수]</b> 최근 N일(기본7) 신규 생성 토픽만
• <b>/wiki_lint</b> ₩0 구조 점검(LLM 없음): 정체 단일소스(병합/삭제 후보)·미해결 모순(검토 필요)·누락 페이지(정합성). 대시보드 위키 상단 점검 패널에도 매시 자동 표시
• <b>/wiki_status</b> 상태·오늘 ₩·한도·큐 · <b>/wiki_run</b> 수동 실행
• <b>/wiki_cost</b> 위키 전용 비용/사용량(오늘·7일·월·전체·일별추이·예상)
• <b>/wiki_drain [한도=20000]</b> 임시 예산 올려서 큐 최대 소진(끝나면 즉시 ₩1000 복귀)
• <b>/wiki_split &lt;토픽&gt;</b> 합쳐진 페이지 해체 → 개별 회사 페이지로 재분배(₩0, 다음 배치에 머지)
• <b>/wiki_dedup [merge A :: B | merge_all]</b> 유사 중복 토픽 감지(접미사 정규화·부분문자열) — 목록 확인 후 개별/전체 병합
• <b>/wiki_rename &lt;옛이름&gt; :: &lt;새이름&gt;</b> 토픽명 변경(인덱스+파일+큐+alias 일괄)
• <b>/wiki_delete &lt;토픽&gt;</b> 토픽 완전 삭제(인덱스+페이지+큐). 삭제 후 backfill·재인제스트로 안 돌아옴(영구)
• <b>/wiki_backfill [개월|all]</b> 기존 자료도 위키화(적재 ₩0, 매시 배치·일일 예산 캡 내 분산)
• <b>/wiki_pending</b> 큐 대기 현황(토픽별 문서 수)
• <b>/wiki_failed [clear|retry 토픽|retry_all]</b> 머지 실패 목록 — 3회 연속 실패 시 큐에서 분리, 재시도/삭제 가능
• <b>/wiki_off · /wiki_on</b> 즉시 끄기/켜기(killswitch, 재배포 불필요)

<b>비용/안전</b>: 추가 임베딩 0(라우팅 무료) · 머지만 과금하되 일일 ₩1000 상한 +
25토픽/run 캡으로 이중 차단 · 비용 ↑이면 캡↓/게이트↑ · 원복: /wiki_off 또는
WIKI_ENABLED=0(Chroma/meta.db 안 건드려 끄면 기존 RAG 그대로) · 상세 docs/WIKI.md

═══════════════════════════════════════
<b>📒 14. 학습 노트</b>
═══════════════════════════════════════

검색용 위키와 별개로, <b>내가 직접 공부한 자료를 다시 읽고 되새김질해 오래
체화</b>하는 개인 노트 시스템. 전용 학습 채널에 자료(URL·PDF·유튜브·PPT·워드·
텍스트)를 올리면 봇이 <b>노트 형태로 재구성</b>(요약 아님: 한 줄 요지·개념
지도(Mermaid)·정리·표·수식·핵심용어)해서 대시보드에 쌓는다. 매일 알아서 읽어
되새김질하는 용도. 노트당 flash 합성 ~₩수(파싱 무료, OCR 페이지만 유료).
채널에서 /notes_guide 입력 시 사용법을 채널에 게시(핀 고정용).

<b>명령어</b>
• <b>/notes</b> 노트 개수 + 대시보드 링크
• <b>/notes_guide</b> 학습 노트 상세 사용법(자료 넣기·노트 구성·대시보드·비용)
"""


async def cmd_guide_lookup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/guide_lookup — 봇의 모든 명령어 상세. _HELP_TEXT 가 4000자 cap
    이라 운영 명령어들의 사용법까지 다 넣을 수 없어서 별도 명령어로
    분리. _split_for_telegram 이 길이 초과시 자동 분할 송신."""
    if not _is_owner(update):
        return
    async with _SustainedTyping(update, ctx):
        for chunk in _split_for_telegram(_LOOKUP_GUIDE_TEXT):
            await update.message.reply_text(
                chunk, parse_mode="HTML", disable_web_page_preview=True,
            )


_PATENTS_GUIDE_TEXT = """<b>📘 특허 명령어 상세 가이드</b>

전체 13가지 명령어. KIPRIS Plus (한국, 11개) + EPO OPS (글로벌, 2개) 두 백엔드.

━━━━━━━━━━━━━━━━━━━━━━

<b>1) /search_patents &lt;키워드&gt;</b>
글로벌 free-text 특허 검색 (EPO OPS, DOCDB — EP/WO/US/KR/JP/DE/CN).

• 키워드 1개: <code>/search_patents HBM</code> → 정확히 HBM 매칭
• 다단어: <code>/search_patents hybrid bonding</code> → "hybrid bonding" 구문 그대로
• 한 메시지 = 한 명령 (여러 줄 동시 보내면 첫줄만 명령으로 인식)

결과: 최대 15건, 최신순. 각 행에 한국어 번역 + 출원/공개/우선권 날짜,
출원번호+kind label, family ID, 출원인+국가코드, 발명자 총 인원,
IPC 분류 최대 4개, abstract 900자, Google Patents URL.

━━━━━━━━━━━━━━━━━━━━━━

<b>2) /search_patents_advanced &lt;키워드&gt; [필터]</b>
필터로 검색 범위 좁히기. 필터는 <code>key=value</code> 형식, 공백으로 구분.

<b>필터 옵션:</b>
• <code>applicant=회사명</code> — 출원인 한정 (예: applicant=SAMSUNG)
• <code>inventor=발명자명</code> — 발명자 한정 (예: inventor=Smith)
• <code>ipc=H01L21</code> — IPC subclass 한정 (반도체 제조공정만)
• <code>country=KR</code> — 출원국 한정 (KR/US/EP/WO/JP/CN/DE/TW...)
• <code>from=2023</code> — 공개일 ≥ 2023
• <code>to=2026</code> — 공개일 ≤ 2026

<b>예시:</b>
<code>/search_patents_advanced HBM applicant=SAMSUNG from=2024</code>
<code>/search_patents_advanced photonic ipc=G02F1 country=US</code>
<code>/search_patents_advanced AI accelerator applicant=NVIDIA from=2023 to=2026</code>

━━━━━━━━━━━━━━━━━━━━━━

<b>3) /patent_stats &lt;키워드&gt; [view]</b>
EPO 에서 최대 2000건 가져와 메모리에서 통계 집계. 약 60~120초 소요.

<b>5가지 view:</b>

🔹 <b>overview</b> (기본): 출원인 TOP 10 + 국가별 + 연도별 막대 + IPC TOP 8
   <code>/patent_stats hybrid bonding</code>

🔹 <b>trend</b>: TOP 5 회사 × 연도별 Mermaid xychart (PNG 이미지 렌더)
   <code>/patent_stats hybrid bonding trend</code>

🔹 <b>newcomers</b>: 최근 12개월 첫 등장 출원인
   <code>/patent_stats hybrid bonding newcomers</code>

🔹 <b>network</b>: 공동출원 (회사 A ⇄ 회사 B) 페어
   <code>/patent_stats hybrid bonding network</code>

🔹 <b>keywords</b>: Gemini 가 abstract 들에서 추출한 기술 키워드 (~₩3-5)
   <code>/patent_stats hybrid bonding keywords</code>

회사명은 자동 정규화 — "SAMSUNG ELECTRONICS CO LTD" / "Samsung
Electronics Co., Ltd." 둘 다 SAMSUNG ELECTRONICS 한 그룹으로 카운트.

━━━━━━━━━━━━━━━━━━━━━━

<b>4) /company_patents &lt;회사명&gt;</b>
한국 회사 특허 (KIPRIS Plus 출원인명 검색). 키워드 X, 회사명 정확히.

• <code>/company_patents 삼성전기</code>
• <code>/company_patents SK하이닉스</code>
• <code>/company_patents 한양대학교 산학협력단</code>

결과: 등록번호 우선, 없으면 공개번호, 없으면 출원번호.
한국 특허 전용 (외국 회사는 /search_patents 사용).

━━━━━━━━━━━━━━━━━━━━━━

<b>5) /patent_detail &lt;출원번호&gt; · /citing_patents &lt;출원번호&gt;</b>
KIPRIS Plus 단건 상세 / 인용 네트워크. 출원번호 (숫자만) 입력.

• <code>/patent_detail 1020220012345</code> — 그 특허의 abstract+IPC
• <code>/citing_patents 1020220012345</code> — 이 특허를 인용한 특허 목록

━━━━━━━━━━━━━━━━━━━━━━

<b>6) /kipris_* — KIPRIS Plus 확장 (8개)</b>
같은 KIPRIS Plus 인증키로 추가 endpoint 활용. 모두 50건 + 최신순 + 한국어
번역 (검색형) / 풍부한 메타 (lookup형).

<b>검색형 (키워드 또는 발명자명 입력)</b>
• <code>/kipris_search HBM3</code> — 통합 free-text (제목·초록·청구항·청구범위)
• <code>/kipris_pub HBM3</code> — 공개공보만 (등록 전)
• <code>/kipris_reg HBM3</code> — 등록공보만 (권리 확정)
• <code>/kipris_inventor 김기남</code> — 발명자 이름으로 검색

<b>Lookup형 (출원번호 입력)</b>
• <code>/kipris_status 1020220012345</code> — 행정상태 (출원→공개→심사→등록)
• <code>/kipris_family 1020220012345</code> — DOCDB 패밀리 (해외 출원)
• <code>/kipris_claims 1020220012345</code> — 청구항 텍스트 (독립항+종속항)
• <code>/kipris_priority 1020220012345</code> — 우선권 주장 정보

검색형은 sortSpec=AD desc 로 출원일 최신순. Abstract / IPC / 등록상태가
list response 에 이미 포함돼 한 번의 호출로 충분.

━━━━━━━━━━━━━━━━━━━━━━

<b>비용 / 한도</b>
• EPO OPS: 무료 4GB/월 (rolling 30-day). 개인용 사실상 무한.
  - /search_patents 1회 ≈ 50KB · /patent_stats 1회 ≈ 200KB
  - 매일 stats 10회 + 일반검색 50회 = 월 한도의 ~1%
• KIPRIS Plus: 무료 (한국 특허청 직접)
• 번역: 검색당 ~₩5-7 (Flash-Lite 1배치 콜)
• keywords view 만 추가 Gemini 콜 (~₩3-5)

━━━━━━━━━━━━━━━━━━━━━━

<b>📊 백엔드 활성 상태 (2026-05 기준)</b>

✅ <b>EPO OPS</b> — 활성 (글로벌 특허, /search_patents·_advanced·_stats 동작)
✅ <b>KIPRIS Plus</b> — 활성 (11개 명령어 검증 완료 2026-05)
   기본 3종 (applicantNameSearchInfo / applicationNumberSearchInfo /
   CitingService): /company_patents · /patent_detail · /citing_patents
   확장 8종 (patUtiModInfoSearchSevice 의 freeSearchInfo + 서지정보):
   /kipris_{search,pub,reg,inventor,status,family,claims,priority}
✅ <b>KISTI ScienceON</b> — 활성 (9개 콘텐츠 모두 승인)
   주요 명령어 9종: /kr_papers · /kr_patents · /kr_reports ·
   /kr_trends · /kr_researcher · /kr_organ ·
   /kr_science_trend
   /kr_patents (ScienceON) = 한글 메타 풍부, /search_patents (EPO)
   = 글로벌 영문 위주 — 용도 분리해 사용
✅ <b>KISTI NTIS</b> — 활성 (3건 승인, /kr_rnd_projects 동작 + 자연어 분류추천/연관콘텐츠 가능)

━━━━━━━━━━━━━━━━━━━━━━

모든 검색 결과는 자동으로 대시보드에 기록됨 (⚖️ patent 칩으로 필터).
"""


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
    async with _SustainedTyping(update, ctx):
        for chunk in _split_for_telegram(_HELP_TEXT):
            await update.message.reply_text(
                chunk, parse_mode="HTML", disable_web_page_preview=True,
            )



_PAPERS_GUIDE_TEXT = """<b>📘 논문 명령어 상세 가이드</b>

3가지 명령어. 6개 백엔드 (S2/arXiv/OpenAlex/CrossRef/IEEE/PubMed) 자동
라우팅 + OpenAlex 단일 백엔드 기반 advanced/stats.

━━━━━━━━━━━━━━━━━━━━━━

<b>1) /search_papers &lt;키워드&gt;</b>
다중 소스 free-text 검색. 쿼리 도메인 키워드 (semiconductor /
packaging / hybrid bonding / LLM / cancer 등) 에 따라 최적 백엔드
2-3개 자동 선택 + 병렬 호출 + dedupe.

결과 (최대 15편) 에 한국어 번역 + 풍부한 메타데이터:
• (년도) · 🔓 OA · 논문 type
• 1st author et al. (총 N명) · 학술지 · 인용 N회 · 참고문헌 N개
• 🏛️ 소속 기관 (최대 3곳)
• 🏷️ 주제 분류 (OpenAlex concepts 최대 3개)
• abstract 900자 + PDF/DOI/URL 링크

OA / institution / concept / referenced_count 는 OpenAlex 행에만
표시됨 (다른 5개 소스는 해당 정보 없음 → 자동 skip).

━━━━━━━━━━━━━━━━━━━━━━

<b>2) /search_papers_advanced &lt;키워드&gt; [필터]</b>
OpenAlex 단일 백엔드 + 구조화 필터.

<b>필터 옵션:</b>
• <code>author=이름</code> — 저자 부분일치 (예: author=Lau, author=John Smith)
• <code>venue=학술지</code> — 학술지/학회명 부분일치 (예: venue=Nature)
• <code>concept=주제</code> — OpenAlex 주제 분류 (예: concept=semiconductor)
• <code>from=2022</code> — 출간년도 ≥
• <code>to=2026</code> — 출간년도 ≤
• <code>oa=true</code> — 오픈액세스만
• <code>min_citations=10</code> — 인용 N회 이상
• <code>type=article</code> — 논문 type (article/book-chapter/preprint)

<b>예시:</b>
<code>/search_papers_advanced hybrid bonding author=Lau from=2022</code>
<code>/search_papers_advanced LLM oa=true min_citations=100</code>
<code>/search_papers_advanced cancer immunotherapy venue=Nature from=2023</code>
<code>/search_papers_advanced AI accelerator author=Han concept=hardware</code>

키워드는 비워두고 필터만으로도 검색 가능 (예: <code>author=John LeCun from=2020</code>
→ LeCun 의 2020년 이후 모든 논문).

━━━━━━━━━━━━━━━━━━━━━━

<b>3) /paper_stats &lt;키워드&gt; [view]</b>
OpenAlex 에서 최대 1000편 가져와 통계 집계. 약 20~30초 소요.

<b>5가지 view:</b>

🔹 <b>overview</b> (기본): 저자 TOP 10 + 학술지 TOP 8 + 기관 TOP 8 +
   주제 분류 TOP 8 + 연도별 막대 + 🔓 OA 비율 + 인용 분포
   <code>/paper_stats hybrid bonding</code>

🔹 <b>trend</b>: TOP 5 저자 × 연도별 Mermaid xychart (PNG 이미지 렌더)
   <code>/paper_stats hybrid bonding trend</code>

🔹 <b>newcomers</b>: 최근 12개월 첫 등장 저자
   <code>/paper_stats hybrid bonding newcomers</code>

🔹 <b>network</b>: 공저자 페어 (논문은 공저 많아서 풍부함)
   <code>/paper_stats hybrid bonding network</code>

🔹 <b>keywords</b>: Gemini 가 abstract 들에서 추출한 기술 키워드 (~₩3-5)
   <code>/paper_stats hybrid bonding keywords</code>

🔹 <b>top</b>: 인용수 (OpenAlex cited_by_count) TOP 15 영향력 논문 —
   무료 (인용수가 bulk fetch 응답에 inline, N+1 호출 X)
   <code>/paper_stats hybrid bonding top</code>

저자명 자동 정규화: Jr/Sr/II/III suffix 제거 + smart Title Case
(소문자/혼합대소문자 통일, 짧은 약어는 보존).

━━━━━━━━━━━━━━━━━━━━━━

<b>비용 / 한도</b>
• OpenAlex: 무료, no key (polite UA 만 권장 — .env 의
  OPENALEX_MAILTO 가 자동 부착됨). 일일 요청 한도 사실상 무제한.
  - /search_papers 1회 = 1~3 백엔드 콜
  - /paper_stats 1회 = OpenAlex 2 페이지 (400편)
• 번역: 검색당 ~₩5-7 (Flash-Lite 1배치 콜)
• keywords view 만 추가 Gemini 콜 (~₩3-5)
• 다른 5개 백엔드 (S2/arXiv/CrossRef/IEEE/PubMed): 키 있을 때
  자동 fan-out, 없으면 skip — /search_papers 만 사용

━━━━━━━━━━━━━━━━━━━━━━

<b>특허 vs 논문 명령어 비교</b>
같은 패턴 두 도메인:
• /search_patents  ↔ /search_papers          (free-text, 다중 소스)
• /search_patents_advanced ↔ /search_papers_advanced (구조화 필터)
• /patent_stats    ↔ /paper_stats            (400건 bulk + 5 view)

차이:
• 특허는 EPO OPS 단일 + KIPRIS, 논문은 6 백엔드 + OpenAlex
• 논문 stats 는 OA share / citation distribution 추가
• 논문 network 는 공저자 (보통 3-10인) 가 풍부

━━━━━━━━━━━━━━━━━━━━━━

모든 검색 결과는 자동으로 대시보드에 기록됨 (📄 paper 칩으로 필터).
"""



async def cmd_papers_guide(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/papers_guide — 논문 명령어 3종 (search_papers, advanced, stats)
    의 사용법, 8개 필터, 5가지 view, 비용, 특허 명령어와의 비교까지
    한 곳에서. _HELP_TEXT 4000 cap 보호하려고 분리."""
    if not _is_owner(update):
        return
    async with _SustainedTyping(update, ctx):
        for chunk in _split_for_telegram(_PAPERS_GUIDE_TEXT):
            await update.message.reply_text(
                chunk, parse_mode="HTML", disable_web_page_preview=True,
            )


async def cmd_patents_guide(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/patents_guide — 특허 명령어 5종 (search_patents,
    search_patents_advanced, patent_stats, company_patents,
    patent_detail/citing_patents) 의 사용법, 필터 옵션, view 종류,
    비용/한도, KISTI ScienceON 활성화시 영향까지 전부 한 곳에서.
    _HELP_TEXT 가 4000 cap 에 걸려있어서 별도 명령어로 빼둠."""
    if not _is_owner(update):
        return
    async with _SustainedTyping(update, ctx):
        for chunk in _split_for_telegram(_PATENTS_GUIDE_TEXT):
            await update.message.reply_text(
                chunk, parse_mode="HTML", disable_web_page_preview=True,
            )


_WIKI_GUIDE_TEXT = """<b>📘 위키 머지 시스템 상세 가이드 (/wiki_guide)</b>

이 봇의 위키는 <b>append-only + periodic consolidation</b> 방식으로 동작합니다.
DB 엔진(LSM Tree), 이벤트 소싱, 위키피디아 편집 모델 등에서 검증된 패턴입니다.

━━━━━━━━━━━━━━━━━━━━━━
<b>🔄 머지 전략 (2단계)</b>
━━━━━━━━━━━━━━━━━━━━━━

<b>1단계: Append (대부분의 배치)</b>
• 조건: 기존 페이지 &lt; 30,000자
• 동작: 새 문서를 <b>날짜별 섹션으로 이어붙이기</b>
• 비용: <b>₩0</b> (LLM 호출 없음)
• 효과: 정보 손실 0 — 모든 사실이 원문 그대로 페이지에 남음

<code>## 📌 2026-06-08 업데이트
### 삼성전자 2Q26 실적 프리뷰
[요약 내용]
— 출처: [[삼성전자 2Q26 실적 프리뷰]]</code>

<b>2단계: LLM Consolidation (주기적 정리)</b>
• 조건: 기존 페이지 ≥ 30,000자
• 동작: LLM(Flash)이 전체 페이지를 <b>테마별로 재구성·압축</b>
• 비용: ~₩55 (토픽당 1~2주에 1회)
• 효과: 시계열 압축, 섹션 재정렬, 중복 제거, 모순 감지

<b>특수 케이스: 신규 토픽</b>
• 조건: 아직 위키 페이지가 없을 때
• 동작: LLM이 처음부터 구조화된 초기 페이지 생성
• 이후: append 모드로 전환

━━━━━━━━━━━━━━━━━━━━━━
<b>📊 왜 이 방식인가?</b>
━━━━━━━━━━━━━━━━━━━━━━

<b>이전 방식 (매번 전체 재작성)의 문제:</b>
• 매 배치마다 기존 페이지를 LLM이 전체 재작성 → <b>연쇄 손실 압축</b>
• 236개 소스가 지나간 토픽에서 초기 정보가 완전 소실
• 10K자 페이지에 갇혀 정보가 절대 축적되지 않음

<b>현재 방식의 장점:</b>
• 페이지가 30K자까지 <b>자유 성장</b> → 정보 보존 극대화
• 대부분 배치가 <b>₩0</b> → 예산을 정리 품질에 집중
• 정리 시 <b>12,000 토큰 출력</b> (~18K자) → 풍성한 정리 결과
• git 이력에 정리 전 버전 보존 → 복구 가능

이 패턴은 LSM Tree(LevelDB/RocksDB/Cassandra), Event Sourcing(CQRS),
위키피디아 편집 모델에서 동일한 원리로 사용됩니다.

━━━━━━━━━━━━━━━━━━━━━━
<b>💰 비용 구조</b>
━━━━━━━━━━━━━━━━━━━━━━

• <b>Append</b>: ₩0 (문자열 이어붙이기, LLM 없음)
• <b>Consolidation</b>: ~₩55/회 (입력 25K자 + 출력 12K 토큰)
• <b>신규 토픽 초기화</b>: ~₩25/회
• 일일 예산: <b>₩1,000</b> (초과 시 중단, 자료는 큐 보존, 다음날 재개)

예시: 하루 12토픽 배치 중 10개 append(₩0) + 2개 consolidation(₩110) = <b>₩110/일</b>

━━━━━━━━━━━━━━━━━━━━━━
<b>⚙️ 설정 값</b>
━━━━━━━━━━━━━━━━━━━━━━

• <code>WIKI_CONSOLIDATION_CHARS=30000</code> — 이 이상이면 LLM 정리 발동
• <code>WIKI_MAX_PAGE_CHARS=30000</code> — 정리 시 LLM 입력 상한 (=CONSOLIDATION과 동일, 잘림 방지)
• <code>WIKI_MERGE_MAX_TOKENS=12000</code> — 정리 출력 상한 (~18K 한국어 자)
• <code>WIKI_MAX_DOCS_PER_TOPIC=6</code> — 1회 배치 토픽당 최대 문서
• <code>WIKI_DAILY_BUDGET_KRW=1000</code> — 일일 예산 (KST)
• 배치 주기: <b>매시 정시</b> (KST) — 큐 자동 머지, 일일 예산 캡 내
• 알림: 학습 요약 <b>1일 1회</b>(그날 첫 갱신된 정시 직후, 시각 유동) · 예산/모순 알람은 별도 즉시
• 점검: <b>/wiki_lint</b>(₩0, LLM 없음) — 정체 단일소스·미해결 모순·누락 페이지. 매시 배치가 자동 갱신해 대시보드 위키 상단 점검 패널에 표시(온디맨드 실행도 가능)
• <code>WIKI_MIN_SUMMARY_CHARS=600</code> — 이하 요약은 위키 제외

━━━━━━━━━━━━━━━━━━━━━━
<b>🔮 로드맵</b>
━━━━━━━━━━━━━━━━━━━━━━

<b>Phase 2 (미정):</b> 팩트 테이블 — 인제스트 시 원자적 사실을 SQLite로 추출.
위키 페이지가 팩트 DB의 렌더링 뷰가 되면 정보 손실 완전 해결 + 모순 감지가
DB 쿼리로 가능. 현재 append 방식이 충분하면 보류.
"""


async def cmd_wiki_guide(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/wiki_guide — 위키 머지 시스템의 핵심 메커니즘, 비용 구조, 설정 값 상세."""
    if not _is_owner(update):
        return
    async with _SustainedTyping(update, ctx):
        for chunk in _split_for_telegram(_WIKI_GUIDE_TEXT):
            await update.message.reply_text(
                chunk, parse_mode="HTML", disable_web_page_preview=True,
            )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    async with _SustainedTyping(update, ctx):
        await update.message.reply_text(
            f"문서 {meta.count()}개 / 청크 {vector.chunk_count()}개"
        )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Quick health snapshot — what is the bot doing right now?
    Reads in-memory counters so it returns even when the event loop
    is busy with ingest. Owner-only."""
    if not _is_owner(update):
        return
    async with _SustainedTyping(update, ctx):
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
            cleanup_line = "\n🧹 마지막 메모리 청소: 아직 없음 (3분 주기 자동)"
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
        if wiki.enabled():
            wiki_q = wiki.queue_size()
            wiki_blocked = " ⛔예산초과" if wiki.budget_exceeded() else ""
            out += f"\n📚 위키: 큐 {wiki_q:,}건{wiki_blocked}"
        await update.message.reply_text(out)


async def cmd_usage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Ingest velocity, type breakdown, rough cost band so the user
    can spot anomalies (sudden surge, drop) at a glance."""
    if not _is_owner(update):
        return
    async with _SustainedTyping(update, ctx):
        # All SQLite/Chroma aggregation in ONE thread hop — daily_breakdown
        # alone is 7 queries; running these raw on the event loop stalled
        # heartbeats under ingest+dashboard write contention.
        def _gather_usage():
            return {
                "s": meta.usage_stats(),
                "chunks": vector.chunk_count(),
                "today": cost.today_krw(),
                "week": cost.period_krw(7),
                "mtd": cost.month_to_date_krw(),
                "daily": cost.daily_breakdown(7),
            }
        snap = await asyncio.to_thread(_gather_usage)
        s = snap["s"]
        chunks = snap["chunks"]
        queue_len = len(_INGEST_RETRY_QUEUE)
        failed_len = len(_INGEST_FAILED)

        types_line = ", ".join(f"{t}:{c}" for t, c in s["types"][:8]) or "-"

        today = snap["today"]
        week = snap["week"]
        mtd = snap["mtd"]
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
                           ("wiki", "📚 wiki"), ("unknown", "❓ unknown")):
            if tag in by_purpose:
                d = by_purpose[tag]
                purpose_lines.append(
                    f"    {label}  ₩{d['cost']:,.1f}  ({d['calls']}콜)"
                )
        purpose_breakdown = ("\n" + "\n".join(purpose_lines)) if purpose_lines else ""

        # Context-cache hit rate (Gemini implicit caching, 75% off the
        # repeated system+tool prefix). High % on query traffic = the
        # agent loop is reusing its cached prefix as intended.
        total_in = today.get("total_in", 0)
        total_cached = today.get("total_cached", 0)
        cache_line = ""
        if total_in:
            pct = total_cached / total_in * 100
            cache_line = (
                f"\n    💾 캐시 적중  {pct:.0f}%  "
                f"({total_cached:,}/{total_in:,} 입력토큰 할인)"
            )

        # Tiny inline bar chart for the last 7 days so trends are visible
        # without leaving the /usage screen.
        daily = snap["daily"]
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
            f"{cache_line}"
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
        # Wiki status snippet
        if wiki.enabled():
            def _gather_wiki():
                return (wiki.today_cost_krw(), wiki.budget_krw(),
                        wiki.queue_size(), len(wiki.list_topics()),
                        wiki.budget_exceeded())
            (wiki_today, wiki_budget, wiki_q, wiki_pages,
             wiki_over) = await asyncio.to_thread(_gather_wiki)
            out += (
                f"\n\n📚 위키"
                f"\n  페이지: {wiki_pages}개 · 큐: {wiki_q:,}건"
                f"\n  오늘 비용: ₩{wiki_today:,.0f} / ₩{wiki_budget:,.0f}"
            )
            if wiki_over:
                out += " ⛔초과"
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
    async with _SustainedTyping(update, ctx):
        def _gather_cost():
            return (cost.today_krw(), cost.period_krw(7),
                    cost.month_to_date_krw(), cost.daily_breakdown(14))
        today, week, mtd, daily = await asyncio.to_thread(_gather_cost)

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
                           ("wiki 합성", "wiki"),
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


async def cmd_eval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Run the answer-quality regression eval against data/eval_golden.json."""
    if not _is_owner(update):
        return
    from .agent import eval as _eval
    _eval.ensure_template()
    async with _SustainedTyping(update, ctx):
        ev = await _eval.run_eval()
    report = _eval.format_report(ev)
    await update.message.reply_text(report, parse_mode="HTML",
                                    disable_web_page_preview=True)


async def cmd_eval_seed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Generate a starter eval golden set from recent real Q&A turns.

    /eval_seed [N]  — append up to N (default 20, max 50) new items to
    data/eval_golden.json from qna.db. Objective retrieval-regression
    seed: query + expected_sources auto-filled, expected_facts left
    blank for the user to curate. Never clobbers existing items."""
    if not _is_owner(update):
        return
    n = 20
    if ctx.args:
        try:
            n = max(1, min(50, int(ctx.args[0])))
        except ValueError:
            pass
    async with _SustainedTyping(update, ctx):
        from .agent import eval as _eval
        res = await asyncio.to_thread(_eval.seed_from_qna, n)
    await update.message.reply_text(
        f"📋 골든셋 초안 생성 완료\n"
        f"• 신규 추가: <b>{res['added']}</b>개 (총 {res['total']}개, "
        f"과거 Q&A {res['scanned']}건 스캔)\n"
        f"• 파일: <code>data/eval_golden.json</code>\n\n"
        f"<b>다음 단계 (검증 정확도용):</b>\n"
        f"1. 각 항목 <code>expected_facts</code>에 '답변에 꼭 있어야 할 사실' "
        f"1~2개 채우기 (비우면 출처 체크만, 사실 체크는 스킵)\n"
        f"2. <code>expected_sources</code>를 더 짧고 고유한 키워드로 다듬기 (선택)\n"
        f"3. <code>/eval</code> 로 채점 — 코드 변경 전/후 비교에 사용\n\n"
        f"⚠️ 출처는 과거 답변이 실제 인용한 자료라 '그 자료가 다시 나오나'를 "
        f"객관 검증함. 사실은 순환 채점을 피하려고 비워뒀어 (네가 채움).",
        parse_mode="HTML", disable_web_page_preview=True,
    )


async def cmd_recent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    async with _SustainedTyping(update, ctx):
        n = 10
        if ctx.args and ctx.args[0].isdigit():
            n = max(1, min(int(ctx.args[0]), 50))
        items = await asyncio.to_thread(meta.recent, n)
        if not items:
            await update.message.reply_text("아직 비어있어요.")
            return
        lines = [f"📚 최근 {len(items)}개 학습"]
        for r in items:
            title = _clean_text(r.get("title") or "(제목 없음)")[:90]
            ingested = (r.get("ingested_at") or "")[:10]
            lines.append(f"\n[{r['type']}]  {title}\n  {ingested}  ·  id {r['id']}")
        # /recent 50 ≈ 6,000 chars — over the 4096 cap without a split.
        for chunk in _split_for_telegram("\n".join(lines)):
            await update.message.reply_text(chunk)


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
    # Off-loop: delete_doc does a full-collection metadata scan (seconds
    # at 253k chunks) — raw on the loop it stalls heartbeats.
    n = await asyncio.to_thread(vector.delete_doc, doc_id)
    ok = await asyncio.to_thread(meta.delete, doc_id)
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
    noisy = await asyncio.to_thread(meta.find_noise)
    if not noisy:
        await update.message.reply_text("정리할 노이즈 없음 ✨")
        return
    args = [a.lower() for a in (ctx.args or [])]
    if "confirm" in args:
        # ONE where-$in chroma pass + offloaded meta deletes — the old
        # per-doc delete_doc loop ran N full-collection scans raw on the
        # event loop (minutes at 253k chunks → watchdog-restart risk
        # mid-delete, leaving meta/vector diverged).
        ids = [r["id"] for r in noisy]
        n_chunks = await asyncio.to_thread(vector.delete_docs, ids)
        await asyncio.to_thread(lambda: [meta.delete(i) for i in ids])
        for r in noisy:
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
    candidates = await asyncio.to_thread(meta.find_forwarded_digests)
    if not candidates:
        await update.message.reply_text("📭 자동 포워딩 디지스트 없음 ✨")
        return
    args = [a.lower() for a in (ctx.args or [])]
    if "confirm" in args:
        ids = [d["id"] for d in candidates]
        n_chunks = await asyncio.to_thread(vector.delete_docs, ids)
        await asyncio.to_thread(lambda: [meta.delete(i) for i in ids])
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
    groups = await asyncio.to_thread(meta.find_duplicates)
    if not groups:
        await update.message.reply_text("중복 없음 ✨")
        return
    args = [a.lower() for a in (ctx.args or [])]
    if "confirm" in args:
        doomed: list[dict] = []
        for g in groups:
            keeper = max(g, key=lambda d: len(d.get("summary") or ""))
            doomed.extend(d for d in g if d["id"] != keeper["id"])
        ids = [d["id"] for d in doomed]
        chunks_removed = await asyncio.to_thread(vector.delete_docs, ids)
        await asyncio.to_thread(lambda: [meta.delete(i) for i in ids])
        for d in doomed:
            # Stop the orphan-scan loop: file is still on disk
            # after meta delete; without this, _scan_orphan_files
            # finds the file missing from documents and re-queues
            # it on every restart → infinite dedup/retry cycle.
            fname = _filename_from_source(d.get("source") or "")
            if fname:
                _record_dedup_confirmed(fname)
        await update.message.reply_text(
            f"✅ 중복 {len(doomed)}건 / 청크 {chunks_removed}개 제거 완료\n"
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


# Per-chat /find result cache for /show navigation. Stores the last
# /find's items + first message-id so /show can offer "다음 항목"
# walking from the user's current position instead of forcing them
# to scroll the (often massive) find result back to the top to find
# their next click. TTL-evicted at access time.
_FIND_CONTEXT: dict[int, dict] = {}
_FIND_CONTEXT_TTL = 3600  # 1h — beyond that the user has probably
                          # moved on and a fresh /find is cheaper.

# Summary truncation for the find-nav '다음' preview. 5 full summaries
# can overflow Telegram's 4000-char cap → multi-message split → N
# sequential sends = the slow '다음' the user hit. 250 chars/item keeps
# 5 items inside one message (one round-trip). Full summary stays on
# the initial /find render and via /show_<id>.
_FIND_PREVIEW_SUMMARY_LIMIT = 250


def _set_find_context(chat_id: int, query: str, items: list[dict],
                      first_message_id: int | None,
                      chunk_counts: dict | None = None) -> None:
    """Save /find result for /show navigation. chunk_counts is the
    bulk-fetched {doc_id: count} map from /find so the preview can
    reuse it without re-querying ChromaDB per page."""
    _FIND_CONTEXT[chat_id] = {
        "query": query,
        "items": items,
        "first_message_id": first_message_id,
        "chunk_counts": chunk_counts or {},
        "ts": time.time(),
    }


def _get_find_context(chat_id: int) -> dict | None:
    """Return cached /find context for this chat, or None if absent
    or expired. Removes expired entries on lookup."""
    ctx = _FIND_CONTEXT.get(chat_id)
    if not ctx:
        return None
    if time.time() - ctx.get("ts", 0) > _FIND_CONTEXT_TTL:
        _FIND_CONTEXT.pop(chat_id, None)
        return None
    return ctx


def _find_item_index(items: list[dict], doc_id: str) -> int | None:
    """Return the position of doc_id in the /find result list, or None."""
    for i, m in enumerate(items):
        if (m.get("id") or "") == doc_id:
            return i
    return None


def _format_find_item(m: dict, index: int | None = None,
                      chunk_counts: dict | None = None,
                      summary_limit: int | None = None) -> str:
    """Render one /find result as a per-item block.

    Shared by /find (no index numbering, bulk render) and the
    find-nav preview (numbered #N. for position context, same
    chunk_counts cached at /find time). The two paths render
    identically — same fields, same summary, same icons — so the
    user can decide whether to /show without re-running /find.

    summary_limit: when set, the summary is truncated to that many
    chars (+ /show pointer) so the find-nav '다음' preview stays
    inside ONE Telegram message — one round-trip instead of N
    sequential sends. Full summary still shows on the initial /find
    and via /show. None = full summary (initial /find path)."""
    import html as _html
    import json as _json
    title = _html.escape(_clean_text(
        m.get("title") or "(제목 없음)")[:80])
    ingested = (m.get("ingested_at") or "")[:10]
    source = m.get("source") or ""
    summary_raw = _clean_text(m.get("summary") or "")
    doc_id = m.get("id") or ""
    truncated = (
        summary_limit is not None
        and len(summary_raw) > summary_limit
    )
    if truncated:
        summary_raw = summary_raw[:summary_limit].rstrip()
    summary_full = _html.escape(summary_raw)

    loc = ""
    if source.startswith(("http://", "https://")):
        short_url = source.replace("https://", "").replace(
            "http://", "")[:70]
        loc = f"📎 {short_url}"
    elif source.startswith("tg-"):
        kind = source.split(":", 1)[0]
        loc = f"💬 {kind}"

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

    info_bits: list[str] = []
    if ingested:
        info_bits.append(f"학습 {ingested}")
    if published:
        info_bits.append(f"발행 {published}")
    if chunk_counts is not None:
        n_chunks = int(chunk_counts.get(doc_id, 0) or 0)
        if n_chunks:
            info_bits.append(f"{n_chunks}청크")

    head = f"#{index}. {title}" if index is not None else title
    out = f"\n\n📄 <b>{head}</b>"
    if info_bits:
        out += f"\n  <i>{' · '.join(info_bits)}</i>"
    if doc_id:
        out += f"\n  🆔 /show_{_html.escape(doc_id)}"
    if loc:
        out += f"\n  {loc}"
    if meta_line:
        out += f"\n  🏷 {meta_line}"
    if summary_full:
        out += f"\n\n{summary_full}"
        if truncated:
            out += f"… <i>(전문 /show_{_html.escape(doc_id)})</i>"
    return out


def _format_find_preview_chunk(query: str, items: list[dict],
                               start_idx: int,
                               chunk_counts: dict | None = None,
                               count: int = 5) -> str:
    """Compact preview of items[start_idx:start_idx+count] for the
    find-nav callback. Uses the same per-item renderer as /find so
    the user gets the full summary + meta + source, not just a stub
    — they can decide which next item to /show without re-running
    /find or scrolling back."""
    import html as _html
    end_idx = min(len(items), start_idx + count)
    total = len(items)
    out = [
        f"🔍 <b>'{_html.escape(query)}'</b> — "
        f"검색 결과 #{start_idx + 1}~#{end_idx} / {total}",
    ]
    for i in range(start_idx, end_idx):
        out.append(_format_find_item(
            items[i], index=i + 1, chunk_counts=chunk_counts,
            summary_limit=_FIND_PREVIEW_SUMMARY_LIMIT,
        ))
    return "".join(out)


def _find_next_keyboard(items: list[dict], next_start: int,
                        page_size: int = 5) -> "InlineKeyboardMarkup | None":
    """Build the [⏩ 다음 N개] inline keyboard for find navigation.
    Returns None when next_start ≥ len(items) (nothing more)."""
    total = len(items)
    if next_start >= total:
        return None
    end_idx = min(total, next_start + page_size)
    label = f"⏩ find 다음 #{next_start + 1}~#{end_idx} / {total}"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(label, callback_data=f"findnext:{next_start}"),
    ]])


async def cmd_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Locate saved docs by title/source fragment. Compact per-item
    format + auto-split across multiple Telegram messages so every
    match is visible (no '나머지 N개 생략'). Snippet shortened to
    120 chars — enough to recognise the doc, not so much it buries
    later results."""
    if not _is_owner(update):
        return
    async with _SustainedTyping(update, ctx):
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
        # _format_find_item handles all the title/dates/source/meta/summary
        # rendering — same renderer is reused by the find-nav preview so
        # the page-by-page walking output looks identical to /find.
        blocks: list[str] = [header]
        for m in matches:
            blocks.append(_format_find_item(m, index=None,
                                             chunk_counts=chunk_counts))
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
        # Cache the full result list so /show can offer "다음 항목" walk
        # from the user's clicked position — addresses the pain of scrolling
        # the whole /find back to the top after each /show.
        try:
            _set_find_context(
                update.effective_chat.id, query, matches, first_message_id,
                chunk_counts=chunk_counts,
            )
        except Exception:
            log.exception("find: context cache write failed (non-fatal)")


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
    async with _SustainedTyping(update, ctx):
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
        # Header keyboard composes up to 2 rows:
        #   Row 1: [🌐 한국어 번역] when doc is mostly foreign
        #   Row 2: [⏩ find 다음 #N+1~#N+5] when this doc was reached via /find
        #          — lets the user walk through the find result without
        #          scrolling back to the top of the (possibly massive)
        #          /find message every time.
        kb_rows: list[list[InlineKeyboardButton]] = []
        if show_xlate_btn:
            kb_rows.append([InlineKeyboardButton(
                "🌐 한국어 번역", callback_data=f"xlate:{doc_id}",
            )])
        find_ctx = _get_find_context(update.effective_chat.id)
        find_idx: int | None = None
        if find_ctx:
            find_idx = _find_item_index(find_ctx["items"], doc_id)
            if find_idx is not None:
                next_kb = _find_next_keyboard(
                    find_ctx["items"], find_idx + 1,
                )
                if next_kb is not None:
                    kb_rows.extend(next_kb.inline_keyboard)
        header_kb = InlineKeyboardMarkup(kb_rows) if kb_rows else None

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
        # Also re-attaches the [⏩ find 다음] button to the bottom so the
        # user doesn't have to scroll back up to the header keyboard
        # after reading the whole body.
        footer_kb = None
        if find_ctx and find_idx is not None:
            footer_kb = _find_next_keyboard(
                find_ctx["items"], find_idx + 1,
            )
        if body_pieces:
            try:
                await update.message.reply_text(
                    "⬆️ 처음으로",
                    reply_to_message_id=first_message_id,
                    disable_web_page_preview=True,
                    reply_markup=footer_kb,
                    allow_sending_without_reply=True,
                )
            except Exception:
                log.exception("show: top-link footer send failed — "
                              "retrying without reply")
                # Keep the [⏩ find 다음] button reachable at the bottom
                # even if the reply link errored.
                try:
                    await update.message.reply_text(
                        "⬆️ 처음으로",
                        disable_web_page_preview=True,
                        reply_markup=footer_kb,
                    )
                except Exception:
                    log.exception("show: footer fallback send failed")


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


def _failed_remove_entry(entry: dict) -> dict | None:
    """Remove an exact entry from _INGEST_FAILED by IDENTITY (`is`).

    Earlier the per-#N callbacks used a `len(_INGEST_FAILED) - 1 - idx`
    formula on the assumption that the display order matched newest-
    first insertion order. But _failed_recent_snapshot re-sorts by
    (size ASC, ts DESC) so that formula popped a near-random row —
    the displayed item kept showing up in /failed while a different
    row vanished, which made the user think multi-tap retries were
    queueing up sequentially instead of in parallel (they weren't —
    they were just clobbering the wrong rows).

    The snapshot list shares dict references with _INGEST_FAILED, so
    looking up by identity is exact and race-safe (single-threaded
    asyncio; even if a parallel tap pops first, the `is` miss is
    handled by the None-return)."""
    for i, e in enumerate(_INGEST_FAILED):
        if e is entry:
            return _INGEST_FAILED.pop(i)
    return None


def _failed_retry_one(chat_id: int, idx: int) -> str:
    """Retry just one /failed entry by its #N tag (0-based) in the
    sorted display order. Identifies the exact dict via snapshot then
    removes by identity, so concurrent taps each hit the row the user
    actually pressed (no off-by-many from the old position-formula bug)."""
    snapshot = _failed_recent_snapshot()
    if idx < 0 or idx >= len(snapshot):
        return f"⚠️ #{idx + 1} 범위 초과 (현재 {len(_INGEST_FAILED)}건)"
    target = snapshot[idx]
    payload = target.get("retry")
    if not payload:
        return (
            f"⚠️ #{idx + 1} retry 정보 없음 — 채널/원본에서 직접 다시 "
            "보내주세요"
        )
    entry = _failed_remove_entry(target)
    if entry is None:
        # A concurrent tap already popped this exact row.
        return f"⚠️ #{idx + 1} 방금 다른 작업으로 제거됨"
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
    """Delete a single /failed entry by #N tag (0-based) in the sorted
    display order AND mark its filename/URL as permanently ignored —
    same semantics as the bulk /failed_clear. Without the ignore step
    the next orphan scan re-enqueues the file and the user gets to play
    whack-a-mole with the same row over and over."""
    snapshot = _failed_recent_snapshot()
    if idx < 0 or idx >= len(snapshot):
        return f"⚠️ #{idx + 1} 범위 초과 (현재 {len(_INGEST_FAILED)}건)"
    target = snapshot[idx]
    entry = _failed_remove_entry(target)
    if entry is None:
        return f"⚠️ #{idx + 1} 방금 다른 작업으로 제거됨"
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
    batch_id = f"b{time.time():.0f}"
    for entry in _INGEST_FAILED:
        payload = entry.get("retry")
        if payload:
            payload = dict(payload)
            payload["attempts"] = 0
            payload["chat_id"] = chat_id
            payload["_batch"] = batch_id
            _INGEST_RETRY_QUEUE.append(payload)
            retried += 1
        else:
            kept.append(entry)
    _INGEST_FAILED.clear()
    _INGEST_FAILED.extend(kept)
    _persist_retry_queue()
    _persist_failed_log()
    # Seed the progress tracker; the async caller sends the message and
    # _refresh_retry_progress (drain tick) updates it as items drain.
    if retried:
        _RETRY_PROGRESS.update(
            chat_id=chat_id, msg_id=None, batch_id=batch_id,
            total=retried, last_done=-1,
        )
    msg = (
        f"🔁 retry queue로 {retried}건 재등록\n"
        f"{_RETRY_INGEST_INTERVAL_SEC}초 간격, 최대 "
        f"{_RETRY_INGEST_BATCH}건/회 자동 처리."
    )
    if kept:
        msg += f"\n\n♻️ retry 정보 없는 {len(kept)}건은 그대로 — 채널 스크롤로 직접 다시 보내주세요."
    return msg


async def _start_retry_progress(ctx: ContextTypes.DEFAULT_TYPE,
                                chat_id: int) -> None:
    """Send the initial "🔄 재시도 0/M 완료" message for a batch just
    seeded by _failed_retry_all. No-op if no batch is pending."""
    if _RETRY_PROGRESS["total"] <= 0 or _RETRY_PROGRESS["msg_id"] is not None:
        return
    try:
        sent = await ctx.bot.send_message(
            chat_id, f"🔄 재시도 0/{_RETRY_PROGRESS['total']} 완료")
        _RETRY_PROGRESS["msg_id"] = sent.message_id
        _RETRY_PROGRESS["last_done"] = 0
    except Exception:
        log.exception("retry progress start send failed")


async def _refresh_retry_progress(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Edit the batch progress message to reflect how many batch items
    have left the retry queue. Called from the drain tick; edits only
    when the count changed (no spam) and finalises + clears the tracker
    when the batch drains. 'done' = left the queue (success / dup /
    permanent drop) — items still retrying stay counted as remaining."""
    t = _RETRY_PROGRESS
    if t["msg_id"] is None or t["total"] <= 0:
        return
    remaining = sum(1 for it in _INGEST_RETRY_QUEUE
                    if it.get("_batch") == t["batch_id"])
    done = t["total"] - remaining
    if done == t["last_done"]:
        return
    t["last_done"] = done
    chat_id, msg_id, total = t["chat_id"], t["msg_id"], t["total"]
    finished = remaining <= 0
    text = (f"✅ 재시도 {total}건 처리 완료" if finished
            else f"🔄 재시도 {done}/{total} 완료")
    if finished:
        t.update(chat_id=None, msg_id=None, batch_id=None,
                 total=0, last_done=-1)
    try:
        await ctx.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id, text=text)
    except Exception:
        log.exception("retry progress edit failed")


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
    async with _SustainedTyping(update, ctx):
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
        # Telegram allows ~100 buttons per message and each entry adds 2,
        # so 40 → 82 buttons (incl. the 2 bulk) stays safely under the
        # limit. The 3800-char text LIMIT above can still truncate the
        # list before 40 when titles are long — graceful (bulk buttons
        # remain), no error.
        PER_ITEM_BUTTON_CAP = 40
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
    chat_id = update.effective_chat.id
    await update.message.reply_text(_failed_retry_all(chat_id))
    await _start_retry_progress(ctx, chat_id)


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
    # findnext answers its own query in every branch (with informative
    # "끝까지 봤어"/"만료" alerts). Pre-answering here would swallow those
    # alerts — Telegram rejects a second answer on an already-answered
    # query — so the user got NO feedback on a stale/end button. Skip
    # the blanket answer for findnext and let its branch own it.
    if not (q.data or "").startswith("findnext:"):
        await q.answer()  # dismiss the loading spinner
    chat_id = q.message.chat.id if q.message else config.TELEGRAM_OWNER_ID
    if q.data == "failed_retry":
        await q.edit_message_text(_failed_retry_all(chat_id))
        await _start_retry_progress(ctx, chat_id)
        # User explicitly asked for retry — don't wait for the next 10 s
        # tick (which can be deferred up to ~60 s by busy-skip grace).
        # Kick a drain task right now; the in-task semaphore acquire
        # still bounds parallelism + Gemini concurrency.
        asyncio.create_task(_retry_pending_ingest(ctx))
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
        # Explicit user request → immediate drain (bypass tick wait).
        asyncio.create_task(_retry_pending_ingest(ctx))
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
    elif q.data.startswith("findnext:"):
        # /find result navigation — send a compact preview of the
        # next N items from the cached find result, with another
        # [⏩ 다음] button if more remain. Replies-to the original
        # /find first message so Telegram's quote-bar lets the user
        # jump back to the search header if they want.
        try:
            start_idx = int(q.data.split(":", 1)[1])
        except (IndexError, ValueError):
            try:
                await q.answer("⚠️ 잘못된 콜백 데이터")
            except Exception:
                pass
            return
        find_ctx = _get_find_context(chat_id)
        if not find_ctx:
            try:
                await q.answer(
                    "⏰ find 컨텍스트 만료 (1h). /find 다시 실행해줘.",
                    show_alert=True,
                )
            except Exception:
                pass
            return
        items = find_ctx["items"]
        if start_idx >= len(items):
            try:
                await q.answer("끝까지 봤어 ✅")
            except Exception:
                pass
            return
        try:
            await q.answer()
        except Exception:
            pass
        page_size = 5
        body = _format_find_preview_chunk(
            find_ctx["query"], items, start_idx,
            chunk_counts=find_ctx.get("chunk_counts") or {},
            count=page_size,
        )
        next_kb = _find_next_keyboard(
            items, start_idx + page_size, page_size,
        )
        # Preview now includes full summary per item (same renderer
        # as /find), so 5 items can exceed Telegram's 4000-char cap.
        # Split, send each piece; reply-to original /find on the
        # first (so Telegram's quote-bar lets the user jump back),
        # attach [⏩ 다음] keyboard only to the LAST piece (where
        # the user lands after reading all items).
        pieces = _split_for_telegram(body)
        if not pieces:
            return
        first_msg_id = find_ctx.get("first_message_id")
        for i, piece in enumerate(pieces):
            is_last = (i == len(pieces) - 1)
            kb = next_kb if is_last else None
            send_kw = dict(
                chat_id=chat_id, text=piece, parse_mode="HTML",
                disable_web_page_preview=True, reply_markup=kb,
            )
            # Reply-link only on the first piece, back to the original
            # /find header. allow_sending_without_reply so a missing
            # target (header scrolled far up / gone after the user
            # walked through a /show dump) doesn't make Telegram reject
            # the whole send — otherwise the now-single-piece preview
            # silently vanishes and the button looks dead.
            if i == 0 and first_msg_id is not None:
                send_kw["reply_to_message_id"] = first_msg_id
                send_kw["allow_sending_without_reply"] = True
            try:
                await ctx.bot.send_message(**send_kw)
            except Exception:
                log.exception(
                    "findnext: send preview piece %d failed — "
                    "retrying without reply", i,
                )
                # Last-resort fallback: drop the reply link entirely so
                # the preview (and its [⏩ 다음] button) always reaches
                # the user even if the reply path errored.
                send_kw.pop("reply_to_message_id", None)
                send_kw.pop("allow_sending_without_reply", None)
                try:
                    await ctx.bot.send_message(**send_kw)
                except Exception:
                    log.exception(
                        "findnext: fallback send piece %d also failed", i,
                    )
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
    async with _SustainedTyping(update, ctx):
        if not _INGEST_RETRY_QUEUE:
            await update.message.reply_text("재시도 큐 비어있음 ✨")
            return
        # in-flight items stay in the queue with in_flight_ts (resume
        # safety) — split them out so they don't look like duplicates of
        # the ⏳ [재시도] bubbles the user sees in chat.
        in_flight_n = sum(1 for it in _INGEST_RETRY_QUEUE
                          if it.get("in_flight_ts"))
        waiting_n = len(_INGEST_RETRY_QUEUE) - in_flight_n
        out = (
            f"🔁 재시도 큐 {len(_INGEST_RETRY_QUEUE)}건 "
            f"(🔄 처리중 {in_flight_n} · ⏳ 대기 {waiting_n} · "
            f"{_RETRY_INGEST_INTERVAL_SEC}초 간격, 최대 "
            f"{_RETRY_INGEST_BATCH}건/회)"
        )
        for item in _INGEST_RETRY_QUEUE[:25]:
            kind = item.get("kind", "?")
            title = item.get("file_name") or item.get("url") or "(unknown)"
            attempts = item.get("attempts", 0)
            tag = "🔄 처리중" if item.get("in_flight_ts") else "⏳ 대기"
            out += f"\n• {tag} [{kind}] {title[:80]} (시도 {attempts}회)"
        if len(_INGEST_RETRY_QUEUE) > 25:
            out += f"\n... 외 {len(_INGEST_RETRY_QUEUE) - 25}건"
        out += (
            "\n\n💡 큐가 막혀 새 학습까지 느려질 때:"
            "\n  • /queue_to_failed — 큐만 /failed로, 진행중은 그대로"
            "\n  • /queue_panic — 큐 → /failed + 봇 강제 재시작 (in-flight 까지 진짜 정리)"
        )
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
    # Unbounded host list (~200 chars/entry) overflows 4096 without a split.
    for chunk in _split_for_telegram("\n".join(lines)):
        await update.message.reply_text(
            chunk, disable_web_page_preview=True,
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


async def cmd_unignore(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Revive permanently-ignored URLs/filenames so they can be learned
    again. Match by substring — a YouTube video id (e.g. KMgS3TubGes)
    catches the URL in any form. This is the inverse of /failed_clear &
    [🗑]; block-era false ignores (IP-blocked YouTube videos that got
    🗑'd) are revived here without a JSON hand-edit + restart. The
    in-memory set is updated immediately, so no restart is needed.
    Usage: /unignore <url|조각>  ·  /unignore all (전체 URL 해제)."""
    if not _is_owner(update):
        return
    global _IGNORED_URLS, _IGNORED_FILENAMES
    arg = " ".join(ctx.args).strip() if ctx.args else ""
    if not arg:
        await update.message.reply_text(
            "사용법: <code>/unignore &lt;url 또는 조각&gt;</code>\n"
            "예: <code>/unignore KMgS3TubGes</code> · "
            "<code>/unignore all</code>(전체 URL 해제)\n"
            f"현재 영구 무시: URL {len(_IGNORED_URLS)}건 · "
            f"파일 {len(_IGNORED_FILENAMES)}건",
            parse_mode="HTML")
        return
    if arg.lower() == "all":
        removed_u = sorted(_IGNORED_URLS)
        removed_f = []
        _IGNORED_URLS = set()
    else:
        removed_u = sorted(u for u in _IGNORED_URLS if arg in u)
        removed_f = sorted(f for f in _IGNORED_FILENAMES if arg in f)
        _IGNORED_URLS -= set(removed_u)
        _IGNORED_FILENAMES -= set(removed_f)
    if not removed_u and not removed_f:
        await update.message.reply_text(
            f"해당 없음: '{arg}' 와 일치하는 영구 무시 항목이 없어.\n"
            f"남은 영구 무시: URL {len(_IGNORED_URLS)}건 · "
            f"파일 {len(_IGNORED_FILENAMES)}건")
        return
    _persist_permanently_ignored()
    lines = [f"✅ 영구 무시 해제: URL {len(removed_u)}건 · 파일 {len(removed_f)}건"]
    for u in removed_u[:10]:
        lines.append(f"  • {u}")
    if len(removed_u) > 10:
        lines.append(f"  … 외 {len(removed_u) - 10}건")
    lines.append("이제 다시 올리면 학습 시도함.")
    await update.message.reply_text(
        "\n".join(lines), disable_web_page_preview=True)


async def cmd_kg_extract(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """KG trial: extract atomic fact triples from the N most recent docs
    (default 5, max 20) into the isolated kg.db. Skips docs already
    extracted. Cheap (Flash-Lite, one call/doc)."""
    if not _is_owner(update):
        return
    from .store import kg as _kg, cost as _cost
    from .agent import kg_extract as _kgx
    n = 5
    if ctx.args:
        try:
            n = max(1, min(int(ctx.args[0]), 20))
        except Exception:
            n = 5
    async with _SustainedTyping(update, ctx):
        docs = await asyncio.to_thread(meta.docs_since, None, n)
        already = await asyncio.to_thread(_kg.docs_with_edges)
        processed = 0
        added_total = 0
        for d in docs:
            if d["id"] in already:
                continue
            body = (d.get("summary") or "").strip()
            if len(body) < 40:
                continue
            triples = await _kgx.extract(d.get("title") or "", body)
            added_total += await asyncio.to_thread(_kg.add_edges, d["id"], triples)
            processed += 1
        st = await asyncio.to_thread(_kg.stats)
        nc = await asyncio.to_thread(_cost.purpose_today_month, "kg_extract")
    await update.message.reply_text(
        "🕸 <b>KG 추출 완료</b> (시범)\n"
        f"• 처리 문서: {processed}개 (최근 {n} 중 미추출분)\n"
        f"• 추가 트리플: {added_total}\n"
        f"• 누적: 엣지 {st['edges']} · 엔티티 {st['entities']} · 문서 {st['docs']}\n"
        f"• 오늘 KG 비용: ₩{nc['today_krw']:.1f} ({nc['today_calls']}콜)\n"
        "ℹ️ <code>/kg &lt;개체명&gt;</code> 로 관계 조회",
        parse_mode="HTML")


async def cmd_kg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """KG trial: show the triples around an entity, or overview + top
    entities when called bare."""
    if not _is_owner(update):
        return
    from .store import kg as _kg
    ent = " ".join(ctx.args).strip()
    if not ent:
        st = await asyncio.to_thread(_kg.stats)
        tops = await asyncio.to_thread(_kg.top_entities, 15)
        lines = ["🕸 <b>지식그래프</b> (시범)",
                 f"엣지 {st['edges']} · 엔티티 {st['entities']} · "
                 f"문서 {st['docs']}",
                 "사용: <code>/kg 삼성전기</code> · 채우기: /kg_extract [N]"]
        if tops:
            lines.append("\n<b>주요 개체</b> (연결수):")
            lines += [f"• {html.escape(t['name'])} ({t['deg']})" for t in tops]
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return
    edges = await asyncio.to_thread(_kg.neighbors, ent, 40)
    if not edges:
        await update.message.reply_text(
            f"'{ent}' 관련 트리플 없음.\n/kg_extract 로 먼저 추출해봐.")
        return
    lines = [f"🕸 <b>{html.escape(ent)}</b> 관계 ({len(edges)})"]
    for e in edges:
        c = e.get("confidence") or 0
        lines.append(
            f"• {html.escape(e['src'])} —<i>{html.escape(e['rel'])}</i>→ "
            f"{html.escape(e['dst'])} <span>({c:.2f})</span>")
    await update.message.reply_text(
        "\n".join(lines[:45]), parse_mode="HTML",
        disable_web_page_preview=True)


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
    async with _SustainedTyping(update, ctx):
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
        try:
            pending_links_n = len(await asyncio.to_thread(pending_store.list_links))
        except Exception:
            pending_links_n = 0

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

        total_pending = (queue_n + orphan_n + failed_n + pending_ocr_n
                         + pending_pro_n + pending_links_n)

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
            f"• 본문 링크 대기: <b>{pending_links_n}묶음</b> (/pending_links)",
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
    async with _SustainedTyping(update, ctx):
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
    async with _SustainedTyping(update, ctx):
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
    async with _SustainedTyping(update, ctx):
        ocr_items = await asyncio.to_thread(pending_store.list_ocr)
        pro_items = await asyncio.to_thread(pending_store.list_pro)
        url_items = await asyncio.to_thread(pending_url_decisions.list_overdue)
        link_items = await asyncio.to_thread(pending_store.list_links)
        if not ocr_items and not pro_items and not url_items and not link_items:
            await update.message.reply_text(
                "📭 검토 대기 항목 없음 — 모든 확인 prompt가 처리됨."
            )
            return

        # Header summary
        total = (len(ocr_items) + len(pro_items) + len(url_items)
                 + len(link_items))
        header = [f"📋 검토 대기 항목 ({total}개)"]
        if link_items:
            header.append(f"🔗 본문 링크 {len(link_items)}묶음")
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

        # In-post link prompts: re-send the preview list + buttons for any
        # bundle still holding undone links (a prompt the user scrolled
        # past). Cap to keep the keyboard responsive.
        if link_items:
            await update.message.reply_text(
                f"🔗 본문 링크 대기 {len(link_items)}묶음 — 아래에서 선택 "
                f"(또는 /pending_links):"
            )
            LINK_INLINE_CAP = 10
            for it in link_items[:LINK_INLINE_CAP]:
                await _send_one_link_prompt(
                    ctx, update.effective_chat.id, it["id"],
                    it.get("parent_title") or "", it["links"],
                )
            if len(link_items) > LINK_INLINE_CAP:
                await update.message.reply_text(
                    f"…외 {len(link_items) - LINK_INLINE_CAP}묶음 더 있음. "
                    f"위 처리 후 /pending_links."
                )


async def cmd_pending_links(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Re-list outstanding in-post link prompts (preview + buttons) so a
    missed prompt can be acted on later. State lives in pending_links."""
    if not _is_owner(update):
        return
    async with _SustainedTyping(update, ctx):
        items = await asyncio.to_thread(pending_store.list_links)
        if not items:
            await update.message.reply_text(
                "📭 대기 중인 본문 링크 묶음 없음 — 모두 처리됨."
            )
            return
        await update.message.reply_text(
            f"🔗 본문 링크 대기 {len(items)}묶음:"
        )
        for it in items[:10]:
            await _send_one_link_prompt(
                ctx, update.effective_chat.id, it["id"],
                it.get("parent_title") or "", it["links"],
            )
        if len(items) > 10:
            await update.message.reply_text(
                f"…외 {len(items) - 10}묶음. 위 처리 후 /pending_links 재호출."
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
    async with _SustainedTyping(update, ctx):
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
    async with _SustainedTyping(update, ctx):
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
    async with _SustainedTyping(update, ctx):
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
    async with _SustainedTyping(update, ctx):
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
    async with _SustainedTyping(update, ctx):
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
        "ts": datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S"),
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
    async with _SustainedTyping(update, ctx):
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


async def cmd_queue_panic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Emergency 'clean slate' — drain everything to /failed AND restart
    the bot process so stuck in-flight tasks (which Python can't safely
    cancel mid-pipeline, especially in to_thread + _CHROMA_LOCK) are
    actually killed instead of slowly draining their hold on semaphore
    slots.

    Steps:
      1. Drain _INGEST_RETRY_QUEUE → /failed with retry_payload intact
         so the user can re-learn each item one tap at a time.
      2. Clear sibling queues (Pro run, agent overload, pending OCR/Pro
         tables) so the post-restart loop comes up genuinely empty.
      3. Set _RECOVERY_SUPPRESS_PATH so the boot-time orphan scan
         doesn't immediately re-enqueue everything from data/files/.
      4. os._exit(0) → Docker's `restart: unless-stopped` revives the
         container in ~3-5 s with a fresh event loop, no stuck threads,
         no held locks.

    Use when /queue_to_failed alone doesn't unstick things (i.e. the
    queue is empty but ingest is still glacially slow). Recovery:
      • /failed has every dropped item with [🔁 #N] for manual replay.
      • /recover_orphans clears the suppress marker when you want the
        orphan auto-scan back on."""
    if not _is_owner(update):
        return
    async with _SustainedTyping(update, ctx):
        # 1. Drain retry queue → /failed (retry_payload preserved).
        drained = list(_INGEST_RETRY_QUEUE)
        moved = 0
        for item in drained:
            entry = _retry_item_to_failed_entry(
                item, "사용자 요청 — /queue_panic 으로 전체 정리 후 재시작"
            )
            _INGEST_FAILED.append(entry)
            moved += 1
        _INGEST_RETRY_QUEUE.clear()
        if len(_INGEST_FAILED) > _FAILED_MAX:
            del _INGEST_FAILED[: len(_INGEST_FAILED) - _FAILED_MAX]
        _persist_retry_queue()
        _persist_failed_log()

        # 2. Clear sibling queues so restart really starts empty.
        pro_q_n = len(_PENDING_PRO_RUN_QUEUE)
        agent_q_n = len(_RETRY_QUEUE)
        _PENDING_PRO_RUN_QUEUE.clear()
        _RETRY_QUEUE.clear()
        try:
            ocr_n = await asyncio.to_thread(pending_store.delete_all_ocr)
            pro_n = await asyncio.to_thread(pending_store.delete_all_pro)
        except Exception:
            ocr_n = pro_n = 0
            log.exception("queue_panic: clearing pending OCR/Pro failed")

        # 3. Suppress marker so post-restart orphan scan stays quiet.
        try:
            _RECOVERY_SUPPRESS_PATH.touch(exist_ok=True)
        except Exception:
            log.exception("queue_panic: suppress marker write failed")

        # 4. Notify, then exit on a short delay so the reply flushes
        # over HTTP before the process dies.
        await update.message.reply_text(
            f"🆘 패닉 정리 완료\n"
            f"  • /failed 로 이동: {moved}건\n"
            f"  • Pro 큐 비움: {pro_q_n}건\n"
            f"  • Agent 재시도 비움: {agent_q_n}건\n"
            f"  • 보류 OCR/Pro 비움: {ocr_n}/{pro_n}건\n"
            f"  • Orphan 자동 복구 정지 (suppress 마커)\n\n"
            f"🔄 봇 프로세스 종료 → Docker 자동 재시작 (~3-5초).\n"
            f"복구: /failed 에서 🔁 #N 으로 하나씩 다시 학습.\n"
            f"orphan 자동 복구 재개는 /recover_orphans 실행 시."
        )

        async def _exit_for_restart():
            # Tiny grace so the Telegram POST flushes before SIGKILL-ish
            # _exit lands. 1.5 s covers a slow network round trip.
            await asyncio.sleep(1.5)
            log.warning("/queue_panic — exiting process for container restart")
            import os as _os
            _os._exit(0)
        asyncio.create_task(_exit_for_restart())


async def cmd_queue_cancel_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Stop everything in flight — wipe ingest retry queue, pending
    Pro run queue, agent overload retry queue, and both pending DB
    tables. Currently-running ingest finishes its file (no clean
    way to abort mid-pipeline) but nothing new starts. Zero cost.
    Use when the bot is overwhelmed and you want a fresh slate."""
    if not _is_owner(update):
        return
    async with _SustainedTyping(update, ctx):
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
    ids = [m["id"] for m in matches]
    n_chunks = await asyncio.to_thread(vector.delete_docs, ids)
    await asyncio.to_thread(lambda: [meta.delete(i) for i in ids])
    forgotten = []
    for m in matches:
        fname = _filename_from_source(m.get("source") or "")
        if fname:
            _record_dedup_confirmed(fname)
        forgotten.append(f"  ✅ {_clean_text(m['title'])[:60]}")
    await update.message.reply_text(
        f"삭제 완료 · {len(forgotten)}건 / 청크 {n_chunks}개\n"
        + "\n".join(forgotten)
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
        await asyncio.to_thread(dashboard_regen.regenerate)
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
        await asyncio.to_thread(dashboard_regen.regenerate)
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
    matches = await asyncio.to_thread(meta.search_title, query, 500)
    if not matches:
        await update.message.reply_text(f"매칭 없음: '{query}'")
        return
    # Batch delete off-loop: up to 500 per-doc full-collection scans on
    # the event loop took minutes at 253k chunks → heartbeat starvation
    # → watchdog restart mid-delete (meta/vector divergence).
    ids = [m["id"] for m in matches]
    chunks_total = await asyncio.to_thread(vector.delete_docs, ids)
    await asyncio.to_thread(lambda: [meta.delete(i) for i in ids])
    for m in matches:
        fname = _filename_from_source(m.get("source") or "")
        if fname:
            _record_dedup_confirmed(fname)
    await update.message.reply_text(
        f"✅ 일괄 삭제 · {len(ids)}건 / 청크 {chunks_total}개 제거"
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


# Generic app-default PDF titles that should be replaced by the source
# filename. Mirrors loaders._PLACEHOLDER_TITLES (kept in sync manually —
# the listener-side fix prevents NEW occurrences; this command repairs
# docs ingested before that fix). Matched case-insensitively against the
# stored title.
_REPAIR_PLACEHOLDER_TITLES = ("PowerPoint 프레젠테이션",)


async def cmd_fix_placeholder_titles(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Repair docs saved with an app-default title (e.g. 'PowerPoint
    프레젠테이션') by replacing the title with the source filename — in
    place, so chunks/embeddings are untouched (no re-ingest needed, and
    the original PDFs aren't on disk anyway). Only docs whose source
    carries a usable filename are repaired; the rest are reported as
    skipped so the user knows they need a manual /forget + re-send.

    Two-step: bare /fix_placeholder_titles previews, `confirm` executes."""
    if not _is_owner(update):
        return
    confirm = bool(ctx.args) and ctx.args[0].lower() == "confirm"

    rows: list[dict] = []
    for ph in _REPAIR_PLACEHOLDER_TITLES:
        rows.extend(await asyncio.to_thread(meta.find_by_title_exact, ph, 1000))
    if not rows:
        await update.message.reply_text("📭 placeholder 제목 문서 없음.")
        return

    # Build (doc_id, new_title) repair plan; split out docs with no
    # recoverable filename.
    plan: list[tuple[str, str]] = []
    no_filename: list[dict] = []
    for r in rows:
        fname = _filename_from_source(r.get("source") or "")
        if not fname:
            no_filename.append(r)
            continue
        # Strip extension for a cleaner title; keep the rest verbatim.
        new_title = re.sub(r"\.(pdf|pptx?|docx?|xlsx?)$", "", fname,
                           flags=re.IGNORECASE).strip()
        if not new_title:
            no_filename.append(r)
            continue
        plan.append((r["id"], new_title))

    if not confirm:
        import html as _html
        preview = "\n".join(
            f"  • {_html.escape(nt[:55])}" for _, nt in plan[:15]
        )
        more = f"\n... 외 {len(plan) - 15}건" if len(plan) > 15 else ""
        msg = (
            f"🔧 placeholder 제목 복구 대상 {len(plan)}건 "
            f"(파일명으로 교체):\n{preview}{more}"
        )
        if no_filename:
            msg += (f"\n\n⚠️ 파일명 없어 복구 불가 {len(no_filename)}건 "
                    f"(tg-msg/사진/URL 등) — 필요시 /forget 후 재전송.")
        msg += "\n\n실행: <code>/fix_placeholder_titles confirm</code>"
        await update.message.reply_text(msg, parse_mode="HTML")
        return

    fixed = 0
    for doc_id, new_title in plan:
        if await asyncio.to_thread(meta.update_title, doc_id, new_title):
            fixed += 1
    # Dashboard reflects titles → regenerate off-loop.
    try:
        from .dashboard import regenerate as dashboard_regen
        await asyncio.to_thread(dashboard_regen.regenerate)
    except Exception:
        log.exception("dashboard regen after title fix failed")

    await update.message.reply_text(
        f"✅ 제목 복구 {fixed}/{len(plan)}건 완료"
        + (f"\n⚠️ 파일명 없어 건너뜀 {len(no_filename)}건"
           if no_filename else "")
        + "\n이제 /find 로 파일명 검색 가능."
    )


async def cmd_cleanup_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    ctx.args = ["confirm"]
    await cmd_cleanup(update, ctx)


# Marker string the YouTube loader writes to the body when both the
# transcript_api and yt-dlp fetchers fail. Real captions never contain
# this exact phrase, so it's a reliable stub-vs-real signal.
_YT_STUB_MARKER = "자막 자동 fetch 실패"

# Single-flight guard. The stub scan is a heavy ChromaDB op; firing it
# multiple times (impatient re-taps) runs N of them concurrently across
# to_thread workers on the same collection object, which thrash each
# other so none finish. Reject overlap with a clear message instead.
_YT_RESCAN_INFLIGHT = False


async def cmd_youtube_restub_rescan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Single-flight wrapper around the rescan so concurrent invocations
    don't pile up heavy Chroma scans on top of each other."""
    global _YT_RESCAN_INFLIGHT
    if not _is_owner(update):
        return
    if _YT_RESCAN_INFLIGHT:
        await update.message.reply_text(
            "⏳ 이미 스캔/처리 중이에요. 끝날 때까지 기다려주세요 "
            "(다시 누르면 경합으로 더 느려져요)."
        )
        return
    _YT_RESCAN_INFLIGHT = True
    try:
        await _youtube_restub_rescan_impl(update, ctx)
    finally:
        _YT_RESCAN_INFLIGHT = False


async def _youtube_restub_rescan_impl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Rescue YouTube docs that got learned as the loader's stub
    ('📺 자막 자동 fetch 실패…') while yt-dlp was broken: scan the
    learned set, drop the stub rows from meta + vector, and re-queue
    their source URLs so the next retry tick re-extracts captions
    against the now-healthy nightly yt-dlp + Deno EJS path.

    Two-step like the other bulk-delete commands: bare /youtube_restub_rescan
    previews, /youtube_restub_rescan confirm executes."""
    if not _is_owner(update):
        return
    confirm = bool(ctx.args) and ctx.args[0].lower() == "confirm"
    chat_id = update.effective_chat.id

    candidates = await asyncio.to_thread(meta.find_youtube_docs)
    if not candidates:
        await update.message.reply_text("📭 YouTube 학습 자료 없음.")
        return

    # Heads-up before the (still bounded but visibly slow) Chroma scan.
    # On a 178k-chunk collection one batched where-$in get can take a
    # few seconds even after the N→1 fix, so the user gets the message
    # instead of staring at a silent prompt.
    progress = await update.message.reply_text(
        f"🔍 YouTube 자료 {len(candidates)}건 스캔 중…"
    )

    # Single batched chunk fetch (was N per-doc round trips, each a
    # full metadata scan over the whole collection — that's why the
    # first version felt frozen).
    doc_ids = [c["id"] for c in candidates]
    hit_ids = await asyncio.to_thread(
        vector.find_doc_ids_containing, doc_ids, _YT_STUB_MARKER
    )
    stubs = [c for c in candidates if c["id"] in hit_ids]

    if not stubs:
        await update.message.reply_text(
            f"✅ YouTube 학습 {len(candidates)}건 중 stub 없음 — 재학습 대상 0."
        )
        return

    if not confirm:
        import html as _html
        preview = "\n".join(
            f"  • {_html.escape((s.get('title') or '')[:50])} — "
            f"{_html.escape((s.get('source') or '')[:60])}"
            for s in stubs[:10]
        )
        more = f"\n... 외 {len(stubs) - 10}건" if len(stubs) > 10 else ""
        await update.message.reply_text(
            f"📺 stub으로 학습된 YouTube {len(stubs)}건:\n"
            f"{preview}{more}\n\n"
            f"전부 삭제 + 재학습 큐 투입:\n"
            f"<code>/youtube_restub_rescan confirm</code>",
            parse_mode="HTML",
        )
        return

    stub_ids = [s["id"] for s in stubs]

    # Vector: one batched where-$in delete (single collection scan) for
    # the whole stub set, instead of N per-doc delete_doc() scans.
    try:
        chunks_removed = await asyncio.to_thread(vector.delete_docs, stub_ids)
    except Exception:
        log.exception("vector.delete_docs failed for youtube stubs")
        chunks_removed = 0

    # Meta: cheap indexed single-row deletes, all in one thread hop.
    def _purge_meta(ids: list[str]) -> None:
        for did in ids:
            try:
                meta.delete(did)
            except Exception:
                log.exception("meta.delete failed for %s", did)
    await asyncio.to_thread(_purge_meta, stub_ids)

    # Requeue source URLs (pure in-memory; dedup against existing queue).
    requeued = 0
    skipped_dup = 0
    queued_urls = {it.get("url") for it in _INGEST_RETRY_QUEUE
                   if it.get("kind") == "url"}
    for s in stubs:
        url = s.get("source") or ""
        if not url.startswith("http"):
            continue
        if url in queued_urls:
            skipped_dup += 1
            continue
        _INGEST_RETRY_QUEUE.append({
            "kind": "url",
            "url": url,
            "chat_id": chat_id,
            "attempts": 0,
        })
        queued_urls.add(url)
        requeued += 1
    _persist_retry_queue()

    await update.message.reply_text(
        f"✅ YouTube stub {len(stubs)}건 처리\n"
        f"  • 메타/벡터 삭제: {len(stubs)}건 (청크 {chunks_removed})\n"
        f"  • 재학습 큐 투입: {requeued}건\n"
        f"  • 큐 중복 skip: {skipped_dup}건\n"
        f"→ 다음 retry tick(≈60s)부터 재추출."
    )


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


def _format_papers_text(query: str, results: list[dict]) -> str:
    """Direct deterministic render for /search_papers command output.

    Mirrors _format_patents_text — bypasses the agent's tool-routing
    dance. Per-paper block: 📄 N. title + (year) + 🔓 OA marker if
    open-access + meta line (first author et al. · venue · 인용 N회)
    + 🏛️ institutions + 🏷️ concepts + abstract excerpt + PDF/DOI/URL.
    Richer fields (institutions, concepts, OA status, paper type)
    surface only when OpenAlex was the row's source — other backends
    leave them empty and we silently skip the empty lines."""
    import html as _html
    if not results:
        return (f"🔍 '<b>{_html.escape(query)}</b>' 관련 논문 결과 없음.\n"
                f"검색어를 좁히거나 영문 키워드로 시도하면 결과 ↑.")
    out = [
        f"📄 <b>논문 검색 결과 — '{_html.escape(query)}'</b>",
        f"<i>{len(results)}건 · 6소스 라우팅 (S2 / arXiv / OpenAlex / "
        f"CrossRef / IEEE / PubMed)</i>",
    ]
    for i, p in enumerate(results, 1):
        title_src = (p.get("title_ko") or p.get("title") or "(제목 없음)")
        title = _html.escape(title_src)[:300]
        year = p.get("year")
        venue = _html.escape((p.get("venue") or "")[:80])
        auths = p.get("authors") or []
        auths_total = p.get("authors_total") or len(auths)
        first_auth = _html.escape(auths[0]) if auths else ""
        if first_auth and auths_total > 1:
            first_auth += (f" et al. (총 {auths_total}명)"
                           if auths_total <= 99 else " et al.")
        citations = p.get("citations")
        referenced = p.get("referenced_count")
        is_oa = p.get("is_oa") or False
        oa_status = p.get("oa_status") or ""
        institutions = p.get("institutions") or []
        concepts = p.get("concepts") or []
        paper_type = p.get("paper_type") or ""
        source = p.get("source") or ""
        abstract_src = (p.get("abstract_ko") or p.get("abstract") or "")
        abstract = _html.escape(
            _truncate_at_sentence(abstract_src.strip(), 900)
        )
        pdf = p.get("pdf") or ""
        url = p.get("url") or ""
        doi = p.get("doi") or ""

        meta_parts: list[str] = []
        if first_auth:
            meta_parts.append(first_auth)
        if venue:
            meta_parts.append(venue)
        if isinstance(citations, int) and citations >= 1:
            meta_parts.append(f"인용 {citations}회")
        if isinstance(referenced, int) and referenced >= 1:
            meta_parts.append(f"참고문헌 {referenced}개")
        if source and source not in ("S2",):
            meta_parts.append(f"[{source}]")
        meta_line = " · ".join(meta_parts)

        # Year + OA badge line
        head_bits: list[str] = []
        if year:
            head_bits.append(f"({year})")
        if is_oa:
            label = "🔓 OA"
            if oa_status and oa_status not in ("closed",):
                label = f"🔓 OA ({oa_status})"
            head_bits.append(label)
        if paper_type and paper_type not in ("article", ""):
            head_bits.append(_html.escape(paper_type))

        block = [f"\n📄 <b>{i}. {title}</b>"]
        if head_bits:
            block.append(f"  <i>{' · '.join(head_bits)}</i>")
        if meta_line:
            block.append(f"   {meta_line}")
        if institutions:
            inst_text = " / ".join(_html.escape(x) for x in institutions[:3])
            block.append(f"   🏛️ {inst_text}")
        if concepts:
            cc_text = " · ".join(_html.escape(c) for c in concepts[:3])
            block.append(f"   🏷️ {cc_text}")
        if abstract:
            block.append(f"   {abstract}")
        # PDF wins over landing URL wins over DOI link.
        if pdf:
            block.append(f"   → PDF: {pdf}")
        elif url:
            block.append(f"   → {url}")
        elif doi:
            block.append(f"   → https://doi.org/{doi}")
        out.append("\n".join(block))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Paper stats view formatters — same shapes as patent stats so the
# experience is consistent between /patent_stats and /paper_stats.
# ---------------------------------------------------------------------------


def _format_paper_stats_overview(query: str, stats: dict) -> str:
    """/paper_stats overview — by-author / by-venue / by-year / by-concept
    bars + OA share + citation distribution."""
    import html as _html
    total = stats.get("total", 0)
    if total == 0:
        return (f"📊 '<b>{_html.escape(query)}</b>' 논문 통계 — 분석할 자료 없음.")
    out = [
        f"📊 <b>'{_html.escape(query)}' 논문 통계</b>",
        f"<i>최근 {total}건 분석 · OpenAlex</i>",
    ]
    by_year = stats.get("by_year") or []
    if by_year:
        out.append("\n📅 <b>출간 연도별</b>")
        max_y = max((c for _, c in by_year), default=0)
        for y, c in by_year[:10]:
            pct = (c / total * 100) if total else 0
            out.append(f"  {y}: {_bar(c, max_y)} {c}건 ({pct:.1f}%)")
    by_author = stats.get("by_author") or []
    if by_author:
        out.append("\n👤 <b>저자 TOP 10</b>")
        for rank, (name, c) in enumerate(by_author[:10], 1):
            out.append(f"  {rank}. {_html.escape(name)[:50]} — {c}편")
    by_venue = stats.get("by_venue") or []
    if by_venue:
        out.append("\n📚 <b>학술지/학회 TOP 8</b>")
        for v, c in by_venue[:8]:
            out.append(f"  {_html.escape(v)[:60]} — {c}편")
    by_institution = stats.get("by_institution") or []
    if by_institution:
        out.append("\n🏛️ <b>소속 기관 TOP 8</b>")
        for inst, c in by_institution[:8]:
            out.append(f"  {_html.escape(inst)[:60]} — {c}편")
    by_concept = stats.get("by_concept") or []
    if by_concept:
        out.append("\n🏷️ <b>주제 분류 TOP 8</b>")
        for cc, c in by_concept[:8]:
            out.append(f"  {_html.escape(cc)[:60]} — {c}편")
    oa_share = stats.get("oa_share", 0)
    out.append(f"\n🔓 <b>오픈액세스:</b> {stats.get('oa_count', 0)}편 "
               f"({oa_share:.1f}%)")
    citation_buckets = stats.get("citation_buckets") or []
    if citation_buckets:
        out.append("\n💬 <b>인용 분포</b>")
        bucket_order = ["1000+", "100-999", "10-99", "1-9", "0"]
        bd = dict(citation_buckets)
        for k in bucket_order:
            if k in bd:
                out.append(f"  {k}회: {bd[k]}편")
    out.append(
        f"\n🔗 <b>추가 분석:</b>\n"
        f"  • /paper_stats {query} trend — 저자별 연도 추세\n"
        f"  • /paper_stats {query} newcomers — 신규 저자\n"
        f"  • /paper_stats {query} network — 공저자 네트워크\n"
        f"  • /paper_stats {query} keywords — Gemini 키워드 추출"
    )
    return "\n".join(out)


def _format_paper_stats_trend(query: str, trend: dict) -> str:
    import html as _html
    years = trend.get("years") or []
    series = trend.get("series") or {}
    top = trend.get("top_authors") or []
    if not (years and series and top):
        return (f"📈 '<b>{_html.escape(query)}</b>' 추세 분석 — 데이터 부족.")
    out = [
        f"📈 <b>'{_html.escape(query)}' 저자별 연도 추세</b>",
        f"<i>TOP {len(top)} 저자 × {min(years)}~{max(years)} 연도별 건수</i>",
        "",
        "```mermaid",
        "xychart-beta",
        f'  title "{query} — 저자별 연도별 출간량"',
        f"  x-axis [{', '.join(str(y) for y in years)}]",
        "  y-axis \"건수\"",
    ]
    for name in top:
        counts = series.get(name, [])
        out.append(f"  bar [{', '.join(str(n) for n in counts)}]")
    out.append("```")
    out.append("\n<b>범례:</b>")
    for rank, name in enumerate(top, 1):
        total = sum(series.get(name, []))
        out.append(f"  bar{rank}. {_html.escape(name)[:40]} ({total}편 누계)")
    return "\n".join(out)


def _format_paper_stats_newcomers(query: str, newcomers: list[dict]) -> str:
    import html as _html
    if not newcomers:
        return (f"🆕 '<b>{_html.escape(query)}</b>' 신규 저자 없음.")
    out = [
        f"🆕 <b>'{_html.escape(query)}' 신규 저자</b>",
        f"<i>분석 corpus 에서 최근 등장한 저자 {len(newcomers)}명</i>",
    ]
    for r in newcomers:
        out.append(
            f"  • {_html.escape(r['name'])[:50]} — "
            f"첫 등장 {r['first_year']} · 누적 {r['total']}편"
        )
    return "\n".join(out)


def _format_paper_stats_network(query: str, pairs: list[dict]) -> str:
    import html as _html
    if not pairs:
        return (f"🤝 '<b>{_html.escape(query)}</b>' 공저자 관계 없음.")
    out = [
        f"🤝 <b>'{_html.escape(query)}' 공저자 네트워크</b>",
        f"<i>2~10인 공저 케이스 {len(pairs)}쌍</i>",
    ]
    for r in pairs[:15]:
        out.append(
            f"  • {_html.escape(r['a'])[:35]} ⇄ "
            f"{_html.escape(r['b'])[:35]} — {r['count']}편"
        )
    return "\n".join(out)


def _format_paper_stats_keywords(query: str, keywords: list[str]) -> str:
    import html as _html
    if not keywords:
        return (f"☁️ '<b>{_html.escape(query)}</b>' 키워드 추출 실패.")
    out = [
        f"☁️ <b>'{_html.escape(query)}' 기술 키워드 클라우드</b>",
        f"<i>분석한 abstract 들에서 추출한 핵심 명사구 {len(keywords)}개</i>",
        "",
        " · ".join(_html.escape(kw) for kw in keywords[:30]),
    ]
    return "\n".join(out)


def _format_paper_stats_top(query: str, papers: list[dict],
                            top_n: int = 15) -> str:
    """/paper_stats <q> top — sort the bulk-fetched papers by
    cited_by_count and surface the TOP-N. OpenAlex returns the
    citation count inline so this is free (no N+1 calls)."""
    import html as _html
    ranked = sorted(papers,
                    key=lambda p: int(p.get("citations") or 0),
                    reverse=True)
    ranked = [p for p in ranked if (p.get("citations") or 0) > 0]
    if not ranked:
        return (f"🏆 '<b>{_html.escape(query)}</b>' 인용 분석 — "
                f"인용 정보 있는 논문 없음.")
    out = [
        f"🏆 <b>'{_html.escape(query)}' 영향력 TOP {min(top_n, len(ranked))}</b>",
        f"<i>인용수 (OpenAlex cited_by_count) 기준 내림차순</i>",
    ]
    for i, p in enumerate(ranked[:top_n], 1):
        title = _html.escape((p.get("title") or "(제목 없음)")[:200])
        year = p.get("year") or ""
        venue = _html.escape((p.get("venue") or "")[:60])
        cits = int(p.get("citations") or 0)
        auths = p.get("authors") or []
        first_auth = _html.escape(auths[0]) if auths else ""
        if first_auth and len(auths) > 1:
            first_auth += f" et al. ({len(auths)}명)"
        is_oa = "🔓 " if p.get("is_oa") else ""
        doi = p.get("doi") or ""
        block = [f"\n📄 <b>{i}. [인용 {cits:,}] {is_oa}{title}</b>"]
        meta_bits: list[str] = []
        if year:
            meta_bits.append(str(year))
        if first_auth:
            meta_bits.append(first_auth)
        if venue:
            meta_bits.append(venue)
        if meta_bits:
            block.append(f"   {' · '.join(meta_bits)}")
        if doi:
            block.append(f"   → https://doi.org/{doi}")
        elif p.get("url"):
            block.append(f"   → {p['url']}")
        out.append("\n".join(block))
    return "\n".join(out)


async def cmd_paper_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/paper_stats <keyword> [view] — OpenAlex bulk fetch (최대 1000편)
    aggregate analytics. view: overview / trend / newcomers /
    network / keywords / top.  `top` ranks the bulk-fetched papers by
    cited_by_count (free — OpenAlex returns citations inline; no N+1
    follow-up calls)."""
    if not _is_owner(update):
        return
    args = list(ctx.args)
    if not args:
        await update.message.reply_text(
            "사용법: /paper_stats <키워드> [view]\n"
            "view: overview (기본) · trend · newcomers · network · "
            "keywords · top\n"
            "예: /paper_stats hybrid bonding trend"
        )
        return
    valid_views = {"overview", "trend", "newcomers", "network",
                   "keywords", "top"}
    view = "overview"
    if args[-1].lower() in valid_views:
        view = args[-1].lower()
        args = args[:-1]
    q = " ".join(args).strip()
    if not q:
        await update.message.reply_text("사용법: /paper_stats <키워드> [view]")
        return
    from .agent import papersearch
    async with _SustainedTyping(update, ctx):
        status = await update.message.reply_text(
            f"📊 '{q}' 논문 통계 분석 중 (최대 1000편, 약 20~30초 소요)..."
        )
        try:
            papers = await papersearch._openalex_bulk(q, max_count=1000)
        except Exception as e:
            log.exception("paper_stats bulk fetch failed for %r", q)
            await _edit_or_send(
                ctx, status.chat.id, status.message_id,
                f"⚠️ 논문 통계 가져오기 실패: {_explain_error(e)}",
            )
            return
        if not papers:
            await _edit_or_send(
                ctx, status.chat.id, status.message_id,
                f"📊 '{q}' 관련 논문 없음. 키워드 조정 후 재시도.",
            )
            return
        try:
            if view == "trend":
                data = papersearch.compute_paper_trend(papers)
                body = _format_paper_stats_trend(q, data)
            elif view == "newcomers":
                data = papersearch.compute_paper_newcomers(papers)
                body = _format_paper_stats_newcomers(q, data)
            elif view == "network":
                data = papersearch.compute_paper_coauthors(papers)
                body = _format_paper_stats_network(q, data)
            elif view == "keywords":
                data = await papersearch.extract_paper_keywords(papers)
                body = _format_paper_stats_keywords(q, data)
            elif view == "top":
                ranked_top = sorted(
                    papers,
                    key=lambda p: int(p.get("citations") or 0),
                    reverse=True,
                )[:15]
                from .agent.translate import translate_and_overwrite
                await translate_and_overwrite(ranked_top)
                body = _format_paper_stats_top(q, ranked_top)
            else:
                data = papersearch.compute_paper_stats(papers)
                body = _format_paper_stats_overview(q, data)
        except Exception as e:
            log.exception("paper_stats render failed (view=%s)", view)
            await _edit_or_send(
                ctx, status.chat.id, status.message_id,
                f"⚠️ 통계 렌더 실패: {_explain_error(e)}",
            )
            return
        _record_command_qna(
            update,
            question=(update.message.text or f"/paper_stats {q} {view}").strip(),
            body=body,
            tools=["paper_stats"],
        )
        await _send_body_with_mermaid(update, ctx, body, status_msg=status)


async def cmd_search_papers_advanced(
        update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/search_papers_advanced <키워드> [filters]
    filters: author=X · venue=Y · ipc=H01L (n/a for papers, use concept=) ·
             concept=Z · from=YYYY · to=YYYY · oa=true · min_citations=N
    Example:
      /search_papers_advanced hybrid bonding author=Smith from=2023 oa=true
    """
    if not _is_owner(update):
        return
    args = list(ctx.args)
    if not args:
        await update.message.reply_text(
            "사용법: /search_papers_advanced <키워드> [필터]\n"
            "필터: author=저자 · venue=학술지 · concept=주제 · "
            "from=YYYY · to=YYYY · oa=true · min_citations=10 · type=article\n"
            "예: /search_papers_advanced hybrid bonding author=Lau from=2022 oa=true"
        )
        return
    filters = {
        "author": "", "venue": "", "concept": "",
        "from": "", "to": "", "oa": "", "min_citations": "", "type": "",
    }
    keyword_parts: list[str] = []
    for a in args:
        if "=" in a:
            k, v = a.split("=", 1)
            k = k.lower().strip()
            v = v.strip()
            if k in filters and v:
                filters[k] = v
                continue
        keyword_parts.append(a)
    q = " ".join(keyword_parts).strip()
    if not q and not any(filters.values()):
        await update.message.reply_text(
            "키워드 또는 최소 1개 필터 필요. /search_papers_advanced <키워드> ..."
        )
        return
    year_from = None
    year_to = None
    try:
        year_from = int(filters["from"]) if filters["from"] else None
    except ValueError:
        await update.message.reply_text("from= 은 연도(YYYY) 여야 함")
        return
    try:
        year_to = int(filters["to"]) if filters["to"] else None
    except ValueError:
        await update.message.reply_text("to= 은 연도(YYYY) 여야 함")
        return
    min_citations = None
    if filters["min_citations"]:
        try:
            min_citations = int(filters["min_citations"])
        except ValueError:
            await update.message.reply_text("min_citations= 은 정수여야 함")
            return
    oa_only = filters["oa"].lower() in ("true", "1", "yes", "y")
    from .agent import papersearch
    async with _SustainedTyping(update, ctx):
        label = q if q else "(no keyword)"
        extra: list[str] = []
        if filters["author"]:
            extra.append(f"저자={filters['author']}")
        if filters["venue"]:
            extra.append(f"학술지={filters['venue']}")
        if filters["concept"]:
            extra.append(f"주제={filters['concept']}")
        if year_from:
            extra.append(f"≥{year_from}")
        if year_to:
            extra.append(f"≤{year_to}")
        if oa_only:
            extra.append("OA only")
        if min_citations:
            extra.append(f"인용≥{min_citations}")
        if filters["type"]:
            extra.append(f"type={filters['type']}")
        if extra:
            label += f" [{' · '.join(extra)}]"
        status = await update.message.reply_text(
            f"🔍 '{label}' 고급 논문 검색 중 (한국어 번역 포함)..."
        )
        try:
            results = await papersearch.search_advanced(
                q, limit=50,
                author=filters["author"], venue=filters["venue"],
                year_from=year_from, year_to=year_to,
                oa_only=oa_only, min_citations=min_citations,
                concept=filters["concept"], type_=filters["type"],
            )
        except Exception as e:
            log.exception("search_papers_advanced failed for %r", q)
            await _edit_or_send(
                ctx, status.chat.id, status.message_id,
                f"⚠️ 검색 실패: {_explain_error(e)}",
            )
            return
        results = await _translate_results_korean(results)
        body = _format_papers_text(label, results)
        _record_command_qna(
            update,
            question=(update.message.text or
                      f"/search_papers_advanced {q}").strip(),
            body=body,
            tools=["search_papers_advanced", "search_papers"],
            sources=[p.get("doi", "") for p in results if p.get("doi")],
        )
        pieces = _split_for_telegram(body)
        if pieces:
            try:
                await ctx.bot.edit_message_text(
                    chat_id=status.chat.id, message_id=status.message_id,
                    text=pieces[0], parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                try:
                    await update.message.reply_text(
                        pieces[0], parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    log.exception("search_papers_advanced fallback send failed")
            for piece in pieces[1:]:
                try:
                    await update.message.reply_text(
                        piece, parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    log.exception("search_papers_advanced chunked send failed")


async def cmd_search_papers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Direct /search_papers — bypasses the agent.

    Same rationale as cmd_search_patents: the agent loop has dropped
    explicit tool requests in the past ("agent skipped tools" then
    nudge-then-refuse), and the LLM rendering cost is real if we go
    that path twice (compose + verify). Direct call ensures the user
    always gets results when the backends have them, and the
    deterministic renderer is consistent with the patents path.

    Natural-language "논문 찾아줘" / "papers" queries still route
    through the agent + (P) framework for the richer prose rendering
    when the model cooperates."""
    if not _is_owner(update):
        return
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("사용법: /search_papers <검색어>")
        return
    async with _SustainedTyping(update, ctx):
        status = await update.message.reply_text(
            f"🔍 '{q}' 논문 검색 중... (한국어 번역 포함)"
        )
        try:
            from .agent import papersearch
            results = await papersearch.search(q, limit=50)
        except Exception as e:
            log.exception("search_papers direct call failed for %r", q)
            try:
                await ctx.bot.edit_message_text(
                    chat_id=status.chat.id, message_id=status.message_id,
                    text=f"⚠️ 논문 검색 실패: {_explain_error(e)}",
                )
            except Exception:
                pass
            return
        results = await _translate_results_korean(results)
        body = _format_papers_text(q, results)
        _record_command_qna(
            update,
            question=(update.message.text or f"/search_papers {q}").strip(),
            body=body,
            tools=["search_papers"],
            sources=[p.get("doi", "") for p in results if p.get("doi")],
        )
        pieces = _split_for_telegram(body)
        if pieces:
            try:
                await ctx.bot.edit_message_text(
                    chat_id=status.chat.id, message_id=status.message_id,
                    text=pieces[0], parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                log.warning("search_papers status edit failed, sending fresh")
                try:
                    await update.message.reply_text(
                        pieces[0], parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    log.exception("search_papers fallback send failed")
            for piece in pieces[1:]:
                try:
                    await update.message.reply_text(
                        piece, parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    log.exception("search_papers chunked send failed")


def _truncate_at_sentence(text: str, cap: int) -> str:
    """Trim `text` to roughly `cap` chars, breaking at the nearest
    sentence boundary inside the cap window. Adds " …" suffix when
    truncated. Korean sentences usually end with 다./요./니다./까? +
    space; English ends with ". ". Uses whichever endpoint is
    closest to the cap. Falls back to a hard char cut + "…" when no
    sentence boundary is found inside the search window."""
    if not text or len(text) <= cap:
        return text
    cutoff = text[:cap]
    # Search the last ~250 chars of the cap for any sentence end.
    search_from = max(0, cap - 250)
    candidates = [
        cutoff.rfind("다. ", search_from),
        cutoff.rfind("다.\n", search_from),
        cutoff.rfind("요. ", search_from),
        cutoff.rfind("요.\n", search_from),
        cutoff.rfind("니다. ", search_from),
        cutoff.rfind("니다.\n", search_from),
        cutoff.rfind(". ", search_from),
        cutoff.rfind(".\n", search_from),
        cutoff.rfind("? ", search_from),
        cutoff.rfind("! ", search_from),
    ]
    end = max(candidates)
    if end < 0:
        return cutoff.rstrip() + "…"
    # Include the punctuation that defined the sentence end.
    sep_len = 2  # most candidates are 2 chars ("다.", ". ", etc.)
    # but "니다." is 3 chars + trailing space — handle generically by
    # walking forward to next whitespace.
    end_with_punct = end
    while end_with_punct < len(cutoff) and cutoff[end_with_punct] not in (" ", "\n"):
        end_with_punct += 1
    return cutoff[:end_with_punct] + " …"


async def _translate_results_korean(results: list[dict]) -> list[dict]:
    """Thin wrapper around `agent.translate.translate_to_korean`.

    Kept as a local name so the existing call sites in bot.py don't
    have to import the agent package. The shared implementation
    batches 50-row searches across ≤15-row chunks in parallel so
    nothing falls off the end past max_tokens (the older single-call
    path silently truncated paper #~30+ at limit=50). The same shared
    function is invoked from the agent tools layer (search_papers,
    search_patents, …) so natural-language queries also come back in
    Korean — see `agent.translate` for the full story."""
    from .agent.translate import translate_to_korean
    return await translate_to_korean(results)


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Strip HTML tags for plain-text storage in qna.db. The dashboard
    re-escapes whatever is stored, so HTML markup like <b>...</b>
    would appear as literal '<b>...</b>' tag source in the browser if
    we didn't strip it first."""
    if not text:
        return ""
    return _HTML_TAG_RE.sub("", text)


def _record_command_qna(update, question: str, body: str,
                        tools: list[str],
                        sources: list[str] | None = None,
                        model: str | None = None) -> None:
    """Persist a direct-command Q&A to the dashboard archive +
    trigger a regen so the card shows up immediately. Errors are
    swallowed — archiving must never block the user reply. Strips
    HTML tags from the answer body so the dashboard's _esc() doesn't
    render them as literal <b> source."""
    try:
        chat_id = (update.effective_chat.id if update.effective_chat
                   else update.message.chat.id)
        plain_answer = _strip_html(body)
        qna.record(
            chat_id=chat_id,
            question=question,
            answer=plain_answer,
            sources=sources or [],
            tools=tools,
            model=model,
        )
    except Exception:
        log.exception("command qna record failed (q=%r)", question[:60])
        return
    try:
        # Offload to the thread pool — regenerate() rewrites the detail
        # pages and would otherwise block the event loop for the whole
        # write. Fire-and-forget: the non-blocking lock inside
        # regenerate() makes a concurrent/queued call a safe no-op.
        from .dashboard import regenerate as dashboard_regen
        asyncio.get_running_loop().run_in_executor(
            None, dashboard_regen.regenerate)
    except Exception:
        log.exception("dashboard regenerate after command qna failed")



def _format_patents_text(query: str, results: list[dict]) -> str:
    """Direct deterministic render for /search_patents command output.

    Bypasses the agent so the result is guaranteed regardless of
    Gemini's tool-selection behaviour (which has been observed
    refusing to call search_patents even with explicit prompt
    coercion, then serving the "no source" refusal). Uses the same
    information density as the agent's (P-2) framework but without
    an LLM call — title + meta block + abstract excerpt + URL per
    patent, sized for Telegram readability. Renders the richer EPO
    fields (IPC, family, priority, kind) when present, gracefully
    omits them for KIPRIS rows."""
    import html as _html
    if not results:
        return (f"🔍 '<b>{_html.escape(query)}</b>' 관련 특허 결과 없음.\n"
                f"검색어를 영문 키워드로 좁히면 결과가 나올 가능성 ↑.")
    # Detect backend by inspecting the rows' source marker. KIPRIS
    # rows carry source='KIPRIS' (set by _kipris_to_unified); EPO
    # rows source='EPO'. Mixed lists are rare but possible — show
    # both labels when so.
    _source_label_map = {"KIPRIS": "KIPRIS Plus", "EPO": "EPO OPS"}
    raw_sources = {p.get("source", "EPO") or "EPO" for p in results}
    sources = sorted(_source_label_map.get(s, s) for s in raw_sources)
    source_label = " + ".join(sources) or "EPO OPS"
    out = [
        f"⚖️ <b>특허 검색 결과 — '{_html.escape(query)}'</b>",
        f"<i>{len(results)}건 · {_html.escape(source_label)}</i>",
    ]
    for i, p in enumerate(results, 1):
        title_src = (p.get("title_ko") or p.get("title") or "(제목 없음)")
        title = _html.escape(title_src)[:300]
        num = p.get("patent_number") or ""
        kind_lbl = p.get("kind_label") or ""
        pub_date = (p.get("publication_date") or p.get("date") or "")[:10]
        app_date = (p.get("application_date") or "")[:10]
        prio_date = (p.get("priority_date") or "")[:10]
        family_id = p.get("family_id") or ""
        assignee_raw = (p.get("assignee") or "").strip()
        assignee = _html.escape(assignee_raw[:80])
        app_countries = p.get("applicant_countries") or []
        country_tag = f"[{','.join(app_countries[:3])}]" if app_countries else ""
        inv_list = p.get("inventors") or []
        inv_total = p.get("inventors_total") or len(inv_list)
        inv = _html.escape(inv_list[0]) if inv_list else ""
        if inv and inv_total > 1:
            inv += f" et al. (총 {inv_total}명)" if inv_total <= 99 else " et al."
        ipc_codes = p.get("ipc") or []
        ipc_line = (" · ".join(_html.escape(c) for c in ipc_codes[:4])
                    if ipc_codes else "")
        abstract_src = (p.get("abstract_ko") or p.get("abstract") or "")
        abstract = _html.escape(
            _truncate_at_sentence(abstract_src.strip(), 900)
        )
        url = p.get("url") or ""

        block = [f"\n⚖️ <b>{i}. {title}</b>"]
        # Date line: 출원 / 공개 / 우선권 (보이는 것만)
        date_parts: list[str] = []
        if app_date:
            date_parts.append(f"출원 {app_date}")
        if pub_date and pub_date != app_date:
            date_parts.append(f"공개 {pub_date}")
        if prio_date and prio_date != app_date:
            date_parts.append(f"우선권 {prio_date}")
        if date_parts:
            block.append(f"  📅 <i>{' · '.join(date_parts)}</i>")
        # Number line: 출원번호 + kind label + family
        num_parts: list[str] = []
        if num:
            num_disp = num
            if kind_lbl:
                num_disp += f" ({kind_lbl})"
            num_parts.append(num_disp)
        if family_id:
            num_parts.append(f"Family {family_id}")
        if num_parts:
            block.append(f"  🔢 {' · '.join(num_parts)}")
        # Applicant + countries
        if assignee:
            asg_line = f"🏢 출원인: {assignee}"
            if country_tag:
                asg_line += f" {country_tag}"
            block.append(f"  {asg_line}")
        # Inventors
        if inv:
            block.append(f"  👤 발명자: {inv}")
        # IPC
        if ipc_line:
            block.append(f"  🏷️ 분류: {ipc_line}")
        # Abstract
        if abstract:
            block.append(f"  📝 {abstract}")
        if url:
            block.append(f"   → {url}")
        out.append("\n".join(block))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Stats view formatters — text bars + mermaid xychart for the trend
# view. Tables are rendered as monospace-aligned text blocks inside
# <pre> so Telegram preserves the column layout.
# ---------------------------------------------------------------------------

def _bar(count: int, max_count: int, width: int = 12) -> str:
    """Unicode bar of `width` blocks, scaled to max_count."""
    if max_count <= 0:
        return ""
    filled = max(1, round(width * count / max_count))
    return "█" * filled


def _format_patent_stats_overview(query: str, stats: dict) -> str:
    """Render the default /patent_stats view — by-applicant /
    by-country / by-year / by-IPC bar charts."""
    import html as _html
    total = stats.get("total", 0)
    if total == 0:
        return (f"📊 '<b>{_html.escape(query)}</b>' 특허 통계 — 분석할 자료 없음.\n"
                f"검색어를 좁혀서 다시 시도해줘.")
    out = [
        f"📊 <b>'{_html.escape(query)}' 특허 통계</b>",
        f"<i>최근 {total}건 분석 · EPO OPS</i>",
    ]
    # By year (most recent first)
    by_year = stats.get("by_year") or []
    if by_year:
        out.append("\n📅 <b>출원 연도별</b>")
        max_y = max((c for _, c in by_year), default=0)
        for y, c in by_year[:10]:
            pct = (c / total * 100) if total else 0
            out.append(f"  {y}: {_bar(c, max_y)} {c}건 ({pct:.1f}%)")
    # By country
    by_country = stats.get("by_country") or []
    if by_country:
        out.append("\n🌍 <b>출원 국가/기관별</b>")
        max_c = max((c for _, c in by_country), default=0)
        cc_label = {
            "US": "US (미국)", "EP": "EP (유럽특허청)",
            "WO": "WO (PCT 국제출원)", "KR": "KR (한국)",
            "JP": "JP (일본)", "CN": "CN (중국)",
            "DE": "DE (독일)", "TW": "TW (대만)",
            "GB": "GB (영국)", "FR": "FR (프랑스)",
        }
        for cc, c in by_country[:8]:
            label = cc_label.get(cc, cc)
            pct = (c / total * 100) if total else 0
            out.append(f"  {label}: {_bar(c, max_c)} {c}건 ({pct:.1f}%)")
    # By applicant TOP
    by_applicant = stats.get("by_applicant") or []
    if by_applicant:
        out.append("\n🏢 <b>출원인 TOP 10</b>")
        for rank, (name, c) in enumerate(by_applicant[:10], 1):
            out.append(f"  {rank}. {_html.escape(name)[:50]} — {c}건")
    # By IPC subclass
    by_ipc = stats.get("by_ipc") or []
    if by_ipc:
        out.append("\n🏷️ <b>IPC 분류 TOP 8</b> (subclass level)")
        for ipc, c in by_ipc[:8]:
            label = _IPC_LABELS.get(ipc, "")
            line = f"  {_html.escape(ipc)}"
            if label:
                line += f" ({label})"
            line += f" — {c}건"
            out.append(line)
    out.append(
        "\n🔗 <b>추가 분석:</b>\n"
        f"  • /patent_stats {query} trend — 회사별 연도 추세\n"
        f"  • /patent_stats {query} newcomers — 신규 진입자\n"
        f"  • /patent_stats {query} network — 공동출원 네트워크\n"
        f"  • /patent_stats {query} keywords — 키워드 cloud (Gemini)"
    )
    return "\n".join(out)


# IPC subclass → 한국어 짧은 설명 (반도체/패키지/전자 도메인 위주, 다
# 외워서 사람이 읽기 좋게 매핑. 없는 코드는 그냥 코드만 노출).
_IPC_LABELS = {
    "H01L21": "반도체 제조공정",
    "H01L23": "반도체 패키지/하우징",
    "H01L24": "반도체 본딩/배선",
    "H01L25": "다중칩/3D 적층",
    "H01L27": "집적회로 일반",
    "H01L29": "반도체 소자 구조",
    "H01L31": "광 반도체 소자",
    "H01L33": "LED",
    "H01L51": "유기반도체/OLED",
    "G02F1": "광변조/디스플레이",
    "G06F": "디지털 데이터 처리",
    "G06N": "AI/머신러닝",
    "H04L": "디지털 통신",
    "H04N": "이미지 통신",
    "B23K": "용접/접합",
    "C09J": "접착제",
    "C23C": "박막 증착",
    "C30B": "단결정 성장",
    "G01N": "측정/분석",
    "G03F": "포토리소그래피",
}


def _format_patent_stats_trend(query: str, trend: dict) -> str:
    """Mermaid xychart bar of yearly counts for top 5 applicants."""
    import html as _html
    years = trend.get("years") or []
    series = trend.get("series") or {}
    top = trend.get("top_applicants") or []
    if not (years and series and top):
        return (f"📈 '<b>{_html.escape(query)}</b>' 추세 분석 — 데이터 부족.")
    out = [
        f"📈 <b>'{_html.escape(query)}' 회사별 연도 추세</b>",
        f"<i>TOP {len(top)} 출원인 × {min(years)}~{max(years)} 연도별 건수</i>",
        "",
        "```mermaid",
        "xychart-beta",
        f'  title "{query} — 회사별 연도별 출원량"',
        f"  x-axis [{', '.join(str(y) for y in years)}]",
        "  y-axis \"건수\"",
    ]
    for name in top:
        counts = series.get(name, [])
        out.append(f"  bar [{', '.join(str(n) for n in counts)}]")
    out.append("```")
    out.append("\n<b>범례:</b>")
    for rank, name in enumerate(top, 1):
        total = sum(series.get(name, []))
        out.append(f"  bar{rank}. {_html.escape(name)[:40]} ({total}건 누계)")
    return "\n".join(out)


def _format_patent_stats_newcomers(query: str, newcomers: list[dict]) -> str:
    """Recently-entered applicants (first publication within cutoff)."""
    import html as _html
    if not newcomers:
        return (f"🆕 '<b>{_html.escape(query)}</b>' 신규 진입자 없음 "
                f"(최근 12개월 기준).")
    out = [
        f"🆕 <b>'{_html.escape(query)}' 신규 진입자</b>",
        f"<i>최근 12개월 내 첫 등장 출원인 {len(newcomers)}곳</i>",
    ]
    for r in newcomers:
        out.append(
            f"  • {_html.escape(r['name'])[:50]} — "
            f"첫 등장 {r['first_date']} · 누적 {r['total']}건"
        )
    return "\n".join(out)


def _format_patent_stats_network(query: str, pairs: list[dict]) -> str:
    """Co-applicant pairs — joint patents."""
    import html as _html
    if not pairs:
        return (f"🤝 '<b>{_html.escape(query)}</b>' 공동출원 관계 없음 "
                f"(분석한 corpus에서).")
    out = [
        f"🤝 <b>'{_html.escape(query)}' 공동출원 네트워크</b>",
        f"<i>2인 이상 함께 출원한 케이스 {len(pairs)}쌍</i>",
    ]
    for r in pairs[:15]:
        out.append(
            f"  • {_html.escape(r['a'])[:35]} ⇄ "
            f"{_html.escape(r['b'])[:35]} — {r['count']}건"
        )
    return "\n".join(out)


def _format_patent_stats_keywords(query: str, keywords: list[str]) -> str:
    """Gemini-extracted technical noun phrases."""
    import html as _html
    if not keywords:
        return (f"☁️ '<b>{_html.escape(query)}</b>' 키워드 추출 실패.")
    out = [
        f"☁️ <b>'{_html.escape(query)}' 기술 키워드 클라우드</b>",
        f"<i>분석한 abstract 들에서 추출한 핵심 명사구 {len(keywords)}개</i>",
        "",
        " · ".join(_html.escape(kw) for kw in keywords[:30]),
    ]
    return "\n".join(out)


async def cmd_patent_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/patent_stats <keyword> [overview|trend|newcomers|network|keywords]
    — aggregate analytics over up to 400 EPO patents matching the
    keyword. Default view is 'overview' (applicant/country/year/IPC
    top-N bar charts). Other views give the time-series, newcomer,
    co-applicant network, and keyword cloud cuts of the same corpus."""
    if not _is_owner(update):
        return
    args = list(ctx.args)
    if not args:
        await update.message.reply_text(
            "사용법: /patent_stats <키워드> [view]\n"
            "view: overview (기본) · trend · newcomers · network · keywords\n"
            "예: /patent_stats hybrid bonding trend"
        )
        return
    # Last arg is the view selector if it matches one of our keywords
    valid_views = {"overview", "trend", "newcomers", "network", "keywords"}
    view = "overview"
    if args[-1].lower() in valid_views:
        view = args[-1].lower()
        args = args[:-1]
    q = " ".join(args).strip()
    if not q:
        await update.message.reply_text("사용법: /patent_stats <키워드> [view]")
        return
    from .agent import patentsearch
    if not patentsearch.has_global_backend():
        await update.message.reply_text(
            "⚠️ EPO_API_KEY/EPO_API_SECRET 누락 — 글로벌 특허 분석 불가."
        )
        return
    async with _SustainedTyping(update, ctx):
        status = await update.message.reply_text(
            f"📊 '{q}' 특허 통계 분석 중 (최대 2000건, 약 60~120초 소요)..."
        )
        try:
            patents = await patentsearch._epo_search_bulk(q, max_count=2000)
        except Exception as e:
            log.exception("patent_stats bulk fetch failed for %r", q)
            await _edit_or_send(
                ctx, status.chat.id, status.message_id,
                f"⚠️ 특허 통계 가져오기 실패: {_explain_error(e)}",
            )
            return
        if not patents:
            await _edit_or_send(
                ctx, status.chat.id, status.message_id,
                f"📊 '{q}' 관련 특허 없음. 키워드 조정 후 재시도.",
            )
            return
        try:
            if view == "trend":
                data = patentsearch.compute_patent_trend(patents)
                body = _format_patent_stats_trend(q, data)
            elif view == "newcomers":
                data = patentsearch.compute_patent_newcomers(patents)
                body = _format_patent_stats_newcomers(q, data)
            elif view == "network":
                data = patentsearch.compute_patent_coapplicants(patents)
                body = _format_patent_stats_network(q, data)
            elif view == "keywords":
                data = await patentsearch.extract_patent_keywords(patents)
                body = _format_patent_stats_keywords(q, data)
            else:
                data = patentsearch.compute_patent_stats(patents)
                body = _format_patent_stats_overview(q, data)
        except Exception as e:
            log.exception("patent_stats render failed (view=%s)", view)
            await _edit_or_send(
                ctx, status.chat.id, status.message_id,
                f"⚠️ 통계 렌더 실패: {_explain_error(e)}",
            )
            return
        _record_command_qna(
            update,
            question=(update.message.text or f"/patent_stats {q} {view}").strip(),
            body=body,
            tools=["patent_stats"],
        )
        await _send_body_with_mermaid(update, ctx, body, status_msg=status)


async def cmd_search_patents_advanced(
        update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/search_patents_advanced <키워드> [filters]
    — keyword + CQL-style filters. Filters use `key=value` syntax:
      applicant=SAMSUNG  inventor=Smith  ipc=H01L21
      country=KR  from=2023  to=2026
    Example:
      /search_patents_advanced hybrid bonding applicant=SAMSUNG from=2024
    """
    if not _is_owner(update):
        return
    args = list(ctx.args)
    if not args:
        await update.message.reply_text(
            "사용법: /search_patents_advanced <키워드> [필터]\n"
            "필터: applicant=회사 · inventor=발명자 · ipc=H01L21 · "
            "country=KR · from=2023 · to=2026\n"
            "예: /search_patents_advanced hybrid bonding applicant=SAMSUNG from=2024"
        )
        return
    filters = {
        "applicant": "", "inventor": "", "ipc": "",
        "country": "", "from": "", "to": "",
    }
    keyword_parts: list[str] = []
    for a in args:
        if "=" in a:
            k, v = a.split("=", 1)
            k = k.lower().strip()
            v = v.strip()
            if k in filters and v:
                filters[k] = v
                continue
        keyword_parts.append(a)
    q = " ".join(keyword_parts).strip()
    if not q:
        await update.message.reply_text(
            "키워드가 비어있음. /search_patents_advanced <키워드> ..."
        )
        return
    year_from = None
    year_to = None
    try:
        year_from = int(filters["from"]) if filters["from"] else None
    except ValueError:
        await update.message.reply_text("from= 은 연도(YYYY) 여야 함")
        return
    try:
        year_to = int(filters["to"]) if filters["to"] else None
    except ValueError:
        await update.message.reply_text("to= 은 연도(YYYY) 여야 함")
        return
    from .agent import patentsearch
    if not patentsearch.has_global_backend():
        await update.message.reply_text(
            "⚠️ EPO_API_KEY/EPO_API_SECRET 누락."
        )
        return
    async with _SustainedTyping(update, ctx):
      label = q
      extra: list[str] = []
      if filters["applicant"]:
          extra.append(f"출원인={filters['applicant']}")
      if filters["inventor"]:
          extra.append(f"발명자={filters['inventor']}")
      if filters["ipc"]:
          extra.append(f"IPC={filters['ipc']}")
      if filters["country"]:
          extra.append(f"국가={filters['country']}")
      if year_from:
          extra.append(f"≥{year_from}")
      if year_to:
          extra.append(f"≤{year_to}")
      if extra:
          label += f" [{' · '.join(extra)}]"
      status = await update.message.reply_text(
          f"🔍 '{label}' 고급 특허 검색 중 (한국어 번역 포함)..."
      )
      try:
          results = await patentsearch.search_advanced(
              q, limit=50,
              applicant=filters["applicant"],
              inventor=filters["inventor"],
              ipc=filters["ipc"],
              country=filters["country"],
              year_from=year_from, year_to=year_to,
          )
      except Exception as e:
          log.exception("search_patents_advanced failed for %r", q)
          await _edit_or_send(
              ctx, status.chat.id, status.message_id,
              f"⚠️ 검색 실패: {_explain_error(e)}",
          )
          return
      results = await _translate_results_korean(results)
      body = _format_patents_text(label, results)
      _record_command_qna(
          update,
          question=(update.message.text or
                    f"/search_patents_advanced {q}").strip(),
          body=body,
          tools=["search_patents_advanced", "search_patents"],
          sources=[p.get("patent_number", "")
                   for p in results if p.get("patent_number")],
      )
      pieces = _split_for_telegram(body)
      if pieces:
          try:
              await ctx.bot.edit_message_text(
                  chat_id=status.chat.id, message_id=status.message_id,
                  text=pieces[0], parse_mode="HTML",
                  disable_web_page_preview=True,
              )
          except Exception:
              try:
                  await update.message.reply_text(
                      pieces[0], parse_mode="HTML",
                      disable_web_page_preview=True,
                  )
              except Exception:
                  log.exception("search_patents_advanced fallback send failed")
          for piece in pieces[1:]:
              try:
                  await update.message.reply_text(
                      piece, parse_mode="HTML",
                      disable_web_page_preview=True,
                  )
              except Exception:
                log.exception("search_patents_advanced chunked send failed")


async def cmd_search_patents(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Direct /search_patents — bypasses the agent.

    Global free-text patent search via EPO OPS (DOCDB — EP/WO/US/
    KR/JP/DE/CN). When EPO_API_KEY + EPO_API_SECRET are missing,
    has_global_backend() returns False and we point the user at
    /company_patents (KIPRIS, applicant-only) instead."""
    if not _is_owner(update):
        return
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("사용법: /search_patents <검색어>")
        return
    from .agent import patentsearch
    if not patentsearch.has_global_backend():
        await update.message.reply_text(
            "⚠️ 글로벌 특허 검색 백엔드 미활성.\n"
            ".env 에 EPO_API_KEY / EPO_API_SECRET 누락 — "
            "developers.epo.org 의 My Apps 에서 키 받아서 박아줘.\n\n"
            "💡 한국 회사 특허는 지금 바로:\n"
            "    /company_patents 삼성전기\n"
            "    /company_patents SK하이닉스"
        )
        return
    async with _SustainedTyping(update, ctx):
        status = await update.message.reply_text(
            f"🔍 '{q}' 특허 검색 중... (한국어 번역 포함)"
        )
        try:
            results = await patentsearch.search(q, limit=50)
        except Exception as e:
            log.exception("search_patents direct call failed for %r", q)
            await _edit_or_send(
                ctx, status.chat.id, status.message_id,
                f"⚠️ 특허 검색 실패: {_explain_error(e)}",
            )
            return
        results = await _translate_results_korean(results)
        body = _format_patents_text(q, results)
        _record_command_qna(
            update,
            question=(update.message.text or f"/search_patents {q}").strip(),
            body=body,
            tools=["search_patents"],
            sources=[p.get("patent_number", "")
                     for p in results if p.get("patent_number")],
        )
        pieces = _split_for_telegram(body)
        if pieces:
            try:
                await ctx.bot.edit_message_text(
                    chat_id=status.chat.id, message_id=status.message_id,
                    text=pieces[0], parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                log.warning("search_patents status edit failed, sending fresh")
                try:
                    await update.message.reply_text(
                        pieces[0], parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    log.exception("search_patents fallback send failed")
            for piece in pieces[1:]:
                try:
                    await update.message.reply_text(
                        piece, parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    log.exception("search_patents chunked send failed")


async def cmd_company_patents(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Direct /company_patents — KIPRIS Plus applicant-name lookup.

    Same bypass-the-agent pattern as /search_patents and
    /search_papers: deterministic backend call + shared formatter +
    Korean translation, so the user gets a predictable result block.
    The agent's natural-language path (e.g. "삼성전기 특허 알려줘")
    can still route through search_company_patents tool, but the
    slash command short-circuits the LLM round trip entirely.
    """
    if not _is_owner(update):
        return
    applicant = " ".join(ctx.args).strip()
    if not applicant:
        await update.message.reply_text(
            "사용법: /company_patents <한국 회사명>\n"
            "예: /company_patents 삼성전기\n"
            "    /company_patents SK하이닉스\n"
            "    /company_patents 한양대학교 산학협력단"
        )
        return
    async with _SustainedTyping(update, ctx):
        status = await update.message.reply_text(
            f"🔍 '{applicant}' 한국 특허 검색 중... (KIPRIS · 한국어 번역 포함)"
        )
        try:
            from .agent import patentsearch
            results = await patentsearch.search_by_applicant(applicant, limit=50)
        except Exception as e:
            log.exception("company_patents direct call failed for %r", applicant)
            try:
                await ctx.bot.edit_message_text(
                    chat_id=status.chat.id, message_id=status.message_id,
                    text=f"⚠️ KIPRIS 검색 실패: {_explain_error(e)}",
                )
            except Exception:
                pass
            return
        results = await _translate_results_korean(results)
        body = _format_patents_text(
            f"{applicant} (KIPRIS 출원인 검색)", results,
        )
        _record_command_qna(
            update,
            question=(update.message.text or f"/company_patents {applicant}").strip(),
            body=body,
            tools=["search_company_patents"],
            sources=[p.get("patent_number", "")
                     for p in results if p.get("patent_number")],
        )
        pieces = _split_for_telegram(body)
        if pieces:
            try:
                await ctx.bot.edit_message_text(
                    chat_id=status.chat.id, message_id=status.message_id,
                    text=pieces[0], parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                log.warning("company_patents status edit failed, sending fresh")
                try:
                    await update.message.reply_text(
                        pieces[0], parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    log.exception("company_patents fallback send failed")
            for piece in pieces[1:]:
                try:
                    await update.message.reply_text(
                        piece, parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    log.exception("company_patents chunked send failed")


async def cmd_patent_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/patent_detail <KR 출원번호> — KIPRIS 단건 상세 조회.

    Use case: /company_patents 결과의 KR-출원####### 번호를 보고
    abstract / IPC / 상세 정보가 더 필요할 때 그 번호로 deep-link.
    Detail endpoint returns Abstract + InternationalpatentclassificationNumber
    that the list endpoint omits.
    """
    if not _is_owner(update):
        return
    app_num = " ".join(ctx.args).strip()
    if not app_num:
        await update.message.reply_text(
            "사용법: /patent_detail <KR 출원번호 13자리>\n"
            "예: /patent_detail 1020230012345\n"
            "(/company_patents 결과의 'KR-출원…' 뒤 숫자를 그대로 입력)"
        )
        return
    # Tolerant of "KR-출원####" prefix users might copy from /company_patents
    digits = "".join(ch for ch in app_num if ch.isdigit())
    if not digits:
        await update.message.reply_text(
            "출원번호는 숫자만 인식. 예: 1020230012345"
        )
        return
    async with _SustainedTyping(update, ctx):
        status = await update.message.reply_text(
            f"🔍 출원 {digits} 상세 조회 중... (KIPRIS · 한국어 번역 포함)"
        )
        try:
            from .agent import patentsearch
            row = await patentsearch.get_patent_detail(digits)
        except Exception as e:
            log.exception("patent_detail call failed for %r", digits)
            try:
                await ctx.bot.edit_message_text(
                    chat_id=status.chat.id, message_id=status.message_id,
                    text=f"⚠️ KIPRIS 상세 조회 실패: {_explain_error(e)}",
                )
            except Exception:
                pass
            return
        if not row:
            try:
                await ctx.bot.edit_message_text(
                    chat_id=status.chat.id, message_id=status.message_id,
                    text=f"🔍 출원번호 {digits} — KIPRIS 매칭 없음 "
                         "(번호 오타 또는 비공개 출원).",
                )
            except Exception:
                pass
            return
        results = await _translate_results_korean([row])
        body = _format_patents_text(f"출원번호 {digits} 상세", results)
        _record_command_qna(
            update,
            question=(update.message.text or f"/patent_detail {digits}").strip(),
            body=body,
            tools=["get_patent_detail"],
            sources=[results[0].get("patent_number", "")] if results else [],
        )
        pieces = _split_for_telegram(body)
        if pieces:
            try:
                await ctx.bot.edit_message_text(
                    chat_id=status.chat.id, message_id=status.message_id,
                    text=pieces[0], parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                log.warning("patent_detail status edit failed, sending fresh")
                try:
                    await update.message.reply_text(
                        pieces[0], parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    log.exception("patent_detail fallback send failed")
            for piece in pieces[1:]:
                try:
                    await update.message.reply_text(
                        piece, parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    log.exception("patent_detail chunked send failed")


async def cmd_citing_patents(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/citing_patents <KR 출원번호> — 이 특허를 인용한 후행 특허 조회.

    KIPRIS Citing 서비스 결과는 인용 측 출원번호 + 상태 + 인용 문헌
    type 정도만 줘서 thin row 로 렌더링. 더 자세한 인용 특허 정보가
    필요하면 결과의 KR 출원번호로 /patent_detail 한 번 더.
    """
    if not _is_owner(update):
        return
    app_num = " ".join(ctx.args).strip()
    if not app_num:
        await update.message.reply_text(
            "사용법: /citing_patents <KR 출원번호 13자리>\n"
            "예: /citing_patents 1020200012345\n"
            "(이 출원을 인용한 후행 특허 목록 조회)"
        )
        return
    digits = "".join(ch for ch in app_num if ch.isdigit())
    if not digits:
        await update.message.reply_text(
            "출원번호는 숫자만 인식. 예: 1020200012345"
        )
        return
    async with _SustainedTyping(update, ctx):
        status = await update.message.reply_text(
            f"🔍 출원 {digits} 인용 특허 조회 중... (KIPRIS)"
        )
        try:
            from .agent import patentsearch
            rows = await patentsearch.get_citing_patents(digits)
        except Exception as e:
            log.exception("citing_patents call failed for %r", digits)
            try:
                await ctx.bot.edit_message_text(
                    chat_id=status.chat.id, message_id=status.message_id,
                    text=f"⚠️ KIPRIS 인용 조회 실패: {_explain_error(e)}",
                )
            except Exception:
                pass
            return
        if not rows:
            try:
                await ctx.bot.edit_message_text(
                    chat_id=status.chat.id, message_id=status.message_id,
                    text=f"🔍 출원 {digits} 를 인용한 후행 특허 없음 "
                         "(또는 KIPRIS 미수록).",
                )
            except Exception:
                pass
            return
        rows = await _translate_results_korean(rows)
        body = _format_patents_text(f"출원번호 {digits} 인용 특허", rows)
        _record_command_qna(
            update,
            question=(update.message.text or f"/citing_patents {digits}").strip(),
            body=body,
            tools=["get_citing_patents"],
            sources=[r.get("patent_number", "") for r in rows if r.get("patent_number")],
        )
        pieces = _split_for_telegram(body)
        if pieces:
            try:
                await ctx.bot.edit_message_text(
                    chat_id=status.chat.id, message_id=status.message_id,
                    text=pieces[0], parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                log.warning("citing_patents status edit failed, sending fresh")
                try:
                    await update.message.reply_text(
                        pieces[0], parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    log.exception("citing_patents fallback send failed")
            for piece in pieces[1:]:
                try:
                    await update.message.reply_text(
                        piece, parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    log.exception("citing_patents chunked send failed")


# ---------------------------------------------------------------------------
# KIPRIS Plus — 11 new commands pre-built 2026-05 alongside the user's
# 활용신청. Each calls a different KIPRIS endpoint; the shared helper
# below handles status / formatting / dashboard recording.
# ---------------------------------------------------------------------------


async def _kipris_search_command(
    update, ctx, query: str, label: str, emoji: str,
    fn, tool_name: str, enrich: bool = False,
) -> None:
    """Shared body for the 11 new KIPRIS list-style search commands
    that return list[dict] of patent-shape rows. Mirrors the existing
    /company_patents pipeline: typing → KIPRIS call → optional
    detail enrichment → Korean title translation → format → record →
    send chunked."""
    if not _is_owner(update):
        return
    if not query:
        return
    async with _SustainedTyping(update, ctx):
        status = await update.message.reply_text(
            f"{emoji} '{query}' {label} 검색 중... (KIPRIS · 한국어 번역 포함)"
        )
        try:
            results = await fn(query, limit=50)
        except Exception as e:
            log.exception("kipris %s direct call failed for %r", label, query)
            await _edit_or_send(
                ctx, status.chat.id, status.message_id,
                f"⚠️ KIPRIS {label} 검색 실패: {_explain_error(e)}",
            )
            return
        if enrich and results:
            try:
                from .agent import patentsearch as _ps
                results = await _ps._enrich_kipris_rows_with_details(results)
            except Exception:
                log.exception("kipris %s enrichment failed", label)
        if not results:
            await _edit_or_send(
                ctx, status.chat.id, status.message_id,
                f"🔍 '{_html_escape_safe(query)}' KIPRIS {label} 결과 없음.\n"
                f"활용신청 승인 전이거나 매칭 0건 — 승인 메일 받은 뒤 "
                f"재시도 / 더 일반적인 키워드로 시도."
            )
            # Record the empty hit so the dashboard reflects activity.
            _record_command_qna(
                update,
                question=(update.message.text or
                          f"/{tool_name} {query}").strip(),
                body=f"🔍 '{query}' KIPRIS {label} 결과 없음.",
                tools=[tool_name],
            )
            return
        results = await _translate_results_korean(results)
        body = _format_patents_text(f"{query} (KIPRIS {label})", results)
        _record_command_qna(
            update,
            question=(update.message.text or
                      f"/{tool_name} {query}").strip(),
            body=body, tools=[tool_name],
            sources=[p.get("patent_number", "")
                     for p in results if p.get("patent_number")],
        )
        pieces = _split_for_telegram(body)
        if not pieces:
            return
        try:
            await ctx.bot.edit_message_text(
                chat_id=status.chat.id, message_id=status.message_id,
                text=pieces[0], parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            try:
                await update.message.reply_text(
                    pieces[0], parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                log.exception("kipris %s fallback failed", label)
        for piece in pieces[1:]:
            try:
                await update.message.reply_text(
                    piece, parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                log.exception("kipris %s chunked send failed", label)


async def _kipris_lookup_command(
    update, ctx, query: str, label: str, emoji: str,
    fn, tool_name: str, is_list: bool = False,
) -> None:
    """Shared body for the lookup-style KIPRIS commands that return
    raw dict / list[dict] of free-form fields (status / class /
    family / rights / claims / priority / trend). Renders as a
    plain key:value dump until per-service schemas are confirmed
    post-approval."""
    if not _is_owner(update):
        return
    if not query:
        return
    async with _SustainedTyping(update, ctx):
        status = await update.message.reply_text(
            f"{emoji} '{query}' {label} 조회 중..."
        )
        try:
            result = await fn(query)
        except Exception as e:
            log.exception("kipris %s lookup failed for %r", label, query)
            await _edit_or_send(
                ctx, status.chat.id, status.message_id,
                f"⚠️ KIPRIS {label} 조회 실패: {_explain_error(e)}",
            )
            return
        import html as _html
        if not result:
            body = (f"🔍 '{_html.escape(query)}' KIPRIS {label} 결과 없음.\n"
                    f"활용신청 승인 전이거나 데이터 없음 — 승인 메일 받은 "
                    f"뒤 재시도.")
        else:
            out = [f"{emoji} <b>KIPRIS {label} — '{_html.escape(query)}'</b>"]
            rows = result if is_list else [result]
            out.append(f"<i>{len(rows)}건 · KIPRIS Plus</i>")
            for i, r in enumerate(rows, 1):
                block = [f"\n{emoji} <b>{i}.</b>"]
                for k, v in r.items():
                    if v:
                        block.append(
                            f"  <b>{_html.escape(str(k))}</b>: "
                            f"{_html.escape(str(v)[:200])}"
                        )
                out.append("\n".join(block))
            body = "\n".join(out)
        _record_command_qna(
            update,
            question=(update.message.text or
                      f"/{tool_name} {query}").strip(),
            body=body, tools=[tool_name],
        )
        pieces = _split_for_telegram(body)
        if not pieces:
            return
        try:
            await ctx.bot.edit_message_text(
                chat_id=status.chat.id, message_id=status.message_id,
                text=pieces[0], parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            await update.message.reply_text(
                pieces[0], parse_mode="HTML",
                disable_web_page_preview=True,
            )
        for piece in pieces[1:]:
            try:
                await update.message.reply_text(
                    piece, parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                log.exception("kipris %s chunked send failed", label)


# --- Search-style commands (return patent list, use _format_patents_text) ---


async def cmd_kipris_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/kipris_search <키워드> — KIPRIS Plus 통합 free-text 검색 (#3).
    제목·초록·청구항을 가로질러 매칭. 100건 / 최신순 / 상위 30건
    abstract 보강 — /company_patents 동등 품질."""
    q = " ".join(ctx.args or []).strip()
    if not q:
        await update.message.reply_text("사용법: /kipris_search <키워드>")
        return
    from .agent import patentsearch
    await _kipris_search_command(
        update, ctx, q, "통합검색", "⚖️",
        patentsearch.kipris_search, "kipris_search", enrich=False,
    )


async def cmd_kipris_pub(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/kipris_pub <키워드> — KIPRIS Plus 공개공보 only (#1)."""
    q = " ".join(ctx.args or []).strip()
    if not q:
        await update.message.reply_text("사용법: /kipris_pub <키워드>")
        return
    from .agent import patentsearch
    await _kipris_search_command(
        update, ctx, q, "공개공보", "📖",
        patentsearch.kipris_pub_search, "kipris_pub",
    )


async def cmd_kipris_reg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/kipris_reg <키워드> — KIPRIS Plus 등록공보 only (#2)."""
    q = " ".join(ctx.args or []).strip()
    if not q:
        await update.message.reply_text("사용법: /kipris_reg <키워드>")
        return
    from .agent import patentsearch
    await _kipris_search_command(
        update, ctx, q, "등록공보", "✅",
        patentsearch.kipris_reg_search, "kipris_reg",
    )


async def cmd_kipris_inventor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/kipris_inventor <발명자명> — KIPRIS Plus 발명자 검색 (#13).
    회사 떠난 핵심 인력 추적 등."""
    q = " ".join(ctx.args or []).strip()
    if not q:
        await update.message.reply_text(
            "사용법: /kipris_inventor <발명자명>\n"
            "예: /kipris_inventor 홍길동"
        )
        return
    from .agent import patentsearch
    await _kipris_search_command(
        update, ctx, q, "발명자 검색", "👤",
        patentsearch.kipris_inventor_search, "kipris_inventor",
    )


# --- Lookup-style commands (return dict / list of free-form fields) ---


async def cmd_kipris_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/kipris_status <KR 출원번호> — 행정상태 (#4)."""
    q = " ".join(ctx.args or []).strip()
    if not q:
        await update.message.reply_text(
            "사용법: /kipris_status <KR 출원번호 13자리>"
        )
        return
    first_token = (q.split() or [""])[0]
    digits = "".join(ch for ch in first_token if ch.isdigit())
    if not digits:
        await update.message.reply_text("출원번호는 숫자만 인식.")
        return
    from .agent import patentsearch
    await _kipris_lookup_command(
        update, ctx, digits, "행정상태", "📋",
        patentsearch.kipris_admin_status, "kipris_status",
    )


async def cmd_kipris_family(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/kipris_family <KR 출원번호> — 패밀리 특허 (#6)."""
    q = " ".join(ctx.args or []).strip()
    if not q:
        await update.message.reply_text(
            "사용법: /kipris_family <KR 출원번호 13자리>"
        )
        return
    first_token = (q.split() or [""])[0]
    digits = "".join(ch for ch in first_token if ch.isdigit())
    if not digits:
        await update.message.reply_text("출원번호는 숫자만 인식.")
        return
    from .agent import patentsearch
    await _kipris_lookup_command(
        update, ctx, digits, "패밀리 특허", "🌐",
        patentsearch.kipris_family_search, "kipris_family",
        is_list=True,
    )


async def cmd_kipris_claims(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/kipris_claims <KR 출원번호> — 청구항 (#9)."""
    q = " ".join(ctx.args or []).strip()
    if not q:
        await update.message.reply_text(
            "사용법: /kipris_claims <KR 출원번호 13자리>"
        )
        return
    first_token = (q.split() or [""])[0]
    digits = "".join(ch for ch in first_token if ch.isdigit())
    if not digits:
        await update.message.reply_text("출원번호는 숫자만 인식.")
        return
    from .agent import patentsearch
    await _kipris_lookup_command(
        update, ctx, digits, "청구항", "📜",
        patentsearch.kipris_claim_lookup, "kipris_claims",
        is_list=True,
    )


async def cmd_kipris_priority(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/kipris_priority <KR 출원번호> — 우선권 (#10)."""
    q = " ".join(ctx.args or []).strip()
    if not q:
        await update.message.reply_text(
            "사용법: /kipris_priority <KR 출원번호 13자리>"
        )
        return
    first_token = (q.split() or [""])[0]
    digits = "".join(ch for ch in first_token if ch.isdigit())
    if not digits:
        await update.message.reply_text("출원번호는 숫자만 인식.")
        return
    from .agent import patentsearch
    await _kipris_lookup_command(
        update, ctx, digits, "우선권", "⭐",
        patentsearch.kipris_priority_lookup, "kipris_priority",
        is_list=True,
    )


_KISTI_FIELD_LABELS = {
    # Long-name keys returned by ScienceON /openapicall.do (confirmed
    # live 2026-05). The earlier short-codes (TI/AU/AB/JN/AP/IN ...)
    # were a guess from vendored kisti-mcp and don't match the actual
    # response — leave them in the table as harmless fallbacks for
    # any target that might use them, but the new keys below are the
    # ones that actually fire for ARTI/PATENT/REPORT.
    "Title": "제목", "Title2": "원어 제목",
    "Author": "저자", "Author2": "원어 저자",
    "Abstract": "초록", "Abstract2": "원어 초록",
    "Affiliation": "기관", "Affiliation2": "원어 기관",
    "JournalName": "저널", "Publisher": "출판사",
    "Pubyear": "발행연도", "Pubdate": "발행일",
    "PageInfo": "페이지", "Keyword": "키워드", "Keyword2": "원어 키워드",
    "VolNo1": "권", "VolNo2": "호", "ISSN": "ISSN", "ISBN": "ISBN",
    "DOI": "DOI", "CN": "CN", "Lang": "언어", "Degree": "학위",
    # RESEARCHER target (confirmed live 2026-05): AuthorNameKor /
    # AuthorNameEng / AuthorInstKor / AuthorInstEng + Article/
    # Patent/Report counts as productivity signal.
    "AuthorNameKor": "이름", "AuthorNameEng": "이름(EN)",
    "AuthorInstKor": "소속", "AuthorInstEng": "소속(EN)",
    "ArticleCnt": "논문", "PatentCnt": "특허", "ReportCnt": "보고서",
    "Email": "Email", "Rno": "Rno",
    # ORGAN target (confirmed live 2026-05): OrganKor / OrganEng +
    # the same productivity counts (Article/Patent/Report).
    "OrganKor": "기관명", "OrganEng": "기관명(EN)",
    # PATENT target (confirmed live 2026-05): Applicants /
    # ApplDate / GrantDate / PublDate / NoticeDate / PatentStatus
    # plus their numeric counterparts. KISTI's PATENT index has no
    # Inventor field — only Applicants — so 발명자 stays unmapped.
    "Applicants": "출원인",
    "ApplDate": "출원일", "ApplNum": "출원번호",
    "GrantDate": "등록일", "GrantNum": "등록번호",
    "PublDate": "공개일", "PublNum": "공개번호",
    "NoticeDate": "공고일", "NoticeNumber": "공고번호",
    "PatentStatus": "상태", "Nation": "국가",
    # TREND target (ScienceON 큐레이션 트렌드 보고서): Definition
    # carries the descriptive body, RelatedKeywords + PdfURL +
    # DefinitionSourceURL extend the meta line.
    "Definition": "정의", "RelatedKeywords": "연관 키워드",
    "PdfURL": "PDF", "DefinitionSourceURL": "출처",
    # ATT target (해외 과학기술 동향): Title/Abstract/Author/Keyword/
    # CN/Pubyear/ContentURL already map via the ARTI shape. Subject
    # / SubjectCode / RegDate are ATT-specific extras.
    "Subject": "주제", "SubjectCode": "주제코드", "RegDate": "등록일",
    # Legacy / fallback short codes — kept in case any of the 6 newly
    # added targets (RESEARCHER/ORGAN/TREND/SNEWS/SCENT/ATT) return
    # them. Harmless if absent.
    "TI": "제목", "TIE": "원어 제목",
    "AU": "저자", "AUE": "원어 저자",
    "AB": "초록", "ABE": "원어 초록",
    "AF": "기관", "JN": "저널",
    "YR": "발행연도", "VL": "권", "IS": "호",
    "PG": "페이지", "KW": "키워드",
    # patent-specific
    "APN": "출원번호", "APD": "출원일",
    "RGN": "등록번호", "RGD": "등록일",
    "AP": "출원인", "IN": "발명자", "IPC": "IPC",
    # report-specific
    "OR": "발행기관", "RN": "보고서번호", "DT": "발간일",
    # researcher / organ specific (RESEARCHER, ORGAN targets)
    "AUI": "이름", "AFI": "소속", "AFE": "소속(EN)",
    "ORN": "기관명", "ORE": "기관명(EN)",
    "MJ": "전공", "POS": "직위", "NM": "이름", "ENM": "이름(EN)",
}


# Per-target render metadata. kind → (emoji, header label, deep-link
# subpath on ScienceON web). All 9 ScienceON contents covered.
_KISTI_KIND_META: dict[str, tuple[str, str, str]] = {
    "paper":         ("📄",  "ScienceON 논문",       "Article"),
    "patent":        ("⚖️",  "ScienceON 특허",       "Patent"),
    "report":        ("📑",  "ScienceON 보고서",      "Report"),
    "trend":         ("🌐",  "ScienceON 해외동향",    "Trend"),
    "researcher":    ("👤",  "ScienceON 연구자",      "Researcher"),
    "organ":         ("🏛️",  "ScienceON 연구기관",    "Organ"),
    "science_trend": ("📈",  "ScienceON Trend",       "Trend"),
    # SCENT (과학향기) and SNEWS (과기뉴스) removed 2026-05: the
    # ScienceON API rejects every searchField value we could try
    # ('searchField 값 오류' on BI/TI/WORD/KW/AB/CT). The dev
    # portal's per-content API spec is undiscoverable from outside,
    # so the two commands were withdrawn rather than left as dead
    # entries. Re-enable when KISTI surfaces the right code.
}


def _format_kisti_results(query: str, results: list[dict],
                          kind: str) -> str:
    """Format ScienceON metaCode-keyed result rows for Telegram.
    `kind` selects the lead emoji + label set + detail-page subpath
    from _KISTI_KIND_META. All 9 ScienceON targets share the same
    metaCode field layout so a single renderer works across them —
    rows that don't have a given field silently skip it."""
    import html as _html
    emoji, label, sub = _KISTI_KIND_META.get(
        kind, ("📌", "ScienceON", "Article"),
    )
    if not results:
        return (
            f"🔍 '<b>{_html.escape(query)}</b>' KISTI ScienceON 결과 없음.\n"
            f"키워드를 좁히거나 영문으로 시도하면 결과 ↑.\n"
            f"(KISTI 인증 미설정 시에도 동일 메시지 — SCIENCEON_API_KEY "
            f"/ CLIENT_ID / MAC_ADDRESS 세 개 모두 .env 에 필요.)"
        )
    out = [
        f"{emoji} <b>{label} 검색 결과 — '{_html.escape(query)}'</b>",
        f"<i>{len(results)}건 · KISTI ScienceON</i>",
    ]
    # Diagnostic: log the first row's keys once per call so any new
    # ScienceON target whose metaCode shape we haven't mapped yet shows
    # up in deploy logs (RESEARCHER/ORGAN/TREND/SNEWS use different
    # field codes than ARTI/PATENT/REPORT).
    if results:
        try:
            log.info("kisti %s first-row keys: %s",
                     kind, sorted(results[0].keys()))
        except Exception:
            pass
    # Title fallback chain — new ScienceON keys first, then short
    # legacy codes for any target whose response format we haven't
    # confirmed yet. RESEARCHER/ORGAN may use Author/Affiliation as
    # their "title" since they have no Title field.
    # Title_ko (added by agent.translate.translate_kisti_rows for
    # non-Korean rows) always wins so the display headline is
    # readable on mobile. Original Title still appears below as a
    # small line for term cross-reference.
    _title_keys = ("Title_ko",
                   "Title", "Title2",
                   "TI", "TIE",
                   # RESEARCHER target: name fields fill the headline
                   "AuthorNameKor", "AuthorNameEng",
                   # ORGAN target: organisation name fills the headline
                   "OrganKor", "OrganEng",
                   "Author", "AUI", "AUE", "AU",
                   "Affiliation", "AFI", "AFE", "AF",
                   "AuthorInstKor", "AuthorInstEng",
                   "ORN", "ORE", "SBJ", "SUB", "HD", "HDN", "NM", "ENM")
    _meta_keys = ("Author", "Author2", "Affiliation", "JournalName",
                  "Pubyear", "Publisher", "PageInfo", "VolNo1", "VolNo2",
                  "ISSN", "ISBN", "Keyword", "Lang", "Degree",
                  # RESEARCHER target meta: institution + productivity
                  "AuthorInstKor", "AuthorInstEng",
                  "ArticleCnt", "PatentCnt", "ReportCnt", "Email",
                  # PATENT target meta: applicant + dates + status
                  # (most informative cap-6 picks come from here)
                  "Applicants", "ApplDate", "GrantDate", "PublDate",
                  "PatentStatus", "Nation", "IPC",
                  "ApplNum", "GrantNum", "PublNum", "NoticeDate",
                  "NoticeNumber",
                  # TREND target meta: related keywords + PDF link
                  # (Definition itself goes to the abstract line)
                  "RelatedKeywords", "PdfURL",
                  # ATT target meta: subject + registration date
                  "Subject", "RegDate",
                  "CN", "DOI",
                  # legacy short codes — fire on any target still
                  # using the older metaCode shape
                  "AU", "AUE", "AUI", "AP", "IN", "JN",
                  "AF", "AFI", "AFE", "OR", "ORN", "MJ", "POS",
                  "YR", "APD", "RGD", "DT", "IPC")
    for i, r in enumerate(results, 1):
        title_raw = ""
        for tk in _title_keys:
            v = (r.get(tk) or "").strip()
            if v:
                title_raw = v
                break
        title = _html.escape(title_raw or "(제목 없음)")[:240]
        # Secondary line under the headline:
        #  - Paper/patent: original (non-Korean) title if we showed
        #    a translated Title_ko above.
        #  - Researcher: English name (AuthorNameEng) when we chose
        #    Korean name as headline.
        original_title = ""
        if r.get("Title_ko"):
            orig = (r.get("Title") or r.get("Title2") or "").strip()
            if orig and orig != title_raw:
                original_title = _html.escape(orig)[:240]
        elif r.get("AuthorNameKor") and r.get("AuthorNameEng"):
            eng = r.get("AuthorNameEng", "").strip()
            if eng and eng != title_raw:
                original_title = _html.escape(eng)[:240]
        elif r.get("OrganKor") and r.get("OrganEng"):
            eng = r.get("OrganEng", "").strip()
            if eng and eng != title_raw:
                original_title = _html.escape(eng)[:240]
        parts: list[str] = []
        for k in _meta_keys:
            v = (r.get(k) or "").strip()
            if v:
                lbl = _KISTI_FIELD_LABELS.get(k, k)
                parts.append(f"{lbl} {_html.escape(v[:80])}")
        meta_line = " · ".join(parts[:6])  # cap to keep readable

        block = [f"\n{emoji} <b>{i}. {title}</b>"]
        if original_title:
            block.append(f"   <i>{original_title}</i>")
        if meta_line:
            block.append(f"   {meta_line}")
        abstract = (r.get("Abstract_ko")
                    or r.get("Abstract") or r.get("Abstract2")
                    or r.get("AB") or r.get("ABE")
                    or r.get("Definition") or "").strip()
        if abstract:
            block.append(f"   {_html.escape(_truncate_at_sentence(abstract, 700))}")
        # Prefer the ContentURL the API itself hands back (more
        # accurate than reconstructing from CN + subpath). Fall back
        # to the constructed link, then MobileURL.
        deep = (r.get("ContentURL") or "").strip()
        if not deep:
            cn = (r.get("CN") or "").strip()
            if cn:
                deep = (f"https://scienceon.kisti.re.kr/srch/"
                        f"selectPORSrch{sub}.do?cn={cn}")
        if not deep:
            deep = (r.get("MobileURL") or "").strip()
        if deep:
            block.append(f"   → {deep}")
        out.append("\n".join(block))
    return "\n".join(out)


_HANGUL_RE = re.compile(r"[가-힣]")


def _looks_korean(text: str) -> bool:
    """Heuristic for when the ScienceON Lang field is missing or
    ambiguous: count Hangul vs. non-whitespace chars. ≥30% Hangul
    is treated as Korean (titles often mix Korean + English acronyms
    like 'HBM3의 신뢰성 분석' — still Korean, we don't translate)."""
    if not text:
        return False
    body = "".join(text.split())
    if not body:
        return False
    hangul = len(_HANGUL_RE.findall(body))
    return (hangul / len(body)) >= 0.30


_KOREAN_LANG_VALUES = {
    "한국어", "ko", "kor", "korean", "한국", "kr",
}


def _kisti_row_needs_translation(row: dict) -> bool:
    """Decide whether a ScienceON row's title should be translated.
    User-confirmed policy:
      1. Lang field present & Korean → skip
      2. Lang field present & non-Korean (English / 영어 / 기타 / 등)
         → translate
      3. Lang missing → Hangul-ratio heuristic on the candidate title
    """
    lang = (row.get("Lang") or "").strip().lower()
    if lang and lang in _KOREAN_LANG_VALUES:
        return False
    title_candidates = (
        row.get("Title") or row.get("Title2") or row.get("TI")
        or row.get("TIE") or ""
    ).strip()
    if not title_candidates:
        return False
    if lang:  # any non-Korean Lang → translate
        return True
    # Lang empty: trust the title's character composition
    return not _looks_korean(title_candidates)


async def _kisti_search_command(update, ctx, query: str, kind: str,
                                fn) -> None:
    """Shared body for /kr_papers, /kr_patents, /kr_reports."""
    async with _SustainedTyping(update, ctx):
        status = await update.message.reply_text(
            f"🔍 '{query}' KISTI ScienceON 검색 중..."
        )
        try:
            result = await fn(query, limit=30)
        except Exception as e:
            log.exception("kisti %s search failed for %r", kind, query)
            try:
                await ctx.bot.edit_message_text(
                    chat_id=status.chat.id, message_id=status.message_id,
                    text=f"⚠️ KISTI ScienceON 검색 실패: {_explain_error(e)}",
                )
            except Exception:
                pass
            return
        # kisti_scienceon search_* functions return list[dict] directly.
        # Defensive: support dict-wrapped {"results": [...]} too in case
        # the caller hands in an agent-tool wrapper instead of the raw
        # client function.
        if isinstance(result, list):
            rows = result
        elif isinstance(result, dict):
            rows = result.get("results") or []
        else:
            rows = []
        # Translate both title AND abstract of non-Korean rows so the
        # rendered card body is fully Korean. Shared agent.translate
        # helper handles batching + skip-Korean-rows; same code path
        # the natural-language search_kr_* agent tools use.
        from .agent.translate import translate_kisti_rows
        await translate_kisti_rows(rows)
        body = _format_kisti_results(query, rows, kind)
        _kisti_tool_name = {
            "paper":         "search_kr_papers",
            "patent":        "search_kr_patents_kisti",
            "report":        "search_kr_reports",
            "trend":         "search_kr_trends",
            "researcher":    "search_kr_researchers",
            "organ":         "search_kr_organs",
            "science_trend": "search_kr_science_trends",
        }.get(kind, f"search_kr_{kind}")
        _record_command_qna(
            update,
            question=(update.message.text or f"/kr_{kind} {query}").strip(),
            body=body,
            tools=[_kisti_tool_name],
            sources=[r.get("CN", "") for r in rows if r.get("CN")],
        )
        pieces = _split_for_telegram(body)
        if pieces:
            try:
                await ctx.bot.edit_message_text(
                    chat_id=status.chat.id, message_id=status.message_id,
                    text=pieces[0], parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                log.warning("kisti status edit failed, sending fresh")
                try:
                    await update.message.reply_text(
                        pieces[0], parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    log.exception("kisti fallback send failed")
            for piece in pieces[1:]:
                try:
                    await update.message.reply_text(
                        piece, parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    log.exception("kisti chunked send failed")


async def cmd_kr_papers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/kr_papers <키워드> — KISTI ScienceON 논문 검색
    (SCIE/SCOPUS/KSCI 99%+ 커버, 한국 학술 전문)."""
    if not _is_owner(update):
        return
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("사용법: /kr_papers <검색어>")
        return
    from .agent import kisti_scienceon as _kisti
    await _kisti_search_command(
        update, ctx, q, "paper", _kisti.search_papers,
    )


async def cmd_kr_patents(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/kr_patents <키워드> — KISTI ScienceON 특허 검색
    (KIPRIS applicant 검색의 키워드 부족분 보완)."""
    if not _is_owner(update):
        return
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("사용법: /kr_patents <검색어>")
        return
    from .agent import kisti_scienceon as _kisti
    await _kisti_search_command(
        update, ctx, q, "patent", _kisti.search_patents,
    )


async def cmd_kr_reports(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/kr_reports <키워드> — KISTI ScienceON R&D 보고서 검색."""
    if not _is_owner(update):
        return
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("사용법: /kr_reports <검색어>")
        return
    from .agent import kisti_scienceon as _kisti
    await _kisti_search_command(
        update, ctx, q, "report", _kisti.search_reports,
    )


async def cmd_kr_trends(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/kr_trends <키워드> — KISTI ScienceON 해외과학기술동향 (ATT)."""
    if not _is_owner(update):
        return
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("사용법: /kr_trends <검색어>")
        return
    from .agent import kisti_scienceon as _kisti
    await _kisti_search_command(
        update, ctx, q, "trend", _kisti.search_trends,
    )


async def cmd_kr_researcher(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/kr_researcher <이름|연구분야> — KISTI ScienceON 식별 연구자
    인덱스 (RESEARCHER). 국내 연구자 프로필 + 그 연구자의 논문/보고서/
    특허 목록 링크."""
    if not _is_owner(update):
        return
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("사용법: /kr_researcher <이름 또는 키워드>")
        return
    from .agent import kisti_scienceon as _kisti
    await _kisti_search_command(
        update, ctx, q, "researcher", _kisti.search_researchers,
    )


async def cmd_kr_organ(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/kr_organ <기관명> — KISTI ScienceON 식별 연구기관 인덱스 (ORGAN).
    기관 프로필 + 그 기관의 publications. 회사 분석 시 KIPRIS 출원인
    검색과 조합하면 더 풍부."""
    if not _is_owner(update):
        return
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("사용법: /kr_organ <기관명>")
        return
    from .agent import kisti_scienceon as _kisti
    await _kisti_search_command(
        update, ctx, q, "organ", _kisti.search_organs,
    )


async def cmd_kr_science_trend(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/kr_science_trend <키워드> — KISTI ScienceON Trend (TREND).
    큐레이션 토픽 트렌드 리포트 (논문/특허 통계 + 전문가 해설)."""
    if not _is_owner(update):
        return
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("사용법: /kr_science_trend <검색어>")
        return
    from .agent import kisti_scienceon as _kisti
    await _kisti_search_command(
        update, ctx, q, "science_trend", _kisti.search_science_trends,
    )


def _dedup_ntis_projects(rows: list[dict]) -> list[dict]:
    """Group multi-year NTIS project rows by (ProjectTitle,
    ResearchLeader, ResearchAgency) and collapse to the latest year.

    NTIS returns each annual phase of a multi-year project as a
    separate row. For "양자컴퓨팅" a single project (e.g. 광자 기반
    범용 양자컴퓨팅 프로세서 개발) can occupy 3-5 of the top 10
    results, drowning out variety. This dedup keeps the LATEST
    year's row + attaches a phases summary (years list +
    other-pjt-ids) so info isn't lost — the formatter renders it
    as a "📅 다년 과제 — N개 연차 통합 (2026, 2025, 2024)" line.

    Preserves original relevance order (uses first-occurrence
    position as the sort key for surviving rows)."""
    from collections import defaultdict
    groups: dict[tuple, list[dict]] = defaultdict(list)
    first_seen: dict[tuple, int] = {}
    for i, r in enumerate(rows):
        title = (r.get("ProjectTitle") or "").strip()
        if not title:
            # No title → keep as-is, don't group
            key = ("_no_dedup_", id(r), 0)
        else:
            key = (
                title,
                (r.get("ResearchLeader") or "").strip(),
                (r.get("ResearchAgency") or "").strip(),
            )
        groups[key].append(r)
        if key not in first_seen:
            first_seen[key] = i
    out: list[dict] = []
    for key, group in groups.items():
        # Sort group by year DESC, keep latest as the surviving row
        def _year_int(r: dict) -> int:
            y = r.get("ResearchYear") or "0"
            try:
                return int(str(y)[:4])
            except (ValueError, TypeError):
                return 0
        group_sorted = sorted(group, key=_year_int, reverse=True)
        latest = dict(group_sorted[0])  # copy so input not mutated
        if len(group_sorted) > 1:
            years_seen: list[str] = []
            other_ids: list[str] = []
            for r in group_sorted:
                y = r.get("ResearchYear")
                if y and str(y) not in years_seen:
                    years_seen.append(str(y))
            for r in group_sorted[1:]:
                pid = r.get("ProjectNumber")
                if pid:
                    other_ids.append(pid)
            latest["_phases_count"] = len(group_sorted)
            latest["_phases_years"] = years_seen
            latest["_phases_other_ids"] = other_ids
        out.append(latest)
    # Restore relevance order based on each group's first occurrence
    out.sort(key=lambda r: first_seen.get((
        (r.get("ProjectTitle") or "").strip(),
        (r.get("ResearchLeader") or "").strip(),
        (r.get("ResearchAgency") or "").strip(),
    ), 9999) if r.get("ProjectTitle") else 9999)
    return out


def _format_ntis_projects(query: str, rows: list[dict]) -> str:
    """NTIS public_project rows. Dedups multi-year projects (same
    title + leader + agency) into a single row with 다년 과제 badge.
    The new schema (2026-05+) exposes ProjectNumber / ProjectTitle /
    ResearchLeader (Manager) / ResearchAgency (OrderAgency) /
    Researchers (count) / Abstract / Goal / Effect / Keyword. The
    legacy schema's Korean tag names (과제명·과제번호·수행기관·
    연구책임자·연구기간·연구비) are kept as fallback lookups so older
    endpoints / cached rows still render."""
    import html as _html
    if not rows:
        return (
            f"🔍 '<b>{_html.escape(query)}</b>' NTIS 국가R&D 결과 없음.\n"
            f"(NTIS_API_KEY 미설정 시에도 동일 메시지 — ntis.go.kr 활용신청 필요.)"
        )
    raw_count = len(rows)
    rows = _dedup_ntis_projects(rows)
    dedup_count = len(rows)
    collapsed = raw_count - dedup_count
    head_meta = f"<i>{dedup_count}건"
    if collapsed > 0:
        head_meta += f" (원본 {raw_count}건에서 다년 과제 {collapsed}개 통합)"
    head_meta += " · NTIS</i>"
    out = [
        f"🔬 <b>NTIS 국가R&D 과제 검색 결과 — '{_html.escape(query)}'</b>",
        head_meta,
    ]
    for i, r in enumerate(rows, 1):
        title = _html.escape(
            (r.get("Title_ko")
             or r.get("ProjectTitle") or r.get("과제명")
             or r.get("title") or "(제목 없음)")
        )[:240]
        pjt_id = (r.get("ProjectNumber") or r.get("과제번호")
                  or r.get("pjtId") or "").strip()
        agency = (r.get("ResearchAgency") or r.get("수행기관")
                  or r.get("agency") or "").strip()
        leader = (r.get("ResearchLeader") or r.get("연구책임자")
                  or r.get("leader") or "").strip()
        researchers = (r.get("Researchers") or "").strip()
        period = (r.get("ResearchPeriod") or r.get("연구기간")
                  or r.get("period") or "").strip()
        budget = (r.get("ResearchExpenses") or r.get("연구비")
                  or r.get("budget") or "").strip()
        year = (r.get("ResearchYear") or "").strip()
        abstract = (r.get("Abstract") or "").strip()
        goal = (r.get("Goal") or "").strip()
        effect = (r.get("Effect") or "").strip()
        keyword = (r.get("Keyword") or "").strip()
        parts: list[str] = []
        if pjt_id:
            parts.append(f"과제번호 {_html.escape(pjt_id)}")
        if agency:
            parts.append(f"관리기관 {_html.escape(agency[:60])}")
        if leader:
            parts.append(f"책임자 {_html.escape(leader[:40])}")
        if researchers:
            parts.append(f"연구원 {_html.escape(researchers[:30])}")
        if year:
            parts.append(f"연도 {_html.escape(year)}")
        if period:
            parts.append(f"기간 {_html.escape(period[:40])}")
        if budget:
            parts.append(f"연구비 {_html.escape(budget[:30])}")
        # Multi-year project badge from _dedup_ntis_projects metadata.
        phases_count = r.get("_phases_count") or 0
        phases_years = r.get("_phases_years") or []
        block = [f"\n🔬 <b>{i}. {title}</b>"]
        if parts:
            block.append(f"  {' · '.join(parts[:6])}")
        if phases_count > 1 and phases_years:
            years_str = ", ".join(str(y) for y in phases_years[:6])
            block.append(
                f"  📅 다년 과제 — {phases_count}개 연차 통합 "
                f"({years_str})"
            )
        if keyword:
            block.append(f"  🏷️ {_html.escape(keyword[:120])}")
        if goal:
            block.append(f"  🎯 {_html.escape(_truncate_at_sentence(goal, 400))}")
        if abstract:
            block.append(f"  📝 {_html.escape(_truncate_at_sentence(abstract, 600))}")
        if effect:
            block.append(f"  💡 {_html.escape(_truncate_at_sentence(effect, 400))}")
        out.append("\n".join(block))
    return "\n".join(out)


async def cmd_kr_rnd_projects(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/kr_rnd_projects <키워드> — NTIS 국가R&D 과제 검색."""
    if not _is_owner(update):
        return
    q = " ".join(ctx.args).strip()
    if not q:
        await update.message.reply_text("사용법: /kr_rnd_projects <검색어>")
        return
    async with _SustainedTyping(update, ctx):
        status = await update.message.reply_text(
            f"🔍 '{q}' NTIS 국가R&D 검색 중..."
        )
        try:
            from .agent import kisti_ntis as _ntis
            rows = await _ntis.search_projects(q, limit=30)
        except Exception as e:
            log.exception("ntis projects failed for %r", q)
            try:
                await ctx.bot.edit_message_text(
                    chat_id=status.chat.id, message_id=status.message_id,
                    text=f"⚠️ NTIS 검색 실패: {_explain_error(e)}",
                )
            except Exception:
                pass
            return
        from .agent.translate import translate_kisti_rows
        await translate_kisti_rows(rows)
        body = _format_ntis_projects(q, rows)
        _record_command_qna(
            update,
            question=(update.message.text or f"/kr_rnd_projects {q}").strip(),
            body=body,
            tools=["search_kr_rnd_projects"],
            sources=[r.get("ProjectNumber") or r.get("과제번호") or ""
                     for r in rows if r.get("ProjectNumber") or r.get("과제번호")],
        )
        pieces = _split_for_telegram(body)
        if pieces:
            try:
                await ctx.bot.edit_message_text(
                    chat_id=status.chat.id, message_id=status.message_id,
                    text=pieces[0], parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                try:
                    await update.message.reply_text(
                        pieces[0], parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    log.exception("ntis projects fallback send failed")
            for piece in pieces[1:]:
                try:
                    await update.message.reply_text(
                        piece, parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    log.exception("ntis projects chunked send failed")




def _format_ntis_related(query: str, rows: list[dict],
                          coll_type: str) -> str:
    """NTIS ConnectionContent 응답 — pjtId 기반 연관 컨텐츠 (논문/
    특허/보고서/관련과제). row shape 가 collection_type 마다 다를
    수 있어서 defensive: 핵심 필드 (title, agency, year, ...) 후보를
    여러 키 변형으로 시도."""
    import html as _html
    label = {
        "paper": "관련 논문", "patent": "관련 특허",
        "researchreport": "관련 보고서", "project": "관련 과제",
    }.get(coll_type, "연관 콘텐츠")
    if not rows:
        return (
            f"🔗 <b>NTIS {label}</b> — '{_html.escape(query)}'\n"
            f"<i>매칭 0건</i>"
        )
    out = [
        f"🔗 <b>NTIS {label} — pjtId {_html.escape(query)}</b>",
        f"<i>{len(rows)}건 · NTIS</i>",
    ]
    # Diagnostic: NTIS related_content responses have an undocumented
    # row shape that varies by collection_type. Dump the first row's
    # keys so the next call shows which fields the formatter should
    # actually read.
    try:
        log.info("ntis related %s first-row keys: %s",
                 coll_type, sorted(rows[0].keys()))
    except Exception:
        pass
    for i, r in enumerate(rows, 1):
        title = (r.get("Title_ko") or
                 r.get("title") or r.get("Title") or
                 r.get("ProjectTitle") or r.get("논문명") or
                 r.get("특허명") or r.get("보고서명") or
                 r.get("KOR_RPT_TITLE_NM") or r.get("KOR_TITLE_NM") or
                 r.get("ENG_RPT_TITLE_NM") or r.get("ENG_TITLE_NM") or
                 "(제목 없음)")
        block = [f"\n🔗 <b>{i}. {_html.escape(str(title)[:200])}</b>"]
        # Common metadata fields
        for k, lbl in [
            ("ProjectNumber", "과제번호"), ("PaperID", "논문ID"),
            ("PatentNumber", "특허번호"), ("ReportNumber", "보고서번호"),
            ("Agency", "기관"), ("agency", "기관"),
            ("Author", "저자"), ("author", "저자"),
            ("Year", "연도"), ("year", "연도"),
            ("Date", "일자"),
            # NTIS ConnectionContent keys (researchreport collection)
            ("RST_ID", "결과ID"), ("creat_dt", "생성일"),
            ("rank", "순위"), ("similarity_score", "유사도"),
        ]:
            v = r.get(k)
            if v:
                block.append(f"  {lbl} {_html.escape(str(v)[:80])}")
        out.append("\n".join(block))
    return "\n".join(out)


async def cmd_kr_related(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/kr_related <pjt_id> [paper|patent|researchreport|project]
    — NTIS 연관 콘텐츠 (ConnectionContent). 과제번호로 관련 논문/
    특허/보고서/연관과제 추천. 기본 type=researchreport.

    예:
      /kr_related 1234567890
      /kr_related 1234567890 paper
      /kr_related 1234567890 patent
    """
    if not _is_owner(update):
        return
    args = list(ctx.args or [])
    if not args:
        await update.message.reply_text(
            "사용법: /kr_related <pjt_id> [paper|patent|researchreport|project]\n"
            "예: /kr_related 1234567890 paper"
        )
        return
    pjt_id = args[0].strip()
    coll = "researchreport"
    if len(args) >= 2 and args[1].lower() in (
            "paper", "patent", "researchreport", "project"):
        coll = args[1].lower()
    async with _SustainedTyping(update, ctx):
        status = await update.message.reply_text(
            f"🔗 NTIS 연관 콘텐츠 ({coll}) 조회 중 — pjtId {pjt_id}..."
        )
        try:
            from .agent import kisti_ntis as _ntis
            rows = await _ntis.related_content(
                pjt_id, collection_type=coll,
            )
        except Exception as e:
            log.exception("ntis related failed")
            await _edit_or_send(
                ctx, status.chat.id, status.message_id,
                f"⚠️ NTIS 연관 콘텐츠 실패: {_explain_error(e)}",
            )
            return
        from .agent.translate import translate_kisti_rows
        await translate_kisti_rows(rows)
        body = _format_ntis_related(pjt_id, rows, coll)
        _record_command_qna(
            update,
            question=(update.message.text or
                      f"/kr_related {pjt_id} {coll}").strip(),
            body=body,
            tools=["get_kr_related_content"],
        )
        pieces = _split_for_telegram(body)
        if pieces:
            try:
                await ctx.bot.edit_message_text(
                    chat_id=status.chat.id, message_id=status.message_id,
                    text=pieces[0], parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                try:
                    await update.message.reply_text(
                        pieces[0], parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    log.exception("ntis related fallback failed")
            for piece in pieces[1:]:
                try:
                    await update.message.reply_text(
                        piece, parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    log.exception("ntis related chunked send failed")


def _html_escape_safe(s: str) -> str:
    import html as _h
    return _h.escape(s)


async def _ntis_simple_search_command(
    update, ctx, query: str, label: str, emoji: str,
    fn, tool_name: str,
) -> None:
    """Shared body for the four 2026-05 NTIS '전체용' commands
    (outcomes / reports / agency / issues). Each takes one search
    function from kisti_ntis and renders results via the existing
    _format_ntis_projects formatter — NTIS uses the same <HIT>
    schema across these services so a single formatter covers them
    until per-service field cleanup is needed. Pre-built 2026-05-20
    alongside the user's NTIS 활용신청; calls degrade to empty rows
    until approval comes in."""
    if not _is_owner(update):
        return
    if not query:
        return
    async with _SustainedTyping(update, ctx):
        status = await update.message.reply_text(
            f"{emoji} NTIS {label} 검색 중 — '{query}'..."
        )
        try:
            rows = await fn(query, limit=30)
        except Exception as e:
            log.exception("ntis %s search failed", label)
            await _edit_or_send(
                ctx, status.chat.id, status.message_id,
                f"⚠️ NTIS {label} 검색 실패: {_explain_error(e)}",
            )
            return
        if not rows:
            body = (
                f"🔍 '<b>{_html_escape_safe(query)}</b>' NTIS {label} "
                f"결과 없음.\n활용신청 승인 전이거나 매칭 0건 — 승인 메일 "
                f"받은 뒤 재시도 / 더 일반적인 키워드로 시도."
            )
        else:
            from .agent.translate import translate_kisti_rows
            await translate_kisti_rows(rows)
            body = _format_ntis_projects(query, rows)
            # Header label swap so the user sees the right section name.
            body = body.replace(
                "🔬 <b>NTIS 국가R&amp;D 과제",
                f"{emoji} <b>NTIS {label}",
            ).replace(
                "🔬 <b>NTIS 국가R&D 과제",
                f"{emoji} <b>NTIS {label}",
            )
        _record_command_qna(
            update, question=(update.message.text or
                              f"/{tool_name} {query}").strip(),
            body=body, tools=[tool_name],
        )
        pieces = _split_for_telegram(body)
        if not pieces:
            return
        try:
            await ctx.bot.edit_message_text(
                chat_id=status.chat.id, message_id=status.message_id,
                text=pieces[0], parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            try:
                await update.message.reply_text(
                    pieces[0], parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                log.exception("ntis %s fallback failed", label)
        for piece in pieces[1:]:
            try:
                await update.message.reply_text(
                    piece, parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                log.exception("ntis %s chunked send failed", label)


async def cmd_kr_outcomes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/kr_outcomes [paper|patent|equipment] <query> — NTIS 국가R&D
    성과검색 (4번, 전체용). 정부R&D 과제에서 산출된 논문 / 특허 /
    연구시설장비 메타. 기본 kind=paper. 활용신청 승인 후 동작."""
    if not _is_owner(update):
        return
    args = list(ctx.args or [])
    if not args:
        await update.message.reply_text(
            "사용법: /kr_outcomes [paper|patent|equipment] <검색어>\n"
            "예: /kr_outcomes paper 양자컴퓨터\n"
            "    /kr_outcomes patent 반도체"
        )
        return
    kind = "paper"
    if args[0].lower() in ("paper", "patent", "equipment"):
        kind = args[0].lower()
        query = " ".join(args[1:]).strip()
    else:
        query = " ".join(args).strip()
    if not query:
        await update.message.reply_text(
            "사용법: /kr_outcomes [paper|patent|equipment] <검색어>"
        )
        return
    from .agent import kisti_ntis as _ntis

    async def _fn(q: str, limit: int = 30):
        return await _ntis.search_outcomes(q, kind=kind, limit=limit)
    label = {"paper": "성과 논문", "patent": "성과 특허",
             "equipment": "성과 시설장비"}.get(kind, "성과")
    emoji = {"paper": "📄", "patent": "⚖️",
             "equipment": "🔬"}.get(kind, "🎯")
    await _ntis_simple_search_command(
        update, ctx, query, label, emoji, _fn, "search_kr_rnd_outcomes",
    )


async def cmd_kr_govt_reports(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/kr_govt_reports <query> — NTIS 국가R&D 연구보고서 검색
    (7번, 전체용). ScienceON REPORT 와 보완 (NTIS 가 예산/과제번호/
    주관기관 등 행정 메타 더 정확). 활용신청 승인 후 동작."""
    if not _is_owner(update):
        return
    query = " ".join(ctx.args or []).strip()
    if not query:
        await update.message.reply_text("사용법: /kr_govt_reports <검색어>")
        return
    from .agent import kisti_ntis as _ntis
    await _ntis_simple_search_command(
        update, ctx, query, "정부R&D 연구보고서", "📑",
        _ntis.search_research_reports, "search_kr_govt_reports",
    )


async def cmd_kr_agency_rnd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/kr_agency_rnd <기관명> — NTIS 수행기관 R&D현황 (6번, 전체용).
    기관별 정부R&D 과제 수 / 예산 / 논문 통계. 회사 IR / 출연(연)
    비교 등에 유용. 활용신청 승인 후 동작."""
    if not _is_owner(update):
        return
    query = " ".join(ctx.args or []).strip()
    if not query:
        await update.message.reply_text(
            "사용법: /kr_agency_rnd <기관명>\n"
            "예: /kr_agency_rnd KAIST"
        )
        return
    from .agent import kisti_ntis as _ntis
    await _ntis_simple_search_command(
        update, ctx, query, "수행기관 R&D현황", "🏛️",
        _ntis.search_agency_rnd, "search_kr_agency_rnd",
    )


async def cmd_kr_rnd_issues(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/kr_rnd_issues <토픽> — NTIS 이슈로보는 R&D (9번, 전체용).
    최신 과학기술 이슈 + 관련 국가R&D 현황 / 키워드 / 트렌드.
    ScienceON TREND 와 비슷하지만 정부R&D 한정. 활용신청 승인 후 동작."""
    if not _is_owner(update):
        return
    query = " ".join(ctx.args or []).strip()
    if not query:
        await update.message.reply_text(
            "사용법: /kr_rnd_issues <토픽>\n"
            "예: /kr_rnd_issues 양자컴퓨터"
        )
        return
    from .agent import kisti_ntis as _ntis
    await _ntis_simple_search_command(
        update, ctx, query, "R&D 이슈/트렌드", "📈",
        _ntis.search_rnd_issues, "search_kr_rnd_issues",
    )


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
    # Dedicated study-notes channel → route to the 체화 pipeline instead
    # of the brain ingest (keeps study material out of the wiki).
    if config.STUDY_CHANNEL_ID and str(msg.chat.id) == config.STUDY_CHANNEL_ID:
        from .notes import telegram as _notes_tg
        await _notes_tg.handle_study_post(msg, ctx)
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


# (F-2) numerical-sanity audit. Catches the obvious failures that the
# LLM verifier sometimes misses because it's looking at sources, not
# arithmetic: 매출 == OP (same hex stamped on both fields), OP > 매출
# (data flipped), 영업이익 마진 >70% (model picked an unrelated number),
# annual row labelled QoQ / quarterly row labelled YoY. All checks are
# regex + arithmetic — zero LLM cost, runs on every reply.
_F2_ROW_RE = re.compile(
    # `A. 2025  매출 258.9조 (...) | OP 6.6조 (...)` — captures the
    # A/F prefix, the period token (year or quarter), and the matching
    # 매출 + OP cells through end-of-line. Tolerant of optional units
    # (조/억) and arbitrary parenthetical annotation inside each cell.
    r"^[ \t]*([AF])\.\s+([A-Za-z0-9]+)\s+"
    r"매출\s+(.+?)\s*\|\s*"
    r"OP\s+(.+?)\s*$",
    re.MULTILINE,
)
_F2_FIRST_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _audit_f2_numbers(body: str) -> list[str]:
    """Scan the (F-2) 실적 데이터 tables and return user-facing
    warnings for obvious anomalies. Empty list ⇒ tables look sane.
    Free, deterministic — runs on every agent reply. Anomalies the
    LLM has historically slipped through:
      • 매출 == OP — model duplicated one number across both fields
        (e.g. SamsungElec 2028 매출 495 / OP 495 = 시총 숫자 오매핑)
      • OP > 매출 — data flipped
      • OP/매출 > 70% — implausible margin (fintech/holdings excepted)
      • 연간 row carries QoQ tag (or quarterly row carries YoY)
    """
    warnings: list[str] = []
    seen: set[str] = set()

    def _add(msg: str) -> None:
        if msg not in seen:
            seen.add(msg)
            warnings.append(msg)

    def _first_num(cell: str) -> float | None:
        cell = cell.strip()
        if not cell or cell.startswith(("—", "-")):
            return None
        m = _F2_FIRST_NUM_RE.search(cell)
        if not m:
            return None
        try:
            return float(m.group(1))
        except ValueError:
            return None

    for m in _F2_ROW_RE.finditer(body):
        af, period, rev_cell, op_cell = m.groups()
        rev = _first_num(rev_cell)
        op = _first_num(op_cell)
        is_quarter = "Q" in period.upper()

        if rev is not None and op is not None and rev > 0.1:
            if rev == op:
                _add(
                    f"{af}. {period}: 매출과 OP가 동일 ({rev}) — "
                    "데이터 추출 오류 가능성 (같은 숫자 두 번 매핑)"
                )
            elif op > rev > 0.01:
                _add(
                    f"{af}. {period}: OP {op} > 매출 {rev} — "
                    "데이터 오독 (OP는 매출의 일부여야 함)"
                )
            else:
                margin = op / rev
                # 70%+ margin is implausible for ops we typically
                # cover (반도체/IT/금융 holdings 같은 outlier 빼면).
                if margin > 0.7:
                    _add(
                        f"{af}. {period}: OP 마진 {margin * 100:.0f}% — "
                        "비현실적 (대부분 산업 <30%)"
                    )

        combined = rev_cell + " " + op_cell
        if not is_quarter and "QoQ" in combined:
            _add(f"{af}. {period}: 연간 row에 QoQ 표기 — YoY 여야 함")
        if is_quarter and "YoY" in combined:
            _add(f"{af}. {period}: 분기 row에 YoY 표기 — QoQ 여야 함")

    return warnings


async def _send_agent_reply(send, result, send_photo=None, inherited: bool = False):
    # `inherited` is retained for the historical call-site shape but
    # is now always False — the inheritance fallback was removed
    # because it masked routing failures (model skipped brain search,
    # made up citations, then we stamped unrelated old sources).
    #
    # Mermaid handling:
    #   • Each ```mermaid``` block is replaced with a sentinel token
    #     before post-processing so the text-only steps (renumber
    #     citations, annotate learn date, strip markdown, format HTML)
    #     never touch raw mermaid syntax — xychart-beta's `[2024, 2025]`
    #     x-axis would otherwise collide with the citation regex.
    #   • When `send_photo` is provided we walk the post-processed body
    #     in segment order: text segment → HTML-format & send; mermaid
    #     segment → render & send photo. The chart appears at the
    #     position the model placed it (inline near its referencing
    #     section), instead of always at the very end.
    #   • Without `send_photo` we fall back to the legacy shape:
    #     return (body, mermaid_blocks) and the caller renders photos
    #     itself after the body. Kept for call sites that haven't been
    #     refactored to provide a photo callable.
    raw = result["text"]
    blocks: list[str] = []

    def _stash(m: "re.Match[str]") -> str:
        idx = len(blocks)
        blocks.append(m.group(1).strip())
        return f"__MERMAID_BLOCK_{idx}__"

    text_only = _MERMAID_BLOCK_RE.sub(_stash, raw)
    body = _strip_markdown(text_only)
    body, ordered_labels = _renumber_citations(
        body, result.get("sources") or [],
    )
    # Off-loop: up to 20 sequential unindexed-LIKE scans over 32.5k rows.
    body = await asyncio.to_thread(
        _annotate_learn_date, body, result.get("sources") or [])
    suffix_lines = []
    if result.get("warning"):
        suffix_lines.append(result["warning"])
    source_urls = result.get("source_urls") or {}
    if ordered_labels:
        suffix_lines.append("📚 출처:" + _format_numbered_sources(
            ordered_labels, source_urls,
        ))
    elif result.get("sources"):
        suffix_lines.append("📚 출처:" + _format_sources_with_url(
            result["sources"], source_urls=source_urls,
        ))
    audit = _audit_f2_numbers(body)
    if audit:
        suffix_lines.append(
            "⚠️ 숫자 검증 경고 (자동 감지):\n"
            + "\n".join(f"  • {a}" for a in audit)
        )
    if result.get("tool_calls"):
        suffix_lines.append(_format_tool_calls(result["tool_calls"]))

    if send_photo is not None and blocks:
        # Inline mode. Split on placeholder tokens, walk parts in
        # order. The split keeps the captured index group so we know
        # which mermaid block to render at each break.
        parts = re.split(r"__MERMAID_BLOCK_(\d+)__", body)
        # parts alternates: text, idx_str, text, idx_str, ..., text
        for i, part in enumerate(parts):
            if i % 2 == 0:
                txt = part.strip()
                if txt:
                    await _send_chunked_html(send, _format_body_html(txt))
            else:
                try:
                    block_idx = int(part)
                except ValueError:
                    continue
                if 0 <= block_idx < len(blocks):
                    code = blocks[block_idx]
                    try:
                        png = await _render_mermaid_png(code)
                        await send_photo(png)
                    except Exception as e:
                        log.warning("inline mermaid render failed: %s", e)
                        try:
                            await send(
                                f"(다이어그램 렌더 실패: "
                                f"{_explain_error(e)})"
                            )
                        except Exception:
                            pass
        if suffix_lines:
            await _send_chunked(send, "\n".join(suffix_lines))
        # Returned blocks list is empty so legacy callers don't
        # double-render the photos we already sent inline.
        body_no_ph = re.sub(r"__MERMAID_BLOCK_\d+__", "", body).strip()
        return body_no_ph, []

    # Legacy mode — drop placeholders from body, return the mermaid
    # list so the caller can render photos after the text send.
    body_no_ph = re.sub(r"__MERMAID_BLOCK_\d+__", "", body).strip()
    body_html = _format_body_html(body_no_ph)
    await _send_chunked_html(send, body_html)
    if suffix_lines:
        await _send_chunked(send, "\n".join(suffix_lines))
    return body_no_ph, blocks


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
    _retry_prefix = {"sent": False}

    async def _send(text, **kw):
        # Prefix the very first text send with the "⏰ 재시도 성공"
        # header so subsequent chunks/photos don't repeat it.
        if not _retry_prefix["sent"]:
            text = f"⏰ 재시도 성공\n\n{text}"
            _retry_prefix["sent"] = True
        await ctx.bot.send_message(chat_id, text, **kw)

    async def _send_photo(png: bytes):
        await ctx.bot.send_photo(
            chat_id, photo=png, caption="🧩 다이어그램",
        )

    await _send_agent_reply(_send, result, send_photo=_send_photo)




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


# Hard ceiling on a single Q&A agent run. Without this a hung Gemini
# call (network stall, provider-side wedge) blocks the awaiting task
# indefinitely. Deep/Pro synthesis on a large compare is the slow path,
# so the bound is generous (10 min) but finite — matches the ingest
# pipeline's wait_for discipline.
_AGENT_TIMEOUT_SEC = 600


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
            result = await asyncio.wait_for(
                agent.run(text, deep=deep, history=history),
                timeout=_AGENT_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            log.warning("agent.run timeout (%ds) for chat %s",
                        _AGENT_TIMEOUT_SEC, chat_id)
            await update.message.reply_text(
                f"⚠️ 응답이 {_AGENT_TIMEOUT_SEC // 60}분을 넘겨 중단했어요. "
                "잠시 후 다시 시도해주세요."
            )
            _ACTIVE_AGENT_RUNS = max(0, _ACTIVE_AGENT_RUNS - 1)
            _LAST_REPLY_AT = datetime.utcnow()
            return
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
        async def _photo_inline(png: bytes):
            await message.reply_photo(photo=png, caption="🧩 다이어그램")

        body, _ = await _send_agent_reply(
            message.reply_text, result, send_photo=_photo_inline,
            inherited=False,
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
            await asyncio.to_thread(dashboard_regen.regenerate)
        except Exception:
            log.exception("dashboard regen failed")
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


def _short_link_label(url: str) -> str:
    """Compact host+tail label for a link button (Telegram caps button
    text; keep it readable)."""
    from urllib.parse import urlparse
    p = urlparse(url)
    host = p.netloc.replace("www.", "")
    tail = (p.path or "").rstrip("/").rsplit("/", 1)[-1][:24]
    return f"{host}/{tail}"[:36] if tail else host[:36]


_LINK_PREVIEW_SEM = asyncio.Semaphore(5)


async def _preview_links(links: list[dict]) -> list[dict]:
    """Enrich raw {url,anchor} link items with a cheap title/desc preview
    (bounded concurrency + short per-link timeout). Falls back to the
    anchor text when metadata fetch yields nothing. Returns items shaped
    for the pending store: {url,title,desc}."""
    from .ingest.loaders import fetch_link_preview

    async def _one(item: dict) -> dict:
        url = item.get("url", "")
        anchor = (item.get("anchor") or "").strip()
        async with _LINK_PREVIEW_SEM:
            try:
                p = await fetch_link_preview(url)
            except Exception:
                p = {"title": "", "desc": ""}
        title = (p.get("title") or anchor or "").strip()[:120]
        return {"url": url, "title": title,
                "desc": (p.get("desc") or "").strip()[:200]}

    return list(await asyncio.gather(*[_one(l) for l in links]))


def _link_prompt_keyboard(row_id: int, links: list[dict]) -> InlineKeyboardMarkup:
    """Build the inline keyboard for one pending_links row. Only
    PENDING links get individual buttons — done ones move into the
    body text (a compact ✅ list) so the keyboard physically shrinks
    after each tap. 전체 학습 / 선택 종료 footer is shown only while
    at least one link is still pending."""
    rows: list[list[InlineKeyboardButton]] = []
    for i, l in enumerate(links):
        if l.get("done"):
            continue  # done items appear in the body text, not the keyboard
        title = l.get("title") or _short_link_label(l["url"])
        rows.append([InlineKeyboardButton(
            f"📥 {i + 1}. {title[:30]}",
            callback_data=f"lnk:{row_id}:{i}",
        )])
    pending_n = sum(1 for l in links if not l.get("done"))
    if pending_n > 0:
        rows.append([
            InlineKeyboardButton("📥 전체 학습",
                                 callback_data=f"lnk:{row_id}:all"),
            InlineKeyboardButton("✅ 선택 종료",
                                 callback_data=f"lnk:{row_id}:skip"),
        ])
    return InlineKeyboardMarkup(rows)


def _link_prompt_body(parent_title: str, links: list[dict]) -> str:
    """Render the message body for a link prompt — a compact ✅ section
    listing already-learned links followed by a full 📥 section for the
    pending ones (title + desc + URL preview). After each tap the body
    is re-rendered so done links visually 'collapse' out of the pending
    detail block; the keyboard shrinks in sync."""
    done = [l for l in links if l.get("done")]
    pending = [(i, l) for i, l in enumerate(links) if not l.get("done")]
    total = len(links)
    lines: list[str] = []
    if done:
        lines.append(
            f"🔗 '{(parent_title or '글')[:50]}' 본문 링크 {total}개 "
            f"— ✅ 학습 {len(done)}건 · 📥 남음 {len(pending)}건"
        )
        lines.append("")
        lines.append("✅ 학습 완료:")
        for l in done:
            title = l.get("title") or _short_link_label(l["url"])
            lines.append(f"   • {title}")
        lines.append("")
    else:
        lines.append(
            f"🔗 '{(parent_title or '글')[:50]}' 본문 링크 {total}개 — 학습할 것 선택"
        )
        lines.append("(글 속 링크만 — 더 깊이는 안 들어감)")
        lines.append("")
    if pending:
        if done:
            lines.append(f"📥 남은 후보 ({len(pending)}):")
        for i, l in pending:
            title = l.get("title") or _short_link_label(l["url"])
            desc = l.get("desc") or ""
            lines.append(f"{i + 1}. {title}")
            if desc:
                lines.append(f"   ↳ {desc}")
            lines.append(f"   🔗 {l['url'][:90]}")
    return "\n".join(lines)


async def _send_one_link_prompt(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int,
                                row_id: int, parent_title: str,
                                links: list[dict]) -> None:
    """Render one persistent link-prompt row as a preview list + inline
    buttons keyed on the pending_links row id. Shared by the post-ingest
    prompt and /pending_links."""
    if not links:
        return
    await ctx.bot.send_message(
        chat_id, _link_prompt_body(parent_title, links)[:4000],
        reply_markup=_link_prompt_keyboard(row_id, links),
        disable_web_page_preview=True,
    )


async def _send_link_prompts(ctx: ContextTypes.DEFAULT_TYPE,
                             chat_id: int, results: list[dict]) -> None:
    """For each freshly-learned URL whose body carried author links,
    persist a pending_links row (so a missed prompt resurfaces via
    /pending) and show a preview list + inline buttons. Depth 1: chosen
    links are ingested but NOT re-scanned for further links."""
    for r in results:
        links = r.get("found_links") or []
        # Drop links the user previously marked as permanently ignored
        # — via "✅ 선택 종료" with them unselected, /failed_clear on
        # the URL, or per-item drop. The whole point of "permanently
        # ignored" is "don't ask me about this again."
        links = [l for l in links if l.get("url") not in _IGNORED_URLS]
        if not links:
            continue
        enriched = await _preview_links(links)
        row_id = await asyncio.to_thread(
            pending_store.add_links,
            chat_id=chat_id,
            parent_title=str(r.get("title") or "")[:200],
            parent_url=str(r.get("source") or "")[:300],
            links=enriched,
        )
        if row_id is None:
            log.warning("pending.add_links returned None — skipping link prompt")
            continue
        await _send_one_link_prompt(
            ctx, chat_id, row_id, str(r.get("title") or ""), enriched)


async def on_link_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """In-post link prompt handler (callback_data 'lnk:<row_id>:<sel>').
    sel is a link index, 'all', 'skip', or 'done' (no-op for already-
    learned buttons). Chosen links go through the resume-safe
    _ingest_one_url with scan_links=False (depth-1 cap). The keyboard
    is updated in place after each tap (✅/📥 reflect state) so the
    user can keep tapping more links without losing the prompt — the
    old behaviour replaced the prompt with "학습 중…" which (a) killed
    the keyboard so only the first tap landed, and (b) never updated
    once ingest finished, leaving a stale message on screen."""
    q = update.callback_query
    await q.answer()
    try:
        _, sid, sel = q.data.split(":", 2)
        row_id = int(sid)
    except (ValueError, AttributeError):
        return
    if sel == "done":
        return  # tap on an already-✅ link — silent no-op
    rec = await asyncio.to_thread(pending_store.get_links, row_id)
    if rec is None:
        try:
            await q.edit_message_text("⌛ 이미 처리됐거나 사라진 링크 목록이야.")
        except Exception:
            pass
        return
    links: list[dict] = rec["links"]
    chat_id = rec["chat_id"]
    if sel == "skip":
        # User said "선택 종료" → treat unselected (pending) links as
        # explicitly unwanted and add them to _IGNORED_URLS so they
        # don't resurface the next time the same parent article is
        # forwarded. Already-done links stay learned (they're already
        # in meta), this only marks the SKIPPED ones as permanent
        # ignore. Matches the "필요없다는 얘기니까" intent.
        pending_urls = [l.get("url") for l in links
                        if not l.get("done") and l.get("url")]
        added = 0
        for u in pending_urls:
            if u not in _IGNORED_URLS:
                _IGNORED_URLS.add(u)
                added += 1
        if added:
            _persist_permanently_ignored()
        await asyncio.to_thread(pending_store.delete_links, row_id)
        if added:
            await q.edit_message_text(
                f"✅ 선택 종료 — 학습한 링크는 유지. "
                f"안 고른 {added}개는 영구 무시 등록 (다음번 같은 글에서 안 보임)."
            )
        else:
            await q.edit_message_text(
                "✅ 선택 종료 — 학습한 링크는 유지, 이 목록은 닫음."
            )
        return
    if sel == "all":
        targets = [i for i, l in enumerate(links) if not l.get("done")]
    else:
        try:
            idx = int(sel)
        except ValueError:
            return
        if idx < 0 or idx >= len(links) or links[idx].get("done"):
            return  # already learned / out of range — q.answer() above sufficed
        targets = [idx]
    if not targets:
        await asyncio.to_thread(pending_store.delete_links, row_id)
        await q.edit_message_text("✅ 이 목록의 링크는 모두 처리했어.")
        return
    # "전체 학습" with N>=2 used to feel broken because the loop below
    # ran ingest sequentially — N links × ~1-5 min each = N minutes of
    # apparent silence with no progress signal. Send a start bubble so
    # the tap clearly registers, and run the ingests in a 4-wide gather
    # below (still capped by _INGEST_SEM globally) so the whole batch
    # finishes in roughly one URL's time instead of N URLs'.
    if sel == "all" and len(targets) >= 2:
        try:
            await ctx.bot.send_message(
                chat_id,
                f"⏳ 전체 학습 시작 — {len(targets)}개 URL 병렬 처리 중...",
                disable_notification=True,
            )
        except Exception:
            pass

    # Ingest the chosen targets in parallel. Don't replace the keyboard
    # with a "학습 중…" message — keeping it alive lets concurrent taps
    # target other links, and avoids leaving a stale "학습 중" once we
    # finish. 4-wide fan-out matches the URL loop in
    # _ingest_message_locked; the global _INGEST_SEM still bounds total
    # concurrent ingests across all messages.
    _LINK_FANOUT = 4
    _link_sem = asyncio.Semaphore(_LINK_FANOUT)
    async def _do_one_link(idx: int):
        async with _link_sem:
            return await _ingest_one_url(
                links[idx]["url"], chat_id, scan_links=False)
    results = await asyncio.gather(*[_do_one_link(i) for i in targets])
    out_lines: list[str] = []
    for idx, res in zip(targets, results):
        line = _format_results([res]).strip()
        out_lines.append(line or f"🚫 학습 안 함: {links[idx]['url'][:60]}")
    # Race-safe done marking: re-read the row from SQLite so a
    # concurrent tap's done flag isn't clobbered by our write.
    fresh_rec = await asyncio.to_thread(pending_store.get_links, row_id)
    if fresh_rec is not None:
        fresh_links = fresh_rec["links"]
        for idx in targets:
            if idx < len(fresh_links):
                fresh_links[idx]["done"] = True
        if all(l.get("done") for l in fresh_links):
            await asyncio.to_thread(pending_store.delete_links, row_id)
            try:
                await q.edit_message_text(
                    f"✅ 본문 링크 모두 학습 완료 ({len(fresh_links)}개)."
                )
            except Exception:
                pass
        else:
            await asyncio.to_thread(pending_store.set_links, row_id, fresh_links)
            try:
                # Re-render BOTH the body text and the keyboard so done
                # links collapse out of the pending detail block and the
                # keyboard physically shrinks — the user sees the prompt
                # visibly contract on each tap, confirming the action
                # landed without having to read a separate result bubble.
                parent_title = fresh_rec.get("parent_title") or ""
                await q.edit_message_text(
                    _link_prompt_body(parent_title, fresh_links)[:4000],
                    reply_markup=_link_prompt_keyboard(row_id, fresh_links),
                    disable_web_page_preview=True,
                )
            except Exception:
                # Edit can fail on very old messages or "not modified"
                # races; ignore — DB write above already persisted state.
                pass
    # Result message as a separate bubble so the prompt itself stays
    # interactive for further taps.
    body = "🔗 링크 학습 결과:\n" + "\n".join(out_lines)
    await ctx.bot.send_message(chat_id, body[:4000],
                               disable_web_page_preview=True)


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
    # Expired Pro-confirmation: surface the notice only. Don't log it as
    # a Q&A — query is empty and model='expired', which produced an
    # un-deletable empty-question junk row on the dashboard.
    if result.get("model") == "expired":
        try:
            await q.message.reply_text(
                result.get("text") or "⚠️ 확인 요청이 만료됐습니다.")
        finally:
            _ACTIVE_AGENT_RUNS = max(0, _ACTIVE_AGENT_RUNS - 1)
            _LAST_REPLY_AT = datetime.utcnow()
        return
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


_INGEST_TIMEOUT_SEC = 900  # 15 minutes per message. Real processing
# (Vision OCR ≤7 pages × ~3s, Flash-Lite summary ~5s, Gemini embed ~3s)
# finishes well under 5 min; the extra margin (was 10 min) is a safety
# buffer for large-batch bursts where event-loop contention can stretch
# an item's wall-clock. The real burst fix is keeping the loop free
# (Chroma/tiktoken offloaded to threads) — this just avoids killing a
# slow-but-progressing item. Downside: a truly stuck item pins its
# semaphore slot for 15 min instead of 10. _IN_FLIGHT_TIMEOUT and the
# retry-path wait_for both derive from this constant, so they scale
# together automatically.


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


def _retry_payload_from_msg(msg, chat_id: int) -> dict | None:
    """Rebuild the retry payload for a message's PRIMARY content so a
    timed-out ingest stays retryable. The per-kind branches in
    _ingest_message_locked build these, but the timeout is caught one
    level up (here) where those are out of scope — so mirror them.
    Returns None when nothing is retryable. Kept read-only / off the
    success path: only the timeout handler calls it."""
    if msg.document:
        d = msg.document
        return {"kind": "doc", "file_id": d.file_id,
                "file_unique_id": d.file_unique_id,
                "file_name": d.file_name, "chat_id": chat_id}
    if msg.photo:
        p = msg.photo[-1]
        return {"kind": "photo", "file_id": p.file_id,
                "file_unique_id": p.file_unique_id,
                "caption": msg.caption or "", "chat_id": chat_id}
    if msg.voice:
        v = msg.voice
        return {"kind": "voice", "file_id": v.file_id,
                "file_unique_id": v.file_unique_id,
                "mime_type": v.mime_type or "audio/ogg",
                "caption": msg.caption or "", "chat_id": chat_id}
    if msg.audio:
        a = msg.audio
        return {"kind": "audio", "file_id": a.file_id,
                "file_unique_id": a.file_unique_id,
                "file_name": a.file_name or "", "title": a.title or "",
                "mime_type": a.mime_type or "audio/mpeg",
                "caption": msg.caption or "", "chat_id": chat_id}
    urls, plain = _collect_message_urls(msg)
    if urls:
        return {"kind": "url", "url": urls[0], "chat_id": chat_id}
    if plain and len(plain) >= 80:
        return {"kind": "text", "text": plain,
                "label": f"tg-msg:{msg.message_id}", "chat_id": chat_id}
    return None


async def _ingest_message(msg, ctx: ContextTypes.DEFAULT_TYPE, notify_chat_id: int):
    """Cap concurrent ingests via semaphore + per-message timeout
    + per-message live status bubble.

    Up to 2 messages run in parallel via the semaphore; the rest
    wait. The 15-min timeout prevents one stuck PDF from hanging
    the whole bot. While work runs we keep editing a single status
    message instead of going silent, so the user sees the ingest
    is alive."""
    # Yield to any in-flight command / Q&A first (bounded) so a user's
    # question isn't stuck behind a just-forwarded document's ingest.
    await _await_interactive_idle()
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
            # Save a retry payload so a timed-out item shows the 🔁 #N
            # button in /failed (and works with 일괄 재시도) — previously
            # timeouts recorded no payload, so they were drop-only.
            _record_failure("timeout", label,
                            f"ingest exceeded {_INGEST_TIMEOUT_SEC}s",
                            retry_payload=_retry_payload_from_msg(
                                msg, notify_chat_id))
        finally:
            if updater_task:
                updater_task.cancel()
                try:
                    await updater_task
                except asyncio.CancelledError:
                    pass
            _unregister_ingest(job_id)
            # _untrack_bubble moved to AFTER the final edit/fallback
            # (below). If untrack ran here and SIGTERM hit between this
            # line and the edit, the bubble vanished from
            # active_bubbles.json (no startup sweep would find it) AND
            # never got its result text → frozen at "⏳ 학습 시작" forever.

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
            # Channel auto-forwards that are purely duplicates: suppress
            # the "♻️ 이미 있음" notification silently (delete the bubble).
            if (final_text.strip()
                    and notify_chat_id != msg.chat_id
                    and all(r.get("status") == "duplicate" for r in results)):
                final_text = ""
            if not final_text.strip():
                # All results were silently handled (blocked host /
                # skipped format) → _format_results returns "". Editing
                # the ⏳ bubble to an empty string fails (Telegram
                # rejects it), leaving it frozen at "학습 시작" forever —
                # which LOOKS stuck though the ingest already finished.
                # Resolve it in place (an edit, not a new send → no
                # flood) so the user sees why nothing was learned.
                sts = ", ".join(sorted({(r.get("status") or "?")
                                        for r in results}))
                det = next((r.get("detail") for r in results
                            if r.get("detail")), "")
                final_text = f"🚫 학습 안 함 ({sts}): {label[:70]}"
                if det:
                    final_text += f"\n   {det[:100]}"
        else:
            # Empty result set: the message carried nothing ingestable
            # (no URL, body under the 80-char text-ingest floor) — e.g.
            # a short alert blurb that got forwarded into the channel
            # ("💥 US 자동매매 크래시 / RuntimeError: boom"). Don't post a
            # "(빈 결과: …)" bubble for these — it's pure noise between
            # real ✅ learns. Quietly resolve the ⏳ status bubble (delete
            # if possible, else leave it) and skip the result send.
            final_text = ""

        sent_ok = False
        if not final_text.strip():
            # Nothing meaningful to show — remove the ⏳ "학습 시작"
            # bubble so it doesn't sit frozen, and send nothing new.
            if status_msg_id:
                try:
                    await ctx.bot.delete_message(
                        chat_id=notify_chat_id, message_id=status_msg_id)
                except Exception:
                    # Can't delete (too old / no rights) — edit to a
                    # minimal marker instead of leaving "학습 시작".
                    try:
                        await ctx.bot.edit_message_text(
                            chat_id=notify_chat_id, message_id=status_msg_id,
                            text="(학습할 내용 없음 — skip)",
                        )
                    except Exception:
                        pass
            sent_ok = True  # intentionally suppress the result send
        # A large forwarded digest's result text can exceed Telegram's
        # 4096 cap — both the edit AND the fallback send used to fail,
        # losing the result entirely. Edit gets the first chunk; any
        # remainder goes out as follow-up sends.
        final_chunks = _split_for_telegram(final_text) or [final_text]
        if status_msg_id and not sent_ok:
            try:
                await ctx.bot.edit_message_text(
                    chat_id=notify_chat_id, message_id=status_msg_id,
                    text=final_chunks[0], disable_web_page_preview=True,
                )
                for extra in final_chunks[1:]:
                    await ctx.bot.send_message(
                        notify_chat_id, extra,
                        disable_web_page_preview=True,
                    )
                sent_ok = True
            except Exception:
                # Edit can fail (too old, network) — fall back to new send.
                log.warning("status final edit failed; sending fresh")
        if not sent_ok:
            try:
                for chunk in final_chunks:
                    await ctx.bot.send_message(
                        notify_chat_id, chunk,
                        disable_web_page_preview=True,
                    )
            except Exception:
                log.exception("ingest result notify failed")
        # Bubble is now in its terminal visual state (or the user has
        # the result via a fresh send) — safe to release tracking. If
        # SIGTERM hits us *before* this line, the entry survives in
        # active_bubbles.json so the next-startup sweep flips it to
        # "⏸ 학습 중단됨" instead of leaving it frozen at "학습 시작".
        _untrack_bubble(notify_chat_id, status_msg_id)

        # OCR-extend prompts run after the final result is visible.
        if results:
            try:
                await _send_ocr_extend_prompts(ctx, notify_chat_id, results)
            except Exception:
                log.exception("ocr extend prompts failed")
            try:
                await _send_link_prompts(ctx, notify_chat_id, results)
            except Exception:
                log.exception("link prompts failed")

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
            await asyncio.to_thread(dest.read_bytes), label,
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
    content = await asyncio.to_thread(
        dest.read_text, encoding="utf-8", errors="ignore")
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


async def _ingest_one_url(url: str, notify_chat_id: int,
                          *, scan_links: bool = True) -> dict:
    """Resume-safe single-URL ingest shared by the main message path and
    the in-post link buttons. Honours the ignored / previously-failed
    skip lists, enqueues an in-flight marker BEFORE the pipeline call so
    a mid-process kill is recoverable, and mirrors the main path's
    status handling. scan_links=False strips found_links so a
    link-button ingest doesn't itself spawn another link prompt
    (depth-1 cap)."""
    if _is_ignored_url(url):
        log.info("url skip — permanently ignored: %s", url[:120])
        return {"status": "skipped", "title": url, "source": url,
                "detail": "영구 무시 URL (/failed_clear)"}
    if _url_in_failed_log(url):
        log.info("url skip — previously failed: %s", url[:120])
        return {"status": "skipped", "title": url, "source": url,
                "detail": "이전에 실패한 URL — 자동 skip"}
    url_retry = {"kind": "url", "url": url}
    url_item = _enqueue_with_inflight({**url_retry, "chat_id": notify_chat_id})
    try:
        r = await pipeline.ingest_url(url)
        r.setdefault("source", url)
        r.setdefault("title", url)
        if r.get("status") in ("empty", "error"):
            r["retry_payload"] = url_retry
        if not scan_links:
            r.pop("found_links", None)
        _finish_inflight(url_item, "done")
        return r
    except Exception as e:
        log.exception("url ingest failed: %s", url)
        if _is_retryable(e):
            _finish_inflight(url_item, "retry")
            return {"status": "queued", "title": url}
        _finish_inflight(url_item, "done")
        return {"status": "error", "error": _explain_error(e),
                "source": url, "retry_payload": url_retry}


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
    if urls:
        # URL loop is fanned out — a 9-URL Korean digest previously
        # waited ~6 min sequentially (each URL serialised behind the
        # previous one's full unshorten + fetch + summarise + embed).
        # 4-wide gather inside a single message gives ~4× speedup; the
        # outer _INGEST_SEM still bounds cross-message
        # concurrency, so total Gemini/network pressure hasn't changed.
        # gather preserves input order so the user sees URL results in
        # the order they appeared in the message.
        _URL_FANOUT = 4
        url_sem = asyncio.Semaphore(_URL_FANOUT)
        async def _do_url(u: str) -> dict:
            async with url_sem:
                # _ingest_one_url owns ignore / failed-log skips, the
                # in-flight enqueue (resume-safety), and status mapping.
                return await _ingest_one_url(u, notify_chat_id)
        url_results = await asyncio.gather(*[_do_url(u) for u in urls])
        results.extend(url_results)

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
        "ts": datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S"),
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
    silent: list[tuple[str, str, str]] = []  # (status, label, detail)
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
            # just log the skip and surface a one-line end-of-result
            # summary so a digest with 9 silent-skipped URLs doesn't
            # look like only the text part was processed.
            log.info("ingest %s: %s — %s",
                     s, r.get("title", "")[:80], r.get("detail", ""))
            label = r.get("source") or r.get("title") or ""
            silent.append((s, str(label)[:90],
                           str(r.get("detail") or "")[:80]))
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
    if silent:
        # Cap the per-line list so a 50-URL digest doesn't blow past
        # Telegram's message limit; remainder is summarised.
        if lines:
            lines.append("")
        lines.append(f"🔇 silent skip {len(silent)}건 "
                     "(차단 호스트 / 이전 실패 / 영구 무시 / 미지원 포맷):")
        for status, label, detail in silent[:10]:
            tag = "🚫" if status == "blocked" else "⏭"
            suffix = f" — {detail}" if detail else ""
            lines.append(f"   {tag} {label}{suffix}")
        if len(silent) > 10:
            lines.append(f"   ... 외 {len(silent) - 10}건")
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
    """Idle-only gc + malloc_trim every 3min. Skip when agent runs
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
    activity (no Q&As happening) still shows up in the totals.

    Offloaded to a worker thread: regenerate() does SQLite reads, a
    Chroma count over the full corpus, and HTML writes. Running it
    directly on the event loop froze the bot ~60s every tick (logs
    showed all scheduler jobs piling up with 'maximum number of
    running instances reached' + 'missed by 0:00:58'). to_thread keeps
    the loop free; regenerate()'s own non-blocking lock prevents
    overlapping runs."""
    try:
        from .dashboard import regenerate as dashboard_regen
        await asyncio.to_thread(dashboard_regen.regenerate)
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

        # Cookie-specific branch: if a burner-cookie file is configured AND
        # we recently hit the bot wall *despite* it, the cookies are
        # expired/banned → tell the user to refresh them (a totally
        # different action than the generic IP-block message).
        ck = (getattr(config, "YT_COOKIES_FILE", "") or "")
        cookies_present = bool(ck and os.path.exists(ck))
        cookie_dead = cookies_present and await asyncio.to_thread(
            yt_dlp_health.cookie_botwall_recent)

        if cookie_dead:
            stable_id = "yt_cookie_dead"
            msg = (
                "🍪 <b>YouTube 쿠키 만료/차단</b>\n"
                "버너 계정 쿠키로도 \"not a bot\" 벽에 막히고 있어 — 쿠키가 "
                "만료됐거나 그 계정이 차단된 거야. <b>쿠키를 갱신해줘.</b>\n\n"
                "1) 버너 계정으로 YouTube 로그인 → 새 시크릿 창에서 "
                "youtube.com 한 번 접속\n"
                "2) 'Get cookies.txt LOCALLY' 확장으로 Export → "
                "cookies.txt 다운로드 → 시크릿 창 바로 닫기\n"
                "3) VM에 덮어쓰기: <code>nano ~/Thesis/data/yt_cookies.txt</code> "
                "(전체 지우고 붙여넣기 → Ctrl+O, Enter, Ctrl+X)\n"
                "4) 계정까지 막혔으면 새 버너 계정으로 다시 추출\n\n"
                "그 사이 유튜브는 /failed 로 빠지고, 꼭 필요한 영상은 "
                "⋯→'스크립트 표시'로 자막 복사해 붙여넣으면 돼."
            )
        else:
            stable_id = "yt_dlp_health"
            summary = await asyncio.to_thread(yt_dlp_health.status_summary)
            rate_pct = int(summary["rate"] * 100)
            cookie_hint = (
                "\n\n💡 버너 쿠키가 설정돼 있는데도 막히면 위 '쿠키 갱신' "
                "안내대로 cookies.txt를 갱신해봐."
                if cookies_present else
                "\n\n💡 자동 우회를 원하면 버너 계정 쿠키를 "
                "<code>data/yt_cookies.txt</code>에 두면 돼."
            )
            msg = (
                f"⚠️ <b>yt-dlp 작동 이상</b>\n"
                f"최근 24시간 yt-dlp 실패율: <b>{rate_pct}%</b> "
                f"({summary['total']}회 시도 중)\n\n"
                f"<b>가장 흔한 원인 = YouTube의 데이터센터 IP 차단</b> "
                f"(\"Sign in to confirm you're not a bot\"). 이 VM은 GCP IP라 "
                f"YouTube가 봇으로 보고 막는 경우가 잦음. 이건 yt-dlp/Deno 문제가 "
                f"아니라 IP 문제라 <b>재배포해도 안 풀림</b>.\n\n"
                f"확인: <code>docker logs --tail 40 thesis-bot-1 | grep -i \"not a bot\\|cloud provider\"</code>\n"
                f"→ 위 문구가 보이면 IP 차단. 보통 수 시간~하루 뒤 자동 해제됨."
                f"{cookie_hint}\n\n"
                f"드물게 진짜 추출기 문제일 때: 아무 커밋이나 푸시하면 auto_pull "
                f"재빌드가 최신 yt-dlp nightly를 끌어옴(EJS+Deno). 위 grep에 IP "
                f"차단 문구가 <b>없을 때만</b> 효과 있음.\n\n"
                f"그 사이 YouTube는 transcript_api 가 가끔 성공할 때만 학습되고, "
                f"실패분은 /failed 로 빠짐(stub 안 만듦). 꼭 필요한 영상은 "
                f"⋯→'스크립트 표시'로 자막 복사해 봇에 붙여넣으면 IP 차단과 무관하게 학습됨."
            )

        # Shared ack/re-arm: suppress while acked, re-fire after the
        # re-arm window if the condition is still bad.
        existing = await asyncio.to_thread(notify_acks.get, stable_id)
        if existing and existing.get("acked"):
            ack_at = existing.get("ack_at") or ""
            try:
                ack_dt = datetime.fromisoformat(ack_at)
            except Exception:
                ack_dt = None
            if ack_dt and (datetime.utcnow() - ack_dt
                           < timedelta(days=_YT_DLP_RE_ARM_DAYS)):
                return
            await asyncio.to_thread(notify_acks.delete, stable_id)
            log.info("%s: re-arming alert (acked %s ago)", stable_id, ack_at)

        await _send_actionable_alert(ctx, stable_id, msg, parse_mode="HTML")
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
        async def _send(text, **kw):
            kw.setdefault("disable_web_page_preview", True)
            await ctx.bot.send_message(chat_id, text, **kw)

        async def _send_photo(png: bytes):
            await ctx.bot.send_photo(
                chat_id, photo=png, caption="🧩 다이어그램",
            )
        try:
            body, _ = await _send_agent_reply(
                _send, result, send_photo=_send_photo,
            )
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
            await asyncio.to_thread(dashboard_regen.regenerate)
        except Exception:
            log.exception("pending pro qna/dashboard record failed")
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
    # Update the /failed_retry progress message first — must run even
    # when the queue is empty so the final "✅ 완료" still renders.
    try:
        await _refresh_retry_progress(ctx)
    except Exception:
        log.exception("retry progress refresh failed")
    if not _INGEST_RETRY_QUEUE:
        return
    # Interactive priority: skip this drain tick while a user command /
    # Q&A is running so the chat gets full Gemini headroom. But cap how
    # long the queue can be starved — after _RETRY_BUSY_SKIP_GRACE
    # consecutive busy skips (~60 s) force a single-slot drain so a
    # steadily-chatting user can't keep the queue parked forever.
    global _RETRY_BUSY_SKIP_COUNT
    if _interactive_busy():
        _RETRY_BUSY_SKIP_COUNT += 1
        if _RETRY_BUSY_SKIP_COUNT < _RETRY_BUSY_SKIP_GRACE:
            return
        log.info(
            "retry tick: %d consecutive busy skips — force-draining "
            "1 slot to prevent starvation",
            _RETRY_BUSY_SKIP_COUNT,
        )
        _RETRY_BUSY_SKIP_COUNT = 0
        free_slots = _INGEST_SEM._value
        n = min(1, free_slots, len(_INGEST_RETRY_QUEUE))
    else:
        _RETRY_BUSY_SKIP_COUNT = 0
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
_IN_FLIGHT_TIMEOUT = _INGEST_TIMEOUT_SEC + 120  # 17 min (timeout + grace)


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
                            await asyncio.to_thread(dest.read_bytes), label,
                            mime_type=_AUDIO_SUFFIX_MIME[suffix],
                        ),
                        timeout=_INGEST_TIMEOUT_SEC,
                    )
                else:
                    content = await asyncio.to_thread(
                        dest.read_text, encoding="utf-8", errors="ignore")
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
                            await asyncio.to_thread(dest.read_bytes), label,
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
                        await asyncio.to_thread(dest.read_bytes), label,
                        caption="",
                        mime_type=_IMAGE_SUFFIX_MIME[suffix],
                    )
                else:
                    try:
                        content = await asyncio.to_thread(
                            dest.read_text, encoding="utf-8", errors="ignore")
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
                _untrack_bubble(chat_id, status_msg_id)
                return
            log.info("ingest retry %d/%d: %s",
                     item["attempts"], _MAX_RETRY_ATTEMPTS, title[:80])
            # Linear backoff per attempt: 1×_RETRY_BACKOFF_SEC on 1st
            # failure, 2× on 2nd, ... so a chronically-stuck item never
            # monopolises the queue. NOTE: with _MAX_RETRY_ATTEMPTS=1
            # (current) the give-up branch above always fires first, so
            # this soft-fail/backoff path is effectively dormant — kept
            # for the case the cap is raised again.
            hold = _RETRY_BACKOFF_SEC * item["attempts"]
            _retry_item_soft_fail(item, hold)
            wait_min = max(1, hold // 60)
            await _edit_or_send(
                ctx, chat_id, status_msg_id,
                f"🔁 일시 오류 — {wait_min}분 후 자동 재시도 "
                f"({item['attempts']}/{_MAX_RETRY_ATTEMPTS}): {title[:80]}\n"
                f"{_explain_error(e)}",
            )
            _untrack_bubble(chat_id, status_msg_id)
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
            # _untrack_bubble moved to AFTER each terminal edit (the
            # exception early-returns above + the visibility block
            # below). See _ingest_message for the rationale — running
            # untrack here let SIGTERM strip the entry before the edit,
            # leaving "⏳ [재시도]" frozen.
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
    # Bubble is now in its terminal visual state (edited or deleted) —
    # release tracking after the edit so a SIGTERM between this point
    # and the previous line lets the startup sweep flip the still-⏳
    # bubble instead of leaving it frozen.
    _untrack_bubble(chat_id, status_msg_id)
    # Item reached a terminal state (ok / duplicate / empty / other).
    # Soft-fail returns earlier via _retry_item_soft_fail, so anything
    # reaching here is done.
    _retry_item_done(item)
    log.info("retry done [%s]: %s",
             (r or {}).get("status", "unknown"), title[:80])


async def _wiki_drain_resume(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """On startup, if a temp budget is active and the queue is non-empty,
    silently resume the drain that was interrupted by the deploy."""
    if not wiki.enabled():
        return
    temp = wiki._read_temp_budget()
    if temp is None or temp <= 0:
        return
    qs = wiki.queue_size()
    if qs <= 0:
        # Drain already finished but the temp-budget override lingered on
        # disk (completed in a prior turn / older build). Clear it so the
        # cap reverts to the default now instead of waiting for midnight.
        wiki.clear_temp_budget()
        return
    log.info("wiki drain resume: temp budget ₩%.0f, queue %d — resuming", temp, qs)
    try:
        owner_id = int(os.environ.get("TELEGRAM_OWNER_ID", "0"))
        if owner_id:
            await ctx.bot.send_message(
                owner_id,
                f"🔄 위키 드레인 자동 재개 (배포 후 복구)\n"
                f"임시 한도 ₩{temp:,.0f}, 큐 {qs}건",
            )
        await wiki.drain_queue()
    except Exception:
        log.exception("wiki drain resume failed")


async def _wiki_batch_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Hourly LLM-Wiki synthesis (P1) + 'what it learned' digest (P4,
    throttled to once/KST day) + contradiction ack-alert (P3) + daily-
    budget block alert. No-op unless WIKI_ENABLED=1, so it's safe to
    register unconditionally. Fully wrapped — a batch error can never
    crash the JobQueue loop or touch the RAG corpus."""
    if not wiki.enabled():
        return
    try:
        summary = await wiki.run_batch()
    except Exception:
        log.exception("wiki batch job failed")
        return
    # Refresh the ₩0 structural lint each hour so the dashboard health
    # panel stays current without rescanning ~1000 pages on its 60s
    # render tick. Off the render path, best-effort (never blocks digest).
    try:
        await asyncio.to_thread(wiki.lint)
    except Exception:
        log.exception("wiki lint refresh failed")
    # Daily-budget breaker hit → actionable alert (fires regardless of
    # whether any pages were produced this run). Stable notify_id dedups
    # within the ack window and re-arms later (per notify_acks).
    if summary.get("budget_blocked"):
        try:
            await _send_actionable_alert(
                ctx, notify_id="wiki_daily_budget",
                message=(
                    f"⛔ <b>위키 일일 예산 초과</b>\n"
                    f"오늘 위키 비용 ₩{summary.get('today_cost', 0):,.0f} ≥ "
                    f"한도 ₩{summary.get('budget', 0):,.0f}\n"
                    "오늘은 위키 머지를 중단했습니다(자료는 큐에 보존, 내일 "
                    "KST 0시 자동 재개). 한도 조정: .env WIKI_DAILY_BUDGET_KRW "
                    "· 상태 /wiki_status"))
        except Exception:
            log.exception("wiki budget alert failed")
    contradictions = summary.get("contradictions", 0)
    # Routine "what it learned" digest: hourly batch, but notify at most
    # once per KST day (the contradiction ack-alert below still fires every
    # run — it's deduped by content hash, so it's important + non-spammy).
    if (summary.get("status") == "ok" and summary.get("pages")
            and not await asyncio.to_thread(wiki.digest_sent_today)):
        pages = summary.get("pages", 0)
        docs = summary.get("docs", 0)
        remaining = summary.get("remaining_in_queue", 0)
        updated = summary.get("updated_topics") or []
        topics_line = ", ".join(html.escape(t) for t in updated[:12]) + (
            "…" if len(updated) > 12 else "")
        blocked_note = "\n⛔ (오늘 예산 도달로 일부 중단)" if summary.get("budget_blocked") else ""
        digest = (
            "📚 <b>위키 업데이트</b>\n"
            f"• 갱신 페이지: {pages}개\n"
            f"• 통합 자료: {docs}건\n"
            f"• ⚠️ 모순 표시: {contradictions}건\n"
            f"• 큐 잔여: {remaining}건"
            f"{blocked_note}"
            + (f"\n\n갱신: {topics_line}" if topics_line else "")
            + "\n\n/wiki_today 자세히 · /wiki &lt;토픽&gt; 열람"
        )
        try:
            await ctx.bot.send_message(
                config.TELEGRAM_OWNER_ID, digest, parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await asyncio.to_thread(wiki.mark_digest_sent)
        except Exception:
            log.exception("wiki digest send failed")
    # Contradictions warrant a deliberate look → actionable ack alert.
    # notify_id is a CONTENT hash of the conflicting topic set (NOT a
    # date — per the ack-store rule), so the same conflict set dedups
    # while a new one re-fires.
    ctopics = summary.get("contradiction_topics") or []
    if contradictions > 0 and ctopics:
        import hashlib
        nid = "wiki_conflict_" + hashlib.sha1(
            ",".join(sorted(ctopics)).encode()).hexdigest()[:10]
        first = html.escape(ctopics[0])
        msg = (
            f"⚠️ <b>위키 모순 {contradictions}건</b> — 검토 필요\n"
            f"토픽: {html.escape(', '.join(ctopics[:10]))}\n"
            f"각 페이지의 <b>## ⚠️ 검토 필요</b> 섹션 확인. 예: <code>/wiki {first}</code>"
        )
        try:
            await _send_actionable_alert(ctx, notify_id=nid, message=msg)
        except Exception:
            log.exception("wiki contradiction alert failed")


def _wiki_md_to_tg_html(md: str) -> str:
    """Convert wiki markdown to Telegram-safe HTML with readability."""
    import re as _re
    lines = md.split("\n")
    out: list[str] = []
    for raw in lines:
        line = raw.rstrip()
        # Skip WIKI_META comment
        if "<!--WIKI_META" in line or "<!--" in line and "WIKI_META" in line:
            continue
        # Headings → bold with spacing
        hm = _re.match(r"^(#{1,4})\s+(.+)$", line)
        if hm:
            level = len(hm.group(1))
            text = html.escape(hm.group(2).strip())
            if level <= 2:
                out.append(f"\n{'━' * 20}\n<b>{text}</b>\n")
            else:
                out.append(f"\n<b>▸ {text}</b>")
            continue
        # Blockquote
        if line.startswith("> "):
            text = html.escape(line[2:])
            text = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
            text = _re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
            out.append(f"  ▎ <i>{text}</i>")
            continue
        # Bullet
        bm = _re.match(r"^(\s*)[-*]\s+(.+)$", line)
        if bm:
            indent = "  " * (len(bm.group(1)) // 2)
            text = html.escape(bm.group(2))
            text = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
            text = _re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
            text = _re.sub(r"`(.+?)`", r"<code>\1</code>", text)
            # Clean source refs: (출처: [[...]]) → small
            text = _re.sub(
                r"\(출처:\s*\[\[(.+?)\]\]\)",
                r'<i>[📎 \1]</i>',
                text,
            )
            # — 출처: [[...]] format
            text = _re.sub(
                r"—\s*출처:\s*\[\[(.+?)\]\]",
                r'<i>[📎 \1]</i>',
                text,
            )
            out.append(f"{indent}• {text}")
            continue
        # Horizontal rule → skip (already using ━ for headers)
        if _re.match(r"^---+$|^\*\*\*+$", line.strip()):
            continue
        # Empty line
        if not line.strip():
            out.append("")
            continue
        # Normal paragraph
        text = html.escape(line)
        text = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
        text = _re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
        text = _re.sub(r"`(.+?)`", r"<code>\1</code>", text)
        text = _re.sub(
            r"\(출처:\s*\[\[(.+?)\]\]\)",
            r'<i>[📎 \1]</i>',
            text,
        )
        text = _re.sub(
            r"—\s*출처:\s*\[\[(.+?)\]\]",
            r'<i>[📎 \1]</i>',
            text,
        )
        out.append(text)
    result = "\n".join(out)
    # Collapse excessive blank lines
    result = _re.sub(r"\n{4,}", "\n\n\n", result)
    return result.strip()


async def cmd_wiki(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/wiki [토픽] — 토픽 없으면 페이지 목록, 있으면 합성 위키 페이지."""
    if not _is_owner(update):
        return
    arg = " ".join(ctx.args).strip() if ctx.args else ""
    async with _SustainedTyping(update, ctx):
        if not arg:
            topics = wiki.list_topics()
            if not topics:
                await update.message.reply_text(
                    "📚 위키 페이지가 아직 없습니다.\n"
                    "WIKI_ENABLED=1 로 켜고 자료가 쌓이면 매시 정시"
                    "(KST) 자동 생성됩니다. 지금 만들려면 /wiki_run."
                )
                return
            from datetime import datetime, timedelta, timezone
            _kst = timezone(timedelta(hours=9))
            _7d_ago = datetime.now(_kst) - timedelta(days=7)
            lines = [f"📚 <b>위키 페이지 {len(topics)}개</b>  (/wiki &lt;토픽&gt;)"]
            for t in topics:
                upd_str = t.get("updated") or ""
                badge = ""
                if upd_str:
                    try:
                        upd_dt = datetime.fromisoformat(upd_str)
                        if upd_dt.tzinfo is None:
                            upd_dt = upd_dt.replace(tzinfo=_kst)
                        badge = " 🆕" if upd_dt >= _7d_ago else ""
                    except (ValueError, TypeError):
                        pass
                date_part = f", {upd_str[:10]}" if upd_str else ""
                lines.append(f"• {html.escape(t['topic'])}  ({t['docs']}건{date_part}){badge}")
            for chunk in _split_for_telegram("\n".join(lines)):
                await update.message.reply_text(
                    chunk, parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            return
        page = wiki.read_page(arg)
        if not page:
            await update.message.reply_text(
                f"📚 '{arg}' 페이지 없음. /wiki 로 목록 확인."
            )
            return
        tg_html = _wiki_md_to_tg_html(page)
        for chunk in _split_for_telegram(tg_html):
            await update.message.reply_text(
                chunk, parse_mode="HTML",
                disable_web_page_preview=True,
            )


async def cmd_wiki_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/wiki_today — 마지막 위키 배치 결과 요약."""
    if not _is_owner(update):
        return
    lr = wiki.last_run()
    if not lr:
        await update.message.reply_text(
            "아직 위키 배치 기록이 없습니다. /wiki_run 으로 수동 실행 가능."
        )
        return
    if lr.get("status") == "budget_blocked" and not lr.get("pages"):
        await update.message.reply_text(
            f"⛔ 마지막 배치({(lr.get('started') or '')[:16]}): 일일 예산 "
            f"₩{lr.get('budget', 0):,.0f} 초과로 시작 전 차단(오늘 ₩"
            f"{lr.get('today_cost', 0):,.0f}). 내일 KST 0시 재개."
        )
        return
    if lr.get("status") == "empty":
        await update.message.reply_text(
            f"마지막 배치({(lr.get('started') or '')[:16]}): "
            "통합할 자료 없음(큐 비어있음)."
        )
        return
    updated = lr.get("updated_topics") or []
    errs = lr.get("errors") or []
    blocked = "\n⛔ 오늘 예산 도달로 일부 중단" if lr.get("budget_blocked") else ""
    body = (
        "📚 <b>마지막 위키 배치</b>\n"
        f"• 시각: {(lr.get('started') or '')[:16]}\n"
        f"• 갱신 페이지: {lr.get('pages', 0)}개\n"
        f"• 통합 자료: {lr.get('docs', 0)}건\n"
        f"• ⚠️ 모순: {lr.get('contradictions', 0)}건\n"
        f"• 큐 잔여: {lr.get('remaining_in_queue', 0)}건"
        f"{blocked}"
        + (f"\n\n갱신: {', '.join(html.escape(t) for t in updated[:15])}"
           if updated else "")
        + (f"\n\n⚠️ 실패 {len(errs)}건:\n"
           + "\n".join(html.escape(e) for e in errs[:5]) if errs else "")
    )
    await update.message.reply_text(
        body, parse_mode="HTML", disable_web_page_preview=True,
    )


async def cmd_wiki_recent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/wiki_recent [일수] — 최근 N일(기본 7) 업데이트된 위키 토픽."""
    if not _is_owner(update):
        return
    days = 7
    if ctx.args:
        try:
            days = max(1, int(ctx.args[0]))
        except ValueError:
            pass
    from datetime import datetime, timedelta, timezone
    _kst = timezone(timedelta(hours=9))
    cutoff = datetime.now(_kst) - timedelta(days=days)
    topics = wiki.list_topics()
    recent = []
    for t in topics:
        upd_str = t.get("updated") or ""
        if not upd_str:
            continue
        # New(생성 N일 이내)인 토픽은 Recent에서 제외 — 두 목록을 배타적으로.
        # New 기간이 끝난(생성 N일 지난) 뒤에야 Recent로 잡힌다.
        cr_str = t.get("created") or ""
        if cr_str:
            try:
                _cr = datetime.fromisoformat(cr_str)
                if _cr.tzinfo is None:
                    _cr = _cr.replace(tzinfo=_kst)
                if _cr >= cutoff:
                    continue
            except (ValueError, TypeError):
                pass
        try:
            upd_dt = datetime.fromisoformat(upd_str)
            if upd_dt.tzinfo is None:
                upd_dt = upd_dt.replace(tzinfo=_kst)
            if upd_dt >= cutoff:
                recent.append((upd_dt, t))
        except (ValueError, TypeError):
            continue
    if not recent:
        await update.message.reply_text(
            f"최근 {days}일 내 업데이트된 위키 페이지 없음 (신규 토픽은 /wiki_new)."
        )
        return
    recent.sort(key=lambda x: x[0], reverse=True)
    lines = [f"🆕 <b>최근 {days}일 위키 업데이트 ({len(recent)}개)</b>"]
    for upd_dt, t in recent:
        ago = (datetime.now(_kst) - upd_dt).days
        ago_str = "오늘" if ago == 0 else f"{ago}일 전"
        lines.append(
            f"• {html.escape(t['topic'])}  ({t['docs']}건, {ago_str})"
        )
    for chunk in _split_for_telegram("\n".join(lines)):
        await update.message.reply_text(
            chunk, parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def cmd_wiki_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/wiki_new [일수] — 최근 N일(기본 7) 새로 생성된 위키 토픽."""
    if not _is_owner(update):
        return
    days = 7
    if ctx.args:
        try:
            days = max(1, int(ctx.args[0]))
        except ValueError:
            pass
    from datetime import datetime, timedelta, timezone
    _kst = timezone(timedelta(hours=9))
    cutoff = datetime.now(_kst) - timedelta(days=days)
    topics = wiki.list_topics()
    new_topics = []
    for t in topics:
        cr_str = t.get("created") or ""
        if not cr_str:
            continue
        try:
            cr_dt = datetime.fromisoformat(cr_str)
            if cr_dt.tzinfo is None:
                cr_dt = cr_dt.replace(tzinfo=_kst)
            if cr_dt >= cutoff:
                new_topics.append((cr_dt, t))
        except (ValueError, TypeError):
            continue
    if not new_topics:
        await update.message.reply_text(f"최근 {days}일 내 새로 생성된 위키 페이지 없음.")
        return
    new_topics.sort(key=lambda x: x[0], reverse=True)
    lines = [f"✨ <b>최근 {days}일 신규 위키 토픽 ({len(new_topics)}개)</b>"]
    for cr_dt, t in new_topics:
        ago = (datetime.now(_kst) - cr_dt).days
        ago_str = "오늘" if ago == 0 else f"{ago}일 전"
        lines.append(
            f"• {html.escape(t['topic'])}  ({t['docs']}건, {ago_str})"
        )
    for chunk in _split_for_telegram("\n".join(lines)):
        await update.message.reply_text(
            chunk, parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def cmd_wiki_lint(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/wiki_lint — ₩0 구조 점검: 정체 단일소스·미해결 모순·고립 토픽."""
    if not _is_owner(update):
        return
    async with _SustainedTyping(update, ctx):
        res = await asyncio.to_thread(wiki.lint)
    stale = res.get("stale_singletons") or []
    contra = res.get("contradictions") or []
    missing = res.get("missing_pages") or []
    lines = [
        "🩺 <b>위키 점검</b> (₩0, LLM 없음)",
        f"📊 토픽 {res.get('total_topics', 0)}개 · 스캔 {res.get('pages_scanned', 0)}",
    ]
    # No truncation — _split_for_telegram chunks long lists across
    # messages, and the dashboard concatenates them. Show every item.
    if contra:
        lines.append(f"\n⚠️ <b>미해결 모순 {len(contra)}개</b> — 검토 <code>/wiki &lt;토픽&gt;</code>")
        for t in contra:
            lines.append(f"• {html.escape(t)}")
    if stale:
        lines.append(
            f"\n🧹 <b>정체 단일소스 {len(stale)}개</b> "
            f"({res.get('stale_days', 30)}일+ 미갱신 → 병합/삭제 후보)")
        for r in stale:
            lines.append(f"• {html.escape(r['topic'])}  ({r['docs']}건, {r['updated']})")
    if missing:
        lines.append(
            f"\n🗂 <b>누락 페이지 {len(missing)}개</b> "
            "(인덱스엔 있으나 .md 없음 → 정합성)")
        for t in missing:
            lines.append(f"• {html.escape(t)}")
    if not contra and not stale and not missing:
        lines.append("\n✅ 이상 없음 — 모순·정체·누락 없음.")
    lines.append(f"\n<i>생성: {html.escape(res.get('generated_at', ''))}</i>")
    for chunk in _split_for_telegram("\n".join(lines)):
        await update.message.reply_text(
            chunk, parse_mode="HTML", disable_web_page_preview=True)


async def cmd_wiki_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/wiki_status — 동작 상태 + 대기 큐 + 페이지 수 + 오늘/이번달 위키 비용."""
    if not _is_owner(update):
        return
    def _gather_status():
        return (wiki.enabled(), wiki.is_disabled(), wiki.queue_size(),
                len(wiki.list_topics()), wiki.today_cost_krw(),
                cost.month_to_date_krw())
    on, killed, qn, pages, today_w, mtd = await asyncio.to_thread(_gather_status)
    budget = config.WIKI_DAILY_BUDGET_KRW
    over = budget > 0 and today_w >= budget
    w = mtd.get("by_purpose", {}).get("wiki", {})
    body = (
        "📚 <b>LLM Wiki 상태</b>\n"
        f"• 동작: {'🟢 ON' if on else '⚪️ OFF'}  "
        f"(WIKI_ENABLED={'1' if config.WIKI_ENABLED else '0'}"
        f", 질의우선={'1' if config.WIKI_QUERY_FIRST else '0'}"
        f"{', ⛔킬스위치' if killed else ''})\n"
        f"• 머지 모델: {html.escape(config.WIKI_MERGE_MODEL)}\n"
        f"• 대기 큐: {qn}건 · 위키 페이지: {pages}개\n"
        f"• 오늘 위키 비용: ₩{today_w:,.0f} / 한도 ₩{budget:,.0f}"
        f"{' ⛔초과·오늘 중단' if over else ''}\n"
        f"• 이번달 위키 비용: ₩{w.get('cost', 0.0):,.1f}  ({w.get('calls', 0)}콜)\n"
        f"• 배치: 매시 정시(KST) · 캡 "
        f"{config.WIKI_MAX_TOPICS_PER_RUN}토픽×{config.WIKI_MAX_DOCS_PER_TOPIC}건/회\n"
        "\n/wiki_run 지금 실행 · /wiki_off 끄기 · /wiki_on 켜기"
    )
    await update.message.reply_text(
        body, parse_mode="HTML", disable_web_page_preview=True,
    )


async def cmd_wiki_cost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/wiki_cost — 위키 전용 비용/사용량: 오늘·7일·이번달·전체, 일별 추이,
    예산 대비, 큐/페이지. /cost 의 wiki 스코프 버전."""
    if not _is_owner(update):
        return
    async with _SustainedTyping(update, ctx):
        def _gather_wiki_cost():
            return (wiki.today_cost_krw(), wiki.budget_krw(),
                    wiki.budget_exceeded(), wiki.month_cost_krw(),
                    wiki.total_cost_krw(), cost.period_krw(7),
                    cost.daily_breakdown(14, purpose="wiki"),
                    wiki.queue_size(), len(wiki.list_topics()))
        (today_w, budget, over, month_w, total_w, week_all, daily,
         qn, pages) = await asyncio.to_thread(_gather_wiki_cost)

        week = week_all.get("by_purpose", {}).get("wiki", {})
        week_cost = week.get("cost", 0.0)
        week_calls = week.get("calls", 0)
        avg_7d = week_cost / 7 if week_cost else 0.0
        projected_monthly = avg_7d * 30

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
            "📚 위키 비용/사용량 (KST · Gemini API)\n"
            f"\n💰 비용"
            f"\n  • 오늘:    ₩{today_w:,.0f} / 한도 ₩{budget:,.0f}"
            f"{'  ⛔초과·오늘 중단' if over else ''}"
            f"\n  • 7일:     ₩{week_cost:,.0f}  ({week_calls}콜)"
            f"\n  • 이번 달: ₩{month_w:,.0f}"
            f"\n  • 전체:    ₩{total_w:,.0f}"
            f"\n\n📈 추세"
            f"\n  • 일평균(7일):    ₩{avg_7d:,.0f}"
            f"\n  • 월 예상(×30):   ₩{projected_monthly:,.0f}"
            f"\n\n📅 최근 14일 위키 지출\n{daily_block}"
            f"\n\n💡 비용 구조"
            f"\n  append ₩0 · consolidation ~₩55/회 · 신규 ~₩25/회"
            f"\n  대부분 배치는 ₩0 (append) — /wiki_guide 상세"
            f"\n\n📦 큐: {qn:,}건 · 위키 페이지: {pages}개"
        )
        await update.message.reply_text(out)


async def cmd_wiki_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/wiki_run — 배치를 지금 수동 실행(매시 정시 외 즉시 반영용)."""
    if not _is_owner(update):
        return
    if not wiki.enabled():
        await update.message.reply_text(
            "위키가 꺼져 있습니다. .env WIKI_ENABLED=1 (+재배포) 후 사용하거나, "
            "killswitch 상태면 /wiki_on. (지금은 자료가 큐에만 쌓입니다.)"
        )
        return
    async with _SustainedTyping(update, ctx):
        await update.message.reply_text("📚 위키 배치 실행 중…")
        try:
            summary = await wiki.run_batch()
        except Exception as e:
            await update.message.reply_text(f"⚠️ 배치 실패: {type(e).__name__}: {e}")
            return
    if summary.get("status") == "budget_blocked" and not summary.get("pages"):
        await update.message.reply_text(
            f"⛔ 오늘 위키 비용 ₩{summary.get('today_cost', 0):,.0f} ≥ 한도 "
            f"₩{summary.get('budget', 0):,.0f} — 차단됨. 내일 재개 또는 "
            ".env WIKI_DAILY_BUDGET_KRW 조정."
        )
        return
    note = " ⛔예산도달로 일부중단" if summary.get("budget_blocked") else ""
    await update.message.reply_text(
        f"완료 · 페이지 {summary.get('pages', 0)} · 자료 {summary.get('docs', 0)} · "
        f"모순 {summary.get('contradictions', 0)} · 큐잔여 "
        f"{summary.get('remaining_in_queue', 0)}{note}\n/wiki_today 자세히"
    )


async def cmd_wiki_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/wiki_off — 런타임 킬스위치(재배포 없이 즉시 중지). 큐 적재·매시
    배치·질의우선 전부 멈춤. 기존 페이지/코퍼스는 그대로. /wiki_on 해제."""
    if not _is_owner(update):
        return
    wiki.set_disabled(True)
    await update.message.reply_text(
        "⛔ 위키 즉시 중지(killswitch). 큐 적재·매시 배치·질의우선 모두 정지. "
        "기존 위키 페이지와 RAG 코퍼스는 그대로. 해제: /wiki_on"
    )


async def cmd_wiki_on(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/wiki_on — 킬스위치 해제(WIKI_ENABLED=1 일 때만 실제 동작)."""
    if not _is_owner(update):
        return
    wiki.set_disabled(False)
    state = "🟢 동작" if wiki.enabled() else "⚪️ 여전히 OFF (WIKI_ENABLED=0)"
    await update.message.reply_text(f"킬스위치 해제. 현재: {state}")


async def cmd_wiki_drain(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/wiki_drain [한도=20000] — 임시 예산 올려서 큐를 최대한 소진.
    드레인이 끝나면 곧바로 기본 한도(₩1000)로 복귀(자정까지 안 기다림)."""
    if not _is_owner(update):
        return
    if not wiki.enabled():
        await update.message.reply_text(
            "위키가 꺼져 있습니다. /wiki_on 또는 .env WIKI_ENABLED=1 후 사용."
        )
        return
    try:
        limit = int(ctx.args[0]) if ctx.args else 20000
    except ValueError:
        limit = 20000
    wiki.set_temp_budget(limit)
    remaining = wiki.queue_size()
    status_msg = await update.message.reply_text(
        f"🔄 위키 드레인 시작 (임시 한도 ₩{limit:,}, 큐 {remaining}건)\n"
        f"예산 소진 또는 큐 완료까지 반복 실행합니다…"
    )
    chat_id = update.effective_chat.id

    batch_num = 0

    async def _on_progress(summary: dict) -> None:
        nonlocal batch_num
        batch_num += 1
        pages = summary.get("pages", 0)
        docs = summary.get("docs", 0)
        rem = summary.get("remaining_in_queue", 0)
        cost = summary.get("today_cost", 0)
        budget = summary.get("budget", limit)
        blocked = " ⛔예산도달" if summary.get("budget_blocked") else ""
        try:
            await ctx.bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg.message_id,
                text=(
                    f"🔄 위키 드레인 진행 중… (배치 #{batch_num})\n"
                    f"이번 배치: 페이지 {pages} · 자료 {docs}\n"
                    f"큐 잔여: {rem}건\n"
                    f"오늘 비용: ₩{cost:,.0f} / ₩{budget:,.0f}{blocked}"
                ),
            )
        except Exception:
            pass

    try:
        results = await wiki.drain_queue(on_progress=_on_progress)
    except Exception as e:
        await ctx.bot.edit_message_text(
            chat_id=chat_id,
            message_id=status_msg.message_id,
            text=f"⚠️ 드레인 실패: {type(e).__name__}: {e}",
        )
        return

    total_pages = sum(r.get("pages", 0) for r in results)
    total_docs = sum(r.get("docs", 0) for r in results)
    final_cost = results[-1].get("today_cost", 0) if results else 0
    final_rem = results[-1].get("remaining_in_queue", 0) if results else 0
    budget_hit = any(r.get("budget_blocked") for r in results)
    await ctx.bot.edit_message_text(
        chat_id=chat_id,
        message_id=status_msg.message_id,
        text=(
            f"✅ 위키 드레인 완료 ({len(results)}회 배치)\n"
            f"총 페이지: {total_pages} · 자료: {total_docs}\n"
            f"큐 잔여: {final_rem}건\n"
            f"오늘 비용: ₩{final_cost:,.0f}"
            + (" ⛔예산도달" if budget_hit else "")
            + "\n기본 한도(₩1,000)로 즉시 복귀 완료."
        ),
    )


async def cmd_wiki_split(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/wiki_split <토픽> — 합쳐진 위키 페이지를 해체하고 개별 회사 페이지로
    재분배. 해당 페이지 삭제 + doc들을 개별 토픽 큐에 재적재(₩0).
    다음 배치에서 각각 머지."""
    if not _is_owner(update):
        return
    topic = " ".join(ctx.args).strip() if ctx.args else ""
    if not topic:
        candidates = wiki.list_mergeable_topics()
        if candidates:
            lines = "\n".join(f"  • {c}" for c in candidates[:30])
            await update.message.reply_text(
                f"분리 가능 토픽 {len(candidates)}개:\n{lines}\n\n"
                "/wiki_split <토픽명> 또는 /wiki_split all"
            )
        else:
            await update.message.reply_text("분리 가능한 합쳐진 토픽이 없습니다.")
        return
    if topic.lower() in ("all", "전체"):
        candidates = wiki.list_mergeable_topics()
        if not candidates:
            await update.message.reply_text("분리 가능한 합쳐진 토픽이 없습니다.")
            return
        results = []
        for c in candidates:
            try:
                r = await asyncio.to_thread(wiki.decompose_merged_topic, c)
                results.append((c, r))
            except Exception as e:
                results.append((c, {"error": str(e)}))
        ok = [(t, r) for t, r in results if not r.get("error")]
        fail = [(t, r) for t, r in results if r.get("error")]
        msg = f"✅ {len(ok)}개 해체 완료"
        if ok:
            msg += "\n" + "\n".join(
                f"  • {t}: 자료 {r['docs']}건 → 재적재 {r['re_enqueued']}건"
                for t, r in ok)
        if fail:
            msg += f"\n⚠️ {len(fail)}개 실패"
        msg += "\n\n/wiki_run 으로 각 회사 페이지 머지"
        await update.message.reply_text(msg)
        return
    try:
        res = await asyncio.to_thread(wiki.decompose_merged_topic, topic)
    except Exception as e:
        await update.message.reply_text(f"⚠️ 분리 실패: {e}")
        return
    if res.get("error"):
        await update.message.reply_text(f"⚠️ {res['error']}")
        return
    note = res.get("note")
    lines = [
        f"✅ 「{topic}」 해체 완료",
        f"• 원본 자료: {res['docs']}건",
        f"• 개별 토픽으로 재적재: {res['re_enqueued']}건",
        f"• 파일 삭제: {'예' if res['file_deleted'] else '아니오'}",
    ]
    if note:
        lines.append(f"ℹ️ {note}")
    else:
        lines.append("다음 배치(/wiki_run)에서 각 회사 페이지로 머지됩니다.")
    await update.message.reply_text("\n".join(lines))


async def cmd_wiki_rename(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/wiki_rename <옛이름> :: <새이름> — 위키 토픽 이름 변경 + alias 저장."""
    if not _is_owner(update):
        return
    text = " ".join(ctx.args).strip() if ctx.args else ""
    if "::" not in text:
        await update.message.reply_text("사용법: /wiki_rename <옛이름> :: <새이름>")
        return
    old, new = [s.strip() for s in text.split("::", 1)]
    if not old or not new:
        await update.message.reply_text("사용법: /wiki_rename <옛이름> :: <새이름>")
        return
    res = await asyncio.to_thread(wiki.rename_topic, old, new)
    if res.get("error"):
        await update.message.reply_text(f"⚠️ {res['error']}")
        return
    await update.message.reply_text(
        f"✅ '{old}' → '{new}'\n"
        f"큐 재매핑: {res.get('remapped_queue', 0)}건\n"
        f"alias 저장 (향후 '{old}' → '{new}' 자동 라우팅)"
    )


async def cmd_wiki_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/wiki_delete <토픽> — 위키 토픽 완전 삭제 (인덱스+페이지+큐)."""
    if not _is_owner(update):
        return
    topic = " ".join(ctx.args).strip() if ctx.args else ""
    if not topic:
        await update.message.reply_text("사용법: /wiki_delete <토픽명>")
        return
    res = await asyncio.to_thread(wiki.delete_topic, topic)
    if res.get("error"):
        await update.message.reply_text(f"⚠️ {res['error']}")
        return
    parts = [f"🗑 '{topic}' 삭제 완료"]
    if res.get("deleted_file"):
        parts.append("  • .md 파일 삭제")
    if res.get("had_index"):
        parts.append("  • 인덱스 제거")
    if res.get("removed_queue", 0):
        parts.append(f"  • 큐 {res['removed_queue']}건 제거")
    await update.message.reply_text("\n".join(parts))


async def cmd_wiki_backfill(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/wiki_backfill [개월=6|all] — 기존 자료(meta.db)를 위키 큐에 적재.
    적재 자체는 ₩0; 실제 머지는 매시 배치가 일일 예산 캡 내에서 처리하므로
    큰 백필도 여러 날에 걸쳐 안전하게 빠진다."""
    if not _is_owner(update):
        return
    if not wiki.enabled():
        await update.message.reply_text(
            "위키가 꺼져 있어 백필 불가. .env WIKI_ENABLED=1 (+재배포) "
            "또는 /wiki_on 후 사용."
        )
        return
    arg = (ctx.args[0].strip().lower() if ctx.args else "6")
    if arg in ("all", "전체"):
        cutoff, scope = None, "전체"
    else:
        try:
            months = max(1, min(120, int(arg)))
        except ValueError:
            await update.message.reply_text(
                "사용법: /wiki_backfill [개월수|all]  예) /wiki_backfill 6"
            )
            return
        cutoff = (datetime.utcnow() - timedelta(days=30 * months)).isoformat()
        scope = f"최근 {months}개월"
    async with _SustainedTyping(update, ctx):
        docs = await asyncio.to_thread(meta.docs_since, cutoff)
        res = await asyncio.to_thread(wiki.backfill, docs)
    if res.get("error"):
        await update.message.reply_text(f"백필 불가: {res['error']}")
        return
    budget = int(config.WIKI_DAILY_BUDGET_KRW)
    await update.message.reply_text(
        f"📚 백필({scope}) 큐 적재 완료\n"
        f"• 적재: {res['enqueued']}건 (대상 {res['total']}건 중)\n"
        f"• 건너뜀: 이미위키 {res['skipped_wikied']} · 큐중복 "
        f"{res['skipped_queued']} · 짧음/기타 {res['skipped_gate']} · "
        f"실패제외 {res.get('skipped_failed', 0)}\n"
        f"• 적재 비용 ₩0 — 매시 배치가 ₩{budget:,}/일 캡 내에서 며칠~몇 주에 "
        f"걸쳐 처리(추정 일회성 ~₩{res['enqueued']:,}, 문서당 ~₩1).\n"
        f"지금 한 묶음 바로 처리하려면 /wiki_run · 상태 /wiki_status"
    )


async def cmd_wiki_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/wiki_pending — 위키 큐 대기 현황 (토픽별 문서 수)."""
    if not _is_owner(update):
        return
    groups = await asyncio.to_thread(wiki.pending_list)
    if not groups:
        await update.message.reply_text("✅ 위키 큐 비어있음")
        return
    total = sum(g["docs"] for g in groups)
    lines = [f"📋 <b>위키 큐 대기: {total}건 ({len(groups)}개 토픽)</b>\n"]
    for g in groups[:30]:
        titles = html.escape(", ".join(g["titles"]))
        if titles:
            titles = f" — {titles}"
        lines.append(
            f"• <b>{html.escape(g['topic'])}</b>: {g['docs']}건{titles}")
    if len(groups) > 30:
        lines.append(f"\n…외 {len(groups) - 30}개 토픽")
    for chunk in _split_for_telegram("\n".join(lines)):
        await update.message.reply_text(chunk, parse_mode="HTML")


async def cmd_wiki_failed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/wiki_failed [clear|retry <토픽>] — 위키 머지 실패 목록 관리."""
    if not _is_owner(update):
        return
    args = (ctx.args or [])
    if args and args[0].lower() == "clear":
        topic = " ".join(args[1:]).strip() or None
        removed = await asyncio.to_thread(wiki.wiki_failed_clear, topic)
        await update.message.reply_text(
            f"🗑 {removed}건 삭제" if removed else "삭제할 항목 없음")
        return
    if args and args[0].lower() == "retry_all":
        res = await asyncio.to_thread(wiki.wiki_failed_retry_all)
        if res["retried"] == 0:
            await update.message.reply_text("재시도할 실패 항목 없음")
        else:
            topics = ", ".join(res["topics"][:10])
            await update.message.reply_text(
                f"🔄 {res['retried']}개 토픽 · {res['requeued']}건 큐 복귀\n"
                f"토픽: {topics}\n다음 배치(/wiki_run)에서 재시도")
        return
    if args and args[0].lower() == "retry":
        topic = " ".join(args[1:]).strip()
        if not topic:
            await update.message.reply_text("사용법: /wiki_failed retry <토픽명>")
            return
        res = await asyncio.to_thread(wiki.wiki_failed_retry, topic)
        if res.get("error"):
            await update.message.reply_text(f"⚠️ {res['error']}")
        else:
            await update.message.reply_text(
                f"🔄 '{topic}' {res.get('requeued', 0)}건 큐 복귀 — "
                f"다음 배치(/wiki_run)에서 재시도")
        return

    failed = wiki.wiki_failed()
    if not failed:
        await update.message.reply_text("✅ 위키 실패 목록 비어있음")
        return
    lines = [f"⚠️ <b>위키 머지 실패: {len(failed)}건</b>\n"]
    for f in failed[:20]:
        promoted = " 🚫큐제거" if f.get("promoted") else ""
        lines.append(
            f"• <b>{html.escape(f['topic'])}</b>"
            f" ({f.get('cycles', 0)}회 실패{promoted})\n"
            f"  오류: {html.escape(str(f.get('last_error', '?'))[:80])}\n"
            f"  최근: {html.escape(str(f.get('last_ts', '?')))}")
    lines.append(
        "\n/wiki_failed clear — 전체 삭제"
        "\n/wiki_failed retry &lt;토픽&gt; — 단건 재시도"
        "\n/wiki_failed retry_all — 전체 재시도")
    for chunk in _split_for_telegram("\n".join(lines)):
        await update.message.reply_text(chunk, parse_mode="HTML")


async def cmd_wiki_dedup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/wiki_dedup [merge A :: B | merge_all] — 유사 토픽 감지 + 병합."""
    if not _is_owner(update):
        return
    args_raw = " ".join(ctx.args or []).strip()

    if args_raw.lower() == "merge_all":
        await update.message.reply_text("🔄 전체 병합 시작...")
        res = await asyncio.to_thread(wiki.merge_all_duplicates)
        await update.message.reply_text(
            f"✅ 전체 병합 완료\n"
            f"감지: {res['pairs']}쌍 · 병합: {res['merged']}건 · "
            f"오류: {res['errors']}건")
        return

    if args_raw.lower().startswith("merge "):
        parts = args_raw[6:].split("::")
        if len(parts) != 2:
            await update.message.reply_text(
                "사용법: /wiki_dedup merge <유지할 토픽> :: <흡수할 토픽>")
            return
        keep, absorb = parts[0].strip(), parts[1].strip()
        if not keep or not absorb:
            await update.message.reply_text(
                "사용법: /wiki_dedup merge <유지할 토픽> :: <흡수할 토픽>")
            return
        res = await asyncio.to_thread(wiki.merge_topics, keep, absorb)
        if res.get("error"):
            await update.message.reply_text(f"⚠️ {res['error']}")
        else:
            await update.message.reply_text(
                f"✅ '{res['absorbed']}' → '{res['keep']}' 병합 완료\n"
                f"총 문서: {res['docs_merged']}개 · "
                f"재큐잉: {res['new_enqueued']}건 · "
                f"파일삭제: {'✓' if res['file_deleted'] else '✗'}")
        return

    pairs = await asyncio.to_thread(wiki.find_duplicates)
    if not pairs:
        await update.message.reply_text("✅ 유사 중복 토픽 없음")
        return
    lines = [f"🔍 <b>유사 중복 토픽 후보: {len(pairs)}쌍</b>\n"]
    for i, (a, b, da, db) in enumerate(pairs[:30], 1):
        lines.append(f"{i}. <b>{html.escape(a)}</b> ({da}건) ↔ "
                     f"<b>{html.escape(b)}</b> ({db}건)")
    lines.append(
        "\n개별: /wiki_dedup merge A :: B"
        "\n전체: /wiki_dedup merge_all")
    for chunk in _split_for_telegram("\n".join(lines)):
        await update.message.reply_text(chunk, parse_mode="HTML")


# ──────────────────────────────────────────────────────────────────────
# Dashboard query bridge (web search box → bot)
# ──────────────────────────────────────────────────────────────────────
# The Second Brain dashboard search box can ask the bot directly: natural
# language → a paid agent.run(); a "/command" → the SAME cmd_* handler the
# Telegram bot runs, executed head-less as the owner (see
# _run_dashboard_command + _build_dash_command_map). Requests are parked in
# dash_queries.db by the dashboard server (a thin 200 MB container that
# can't run the agent); this worker — in the bot, which has the agent warm
# + a memory gate — drains them and writes the answer back. See
# src/store/dash_queries.py for the queue contract.

# NOTE: these two sets are NOT an execution gate — they only drive the
# Commands page badges (💰 paid / ⚠️ 변경 mutation) in regenerate.py. The
# real allowlist is _DASH_COMMAND_HANDLERS, which auto-discovers EVERY
# cmd_*, so the web surface can run anything the Telegram bot can —
# mutations and paid spends included. The only boundary is the secret URL
# token (timing-safe-checked + flood-capped in dashboard/server.py); there
# is no per-command block, so e.g. /wiki_rename or /forget DO run from the
# web. Keep both sets in sync with the handlers they classify so the
# badges stay truthful.
_DASH_PAID_COMMANDS = frozenset({
    "deep", "search_papers", "search_papers_advanced", "paper_stats",
    "search_patents", "search_patents_advanced", "patent_stats",
    "web_search", "search_my_brain", "compare_papers",
    "company_patents", "patent_detail", "citing_patents",
    "kipris_search", "kipris_pub", "kipris_reg", "kipris_inventor",
    "kipris_status", "kipris_family", "kipris_claims", "kipris_priority",
    "kr_papers", "kr_patents", "kr_reports", "kr_trends",
    "kr_researcher", "kr_organ", "kr_science_trend",
    "kr_rnd_projects", "kr_related", "kr_outcomes",
    "kr_govt_reports", "kr_agency_rnd", "kr_rnd_issues",
    "ingest_url", "eval", "eval_seed",
    "wiki_run", "wiki_drain", "wiki_backfill",
})

_DASH_MUTATION_COMMANDS = frozenset({
    "reset", "forget", "forget_search", "forget_search_all",
    "forget_qna", "forget_qna_search",
    "forget_forwards", "forget_forwards_confirm",
    "failed_retry", "failed_clear",
    "queue_to_failed", "queue_panic", "queue_cancel_all",
    "dedupe", "dedupe_confirm", "cleanup", "cleanup_confirm",
    "wiki_delete", "wiki_rename", "wiki_split", "wiki_dedup",
    "wiki_off", "wiki_on",
    "reset_blocked_hosts",
    "pending_approve_all", "pending_approve_all_confirm", "pending_cancel_all",
    "pending_ocr", "pending_pro", "ocr_extend",
    "youtube_restub_rescan", "fix_placeholder_titles",
    "ingest_url",
})


class _CapturedReply:
    """Collects the text a handler would have sent to Telegram, so a
    read-only command can run head-less for the dashboard. Inline
    keyboards and media are dropped — the dashboard renders text only."""

    def __init__(self):
        self.chunks: list[str] = []

    def add(self, text) -> None:
        if text:
            self.chunks.append(str(text))

    def text(self) -> str:
        return "\n\n".join(self.chunks).strip()


class _FakeChat:
    def __init__(self):
        self.id = config.TELEGRAM_OWNER_ID
        self.type = "private"


class _FakeUser:
    def __init__(self):
        self.id = config.TELEGRAM_OWNER_ID
        self.username = "dashboard"
        self.first_name = "dashboard"
        self.is_bot = False


class _FakeMessage:
    def __init__(self, sink: "_CapturedReply", text: str = ""):
        self._sink = sink
        self.text = text
        self.caption = None
        self.chat = _FakeChat()
        self.message_id = 0

    async def reply_text(self, text=None, **kw):
        self._sink.add(text)
        return _FakeMessage(self._sink)

    async def reply_html(self, text=None, **kw):
        self._sink.add(text)
        return _FakeMessage(self._sink)

    async def reply_markdown(self, text=None, **kw):
        self._sink.add(text)
        return _FakeMessage(self._sink)

    async def reply_markdown_v2(self, text=None, **kw):
        self._sink.add(text)
        return _FakeMessage(self._sink)

    async def edit_text(self, text=None, **kw):
        self._sink.add(text)
        return _FakeMessage(self._sink)

    async def reply_photo(self, photo=None, caption=None, **kw):
        self._sink.add(caption)
        return _FakeMessage(self._sink)

    async def reply_document(self, document=None, caption=None, **kw):
        self._sink.add(caption)
        return _FakeMessage(self._sink)

    async def reply_chat_action(self, *a, **kw):
        return None


class _FakeBot:
    def __init__(self, sink: "_CapturedReply"):
        self._sink = sink

    async def send_message(self, chat_id=None, text=None, **kw):
        self._sink.add(text)
        return _FakeMessage(self._sink)

    async def send_photo(self, chat_id=None, caption=None, **kw):
        self._sink.add(caption)
        return _FakeMessage(self._sink)

    async def send_document(self, chat_id=None, caption=None, **kw):
        self._sink.add(caption)
        return _FakeMessage(self._sink)

    def __getattr__(self, name):
        # Any other bot API call → async no-op, so a read-only handler
        # that pokes an unsupported method can't crash the head-less run.
        async def _noop(*a, **kw):
            return None
        return _noop


class _FakeUpdate:
    def __init__(self, sink: "_CapturedReply", text: str):
        self.message = _FakeMessage(sink, text)
        self.effective_message = self.message
        self.effective_chat = _FakeChat()
        self.effective_user = _FakeUser()
        self.callback_query = None


class _FakeContext:
    def __init__(self, sink: "_CapturedReply", args: list[str]):
        self.args = args
        self.bot = _FakeBot(sink)
        self.bot_data = {}
        self.chat_data = {}
        self.user_data = {}
        self.application = None
        self.job_queue = None


async def _run_dashboard_command(cmd: str, args: list[str],
                                  raw_text: str = "") -> dict:
    """Run any registered command head-less for the dashboard and return
    its captured text. Returns {kind:'command', text|error}."""
    handler = _DASH_COMMAND_HANDLERS.get(cmd)
    if handler is None:
        return {"kind": "command",
                "error": f"'/{cmd}' 은 등록된 명령어가 아니에요."}
    sink = _CapturedReply()
    raw = raw_text or ("/" + cmd + (" " + " ".join(args) if args else "")).strip()
    update = _FakeUpdate(sink, raw)
    ctx = _FakeContext(sink, list(args))
    try:
        await handler(update, ctx)
    except Exception as e:
        log.exception("dashboard command /%s failed", cmd)
        return {"kind": "command", "error": f"명령 실행 오류: {str(e)[:200]}"}
    out = sink.text()
    return {"kind": "command", "text": out or "(응답이 비어 있어요)"}


async def _dash_query_worker(ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    """Drain one parked dashboard query per tick: any /command or a
    natural-language agent.run(). Serial by design (one per tick) so
    concurrent web asks can't stack agent runs and OOM the bot."""
    try:
        pending = dash_queries.claim_pending(limit=1)
    except Exception:
        log.exception("dash worker: claim failed")
        return
    for item in pending:
        qid = item["id"]
        q = (item.get("query") or "").strip()
        try:
            if q.startswith("/"):
                parts = q[1:].split()
                cmd = (parts[0].split("@", 1)[0].lower() if parts else "")
                args = parts[1:]
                raw_override = ""
                m_show = _SHOW_ID_RE.match(q)
                if m_show:
                    cmd, args, raw_override = "show_id", [], q
                res = await _run_dashboard_command(cmd, args, raw_text=raw_override)
                dash_queries.complete(
                    qid, res.get("text") or "", kind="command",
                    error=res.get("error"))
                continue

            # Natural language → paid agent. Respect the same memory gate
            # the Telegram path uses; defer (re-queue) under pressure
            # rather than risk an OOM kill mid-answer.
            if _mem_pressure() >= _MEM_REFUSE_THRESHOLD:
                dash_queries.complete(
                    qid, "", kind="qa",
                    error="서버 메모리 사용량이 높아 지금은 답변할 수 없어요. "
                          "잠시 후 다시 시도해주세요.")
                continue
            try:
                result = await asyncio.wait_for(
                    agent.run(q, deep=False), timeout=_AGENT_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                dash_queries.complete(
                    qid, "", kind="qa",
                    error=f"응답이 {_AGENT_TIMEOUT_SEC // 60}분을 넘겨 중단했어요. "
                          "다시 시도해주세요.")
                continue
            result = result or {}
            text = result.get("text") or ""
            sources = result.get("sources") or []
            if result.get("error") and not text:
                dash_queries.complete(qid, "", kind="qa",
                                      error=str(result.get("error"))[:300])
                continue
            dash_queries.complete(qid, text, sources=sources, kind="qa")
            # Archive so the asked question also lands as a dashboard card
            # on the next regenerate, identical to a Telegram-asked one.
            try:
                qna.record(
                    chat_id=config.TELEGRAM_OWNER_ID,
                    question=q,
                    answer=text,
                    sources=sources,
                    tools=result.get("tool_calls") or [],
                    model=result.get("model"),
                    warning=result.get("warning"),
                )
            except Exception:
                log.exception("dash worker: qna.record failed")
        except Exception as e:
            log.exception("dash worker: failed for #%s", qid)
            try:
                dash_queries.complete(qid, "", error=f"오류: {str(e)[:200]}")
            except Exception:
                pass


async def _dash_query_purge(ctx: "ContextTypes.DEFAULT_TYPE") -> None:
    """Hourly: drop finished dashboard queries older than 6h."""
    try:
        n = dash_queries.purge_old(hours=6)
        if n:
            log.info("dash queries purged: %d", n)
    except Exception:
        log.exception("dash query purge failed")


def _build_dash_command_map() -> dict:
    """Auto-discover all cmd_* handlers so the dashboard can run every
    command the Telegram bot can. New handlers are picked up automatically
    — no manual list to maintain."""
    import sys
    mod = sys.modules[__name__]
    result = {}
    for name in dir(mod):
        if name.startswith("cmd_") and callable(getattr(mod, name, None)):
            cmd_name = name[4:]
            result[cmd_name] = getattr(mod, name)
    if "start" in result and "help" not in result:
        result["help"] = result["start"]
    return result


try:
    _DASH_COMMAND_HANDLERS = _build_dash_command_map()
except Exception:  # pragma: no cover — never let this block bot startup
    log.exception("dashboard command map build failed; web commands off")
    _DASH_COMMAND_HANDLERS = {}


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
    wiki.init()
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
    app.add_handler(CommandHandler("guide_lookup", cmd_guide_lookup))
    app.add_handler(CommandHandler("patents_guide", cmd_patents_guide))
    app.add_handler(CommandHandler("papers_guide", cmd_papers_guide))
    app.add_handler(CommandHandler("wiki_guide", cmd_wiki_guide))
    app.add_handler(CommandHandler(
        "search_papers_advanced", cmd_search_papers_advanced))
    app.add_handler(CommandHandler("paper_stats", cmd_paper_stats))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("eval", cmd_eval))
    app.add_handler(CommandHandler("eval_seed", cmd_eval_seed))
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
            r"|findnext:\d+"
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
        on_link_callback, pattern=r"^lnk:"
    ))
    app.add_handler(CallbackQueryHandler(
        on_ack_callback, pattern=r"^ack:"
    ))
    # Study-notes (체화) subsystem: /notes, /review + grade callbacks.
    # Dormant until STUDY_CHANNEL_ID is set, but the commands work now.
    try:
        from .notes import telegram as _notes_tg
        _notes_tg.register(app)
    except Exception:
        log.exception("study-notes handler registration failed (non-fatal)")
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("audit", cmd_audit))
    app.add_handler(CommandHandler("blocked_hosts", cmd_blocked_hosts))
    app.add_handler(CommandHandler("reset_blocked_hosts", cmd_reset_blocked_hosts))
    app.add_handler(CommandHandler("unignore", cmd_unignore))
    app.add_handler(CommandHandler("kg_extract", cmd_kg_extract))
    app.add_handler(CommandHandler("kg", cmd_kg))
    app.add_handler(CommandHandler("orphans", cmd_orphans))
    app.add_handler(CommandHandler("recover_orphans", cmd_recover_orphans))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("pending_ocr", cmd_pending_ocr))
    app.add_handler(CommandHandler("pending_links", cmd_pending_links))
    app.add_handler(CommandHandler("pending_pro", cmd_pending_pro))
    app.add_handler(CommandHandler("ocr_extend", cmd_ocr_extend))
    app.add_handler(CommandHandler("pending_approve_all", cmd_pending_approve_all))
    app.add_handler(CommandHandler(
        "pending_approve_all_confirm", cmd_pending_approve_all_confirm,
    ))
    app.add_handler(CommandHandler("pending_cancel_all", cmd_pending_cancel_all))
    app.add_handler(CommandHandler("queue_cancel_all", cmd_queue_cancel_all))
    app.add_handler(CommandHandler("queue_to_failed", cmd_queue_to_failed))
    app.add_handler(CommandHandler("queue_panic", cmd_queue_panic))
    app.add_handler(CommandHandler("cleanup", cmd_cleanup))
    app.add_handler(CommandHandler("cleanup_confirm", cmd_cleanup_confirm))
    app.add_handler(CommandHandler("youtube_restub_rescan", cmd_youtube_restub_rescan))
    app.add_handler(CommandHandler("fix_placeholder_titles", cmd_fix_placeholder_titles))
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
    app.add_handler(CommandHandler("search_patents", cmd_search_patents))
    app.add_handler(CommandHandler(
        "search_patents_advanced", cmd_search_patents_advanced))
    app.add_handler(CommandHandler("patent_stats", cmd_patent_stats))
    app.add_handler(CommandHandler("company_patents", cmd_company_patents))
    app.add_handler(CommandHandler("patent_detail", cmd_patent_detail))
    app.add_handler(CommandHandler("citing_patents", cmd_citing_patents))
    # 11 new KIPRIS Plus commands (pre-built 2026-05, await approval)
    app.add_handler(CommandHandler("kipris_search", cmd_kipris_search))
    app.add_handler(CommandHandler("kipris_pub", cmd_kipris_pub))
    app.add_handler(CommandHandler("kipris_reg", cmd_kipris_reg))
    app.add_handler(CommandHandler("kipris_inventor", cmd_kipris_inventor))
    app.add_handler(CommandHandler("kipris_status", cmd_kipris_status))
    app.add_handler(CommandHandler("kipris_family", cmd_kipris_family))
    app.add_handler(CommandHandler("kipris_claims", cmd_kipris_claims))
    app.add_handler(CommandHandler("kipris_priority", cmd_kipris_priority))
    app.add_handler(CommandHandler("kr_papers", cmd_kr_papers))
    app.add_handler(CommandHandler("kr_patents", cmd_kr_patents))
    app.add_handler(CommandHandler("kr_reports", cmd_kr_reports))
    app.add_handler(CommandHandler("kr_trends", cmd_kr_trends))
    app.add_handler(CommandHandler("kr_researcher", cmd_kr_researcher))
    app.add_handler(CommandHandler("kr_organ", cmd_kr_organ))
    app.add_handler(CommandHandler("kr_science_trend",
                                    cmd_kr_science_trend))
    app.add_handler(CommandHandler("kr_rnd_projects", cmd_kr_rnd_projects))
    app.add_handler(CommandHandler("kr_related", cmd_kr_related))
    app.add_handler(CommandHandler("kr_outcomes", cmd_kr_outcomes))
    app.add_handler(CommandHandler("kr_govt_reports", cmd_kr_govt_reports))
    app.add_handler(CommandHandler("kr_agency_rnd", cmd_kr_agency_rnd))
    app.add_handler(CommandHandler("kr_rnd_issues", cmd_kr_rnd_issues))
    app.add_handler(CommandHandler("web_search", cmd_web_search))
    app.add_handler(CommandHandler("ingest_url", cmd_ingest_url))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("wiki", cmd_wiki))
    app.add_handler(CommandHandler("wiki_today", cmd_wiki_today))
    app.add_handler(CommandHandler("wiki_recent", cmd_wiki_recent))
    app.add_handler(CommandHandler("wiki_new", cmd_wiki_new))
    app.add_handler(CommandHandler("wiki_lint", cmd_wiki_lint))
    app.add_handler(CommandHandler("wiki_status", cmd_wiki_status))
    app.add_handler(CommandHandler("wiki_cost", cmd_wiki_cost))
    app.add_handler(CommandHandler("wiki_run", cmd_wiki_run))
    app.add_handler(CommandHandler("wiki_backfill", cmd_wiki_backfill))
    app.add_handler(CommandHandler("wiki_pending", cmd_wiki_pending))
    app.add_handler(CommandHandler("wiki_failed", cmd_wiki_failed))
    app.add_handler(CommandHandler("wiki_off", cmd_wiki_off))
    app.add_handler(CommandHandler("wiki_on", cmd_wiki_on))
    app.add_handler(CommandHandler("wiki_drain", cmd_wiki_drain))
    app.add_handler(CommandHandler("wiki_split", cmd_wiki_split))
    app.add_handler(CommandHandler("wiki_rename", cmd_wiki_rename))
    app.add_handler(CommandHandler("wiki_delete", cmd_wiki_delete))
    app.add_handler(CommandHandler("wiki_dedup", cmd_wiki_dedup))

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
            # 15s (was 60s) so a dashboard 휴지통 delete drops off the list
            # within ~15s instead of lingering up to a minute. The DELETE
            # removes the qna.db row immediately, but the static index.html
            # only rebuilds on this tick — server.py can't regenerate inline
            # (chroma import → OOM in the 200MB dashboard container). regenerate()
            # is off-thread + lock-guarded + incremental, so 4× more ticks stay
            # cheap and skip whenever a prior run is still going.
            interval=15,
            first=20,
            name="refresh_dashboard",
        )
        # Dashboard search-box queries: drain every 2s (one per tick →
        # serial agent runs, can't stack and OOM). Hourly purge of old
        # finished rows keeps dash_queries.db tiny.
        app.job_queue.run_repeating(
            _dash_query_worker,
            interval=2,
            first=15,
            name="dash_query_worker",
        )
        app.job_queue.run_repeating(
            _dash_query_purge,
            interval=3600,
            first=1800,
            name="dash_query_purge",
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
            interval=180,
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
        # Hourly LLM-Wiki synthesis batch (P1/P3/P4). Registered always;
        # the callback no-ops unless WIKI_ENABLED=1, so toggling the env
        # never requires re-wiring jobs. run_repeating(1h) with `first`
        # anchored to the next top-of-hour KST so runs land on :00,
        # recomputed every boot so an auto-deploy can't drift the phase.
        # The daily ₩ budget still caps spend (today_cost resets at KST
        # midnight); once the cap is hit the remaining hourly runs no-op
        # until midnight. The "what it learned" digest is throttled to
        # once per KST day (wiki.digest_sent_today) so hourly learning
        # never becomes hourly pings.
        from datetime import timezone as _tz, timedelta as _td
        _kst = _tz(_td(hours=9))
        _now_kst = datetime.now(_kst)
        _next_hour = (_now_kst.replace(minute=0, second=0, microsecond=0)
                      + _td(hours=1))
        app.job_queue.run_repeating(
            _wiki_batch_job,
            interval=3600,
            first=(_next_hour - _now_kst).total_seconds(),
            name="wiki_batch",
        )
        # Resume wiki drain if temp budget survives a restart
        app.job_queue.run_once(_wiki_drain_resume, when=30,
                               name="wiki_drain_resume")
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
        # Every 60s: liveness heartbeat. Stamps data/bot_heartbeat on the
        # asyncio loop so the host watchdog (auto_pull.sh) can detect a
        # wedged loop (hung bot) that Docker's restart-on-exit misses,
        # and force-recreate the container. first=15 so a fresh boot
        # writes one promptly (watchdog's 5-min threshold tolerates the
        # gap regardless).
        app.job_queue.run_repeating(
            _write_heartbeat,
            interval=60,
            first=15,
            name="heartbeat",
        )
        # Post-deploy smoke test: ~3 min after boot (BM25 cache + reranker
        # warmed), run the real read-only hot path (retrieval + embed)
        # against the live corpus and report failures / perf regressions.
        # Catches the integration/scale bug class that local syntax +
        # stub tests structurally cannot (numpy-array truthiness, BM25
        # blocking at real corpus scale). Runs once per restart, i.e.
        # after every auto-deploy.
        app.job_queue.run_once(
            _startup_smoke, when=180, name="startup_smoke",
        )

        # One-shot: backfill/refresh 종류별 category (주식/공부/그외).
        # New notes get it inline from synth. Notes missing a category are
        # always classified. _NOTES_CAT_VERSION gates a *full* re-classify:
        # bump it whenever the classifier prompt changes and every note is
        # re-classified once on the next boot (cheap Flash-Lite, one call
        # each), then a marker file suppresses repeats.
        _NOTES_CAT_VERSION = 3  # v3: 주식=종목/회사분석 중심, 부동산·거시→그외
        async def _notes_category_backfill(_ctx):
            try:
                import json as _json
                from pathlib import Path as _P
                from .notes import store as _nstore, synth as _nsynth
                marker = config.DATA_DIR / "notes_cat_version.json"
                cur = 0
                try:
                    cur = int(_json.loads(
                        marker.read_text(encoding="utf-8")).get("v", 0))
                except Exception:
                    cur = 0
                if cur < _NOTES_CAT_VERSION:
                    # Classifier improved → re-classify all EXCEPT manual
                    # overrides (category_locked) so user picks survive.
                    targets = _nstore.notes_for_reclass()
                    mode = "reclassify-all"
                else:
                    targets = _nstore.notes_missing_category()  # fill gaps only
                    mode = "fill-missing"
                for m in targets:
                    md = ""
                    try:
                        md = _P(m["md_path"]).read_text(encoding="utf-8")
                    except Exception:
                        pass
                    cat = await _nsynth.classify_category(
                        m.get("title") or "", md)
                    _nstore.set_category(m["id"], cat)
                if targets:
                    log.info("notes category backfill (%s): %d note(s)",
                             mode, len(targets))
                if cur < _NOTES_CAT_VERSION:
                    try:
                        tmp = marker.with_suffix(".tmp")
                        tmp.write_text(_json.dumps({"v": _NOTES_CAT_VERSION}),
                                       encoding="utf-8")
                        os.replace(tmp, marker)
                    except Exception:
                        log.warning("notes cat version marker write failed")
            except Exception:
                log.exception("notes category backfill failed (non-fatal)")
        app.job_queue.run_once(
            _notes_category_backfill, when=90, name="notes_category_backfill",
        )
        # One-shot Telegram flood-ban release notification. Today's
        # 22207s ban (logged at 2026-05-14 01:28:56 UTC) lifts at
        # 2026-05-14 07:39:03 UTC. Schedule a single send_message at
        # 07:40 UTC + 60s grace; if the ban is lifted the user gets a
        # phone notification, otherwise the call fails silently and
        # the user just doesn't get the alarm. Safe to leave in code
        # — the run_date check below auto-skips when already past.
        ban_release_at = datetime(2026, 5, 14, 7, 40, 0, tzinfo=_tz.utc)
        if ban_release_at > datetime.now(_tz.utc):
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

    # Global error handler: without one, an unhandled handler exception
    # vanishes into PTB's logger and the user sees a command silently do
    # nothing — exactly how the /wiki datetime crash stayed invisible.
    # Notify the owner (rate-limited to one alert / 5 min so a crash in
    # a hot path can't flood Telegram).
    _err_last_notify = {"ts": 0.0}

    async def _on_handler_error(update_obj, context):
        err = context.error
        log.error("handler error: %s", err, exc_info=err)
        try:
            import time as _time
            now = _time.time()
            if now - _err_last_notify["ts"] < 300:
                return
            _err_last_notify["ts"] = now
            await context.bot.send_message(
                config.TELEGRAM_OWNER_ID,
                f"⚠️ 핸들러 오류: {_explain_error(err)}"[:1000],
            )
        except Exception:
            log.exception("error-handler notify failed")

    app.add_error_handler(_on_handler_error)

    _load_persisted_state()
    _load_dedup_confirmed()
    _load_permanently_ignored()
    # One-time hygiene: drop legacy empty-question 'expired' junk rows
    # (Pro-confirmation timeouts) that couldn't be deleted from the
    # dashboard before the next regen. Idempotent.
    try:
        _purged = qna.purge_expired()
        if _purged:
            log.info("startup: purged %d expired qna rows", _purged)
            from .dashboard import regenerate as _dash_regen
            _dash_regen.regenerate()
    except Exception:
        log.exception("startup qna purge_expired failed")
    # Stamp the heartbeat immediately so the freshly-booted container's
    # file is current before the cold BM25 warm-up below (which delays
    # the first 60s heartbeat tick) — prevents the watchdog from judging
    # a just-deployed bot as hung on the old, inherited stamp.
    _stamp_heartbeat()
    _cleanup_stale_bubbles_at_startup(app)
    _recover_orphan_files_at_startup(app)
    # Build / self-heal the FTS5 keyword index (hybrid retrieval's keyword
    # half) in the background so the first query and keyword recall are
    # ready without blocking startup. Skips fast when already in sync.
    import threading as _fts_th
    _fts_th.Thread(target=vector.fts_backfill, daemon=True).start()
    log.info("bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
