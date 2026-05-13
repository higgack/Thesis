import asyncio
import base64
import logging
import os
import re
import time
import uuid
from datetime import datetime
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
from .ingest import pipeline
from .agent import agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

_INGEST_SEM_CAPACITY = int(os.getenv("INGEST_SEM_CAPACITY", "4"))
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
_RETRY_INGEST_BATCH = 4
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

# Per-ingest tracking — populated while a semaphore slot is actually
# running work so /status can show filename + elapsed time. Earlier
# the status only knew "X/2 busy" with no visibility into WHICH file
# or how long. Critical when the user has just queued multiple 600+
# chunk PDFs and wants to know "is it still chewing or stuck?".
_ACTIVE_INGESTS: dict[str, dict] = {}


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
        if _RETRY_QUEUE_PATH.exists():
            data = json.loads(_RETRY_QUEUE_PATH.read_text(encoding="utf-8"))
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
        if _FAILED_LOG_PATH.exists():
            data = json.loads(_FAILED_LOG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                _INGEST_FAILED.extend(data[-_FAILED_MAX:])
                log.info("restored %d failed entries", len(_INGEST_FAILED))
    except Exception:
        log.exception("failed log load failed")
    try:
        if _HISTORY_PATH.exists():
            data = json.loads(_HISTORY_PATH.read_text(encoding="utf-8"))
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
    return sorted(
        (p for name, p in all_files.items() if name not in known),
        key=lambda p: p.name,
    )


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


def _persist_retry_queue() -> None:
    import json
    try:
        _RETRY_QUEUE_PATH.write_text(
            json.dumps(_INGEST_RETRY_QUEUE, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        log.exception("retry queue persist failed")


def _persist_chat_history() -> None:
    import json
    try:
        _HISTORY_PATH.write_text(
            json.dumps({str(k): v for k, v in _HISTORY.items()},
                       ensure_ascii=False),
            encoding="utf-8",
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
    import json
    try:
        _FAILED_LOG_PATH.write_text(
            json.dumps(_INGEST_FAILED, ensure_ascii=False),
            encoding="utf-8",
        )
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
    if update.effective_chat:
        await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)


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


_HELP_TEXT = """<b>🧠 SECOND BRAIN 봇 사용법</b>

<b>【1. 명령어】</b>
▸ 조회: /find &lt;키워드&gt; · /recent [N] · /stats · /status · /usage · /cost
▸ 대화: /reset (메모리 초기화)
▸ 장애: /failed · /failed_retry · /failed_clear · /queue
       /recover_orphans (디스크에 있지만 미학습 일괄 재학습)
       /queue_cancel_all (전체 큐·보류 일괄 취소)
▸ 보류 (5분 미선택 자동 보관):
       /pending · /pending_ocr &lt;N&gt; · /pending_pro &lt;N&gt;
       /pending_approve_all → /pending_approve_all_confirm (일괄 승인)
       /pending_cancel_all (일괄 취소)
       /ocr_extend &lt;doc_id|키워드&gt; (학습된 PDF OCR 추가 확장)
▸ 삭제: /forget &lt;id&gt; · /forget_search[_all] &lt;키워드&gt;
       /forget_qna[_search] &lt;id|키워드&gt;
       /dedupe → /dedupe_confirm  중복 doc (본문 가장 긴 것 1개만 유지)
       /cleanup → /cleanup_confirm  노이즈 doc (짧은 text 자료)
       /forget_forwards → /forget_forwards_confirm  자동 포워딩 디지스트
▸ 고급: /deep &lt;질문&gt; (Pro 모델 강제)
▸ /start /help

<b>【2. 핵심 원리】</b>
 • 채널 무엇이든 → 자동 수집·요약·임베딩·Obsidian
 • DM 자연어 → 에이전트가 도구 자동 선택
 • 메모리 7턴 (대명사 OK, /reset으로 초기화)
 • 쿼리 확장: 짧은 질문 자동 facet 분해
 • 비용·Q&amp;A SQLite 영구 누적 + 정적 대시보드
 • 임베딩 + reranker 모두 로컬 (Gemini 호출 X)
 • 답변 끝: (사용 자료 시점: 발행일 · 학습: YYYY.MM)
 • 분석성 질문은 자동 CoT + 반론·리스크 검토

<b>【3. 답변 출처 도구】</b>
 🧠 search_my_brain  저장 자료 단일 검색 (TOP_K 10)
 🧠 compare_papers  다수(50) 통합·비교 (20개+ Pro/Flash/취소 확인)
 🧠 recent_docs  최근 학습 목록
 📄 search_papers  외부 학술 (S2→arXiv)
 🌐 web_search  실시간 구글 (명시 시만)
 📥 ingest_url  URL 학습

<b>【4. 자연어 트리거】</b>
 🧠 brain — "삼성전기 MLCC 동향"
 🧠 compare — "정리/리뷰/통합/비교/전체"
 📄 papers — "찾아줘/추천/새로운/어떤 논문"
 🌐 web — "웹/구글/오늘/실시간/지금" 필수 ("최근/요즘"만으론 X)
 📥 ingest — "이거 학습해줘 URL" 또는 URL만
 후속 질문 — 대명사 OK

<b>【5. 자료 인입】</b>
 URL/PDF/PPTX/DOCX/XLSX/이미지/음성/YouTube/텍스트 그냥 보내기
 • PDF: 텍스트+OCR, 차트 많은 PDF 자동 Vision 10p (초과 시 확인)
 • 이미지: 캡션 ≥80자 / 짧으면 OCR / [OCR] 강제 병행
 • 음성: Gemini STT · YouTube: 자막→Jina fallback
 차단: LinkedIn/FB/IG/카스, Reuters/Bloomberg/WSJ/FT/NYT/WaPo

<b>【6. 자동 포워딩 (multi-channel)】</b>
 LISTEN_CHANNELS (콤마구분) 채널들을 동시 감지:
 [Noah 디지스트] 📋 TG 원문 fetch · 📰 Substack URL relay · 그 외 drop
 [LISTEN_PLAIN_CHANNELS] 본문 그대로 (URL line strip, 이미지 drop)
   · finter_gpt (머니터링 공시) · jubung (리포트) · awake_globalwatch (글로벌)
   · awake_realtimeCheck (52주↑) · Fundeasyearnings (실적/옵션)
 그 외 (잡담/일반) → drop
 큰 채널 백필: tmux + python -m src.scripts.import_channel &lt;ch&gt; --resume

<b>【7. 메타데이터 자동】</b>
 Flash-Lite 요약+메타 1콜 합침 (~₩0.5/doc, Stage 1 절감)
 🏢 회사 · 🏷 태그 · 📅 발행일 (YYYY.MM)
 → /find·중복알림·답변 출처에 표시

<b>【8. 웹 대시보드】</b>
 http://34.50.23.221:8082/1e68e9fae4e6fb1f8298bdee768eb73b/index.html
 Basic Auth: 사용자명/비밀번호 (.env 참조)
 통계 4장 · 검색 · 도구 칩 · 날짜별 접이식 · 1-탭 삭제 · 🔗 원본 링크
 60초 자동 갱신 · 19~07 KST 다크 자동

<b>【9. 답변 품질】</b>
 • 자료 시점 표기 필수 · 자료 부족 시 솔직히 "부족"
 • brain 우선 ("최근/요즘"만으로 web X → "웹에서/오늘" 필요)
 • 후속 질문도 brain 새로 검색 (메모리 only 금지)
 • web 결과는 [도메인]으로 인용
 • 인용은 자료 제목 (숫자 [1] X) · 본문 [N] 자동 매김

<b>【10. 운영 / 비용 (Stage 1+2 절감 적용)】</b>
 • VM: n2-standard-4 16GB · bot 12000m · Semaphore(env INGEST_SEM_CAPACITY)
 • 재시도 5회×90s → /failed
 • 영속: retry/failed/history/qna/cost/dashboard/hf_cache
 • 임베딩: 로컬 BGE-M3 1024-dim (₩0, sentence-transformers)
 • Reranker: 로컬 BGE-reranker-base (₩0)
 • LLM: Gemini Pro·Flash·Flash-Lite (API)
 • compare 20개+ Pro 확인 + PDF 10p+ OCR 확인
   (5분 미선택 → /pending 자동 보관)
 • 메모리 5분 청소 (90% 즉시·95% 거부)
 • 단가 (1M 토큰): Pro ₩1,750·Flash ₩420·Lite ₩140·Embed ₩0
 • 자동 복구 마커: /app/data/no_auto_recovery (queue_cancel_all 시 생성)

<b>【11. 트러블슈팅】</b>
 • "본문 비어있음" → 차단 도메인/paywall
 • 봇 응답 없음 → docker logs thesis-bot-1
 • brain 에러 → BM25 빌드 중, 30초 후
 • 답변 토픽 어긋남 → /reset
 • 비용 급등 → audio/Pro/web 다발 의심
 • 봇 메타글 학습 → digest mode 자동 차단
 • BGE-M3 모델: /app/data/hf_cache (2.3GB) 보존
 • 임베딩 backend 변경: .env EMBED_BACKEND=gemini|bge-m3"""


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    await _typing(update, ctx)
    await update.message.reply_text(
        _HELP_TEXT, parse_mode="HTML", disable_web_page_preview=True,
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
        f"\n📥 인입 진행: {ingest_busy}/{ingest_capacity}{ingest_detail}"
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

    # Daily average over the last 7 days (excluding empty days makes
    # the projection useless on a fresh deploy, so use simple mean).
    avg_7d = week["total_krw"] / 7 if week["total_krw"] else 0.0
    projected_monthly = avg_7d * 30

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
        f"\n\n📈 현재 페이스 → 월 예상치"
        f"\n  ₩{projected_monthly:,.0f}/월  (최근 7일 평균 × 30)"
        f"\n\n🎯 프로젝션 밴드 (Stage 1+2 + 6채널 expander 기준)"
        f"\n  AS IS  (digest 본문만):           ~₩5,600 ~ 9,100/월"
        f"\n  TO BE  (TG/Substack expand + 5plain): ~₩17,100 ~ 26,050/월"
        f"\n  BEFORE (절감 전):                 ~₩30,000 ~ 50,000/월"
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
    n = vector.delete_doc(doc_id)
    ok = meta.delete(doc_id)
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
        f"전부 삭제하려면: /cleanup confirm"
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
        f"전부 삭제하려면: /forget_forwards confirm"
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
        f"\n\n각 그룹에서 본문 가장 긴 것 1개만 남기고 삭제: /dedupe confirm"
    )


async def cmd_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Locate a saved doc by title fragment, return source URL / Obsidian
    path / first-line summary so the user can jump back to the original."""
    if not _is_owner(update):
        return
    await _typing(update, ctx)
    query = " ".join(ctx.args).strip()
    if not query:
        await update.message.reply_text("사용법: /find <제목 일부>")
        return
    matches = meta.search_broad(query, limit=30)
    if not matches:
        await update.message.reply_text(f"매칭 없음: '{query}'")
        return
    cap = 20
    header = f"🔍 '{query}' — {len(matches)}개 매칭"
    if len(matches) > cap:
        header += f" (상위 {cap}개 표시)"
    out = header
    LIMIT = 3800  # safety margin under Telegram 4096
    truncated = 0
    import json as _json
    for i, m in enumerate(matches[:cap]):
        title = _clean_text((m.get("title") or "(제목 없음)"))[:70]
        ingested = (m.get("ingested_at") or "")[:10]
        source = m.get("source") or ""
        obs = m.get("obsidian_path") or ""
        summary_full = _clean_text(m.get("summary") or "")
        summary = summary_full[:500]
        loc_bits = []
        if source.startswith(("http://", "https://")):
            loc_bits.append(f"📎 {source[:80]}")
        elif source:
            loc_bits.append(f"🆔 {source[:50]}")
        if obs:
            loc_bits.append(f"📁 {obs[:60]}")
        meta_bits = []
        meta_raw = m.get("metadata")
        if meta_raw:
            try:
                md = _json.loads(meta_raw)
            except Exception:
                md = {}
            if md.get("company"):
                meta_bits.append(f"🏢 {md['company']}")
            if md.get("tags"):
                meta_bits.append("🏷️ " + ", ".join(md["tags"][:5]))
            if md.get("report_date"):
                meta_bits.append(f"📅 {md['report_date']}")
        item = f"\n\n📄 {title} ({ingested})"
        if loc_bits:
            item += f"\n   {' · '.join(loc_bits)}"
        if meta_bits:
            item += f"\n   {' · '.join(meta_bits)}"
        if summary:
            item += f"\n   {summary}"
        if len(out) + len(item) > LIMIT:
            truncated = cap - i
            break
        out += item
    if truncated:
        out += f"\n\n…(나머지 {truncated}개는 길이 제한으로 생략. 키워드를 좁혀 다시 시도)"
    await update.message.reply_text(out, disable_web_page_preview=True)


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
    n = len(_INGEST_FAILED)
    _INGEST_FAILED.clear()
    _persist_failed_log()
    return f"실패 목록 비웠음 ({n}건)"


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
    recent = list(reversed(_INGEST_FAILED[-50:]))
    for i, r in enumerate(recent):
        ts = (r.get("ts", "")[:16]).replace("T", " ")
        title = _clean_text(r.get("title", "(unknown)"))[:90]
        status = r.get("status", "error")
        detail = _clean_text(r.get("detail", ""))[:120]
        icon = "❌" if status == "error" else "⚠️"
        item = f"\n\n{icon} {ts}\n   {title}"
        if detail and detail != title:
            item += f"\n   {detail}"
        if len(out) + len(item) > LIMIT:
            truncated = len(recent) - i
            break
        out += item
    if truncated:
        out += f"\n\n…(나머지 {truncated}개 생략)"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔁 일괄 재시도", callback_data="failed_retry"),
        InlineKeyboardButton("🗑 비우기", callback_data="failed_clear"),
    ]])
    await update.message.reply_text(
        out, disable_web_page_preview=True, reply_markup=keyboard,
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


async def cmd_recover_orphans(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Manual orphan-file rescan. Same logic as the startup hook —
    finds files on disk not present in meta.documents and pushes
    them onto the retry queue. Safe to run multiple times: the second
    run just enqueues anything that drifted since the last scan.

    Also clears the suppress marker so the next container boot's
    auto-scan resumes (a previous /queue_cancel_all may have set it).
    Running this command is the explicit 'I want orphan recovery
    back on' signal."""
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
    orphans = await asyncio.to_thread(_scan_orphan_files)
    if not orphans:
        msg = "✨ 미학습 파일 없음 — 모든 디스크 파일이 meta에 기록됨."
        if marker_was_present:
            msg += "\n🔓 자동 복구 다시 활성화됨 (재시작 시 자동 스캔 재개)"
        await update.message.reply_text(msg)
        return
    count = _enqueue_orphan_recovery(orphans, update.effective_chat.id)
    per_min = max(1, _RETRY_INGEST_BATCH * 60 // _RETRY_INGEST_INTERVAL_SEC)
    eta_min = max(1, count // per_min)
    preview = "\n".join(f"  • {p.name[:80]}" for p in orphans[:15])
    more = f"\n... 외 {len(orphans) - 15}건" if len(orphans) > 15 else ""
    resume_note = "\n🔓 자동 복구도 재활성화됨" if marker_was_present else ""
    await update.message.reply_text(
        f"🔄 {count}개 미학습 파일 → 재학습 큐에 추가됨{resume_note}\n"
        f"   {_RETRY_INGEST_INTERVAL_SEC}초 간격으로 최대 "
        f"{_RETRY_INGEST_BATCH}개씩 처리 (~{eta_min}분 소요 예상)\n"
        f"   /queue 로 진행 상황 확인 가능\n\n"
        f"{preview}{more}"
    )


async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """List all pending OCR / Pro decisions saved from expired
    inline-button prompts. Each item is numbered so the user can
    trigger /pending_ocr <N> or /pending_pro <N>."""
    if not _is_owner(update):
        return
    await _typing(update, ctx)
    ocr_items = await asyncio.to_thread(pending_store.list_ocr)
    pro_items = await asyncio.to_thread(pending_store.list_pro)
    if not ocr_items and not pro_items:
        await update.message.reply_text(
            "📭 검토 대기 항목 없음 — 모든 확인 prompt가 처리됨."
        )
        return
    lines = [f"📋 검토 대기 항목 ({len(ocr_items) + len(pro_items)}개)"]
    if ocr_items:
        lines.append(f"\n🔵 OCR 확장 가능 ({len(ocr_items)}개)")
        for it in ocr_items[:30]:
            remaining = max(0, it["total_pages"] - it["applied_pages"])
            est = max(10, remaining * 3)
            title = (it.get("title") or "(no title)")[:70]
            lines.append(
                f"  [{it['id']}] {title}\n"
                f"        {it['applied_pages']}/{it['total_pages']}p · "
                f"+{remaining}p 가능 (~₩{est})"
            )
        if len(ocr_items) > 30:
            lines.append(f"  ... 외 {len(ocr_items) - 30}건")
        lines.append("  → /pending_ocr <번호> 로 확장 시작")
    if pro_items:
        lines.append(f"\n🟣 Pro 합성 가능 ({len(pro_items)}개)")
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


async def cmd_ocr_extend(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Manual OCR extension for an already-ingested PDF.

    Same confirmation flow as the auto-trigger at ingest time:
      * compute page count + cost estimate
      * send inline buttons (✅ proceed · ❌ cancel)
      * 5-min TTL — unanswered prompts get promoted to /pending by
        the periodic _promote_expired_pending job (same code path
        as ingest-time OCR prompts).

    Reuses _PENDING_OCR + on_ocr_extend_callback infrastructure so a
    user tap goes straight into the existing extension pipeline."""
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
    _gc_pending_ocr()
    short = uuid.uuid4().hex[:24]
    _PENDING_OCR[short] = {
        "doc_id": doc_id,
        "pdf_path": str(pdf_path),
        "start_page": 1,
        "end_page": total_pages,
        "title": title,
        "chat_id": update.effective_chat.id,
        "ts": time.time(),
    }
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"📄 {total_pages}p OCR (~₩{est_cost})",
            callback_data=f"ocr:{short}:go",
        )],
        [InlineKeyboardButton(
            "❌ 취소",
            callback_data=f"ocr:{short}:skip",
        )],
    ])
    title_short = (title or fname)[:80]
    await update.message.reply_text(
        f"📊 OCR 확장 요청 — {title_short}\n"
        f"총 {total_pages}p (텍스트 충분한 페이지는 자동 skip)\n"
        f"예상 비용: ~₩{est_cost}\n\n"
        f"5분 안에 선택해주세요 (미응답 시 /pending 자동 보관).",
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
    await update.message.reply_text(
        f"✅ 일괄 삭제 · {forgotten}건 / 청크 {chunks_total}개 제거"
    )


# Single-token aliases so the usage guide can render destructive
# operations as one-tap; the original /dedupe and /cleanup still need
# 'confirm' typed manually as a guard, so these wrappers replicate
# that behavior with the arg pre-supplied.
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
_MAX_RETRY_ATTEMPTS = 5
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


def _format_sources_with_url(titles: list[str], cap: int | None = None) -> str:
    """Look up each cited doc title in meta and append the source URL
    if it is an http(s) link, so the user can click straight from the
    bot reply to the original article. By default lists every cited
    source — the chunked Telegram send handles oversized blocks."""
    formatted: list[str] = []
    items = titles if cap is None else titles[:cap]
    for title in items:
        try:
            matches = meta.search_title(title, limit=1)
        except Exception:
            matches = []
        url = ""
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


def _format_numbered_sources(labels: list[str]) -> str:
    """Build the numbered legend rendered after the answer body.
    URLs are looked up via meta.search_title so each cited label
    keeps its clickable source link when one exists."""
    if not labels:
        return ""
    lines: list[str] = []
    for i, label in enumerate(labels, 1):
        url = ""
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
    if ordered_labels:
        # Use the numbered legend built from inline citations — what
        # the user sees in body [1][2] matches the [1] [2] entries
        # below.
        suffix_lines.append("📚 출처:" + _format_numbered_sources(ordered_labels))
    elif result.get("sources"):
        # Tool was called but model didn't cite inline. Fall back to
        # the harvested source list so the user still gets a 출처
        # block.
        suffix_lines.append("📚 출처:" + _format_sources_with_url(result["sources"]))
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


_PENDING_SHORT_TO_FULL: dict[str, str] = {}

# Resumable Vision OCR — when a sparse-text PDF was capped at the
# auto-OCR limit (SPARSE_OCR_AUTO_CAP=20), we keep a 10-min handle on
# the file path + doc_id so the user can opt in to extending coverage
# via inline buttons. Keyed by the 24-char prefix of a uuid because
# Telegram callback_data caps at 64 bytes.
_PENDING_OCR: dict[str, dict] = {}
_PENDING_OCR_TTL_SEC = 300


def _gc_pending_ocr() -> list[dict]:
    """Pop expired OCR-extend prompts and return the popped state
    dicts so callers can promote them to the persistent /pending
    list. Returns [] when nothing expired."""
    now = time.time()
    expired_keys = [k for k, v in _PENDING_OCR.items()
                    if now - v.get("ts", 0) > _PENDING_OCR_TTL_SEC]
    expired: list[dict] = []
    for k in expired_keys:
        v = _PENDING_OCR.pop(k, None)
        if v:
            expired.append(v)
    return expired


async def _send_ocr_extend_prompts(ctx: ContextTypes.DEFAULT_TYPE,
                                   chat_id: int, results: list[dict]) -> None:
    """For each PDF result with capped Vision OCR, send a follow-up
    message with inline buttons offering to OCR the rest. Estimate
    is ~₩3 per Lite Vision page (rendered + extracted text)."""
    _gc_pending_ocr()
    for r in results:
        oc = r.get("ocr_meta")
        if not oc or not oc.get("capped"):
            continue
        applied = int(oc.get("applied_pages", 0))
        total = int(oc.get("total_pages", 0))
        remaining = max(0, total - applied)
        if remaining == 0:
            continue
        cost_est = max(10, remaining * 3)
        short = uuid.uuid4().hex[:24]
        _PENDING_OCR[short] = {
            "doc_id": r.get("doc_id"),
            "pdf_path": oc.get("pdf_path"),
            "start_page": applied + 1,
            "end_page": total,
            "title": r.get("title", ""),
            "chat_id": chat_id,
            "ts": time.time(),
        }
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"📄 나머지 {remaining}p 확장 OCR (최대 ~₩{cost_est})",
                callback_data=f"ocr:{short}:go",
            )],
            [InlineKeyboardButton(
                f"✅ {applied}p로 충분",
                callback_data=f"ocr:{short}:skip",
            )],
        ])
        title_short = (r.get("title") or "PDF")[:80]
        await ctx.bot.send_message(
            chat_id,
            f"📊 {title_short}\n"
            f"총 {total}p 중 {applied}p OCR 완료 (차트/표 본 자동 OCR).\n"
            f"딥리서치/장문 리포트라면 나머지도 OCR하면 검색 정확도 ↑.\n"
            f"※ 텍스트가 이미 충분한 페이지는 자동 스킵 → 실제 비용은 더 적을 수 있음.\n"
            f"5분 안에 선택해주세요 (미응답 시 /pending 으로 자동 이동).",
            reply_markup=kb,
        )


async def on_ocr_extend_callback(update: Update,
                                 ctx: ContextTypes.DEFAULT_TYPE):
    """Run pipeline.extend_pdf_ocr on user approval. Skip path just
    edits the message to confirm the no-op."""
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
    _, short, decision = parts
    _gc_pending_ocr()
    state = _PENDING_OCR.pop(short, None)
    if not state:
        try:
            await q.edit_message_text(
                "⚠️ 확인 요청이 만료됐습니다 (5분 초과). "
                "/pending 에서 확인할 수 있어요."
            )
        except Exception:
            pass
        return
    if decision == "skip":
        try:
            await q.edit_message_text(
                (q.message.text or "") + "\n\n→ ✅ 현재 OCR 분량으로 유지"
            )
        except Exception:
            pass
        return
    if decision != "go":
        return
    try:
        await q.edit_message_text(
            (q.message.text or "") +
            f"\n\n→ 📄 {state['start_page']}-{state['end_page']}p OCR 진행 중..."
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
    pdf_path = state.get("pdf_path") or ""
    if not pdf_path or not Path(pdf_path).exists():
        await q.message.reply_text(
            "⚠️ 원본 PDF 파일을 찾을 수 없습니다 (자동 정리됐을 수 있음). "
            "동일 PDF를 다시 보내주세요."
        )
        return
    typing_task = asyncio.create_task(_sustained_typing(update, ctx))
    try:
        try:
            r = await pipeline.extend_pdf_ocr(
                Path(pdf_path), state["doc_id"],
                int(state["start_page"]), int(state["end_page"]),
            )
        except Exception as e:
            log.exception("extend_pdf_ocr failed")
            await q.message.reply_text(f"⚠️ OCR 확장 실패: {_explain_error(e)}")
            return
    finally:
        typing_task.cancel()
    title_short = (state.get("title") or "PDF")[:80]
    if r.get("status") == "ok":
        skip_note = (f" · {r['pages_skipped']}p 텍스트 충분 스킵"
                     if r.get("pages_skipped") else "")
        await q.message.reply_text(
            f"✅ {title_short}\n"
            f"   +{r['pages_ocrd']}p Vision OCR{skip_note} · "
            f"+{r['chunks_added']} 청크"
        )
    elif r.get("status") == "empty":
        skipped = r.get("pages_skipped", 0)
        if skipped:
            await q.message.reply_text(
                f"✅ {title_short}\n"
                f"   {skipped}p 모두 텍스트 충분 → OCR 스킵 (추가 청크 없음, 비용 0)"
            )
        else:
            await q.message.reply_text(f"⚠️ OCR 결과 없음: {title_short}")
    else:
        await q.message.reply_text(f"⚠️ OCR 결과 없음: {title_short}")


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


_INGEST_TIMEOUT_SEC = 900  # 15 minutes per message — large PDFs with OCR can take this long


_LIVE_EDIT_INTERVAL = 15  # seconds between status edits


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
    seconds so the user can see elapsed time tick forward. Silently
    swallows Telegram errors (rate limit, message-too-old, etc.) so
    the background task never crashes ingest."""
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
        try:
            await ctx.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=f"⏳ {short_label}\n   처리 중 ({_fmt_elapsed(elapsed)})",
            )
        except asyncio.CancelledError:
            return
        except Exception:
            pass


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

        status_msg_id: int | None = None
        try:
            sent = await ctx.bot.send_message(
                notify_chat_id,
                f"⏳ 학습 시작: {label[:80]}",
            )
            status_msg_id = sent.message_id
            _ACTIVE_INGESTS[job_id]["status_msg_id"] = status_msg_id
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
                _ingest_message_locked(msg, ctx, notify_chat_id),
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

        # All-duplicate batches produce an empty final_text from
        # _format_results. Skip the user-facing send (and clean up the
        # ⏳ bubble) so backfill / forwarded channel storms stay silent.
        if not (final_text or "").strip():
            if status_msg_id:
                try:
                    await ctx.bot.delete_message(
                        chat_id=notify_chat_id, message_id=status_msg_id,
                    )
                except Exception:
                    pass
            sent_ok = True
        else:
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


async def _ingest_doc_attachment(msg, ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    file = await ctx.bot.get_file(msg.document.file_id)
    dest = Path(config.DATA_DIR) / "files" / msg.document.file_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    await file.download_to_drive(custom_path=dest)
    label = f"tg-doc:{msg.document.file_unique_id}:{msg.document.file_name}"
    suffix = dest.suffix.lower()
    if suffix == ".pdf":
        return await pipeline.ingest_pdf(dest, label)
    if suffix == ".pptx":
        return await pipeline.ingest_pptx(dest, label)
    if suffix == ".docx":
        return await pipeline.ingest_docx(dest, label)
    if suffix == ".xlsx":
        return await pipeline.ingest_xlsx(dest, label)
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
# .ppt / .ipynb / .zip can't burn cycles failing in a loop. Text
# fallback (.txt/.md/.csv) is intentionally narrow — random binary
# extensions would otherwise slip through the 'else: ingest_text'
# branch and produce garbage chunks.
_SUPPORTED_INGEST_SUFFIXES: frozenset[str] = frozenset({
    ".pdf", ".pptx", ".docx", ".xlsx",
    ".txt", ".md", ".csv",
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


async def _ingest_message_locked(msg, ctx: ContextTypes.DEFAULT_TYPE, notify_chat_id: int):
    text = msg.text or msg.caption or ""
    results = []

    if msg.document:
        try:
            results.append(await _ingest_doc_attachment(msg, ctx))
        except Exception as e:
            log.exception("file ingest failed")
            if _is_retryable(e):
                _INGEST_RETRY_QUEUE.append({
                    "kind": "doc",
                    "file_id": msg.document.file_id,
                    "file_unique_id": msg.document.file_unique_id,
                    "file_name": msg.document.file_name,
                    "chat_id": notify_chat_id,
                    "attempts": 0,
                })
                _persist_retry_queue()
                results.append({"status": "queued",
                                "title": msg.document.file_name})
            else:
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
            try:
                rcap = await pipeline.ingest_text(
                    cap, f"tg-doc-caption:{msg.message_id}",
                )
                if rcap.get("status") in ("empty", "error"):
                    rcap["retry_payload"] = cap_retry
                results.append(rcap)
            except Exception as e:
                log.exception("doc caption ingest failed")
                if _is_retryable(e):
                    _INGEST_RETRY_QUEUE.append({
                        **cap_retry, "chat_id": notify_chat_id, "attempts": 0,
                    })
                    _persist_retry_queue()
                    results.append({"status": "queued", "title": cap[:60]})
                else:
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
        try:
            r = await _ingest_photo_attachment(msg, ctx)
            if r.get("status") in ("empty", "error"):
                r["retry_payload"] = photo_retry
            results.append(r)
        except Exception as e:
            log.exception("photo ingest failed")
            if _is_retryable(e):
                _INGEST_RETRY_QUEUE.append({
                    **photo_retry, "chat_id": notify_chat_id, "attempts": 0,
                })
                _persist_retry_queue()
                results.append({"status": "queued", "title": "photo"})
            else:
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
        try:
            r = await _ingest_voice_attachment(msg, ctx)
            if r.get("status") in ("empty", "error"):
                r["retry_payload"] = voice_retry
            results.append(r)
        except Exception as e:
            log.exception("voice ingest failed")
            if _is_retryable(e):
                _INGEST_RETRY_QUEUE.append({
                    **voice_retry, "chat_id": notify_chat_id, "attempts": 0,
                })
                _persist_retry_queue()
                results.append({"status": "queued", "title": "voice"})
            else:
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
        try:
            r = await _ingest_audio_attachment(msg, ctx)
            if r.get("status") in ("empty", "error"):
                r["retry_payload"] = audio_retry
            results.append(r)
        except Exception as e:
            log.exception("audio ingest failed")
            if _is_retryable(e):
                _INGEST_RETRY_QUEUE.append({
                    **audio_retry, "chat_id": notify_chat_id, "attempts": 0,
                })
                _persist_retry_queue()
                results.append({"status": "queued",
                                "title": audio.file_name or "audio"})
            else:
                results.append({"status": "error",
                                "error": _explain_error(e),
                                "retry_payload": audio_retry})

    urls, plain = _collect_message_urls(msg)
    for url in urls:
        # Skip URLs that have already burned through their retry budget
        # in a prior digest — paywalled domains (Reuters etc.) and broken
        # shorteners stay broken, no point spending another 5 attempts.
        if _url_in_failed_log(url):
            log.info("url skip — previously failed: %s", url[:120])
            results.append({"status": "skipped", "title": url,
                            "source": url,
                            "detail": "이전에 실패한 URL — 자동 skip"})
            continue
        url_retry = {"kind": "url", "url": url}
        try:
            r = await pipeline.ingest_url(url)
            r.setdefault("source", url)
            r.setdefault("title", url)
            if r.get("status") in ("empty", "error"):
                r["retry_payload"] = url_retry
            results.append(r)
        except Exception as e:
            log.exception("url ingest failed: %s", url)
            if _is_retryable(e):
                _INGEST_RETRY_QUEUE.append({
                    **url_retry, "chat_id": notify_chat_id, "attempts": 0,
                })
                _persist_retry_queue()
                results.append({"status": "queued", "title": url})
            else:
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
        try:
            r = await pipeline.ingest_text(plain, f"tg-msg:{msg.message_id}")
            if r.get("status") in ("empty", "error"):
                r["retry_payload"] = text_retry
            results.append(r)
        except Exception as e:
            log.exception("text ingest failed")
            if _is_retryable(e):
                _INGEST_RETRY_QUEUE.append({
                    **text_retry, "chat_id": notify_chat_id, "attempts": 0,
                })
                _persist_retry_queue()
                results.append({"status": "queued", "title": plain[:60]})
            else:
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
    /failed retry re-enqueue the item automatically."""
    entry: dict = {
        "status": status,
        "title": (title or "(unknown)")[:140],
        "detail": (detail or "")[:200],
        "ts": datetime.utcnow().isoformat(),
    }
    if retry_payload:
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
            # Silent — forwarded digests and channel relays re-cite the
            # same upstream URLs constantly, and a '♻️ 이미 있음' line per
            # dedup hit floods the chat (hundreds per digest expansion).
            # User intent for a manual upload is satisfied by the bot's
            # absence of an error reply — if they care they can check
            # /recent or /find.
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
    """Move OCR + Pro inline-button prompts that hit their 10-min
    TTL into the persistent /pending list so the user can decide
    later. Runs every minute. Skips silently when nothing expired."""
    from .agent import agent as agent_mod
    try:
        ocr_expired = _gc_pending_ocr()
        for state in ocr_expired:
            try:
                pending_store.add_ocr(
                    chat_id=int(state.get("chat_id") or config.TELEGRAM_OWNER_ID),
                    doc_id=str(state.get("doc_id") or ""),
                    title=str(state.get("title") or "")[:200],
                    pdf_path=str(state.get("pdf_path") or ""),
                    applied_pages=int(state.get("start_page", 1)) - 1,
                    total_pages=int(state.get("end_page", 0) or 0),
                )
            except Exception:
                log.exception("promote ocr to pending failed")
        if ocr_expired:
            log.info("promoted %d OCR prompts to /pending", len(ocr_expired))
    except Exception:
        log.exception("OCR pending promotion failed")
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


def _pop_eligible_retry_item() -> dict | None:
    """Walk the retry queue from the front and pop the first item whose
    not_before_ts (set on previous failure) has elapsed. Items still in
    backoff stay in place so other queue items keep flowing. Returns
    None when nothing is currently eligible."""
    now = time.time()
    for i, candidate in enumerate(_INGEST_RETRY_QUEUE):
        nb = candidate.get("not_before_ts")
        if nb is None or nb <= now:
            return _INGEST_RETRY_QUEUE.pop(i)
    return None


async def _retry_pending_ingest(ctx: ContextTypes.DEFAULT_TYPE):
    """Drain one queued ingest, sharing the same semaphore as live
    ingests so total concurrent ingests stays bounded."""
    if not _INGEST_RETRY_QUEUE:
        return
    item = _pop_eligible_retry_item()
    if item is None:
        return  # everything currently in backoff
    _persist_retry_queue()
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
                return
    async with _INGEST_SEM:
        retry_job_id = _register_ingest(
            f"[재시도] {title}", item.get("kind", "retry"), chat_id,
        )

        # Retry-queue ingests intentionally skip the ⏳-bubble + live
        # status updater. With 4 concurrent retries + a 100+ item queue
        # the editMessageText cadence (every few seconds × N tasks)
        # saturated the local telegram-bot-api proxy and starved the
        # event loop, blocking command handlers for minutes. The user
        # gets a single final ✅/❌ summary at the bottom of this
        # function instead, which is plenty for backfill / orphan
        # recovery flows where they're not watching individual items.
        status_msg_id: int | None = None
        updater_task = None

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
                    r = await pipeline.ingest_pdf(dest, label)
                elif suffix == ".pptx":
                    r = await pipeline.ingest_pptx(dest, label)
                elif suffix == ".docx":
                    r = await pipeline.ingest_docx(dest, label)
                elif suffix == ".xlsx":
                    r = await pipeline.ingest_xlsx(dest, label)
                elif suffix in _AUDIO_SUFFIX_MIME:
                    r = await pipeline.ingest_audio(
                        dest.read_bytes(), label,
                        mime_type=_AUDIO_SUFFIX_MIME[suffix],
                    )
                else:
                    content = dest.read_text(encoding="utf-8", errors="ignore")
                    r = await pipeline.ingest_text(content, label)
            elif kind == "url":
                r = await pipeline.ingest_url(item["url"])
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
                    return
                label = f"local:{dest.name}"
                suffix = dest.suffix.lower()
                if suffix == ".pdf":
                    r = await pipeline.ingest_pdf(dest, label)
                elif suffix == ".pptx":
                    r = await pipeline.ingest_pptx(dest, label)
                elif suffix == ".docx":
                    r = await pipeline.ingest_docx(dest, label)
                elif suffix == ".xlsx":
                    r = await pipeline.ingest_xlsx(dest, label)
                elif suffix in _AUDIO_SUFFIX_MIME:
                    r = await pipeline.ingest_audio(
                        dest.read_bytes(), label,
                        mime_type=_AUDIO_SUFFIX_MIME[suffix],
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
                        return
                    r = await pipeline.ingest_text(content, label)
            else:
                return
        except Exception as e:
            item["attempts"] += 1
            if item["attempts"] >= _MAX_RETRY_ATTEMPTS or not _is_retryable(e):
                # Persist to /failed so the user can /failed retry later.
                payload = {k: v for k, v in item.items() if k != "chat_id"}
                _record_failure(
                    "error", title[:140], _explain_error(e),
                    retry_payload=payload,
                )
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
            item["not_before_ts"] = time.time() + hold
            _INGEST_RETRY_QUEUE.append(item)
            _persist_retry_queue()
            # No Telegram ping per soft-fail — the user doesn't need to
            # see "wait 1h" messages stacking up during a Gemini outage.
            # /status shows queue depth, /failed shows the eventual
            # permanent failures.
            log.info(
                "retry soft-fail %d/%d (hold %dm): %s — %s",
                item["attempts"], _MAX_RETRY_ATTEMPTS, max(1, hold // 60),
                title[:80], _explain_error(e),
            )
            return
        finally:
            if updater_task:
                updater_task.cancel()
                try:
                    await updater_task
                except asyncio.CancelledError:
                    pass
            _unregister_ingest(retry_job_id)
    # Visibility policy for drained retry items:
    #   - ok (newly learned)     → send '✅ title (chunks)' so the user
    #                              sees real progress through the queue.
    #   - duplicate              → silent (forwarded digests re-cite
    #                              the same URLs hundreds of times).
    #   - empty / error / other  → send so the user can see what's
    #                              stuck even after the silent backoff
    #                              messages above suppress the soft
    #                              fails.
    if r:
        s = r.get("status")
        if s == "ok":
            summary = _format_results([r])
            if summary.strip():
                await _edit_or_send(
                    ctx, chat_id, None, f"⏰ {summary}",
                )
        elif s not in ("duplicate",):
            summary = _format_results([r])
            if summary.strip():
                await _edit_or_send(
                    ctx, chat_id, None, f"⏰ {summary}",
                )
    log.info("retry done [%s]: %s",
             (r or {}).get("status", "unknown"), title[:80])


def main():
    meta.init()
    obsidian.init()
    pending_store.init()
    request = HTTPXRequest(
        connect_timeout=15.0,
        read_timeout=180.0,
        write_timeout=180.0,
        pool_timeout=15.0,
        connection_pool_size=8,
    )
    builder = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .request(request)
        .get_updates_request(request)
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
    app.add_handler(CommandHandler("failed", cmd_failed))
    app.add_handler(CommandHandler("failed_retry", cmd_failed_retry))
    app.add_handler(CommandHandler("failed_clear", cmd_failed_clear))
    app.add_handler(CallbackQueryHandler(
        on_callback_query, pattern=r"^failed_(retry|clear)$"
    ))
    app.add_handler(CallbackQueryHandler(
        on_pro_confirmation_callback, pattern=r"^pro:"
    ))
    app.add_handler(CallbackQueryHandler(
        on_ocr_extend_callback, pattern=r"^ocr:"
    ))
    app.add_handler(CommandHandler("queue", cmd_queue))
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

    _load_persisted_state()
    _recover_orphan_files_at_startup(app)
    vector.warm_bm25_cache()  # background scan; first query stays fast
    log.info("bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
