"""LLM Wiki layer — synthesis/accumulation on top of the existing RAG.

Background (why this exists)
----------------------------
The base system is classic RAG: every question re-retrieves chunks and
re-assembles an answer from scratch — nothing accumulates. Karpathy's
"LLM Wiki" pattern flips that: the LLM reads each new source and *merges*
it into a persistent, cross-referenced markdown wiki, so knowledge
compounds and repeat questions answer from pre-synthesized pages.

This module is that wiki layer. It is **purely additive and dormant by
default** — every entry point short-circuits unless `WIKI_ENABLED=1`
(env) AND a runtime kill-switch file is absent AND the Obsidian vault is
configured. That makes the feature:
  • zero-risk to the existing pipeline when off (the default),
  • cost-controlled when on (importance gate + per-run caps + flash +
    a hard daily ₩ budget circuit breaker),
  • instantly reversible (`/wiki_off` drops a kill-switch file; no
    redeploy), and it never touches Chroma / meta.db, so the core RAG
    corpus is unaffected whatever the wiki does.

Design choices that keep cost + risk low
-----------------------------------------
1. **No inline synthesis.** Ingest only *enqueues* a doc (a cheap local
   JSON append, no LLM, no network). All LLM merges happen in ONE nightly
   batch so a busy ingest hour never multiplies LLM calls.
2. **Free topic routing.** Pages are keyed by the company / tag metadata
   already extracted at ingest (₩0) — no extra embedding call, and the
   page names are human-readable (`삼성전자.md`, `HBM.md`).
3. **Hard caps + daily budget.** Per run: max topics, max docs/topic,
   existing-page char cap, output-token cap. Across a KST day:
   `WIKI_DAILY_BUDGET_KRW` — once today's wiki spend reaches it the batch
   BLOCKS (queued docs wait for tomorrow) and the caller fires an alert.
   The importance gate skips short/low-signal docs entirely.
4. **flash, not pro.** Merge uses WIKI_MERGE_MODEL (default ANSWER_MODEL
   = flash). Every call is tagged `purpose="wiki"` so /usage + cost.db
   show the wiki's real spend, separated from ingest/query.

State files (all atomic tmp→replace, .bak fallback), under DATA_DIR:
  wiki_queue.json     — docs awaiting the next batch (deduped by doc_id)
  wiki_index.json     — topic → {file, title, doc_ids, updated, claims}
  wiki_last_run.json  — summary of the last batch (powers /wiki_today)
  wiki_disabled       — presence = runtime kill-switch (set by /wiki_off)

Wiki pages live in the Obsidian vault at SecondBrain/Wiki/<topic>.md and
are committed to the vault git repo (versioning + one-command rollback),
reusing obsidian.py's git plumbing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config

_KST = timezone(timedelta(hours=9))


def _now_kst_iso() -> str:
    return datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S")
from . import cost, obsidian

log = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Config flags (read defensively so a missing config attr can't crash an
# import; config.py defines the real defaults).
# ----------------------------------------------------------------------

def _flag(name: str, default):
    return getattr(config, name, default)


_QUEUE_PATH = config.DATA_DIR / "wiki_queue.json"
_INDEX_PATH = config.DATA_DIR / "wiki_index.json"
_LASTRUN_PATH = config.DATA_DIR / "wiki_last_run.json"
_LINT_PATH = config.DATA_DIR / "wiki_lint.json"
_FAILED_PATH = config.DATA_DIR / "wiki_failed.json"
_ALIASES_PATH = config.DATA_DIR / "wiki_aliases.json"
_DELETED_TOPICS_PATH = config.DATA_DIR / "wiki_deleted_topics.json"
_KILL_PATH = config.DATA_DIR / "wiki_disabled"


def _wiki_dir() -> Path | None:
    """<vault>/SecondBrain/Wiki — None when no vault is configured."""
    if not config.OBSIDIAN_VAULT_PATH:
        return None
    return Path(config.OBSIDIAN_VAULT_PATH).resolve() / "SecondBrain" / "Wiki"


# ----------------------------------------------------------------------
# Enable / kill-switch
# ----------------------------------------------------------------------

def is_disabled() -> bool:
    """Runtime kill-switch — flipped by /wiki_off without a redeploy."""
    try:
        return _KILL_PATH.exists()
    except Exception:
        return False


def set_disabled(disabled: bool) -> None:
    try:
        if disabled:
            _KILL_PATH.write_text(datetime.utcnow().isoformat(), encoding="utf-8")
        elif _KILL_PATH.exists():
            _KILL_PATH.unlink()
    except Exception:
        log.exception("wiki kill-switch toggle failed")


def enabled() -> bool:
    """Master gate. False (dormant) unless explicitly turned on AND a
    vault exists AND the runtime kill-switch is absent. Checked at every
    entry point so the feature is genuinely inert by default."""
    return (
        bool(_flag("WIKI_ENABLED", False))
        and obsidian.enabled()
        and not is_disabled()
    )


def query_first_enabled() -> bool:
    """Separate, stricter gate for the answer path (P2). Even when the
    nightly builder is on, the user can keep Q&A on the proven RAG path
    until the pages look trustworthy."""
    return enabled() and bool(_flag("WIKI_QUERY_FIRST", False))


# ----------------------------------------------------------------------
# Daily spend circuit breaker (KST)
# ----------------------------------------------------------------------

def today_cost_krw() -> float:
    """Today's (KST) wiki LLM spend, ₩. Reads the shared cost ledger
    filtered to purpose='wiki'. cost.today_krw() buckets by KST, so this
    resets at KST midnight — which is exactly when a blocked batch should
    resume. Used by the budget breaker + /wiki_status."""
    try:
        return float(cost.today_krw().get("by_purpose", {})
                     .get("wiki", {}).get("cost", 0.0))
    except Exception:
        return 0.0


def month_cost_krw() -> float:
    try:
        return float(cost.month_to_date_krw().get("by_purpose", {})
                     .get("wiki", {}).get("cost", 0.0))
    except Exception:
        return 0.0


def total_cost_krw() -> float:
    try:
        return float(cost.all_time_krw().get("by_purpose", {})
                     .get("wiki", {}).get("cost", 0.0))
    except Exception:
        return 0.0


def budget_krw() -> float:
    override = _read_temp_budget()
    if override is not None:
        return override
    return float(_flag("WIKI_DAILY_BUDGET_KRW", 1000))


def _read_temp_budget() -> float | None:
    path = config.DATA_DIR / "wiki_budget_temp.json"
    if not path.exists():
        return None
    try:
        from datetime import datetime as _dt, timedelta, timezone
        kst = timezone(timedelta(hours=9))
        today = _dt.now(kst).strftime("%Y-%m-%d")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("date") == today:
            return float(data["budget"])
        path.unlink(missing_ok=True)
        return None
    except Exception:
        return None


def set_temp_budget(amount: float) -> None:
    from datetime import datetime as _dt, timedelta, timezone
    kst = timezone(timedelta(hours=9))
    today = _dt.now(kst).strftime("%Y-%m-%d")
    _atomic_write_json(config.DATA_DIR / "wiki_budget_temp.json",
                       {"budget": amount, "date": today})


def clear_temp_budget() -> None:
    """Drop the temp-budget override so the daily cap reverts to the
    config default immediately (not at KST midnight). Called when a drain
    turn finishes — if the bot is killed mid-drain this never runs, so the
    file survives and _wiki_drain_resume picks the drain back up."""
    try:
        (config.DATA_DIR / "wiki_budget_temp.json").unlink(missing_ok=True)
    except Exception:
        log.exception("clear_temp_budget failed")


def budget_exceeded() -> bool:
    """True when today's (KST) wiki spend has reached the daily cap.
    0 disables the cap."""
    b = budget_krw()
    return b > 0 and today_cost_krw() >= b


# Digest throttle: the synthesis batch now runs hourly (on the hour), but
# the "what it learned" Telegram digest must stay at most once per KST day
# so hourly learning never turns into hourly pings. State is a tiny dated
# stamp; a wiped/corrupt file fails open (one extra digest, never a loop).
_DIGEST_STATE_PATH = config.DATA_DIR / "wiki_last_digest.json"


def digest_sent_today() -> bool:
    """True if the once-daily wiki digest already went out this KST day."""
    try:
        data = _load_json(_DIGEST_STATE_PATH, {})
        return isinstance(data, dict) and data.get("date") == _now_kst_iso()[:10]
    except Exception:
        return False


def mark_digest_sent() -> None:
    """Stamp today's KST date so further hourly runs skip the digest."""
    try:
        _atomic_write_json(_DIGEST_STATE_PATH, {"date": _now_kst_iso()[:10]})
    except Exception:
        log.exception("wiki mark_digest_sent failed")


# ----------------------------------------------------------------------
# Atomic JSON state (matches the repo's tmp→replace + .bak convention)
# ----------------------------------------------------------------------

def _atomic_write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    bak = path.with_suffix(path.suffix + ".bak")
    # fsync before replace: without it a host crash can replace the file
    # with an unsynced (possibly empty) tmp. copy2 instead of moving the
    # live file to .bak: the old move left a window where `path` didn't
    # exist at all, and concurrent readers (dashboard thread, backfill
    # thread) saw "missing → empty default" mid-cycle.
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False))
        f.flush()
        os.fsync(f.fileno())
    if path.exists():
        try:
            shutil.copy2(path, bak)
        except OSError:
            pass
    os.replace(tmp, path)


def _load_json(path: Path, default):
    if not path.exists():
        # Crash between the old move-to-.bak and replace could leave only
        # the .bak behind — recover from it instead of silently returning
        # an empty default that the next write would persist.
        bak = path.with_suffix(path.suffix + ".bak")
        if bak.exists():
            try:
                return json.loads(bak.read_text(encoding="utf-8"))
            except Exception:
                pass
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("wiki state load failed (%s); trying .bak", e)
        bak = path.with_suffix(path.suffix + ".bak")
        if bak.exists():
            try:
                return json.loads(bak.read_text(encoding="utf-8"))
            except Exception:
                pass
        return default


# Read-mostly JSON cache keyed by file mtime. resolve_topic /
# _is_known_topic / is_topic_deleted / _load_aliases re-parsed the
# (multi-MB at 1000 topics) index from disk on EVERY call — per queue
# item in run_batch's grouping loop (on the event loop!), per ingested
# doc via topics_for, 32.5k× during a backfill. Results MUST be treated
# as read-only by callers; mutators load fresh via _load_json.
_JSON_CACHE: dict[str, tuple[int, object]] = {}
_JSON_CACHE_LOCK = threading.Lock()

# Serialises read-modify-write cycles on wiki_queue.json across the
# event loop (enqueue, run_batch persists) and worker threads (backfill
# runs via asyncio.to_thread) — without it a minutes-long threaded
# backfill rewrote the queue from a stale snapshot, silently dropping
# entries enqueued by live ingest in the meantime.
_QUEUE_LOCK = threading.Lock()


def _load_json_cached(path: Path, default):
    try:
        mt = path.stat().st_mtime_ns
    except OSError:
        return default
    key = str(path)
    with _JSON_CACHE_LOCK:
        ent = _JSON_CACHE.get(key)
        if ent is not None and ent[0] == mt:
            return ent[1]
    data = _load_json(path, default)
    with _JSON_CACHE_LOCK:
        _JSON_CACHE[key] = (mt, data)
    return data


# ----------------------------------------------------------------------
# Topic routing (FREE — reuses ingest-time metadata, no LLM/embedding)
# ----------------------------------------------------------------------

_SAFE = re.compile(r"[^\w가-힣\- ]+")


def _slug(s: str) -> str:
    s = _SAFE.sub(" ", s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s[:80] or "기타"


_CORP_TOKENS = frozenset({
    "inc", "corp", "corporation", "ltd", "limited", "co", "company",
    "llc", "plc", "ag", "sa", "se", "group", "holdings", "pbc", "nv",
    "association", "institute", "foundation",
})


def _is_known_topic(name: str) -> bool:
    """True if `name` already exists as a wiki topic (index or alias),
    matched loosely via dedup-key. Used to gate single-space splitting:
    we only break 'A B' into ['A','B'] when BOTH halves are already
    topics, so '삼성전자 SK하이닉스' splits but 'Applied Materials' (no
    'Applied'/'Materials' topic) stays whole."""
    name = (name or "").strip()
    if not name:
        return False
    if name in _load_aliases():
        return True
    idx = _load_json_cached(_INDEX_PATH, {})
    if not isinstance(idx, dict) or not idx:
        return False
    if name in idx:
        return True
    k = _dedup_key(name)
    if not k:
        return False
    return any(_dedup_key(e) == k for e in idx)


def _split_multi_topic(raw: str) -> list[str]:
    """Split a multi-company/topic string into individual names.
    Handles: '삼성전자, SK하이닉스' / '삼성전자 SK하이닉스' /
    '넥스틴, 파크시스템스, 인텍플러스'.
    Corporate suffixes (Inc, Corp, Ltd, …) are merged back with the
    preceding token so 'Broadcom Inc' stays as one entity.
    Single-space splitting only fires when every fragment is ALREADY a
    known topic — otherwise multi-word single names ('Applied Materials',
    '어플라이드 머티리얼즈') wrongly fragment into junk topics."""
    parts = re.split(r"[,;/·]|\s{2,}", raw)
    if len(parts) == 1:
        tokens = raw.split()
        if len(tokens) >= 2 and all(len(t) >= 2 for t in tokens):
            merged: list[str] = []
            for t in tokens:
                if merged and t.lower().rstrip(".") in _CORP_TOKENS:
                    merged[-1] += " " + t
                else:
                    merged.append(t)
            # Gate the ambiguous single-space split: only multi-company
            # when each fragment independently resolves to an existing
            # topic. Under-splitting is recoverable (/wiki_split); the
            # old over-split silently created junk topics the user had to
            # delete one by one.
            if len(merged) >= 2 and all(_is_known_topic(t) for t in merged):
                parts = merged
            else:
                parts = [raw]
    final: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if final and p.lower().rstrip(".") in _CORP_TOKENS:
            final[-1] += ", " + p
        else:
            final.append(p)
    return [p.strip() for p in final if p.strip()]


def topics_for(metadata: dict | None, title: str) -> list[str]:
    """Return a LIST of wiki topics for a doc. Multi-company docs get
    routed to multiple pages (one queue entry per company).
    Each topic is resolved against existing index + aliases so
    'Broadcom Inc' → 'Broadcom', 'Samsung Electronics' → '삼성전자' etc."""
    md = metadata or {}
    company = (md.get("company") or "").strip()
    if company:
        split = _split_multi_topic(company)
        if len(split) >= 2:
            return [resolve_topic(t) for t in split]
        return [resolve_topic(company)]
    tags = md.get("tags") or []
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, str) and t.strip():
                return [resolve_topic(t.strip())]
    return ["기타"]


def topic_for(metadata: dict | None, title: str) -> str:
    """Legacy single-topic API — returns first topic."""
    return topics_for(metadata, title)[0]


# ----------------------------------------------------------------------
# Enqueue (called from the ingest pipeline — cheap, no LLM)
# ----------------------------------------------------------------------

def enqueue(*, doc_id: str, title: str, summary: str, doc_type: str,
            source: str, metadata: dict | None) -> None:
    """Append a freshly-ingested doc to the batch queue. No-op unless the
    wiki is enabled. Importance gate drops short/low-signal docs so they
    stay archive-only (no wiki spend). Deduped by doc_id. Wrapped by the
    caller in try/except, but also self-guards so it can NEVER break
    ingest."""
    try:
        if not enabled():
            return
        min_chars = int(_flag("WIKI_MIN_SUMMARY_CHARS", 800))
        if len((summary or "").strip()) < min_chars:
            return
        tlist = topics_for(metadata, title)
        # Merge only ever reads summary[:WIKI_DOC_SUMMARY_CHARS]; storing
        # the full multi-KB summary made the queue file tens of MB after
        # a backfill, re-serialized on every live ingest.
        keep = int(_flag("WIKI_DOC_SUMMARY_CHARS", 1200)) + 100
        with _QUEUE_LOCK:
            q = _load_json(_QUEUE_PATH, [])
            if not isinstance(q, list):
                q = []
            queued_keys = {
                (it.get("doc_id"), it.get("topic"))
                for it in q if isinstance(it, dict)
            }
            ts = datetime.utcnow().isoformat(timespec="seconds")
            added = False
            for topic in tlist:
                if topic == "기타" or is_topic_deleted(topic) or is_topic_blocked(topic):
                    continue
                if (doc_id, topic) in queued_keys:
                    continue
                q.append({
                    "doc_id": doc_id,
                    "title": title,
                    "summary": summary[:keep],
                    "doc_type": doc_type,
                    "source": source,
                    "topic": topic,
                    "ts": ts,
                })
                added = True
            if added:
                _atomic_write_json(_QUEUE_PATH, q)
    except Exception:
        log.exception("wiki enqueue failed (ignored — ingest unaffected)")


def queue_size() -> int:
    q = _load_json_cached(_QUEUE_PATH, [])
    return len(q) if isinstance(q, list) else 0


def pending_list() -> list[dict]:
    """Queue contents grouped by topic for /wiki_pending display."""
    q = _load_json_cached(_QUEUE_PATH, [])
    if not isinstance(q, list):
        return []
    groups: dict[str, dict] = {}
    for it in q:
        if not isinstance(it, dict):
            continue
        topic = it.get("topic", "기타")
        g = groups.setdefault(topic, {"topic": topic, "docs": 0, "titles": []})
        g["docs"] += 1
        title = it.get("title", "")
        if title and len(g["titles"]) < 3:
            g["titles"].append(title[:60])
    return sorted(groups.values(), key=lambda x: x["docs"], reverse=True)


_MAX_FAIL_CYCLES = 3


def _load_failed() -> list[dict]:
    return _load_json(_FAILED_PATH, [])


def _save_failed(data: list[dict]) -> None:
    _atomic_write_json(_FAILED_PATH, data)


def record_failure(topic: str, docs: list[dict], error: str) -> None:
    """Track a merge failure. After _MAX_FAIL_CYCLES consecutive failures
    for the same topic, move its docs from queue to failed list.
    Persists on BOTH the new-entry and the increment path — otherwise the
    cycle counter never reaches _MAX_FAIL_CYCLES and a permanently-failing
    topic loops in the queue forever, re-spending tokens every batch."""
    failed = _load_failed()
    if not isinstance(failed, list):
        failed = []
    doc_ids = [d.get("doc_id") for d in docs if d.get("doc_id")]
    existing = next((f for f in failed if f.get("topic") == topic), None)
    if existing:
        existing["cycles"] = existing.get("cycles", 0) + 1
        existing["last_error"] = error[:200]
        existing["last_ts"] = _now_kst_iso()
        # Keep the doc_id set current so retry/backfill-skip stay accurate.
        merged = set(existing.get("doc_ids") or []) | set(doc_ids)
        existing["doc_ids"] = sorted(merged)
        existing["doc_count"] = len(merged)
        _save_failed(failed)
        return
    failed.append({
        "topic": topic,
        "cycles": 1,
        "first_ts": _now_kst_iso(),
        "last_ts": _now_kst_iso(),
        "last_error": error[:200],
        "doc_count": len(docs),
        "doc_ids": doc_ids,
        "doc_titles": [d.get("title", "")[:60] for d in docs[:5]],
    })
    _save_failed(failed)


def _failed_doc_ids() -> set:
    """All doc_ids currently quarantined in the failed list, so backfill
    doesn't resurrect a doc that merge has already given up on."""
    failed = _load_failed()
    ids: set = set()
    if isinstance(failed, list):
        for f in failed:
            if isinstance(f, dict):
                ids.update(f.get("doc_ids") or [])
    return ids


def promote_to_failed(topic: str) -> bool:
    """After _MAX_FAIL_CYCLES, move topic's docs from queue to failed.
    Collects ALL doc_ids being removed from the queue (not just the
    subset that was attempted in the last merge) so wiki_failed_retry
    can recover them all."""
    failed = _load_failed()
    rec = next((f for f in failed if f.get("topic") == topic), None)
    if not rec or rec.get("cycles", 0) < _MAX_FAIL_CYCLES:
        return False
    with _QUEUE_LOCK:
        q = _load_json(_QUEUE_PATH, [])
        if not isinstance(q, list):
            return False
        evicted = [it for it in q
                   if isinstance(it, dict) and it.get("topic") == topic]
        remaining = [it for it in q
                     if not (isinstance(it, dict) and it.get("topic") == topic)]
        if not evicted:
            return False
        _atomic_write_json(_QUEUE_PATH, remaining)
    all_ids = set(rec.get("doc_ids") or [])
    all_ids.update(it.get("doc_id") for it in evicted if it.get("doc_id"))
    rec["doc_ids"] = sorted(all_ids)
    rec["doc_count"] = len(all_ids)
    rec["promoted"] = True
    _save_failed(failed)
    return True


def wiki_failed() -> list[dict]:
    """Return list of failed wiki merge entries for /wiki_failed."""
    return _load_failed()


def wiki_failed_count() -> int:
    return len(_load_failed())


def wiki_failed_clear(topic: str | None = None) -> int:
    """Clear failed entries. If topic is given, clear only that one.
    Returns number of entries removed."""
    failed = _load_failed()
    if not isinstance(failed, list):
        return 0
    if topic is None:
        count = len(failed)
        _save_failed([])
        return count
    new = [f for f in failed if f.get("topic") != topic]
    removed = len(failed) - len(new)
    _save_failed(new)
    return removed


def wiki_failed_retry(topic: str) -> dict:
    """Move a failed topic back to the queue for retry. promote_to_failed
    already removed its docs from the queue, so we must RE-ENQUEUE them
    from meta (reading by stored doc_ids) — just clearing the failed entry
    would lose the docs entirely."""
    from . import meta
    failed = _load_failed()
    rec = next((f for f in failed if f.get("topic") == topic), None)
    if not rec:
        return {"error": f"'{topic}' 실패 목록에 없음"}
    doc_ids = rec.get("doc_ids") or []
    keep = int(_flag("WIKI_DOC_SUMMARY_CHARS", 1200)) + 100
    ts = datetime.utcnow().isoformat(timespec="seconds")
    # Fetch docs from meta BEFORE taking the queue lock so the lock is
    # held only for the load→append→write cycle.
    items: list[dict] = []
    for doc_id in doc_ids:
        try:
            d = meta.get_doc(doc_id)
        except Exception:
            continue
        if not d:
            continue
        items.append({
            "doc_id": doc_id,
            "title": d.get("title") or "",
            "summary": (d.get("summary") or "").strip()[:keep],
            "doc_type": d.get("type") or "",
            "source": d.get("source") or "",
            "topic": topic,
            "ts": ts,
        })
    requeued = 0
    with _QUEUE_LOCK:
        q = _load_json(_QUEUE_PATH, [])
        if not isinstance(q, list):
            q = []
        queued_keys = {(it.get("doc_id"), it.get("topic"))
                       for it in q if isinstance(it, dict)}
        for it in items:
            if (it["doc_id"], topic) in queued_keys:
                continue
            q.append(it)
            queued_keys.add((it["doc_id"], topic))
            requeued += 1
        _atomic_write_json(_QUEUE_PATH, q)
    new_failed = [f for f in failed if f.get("topic") != topic]
    _save_failed(new_failed)
    return {"retried": topic, "requeued": requeued,
            "note": f"{requeued}건 큐 복귀 — 다음 배치(/wiki_run)에서 재시도"}


def wiki_failed_retry_all() -> dict:
    """Retry ALL failed topics at once. Returns aggregate stats."""
    failed = _load_failed()
    if not failed:
        return {"retried": 0, "requeued": 0, "topics": []}
    total_requeued = 0
    topics_retried: list[str] = []
    for rec in list(failed):
        topic = rec.get("topic", "")
        if not topic:
            continue
        res = wiki_failed_retry(topic)
        if not res.get("error"):
            topics_retried.append(topic)
            total_requeued += res.get("requeued", 0)
    return {"retried": len(topics_retried), "requeued": total_requeued,
            "topics": topics_retried}


def _wikied_doc_ids() -> set:
    """All doc_ids already folded into a wiki page (from the index), so
    backfill can skip them."""
    idx = _load_json(_INDEX_PATH, {})
    ids: set = set()
    if isinstance(idx, dict):
        for rec in idx.values():
            if isinstance(rec, dict):
                ids.update(rec.get("doc_ids") or [])
    return ids


def list_mergeable_topics() -> list[str]:
    """Return topic names that look like multi-company merges.
    Cross-checks against the index: a topic is 'merged' only when ≥2
    of its split parts already exist as standalone topics."""
    idx = _load_json(_INDEX_PATH, {})
    if not isinstance(idx, dict):
        idx = {}
    all_topics = set(idx.keys())
    d = _wiki_dir()
    if d and d.exists():
        for p in d.glob("*.md"):
            all_topics.add(p.stem)
    candidates: set[str] = set()
    for topic in all_topics:
        parts = _split_multi_topic(topic)
        if len(parts) < 2:
            continue
        hits = sum(1 for p in parts if p in all_topics and p != topic)
        if hits >= 2:
            candidates.add(topic)
    return sorted(candidates)


def decompose_merged_topic(topic: str) -> dict:
    """Delete a merged multi-company page and re-enqueue its docs so they
    get routed to individual company pages. Returns stats."""
    from . import meta
    idx = _load_json(_INDEX_PATH, {})
    if not isinstance(idx, dict):
        idx = {}
    actual_key = None
    if topic in idx:
        actual_key = topic
    else:
        slug_q = _slug(topic)
        for k in idx:
            if _slug(k) == slug_q:
                actual_key = k
                break
    if not actual_key:
        p = _page_path(topic)
        if p and p.exists():
            p.unlink()
            return {"decomposed": topic, "docs": 0,
                    "re_enqueued": 0, "file_deleted": True,
                    "note": "인덱스 없음 — 파일만 삭제됨 (재큐잉 불가)"}
        candidates = list_mergeable_topics()
        hint = ""
        if candidates:
            hint = "\n\n분리 가능 토픽:\n" + "\n".join(
                f"  • {c}" for c in candidates[:20])
        return {"error": f"토픽 '{topic}' 인덱스에 없음{hint}"}
    topic = actual_key
    rec = idx[topic]
    doc_ids = rec.get("doc_ids") or []
    if not doc_ids:
        return {"error": "doc_ids 비어있음"}

    min_chars = int(_flag("WIKI_MIN_SUMMARY_CHARS", 800))
    keep = int(_flag("WIKI_DOC_SUMMARY_CHARS", 1200)) + 100
    ts = datetime.utcnow().isoformat(timespec="seconds")
    # Build re-enqueue items BEFORE the lock (meta lookups are slow-ish);
    # merge into the live queue under the lock.
    new_items: list[dict] = []
    for doc_id in doc_ids:
        try:
            d = meta.get_doc(doc_id)
        except Exception:
            continue
        if not d:
            continue
        summary = (d.get("summary") or "").strip()
        if len(summary) < min_chars:
            continue
        md = d.get("metadata")
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except Exception:
                md = {}
        for t in topics_for(md, d.get("title") or ""):
            if t == topic:
                continue
            new_items.append({
                "doc_id": doc_id,
                "title": d.get("title") or "",
                "summary": summary[:keep],
                "doc_type": d.get("type") or "",
                "source": d.get("source") or "",
                "topic": t,
                "ts": ts,
            })
    enqueued = 0
    with _QUEUE_LOCK:
        q = _load_json(_QUEUE_PATH, [])
        if not isinstance(q, list):
            q = []
        q = [it for it in q
             if not (isinstance(it, dict) and it.get("topic") == topic)]
        queued_keys = {
            (it.get("doc_id"), it.get("topic"))
            for it in q if isinstance(it, dict)
        }
        for it in new_items:
            if (it["doc_id"], it["topic"]) in queued_keys:
                continue
            q.append(it)
            queued_keys.add((it["doc_id"], it["topic"]))
            enqueued += 1
        _atomic_write_json(_QUEUE_PATH, q)

    p = _page_path(topic)
    deleted_file = False
    if p and p.exists():
        p.unlink()
        deleted_file = True

    del idx[topic]
    _atomic_write_json(_INDEX_PATH, idx)

    return {"decomposed": topic, "docs": len(doc_ids),
            "re_enqueued": enqueued, "file_deleted": deleted_file}


# ----------------------------------------------------------------------
# Dedup — alias system + detect & merge near-duplicate topics
# ----------------------------------------------------------------------

_CORP_SUFFIXES = re.compile(
    r"\s*(?:\b(?:inc|corp|corporation|ltd|limited|co|company|llc|plc|ag|sa|se"
    r"|group|holdings|pbc)\b\.?|주식회사|㈜)\s*$",
    re.IGNORECASE,
)


def _dedup_key(name: str) -> str:
    """Normalize a topic name for duplicate detection."""
    k = _CORP_SUFFIXES.sub("", name).strip()
    k = re.sub(r"[\s_\-·]+", "", k).lower()
    return k


def _is_substr_dup(short: str, long: str) -> bool:
    """True only when *short* is a meaningful prefix of *long*.
    Guards: min 4 chars, ratio ≥ 0.8, must start at position 0.
    Suffix/infix matches are almost always false positives
    (Intel↔Mintel, 디스플레이↔LG디스플레이)."""
    if len(short) < 4 or short == long:
        return False
    if short not in long:
        return False
    if len(short) / len(long) < 0.8:
        return False
    if long.find(short) != 0:
        return False
    return True


def _load_aliases() -> dict[str, str]:
    """Load alias map: {alias_name → canonical_topic}. Cached by mtime —
    treat the returned dict as READ-ONLY (mutators must copy)."""
    d = _load_json_cached(_ALIASES_PATH, {})
    return d if isinstance(d, dict) else {}


def _save_alias(alias: str, canonical: str) -> None:
    """Record an alias so future ingests auto-route."""
    aliases = dict(_load_aliases())  # copy: the loaded dict is cached
    aliases[alias] = canonical
    _atomic_write_json(_ALIASES_PATH, aliases)


def _load_deleted_topics() -> set[str]:
    d = _load_json_cached(_DELETED_TOPICS_PATH, [])
    return set(d) if isinstance(d, list) else set()


def _mark_topic_deleted(topic: str) -> None:
    deleted = _load_deleted_topics()
    deleted.add(topic)
    _atomic_write_json(_DELETED_TOPICS_PATH, sorted(deleted))


def is_topic_deleted(topic: str) -> bool:
    return topic in _load_deleted_topics()


_CJK_RE = re.compile(r"[一-鿿㐀-䶿]")

# Values MUST match the actual canonical topic names in wiki_index.json,
# otherwise a re-incoming CJK doc spawns a NEW page instead of routing to
# the existing topic. (resolve_topic recurses on the value, so an alias
# could mask a mismatch — but pinning the exact topic name here is the
# robust guard and survives an alias-file wipe.)
_CJK_TRANSLATE: dict[str, str] = {
    "三一重工": "싼이중공업",
    "中材科技": "중재과기",
    "乘联会": "중국승용차협회",
    "分析": "분석",
    "南亞科": "난야테크놀로지",
    "博通": "브로드컴",
    "台光電": "타이광전자",
    "台燿": "타이야오",
    "台积电": "TSMC", "台積": "TSMC", "台積電": "TSMC",
    "廣達": "콴타",
    "神達": "미탁",
    "慧榮科技": "실리콘모션",
    "緯穎": "위영",
    "美银证券": "BofA Securities",
    "群聯": "Phison",
    "聯發科": "미디어텍",
    "華邦電": "윈본드",
    "輝達": "엔비디아",
    "金居开发": "진쥐개발",
    "锂": "리튬",
    "宇树科技股份有限公司": "유니트리",
    "宇树科技": "유니트리",
    "宇树": "유니트리",
}


# Seed aliases: canonical English vendor names that should always route to
# their established Korean topic page. Unlike CJK_TRANSLATE (script-based),
# these are exact English↔Korean pairs so a fresh ingest naming the company
# in English doesn't spawn a parallel page. Values MUST match the canonical
# topic names in wiki_index.json.
_SEED_ALIASES: dict[str, str] = {
    "NVIDIA Corporation": "엔비디아",
    "Samsung SDI": "삼성SDI",
}


# Topics permanently blocked from the wiki — generic, contentless labels
# that an LLM occasionally proposes (e.g. a bare "report"). Checked at every
# entry point so they never spawn or revive a page. Lower-cased compare.
_BLOCKED_TOPICS = frozenset({"report"})


def is_topic_blocked(topic: str) -> bool:
    return (topic or "").strip().lower() in _BLOCKED_TOPICS


def resolve_topic(proposed: str) -> str:
    """Map a proposed topic name to an existing canonical topic.
    Checked at ingest time so duplicates are prevented, not just detected.
    Order: exact alias → CJK translate → dedup-key → substring → passthrough."""
    # 1) Exact alias hit (covers Korean↔English pairs set by merge)
    aliases = _load_aliases()
    if proposed in aliases:
        return aliases[proposed]

    # 1a) Seed alias (canonical English vendor name → Korean topic). Persist
    # it to the alias file so future lookups short-circuit at step 1.
    seed = _SEED_ALIASES.get(proposed.strip())
    if seed:
        _save_alias(proposed, seed)
        return resolve_topic(seed)

    # 1b) CJK (Chinese/Japanese) → Korean/English translation
    if _CJK_RE.search(proposed):
        translated = _CJK_TRANSLATE.get(proposed.strip())
        if translated:
            _save_alias(proposed, translated)
            return resolve_topic(translated)

    # 2) Dedup-key match against existing index topics (cached read)
    idx = _load_json_cached(_INDEX_PATH, {})
    if not isinstance(idx, dict):
        return proposed
    pk = _dedup_key(proposed)
    if not pk:
        return proposed
    for existing in idx:
        if _dedup_key(existing) == pk:
            if existing != proposed:
                return existing
            return proposed

    # 3) Substring containment (strict: ≥4 chars, ≥70% length ratio)
    for existing in idx:
        ek = _dedup_key(existing)
        if not ek:
            continue
        shorter, longer = (pk, ek) if len(pk) <= len(ek) else (ek, pk)
        if _is_substr_dup(shorter, longer):
            return existing

    return proposed


def find_duplicates() -> list[tuple[str, str, int, int]]:
    """Return pairs of topics that look like duplicates.
    Each tuple: (topic_a, topic_b, docs_a, docs_b).
    Sorted by total doc count descending (most impactful first)."""
    idx = _load_json(_INDEX_PATH, {})
    if not isinstance(idx, dict):
        return []
    by_key: dict[str, list[str]] = {}
    for topic in idx:
        key = _dedup_key(topic)
        if not key:
            continue
        by_key.setdefault(key, []).append(topic)

    # Substring containment (strict: ≥4 chars, ≥70% length ratio)
    topics = list(idx.keys())
    for i, a in enumerate(topics):
        ka = _dedup_key(a)
        if not ka:
            continue
        for b in topics[i + 1:]:
            kb = _dedup_key(b)
            if not kb:
                continue
            if ka == kb:
                continue
            shorter, longer = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
            if _is_substr_dup(shorter, longer):
                merged_key = shorter
                existing = by_key.get(merged_key, [])
                for t in [a, b]:
                    if t not in existing:
                        existing.append(t)
                by_key[merged_key] = existing

    # Also surface alias-linked topics that both still exist in index
    aliases = _load_aliases()
    for alias, canonical in aliases.items():
        if alias in idx and canonical in idx and alias != canonical:
            pair_key_str = min(alias, canonical)
            existing = by_key.get(pair_key_str, [])
            for t in [canonical, alias]:
                if t not in existing:
                    existing.append(t)
            by_key[pair_key_str] = existing

    pairs: list[tuple[str, str, int, int]] = []
    seen: set[tuple[str, str]] = set()
    for group in by_key.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda t: len((idx.get(t) or {}).get("doc_ids") or []),
                   reverse=True)
        primary = group[0]
        for other in group[1:]:
            pair_key = tuple(sorted([primary, other]))
            if pair_key in seen:
                continue
            seen.add(pair_key)
            da = len((idx.get(primary) or {}).get("doc_ids") or [])
            db = len((idx.get(other) or {}).get("doc_ids") or [])
            pairs.append((primary, other, da, db))

    pairs.sort(key=lambda x: x[2] + x[3], reverse=True)
    return pairs


def merge_topics(keep: str, absorb: str) -> dict:
    """Merge topic *absorb* into *keep*: combine doc_ids, re-enqueue
    absorbed docs for re-synthesis, delete absorbed page & index entry,
    and record an alias so future ingests auto-route to *keep*."""
    idx = _load_json(_INDEX_PATH, {})
    if not isinstance(idx, dict):
        idx = {}
    if keep not in idx:
        return {"error": f"토픽 '{keep}' 인덱스에 없음"}
    if absorb not in idx:
        return {"error": f"토픽 '{absorb}' 인덱스에 없음"}

    rec_keep = idx[keep]
    rec_absorb = idx[absorb]

    keep_ids = set(rec_keep.get("doc_ids") or [])
    absorb_ids = set(rec_absorb.get("doc_ids") or [])
    new_ids = absorb_ids - keep_ids

    merged_ids = sorted(keep_ids | absorb_ids)
    rec_keep["doc_ids"] = merged_ids
    rec_keep["updated"] = _now_kst_iso()
    rec_keep["claims"] = (rec_keep.get("claims") or 0) + (rec_absorb.get("claims") or 0)

    del idx[absorb]
    _atomic_write_json(_INDEX_PATH, idx)

    # Record alias so future ingests auto-route (한영 쌍 포함)
    _save_alias(absorb, keep)

    p = _page_path(absorb)
    deleted_file = False
    if p and p.exists():
        try:
            p.unlink()
            deleted_file = True
        except Exception:
            pass

    from . import meta
    keep_chars = int(_flag("WIKI_DOC_SUMMARY_CHARS", 1200)) + 100
    # Queue ts uses the same naive-UTC isoformat as every other writer —
    # this site previously wrote naive-KST (_now_kst_iso), planting a
    # mixed-format landmine in one field.
    ts = datetime.utcnow().isoformat(timespec="seconds")
    new_items: list[dict] = []
    for doc_id in new_ids:
        try:
            d = meta.get_doc(doc_id)
        except Exception:
            continue
        if not d:
            continue
        new_items.append({
            "doc_id": doc_id,
            "title": d.get("title") or "",
            "summary": (d.get("summary") or "").strip()[:keep_chars],
            "doc_type": d.get("type") or "",
            "source": d.get("source") or "",
            "topic": keep,
            "ts": ts,
        })
    enqueued = 0
    with _QUEUE_LOCK:
        q = _load_json(_QUEUE_PATH, [])
        if not isinstance(q, list):
            q = []
        for it in q:
            if isinstance(it, dict) and it.get("topic") == absorb:
                it["topic"] = keep
        queued_keys = {(it.get("doc_id"), it.get("topic"))
                       for it in q if isinstance(it, dict)}
        for it in new_items:
            if (it["doc_id"], keep) in queued_keys:
                continue
            q.append(it)
            queued_keys.add((it["doc_id"], keep))
            enqueued += 1
        _atomic_write_json(_QUEUE_PATH, q)

    return {
        "keep": keep, "absorbed": absorb,
        "docs_merged": len(merged_ids),
        "new_enqueued": enqueued,
        "file_deleted": deleted_file,
    }


def merge_all_duplicates() -> dict:
    """Merge ALL detected duplicate pairs at once. The topic with more
    docs absorbs the smaller one. Returns summary stats."""
    pairs = find_duplicates()
    if not pairs:
        return {"merged": 0, "errors": 0, "pairs": 0}
    merged = 0
    errors = 0
    details: list[str] = []
    for keep, absorb, da, db in pairs:
        # Reload index each time since merge_topics modifies it
        idx = _load_json(_INDEX_PATH, {})
        if not isinstance(idx, dict):
            idx = {}
        if keep not in idx or absorb not in idx:
            continue
        res = merge_topics(keep, absorb)
        if res.get("error"):
            errors += 1
        else:
            merged += 1
            details.append(f"{absorb} → {keep}")
    return {"merged": merged, "errors": errors, "pairs": len(pairs),
            "details": details}


def rename_topic(old_name: str, new_name: str) -> dict:
    """Rename a wiki topic: update index key, rename .md, remap queue, save alias."""
    idx = _load_json(_INDEX_PATH, {})
    if not isinstance(idx, dict):
        return {"error": "인덱스 로드 실패"}

    in_index = old_name in idx
    if in_index and new_name in idx:
        return {"error": f"토픽 '{new_name}' 이미 존재 — /wiki_dedup merge 사용"}

    renamed_file = False
    if in_index:
        rec = idx.pop(old_name)
        new_file = f"{_slug(new_name)}.md"
        old_path = _page_path(old_name)
        rec["file"] = new_file
        rec["title"] = new_name
        idx[new_name] = rec
        _atomic_write_json(_INDEX_PATH, idx)

        d = _wiki_dir()
        if d and old_path and old_path.exists():
            try:
                old_path.rename(d / new_file)
                renamed_file = True
            except Exception:
                pass

    remapped = 0
    with _QUEUE_LOCK:
        q = _load_json(_QUEUE_PATH, [])
        if isinstance(q, list):
            for it in q:
                if isinstance(it, dict) and it.get("topic") == old_name:
                    it["topic"] = new_name
                    remapped += 1
            if remapped:
                _atomic_write_json(_QUEUE_PATH, q)

    if not in_index and remapped == 0:
        return {"error": f"토픽 '{old_name}' 인덱스·큐 어디에도 없음"}

    _save_alias(old_name, new_name)
    return {"ok": True, "old": old_name, "new": new_name,
            "renamed_file": renamed_file, "remapped_queue": remapped}


def delete_topic(topic: str) -> dict:
    """Delete a wiki topic entirely: index entry, .md file, queue entries."""
    idx = _load_json(_INDEX_PATH, {})
    if not isinstance(idx, dict):
        idx = {}

    rec = idx.pop(topic, None)
    deleted_file = False
    if rec:
        p = _page_path(topic)
        if p and p.exists():
            try:
                p.unlink()
                deleted_file = True
            except Exception:
                pass
        _atomic_write_json(_INDEX_PATH, idx)

    removed_queue = 0
    with _QUEUE_LOCK:
        q = _load_json(_QUEUE_PATH, [])
        if isinstance(q, list):
            new_q = [it for it in q
                     if not (isinstance(it, dict) and it.get("topic") == topic)]
            removed_queue = len(q) - len(new_q)
            if removed_queue:
                _atomic_write_json(_QUEUE_PATH, new_q)

    if not rec and removed_queue == 0:
        return {"error": f"토픽 '{topic}' 인덱스/큐에 없음"}
    _mark_topic_deleted(topic)
    return {"ok": True, "topic": topic, "deleted_file": deleted_file,
            "removed_queue": removed_queue, "had_index": rec is not None}


async def classify_stale_singletons(stale_days: int = 30) -> dict:
    """/wiki_prune 데이터 계층: lint의 정체 단일소스(1소스·30일+ 미갱신)
    토픽 이름을 flash-lite로 분류한다 —
      • delete 후보: 일반 개념/추상어/너무 광범위 (철학·리더십·차트·웹3…)
        → 위키 토픽으로 유지할 가치가 없는 노이즈
      • keep: 기업/제품/인물/기관/기술 고유명사 (소스가 더 붙으면 자랄
        수 있는 실체) — 절대 안 건드림
    이름만 보내므로 550개 기준 ~₩10-20. 분류 응답 파싱 실패 시 그 배치는
    전부 keep (fail-safe: 불확실하면 안 지운다)."""
    import asyncio as _aio
    res = await _aio.to_thread(lint, stale_days, False)
    stale = res.get("stale_singletons") or []
    names = [s["topic"] for s in stale if s.get("topic")]
    if not names:
        return {"delete": [], "keep": [], "total": 0}
    from ..llm.gemini import complete
    system = (
        "당신은 개인 투자 리서치 위키의 사서입니다. 아래 위키 토픽 이름"
        " 목록을 두 그룹으로 분류하세요.\n"
        "DELETE = 일반 개념/추상어/형용사적 주제/너무 광범위해서 개별"
        " 위키 페이지로 유지할 가치가 없는 것 (예: 철학, 리더십, 차트,"
        " 웹3, 감원, 정치성향, K-뷰티, RTO)\n"
        "KEEP = 기업·제품·인물·기관·특정 기술의 고유명사 (예: 삼성전자,"
        " DraftKings, Nemotron, 태평염전) — 투자 리서치 대상이 될 수"
        " 있는 실체\n"
        "애매하면 KEEP. 반드시 JSON 하나만 출력:"
        ' {"delete": ["..."], "keep": ["..."]}'
    )
    delete: list[str] = []
    keep: list[str] = []
    for i in range(0, len(names), 100):
        batch = names[i:i + 100]
        try:
            out = await complete(
                config.SUMMARY_MODEL, system, "\n".join(batch),
                max_tokens=8192, temperature=0.0, purpose="wiki",
                timeout=120)
            m = re.search(r"\{.*\}", out or "", re.S)
            j = json.loads(m.group(0)) if m else {}
            dset = {str(x).strip() for x in (j.get("delete") or [])}
        except Exception:
            log.exception("wiki prune classify batch failed — keeping all")
            dset = set()
        for n in batch:
            (delete if n in dset else keep).append(n)
    return {"delete": delete, "keep": keep, "total": len(names)}


def bulk_delete_topics(topics: list[str]) -> dict:
    """Delete many topics (each via delete_topic → 영구 삭제 + 재생성
    차단). Returns {deleted, errors:[...]}."""
    deleted = 0
    errors: list[str] = []
    for t in topics:
        try:
            r = delete_topic(t)
            if r.get("error"):
                errors.append(f"{t}: {r['error']}")
            else:
                deleted += 1
        except Exception as e:  # 한 건 실패가 전체를 멈추지 않게
            errors.append(f"{t}: {str(e)[:80]}")
    return {"deleted": deleted, "errors": errors}


def backfill(docs: list[dict]) -> dict:
    """Enqueue EXISTING corpus docs (from meta.docs_since) into the wiki
    queue so the nightly batch will synthesize them too — not just new
    ingests. Skips docs already in a wiki page, already queued, or below
    the importance gate. Spends ₩0 (enqueue only); the nightly batch does
    the actual merging under the daily budget + per-run caps, so a huge
    backfill simply drains over several capped nights. Returns counts."""
    res = {"enqueued": 0, "skipped_wikied": 0, "skipped_gate": 0,
           "skipped_queued": 0, "skipped_failed": 0, "total": len(docs)}
    if not enabled():
        res["error"] = "wiki disabled"
        return res
    min_chars = int(_flag("WIKI_MIN_SUMMARY_CHARS", 800))
    keep = int(_flag("WIKI_DOC_SUMMARY_CHARS", 1200)) + 100
    wikied = _wikied_doc_ids()
    failed_ids = _failed_doc_ids()
    q0 = _load_json(_QUEUE_PATH, [])
    if not isinstance(q0, list):
        q0 = []
    queued_ids = {it.get("doc_id") for it in q0 if isinstance(it, dict)}
    # Backfill runs in a worker thread for minutes over the full corpus.
    # Collect new items locally and merge into the live queue under the
    # lock at the END — the old load-once/rewrite-at-end pattern silently
    # dropped anything live ingest enqueued (or run_batch consumed)
    # while the scan was running.
    new_items: list[dict] = []
    for d in docs:
        doc_id = d.get("id") or d.get("doc_id")
        if not doc_id:
            continue
        if doc_id in wikied:
            res["skipped_wikied"] += 1
            continue
        if doc_id in queued_ids:
            res["skipped_queued"] += 1
            continue
        if doc_id in failed_ids:
            res["skipped_failed"] += 1
            continue
        summary = (d.get("summary") or "").strip()
        if len(summary) < min_chars:
            res["skipped_gate"] += 1
            continue
        tlist = [t for t in topics_for(d.get("metadata"), d.get("title") or "")
                 if t != "기타" and not is_topic_deleted(t)
                 and not is_topic_blocked(t)]
        if not tlist:
            res["skipped_gate"] += 1
            continue
        ts = datetime.utcnow().isoformat(timespec="seconds")
        for topic in tlist:
            new_items.append({
                "doc_id": doc_id,
                "title": d.get("title") or "",
                "summary": summary[:keep],
                "doc_type": d.get("type") or d.get("doc_type") or "",
                "source": d.get("source") or "",
                "topic": topic,
                "ts": ts,
            })
        queued_ids.add(doc_id)
        res["enqueued"] += 1
    with _QUEUE_LOCK:
        q = _load_json(_QUEUE_PATH, [])
        if not isinstance(q, list):
            q = []
        live_keys = {
            (it.get("doc_id"), it.get("topic"))
            for it in q if isinstance(it, dict)
        }
        for it in new_items:
            if (it["doc_id"], it["topic"]) in live_keys:
                continue
            q.append(it)
        _atomic_write_json(_QUEUE_PATH, q)
    return res


def rebuild_broken_refs() -> dict:
    """Scan all wiki pages for `[[자료 N]]` patterns (broken source refs).
    For affected topics: delete the page, clear doc_ids from the index, and
    re-queue ALL their docs so the next batch rebuilds from scratch — the
    post-processing in _merge_topic will resolve labels to real titles.
    Spends ₩0 (enqueue only). Returns stats."""
    from . import meta
    d = _wiki_dir()
    if not d or not d.exists():
        return {"error": "wiki dir not found", "rebuilt": 0}
    idx = _load_json(_INDEX_PATH, {})
    if not isinstance(idx, dict):
        idx = {}

    _SAFE_RE = re.compile(r"[^\w가-힣\- ]+")
    def _slug_of(s: str) -> str:
        s = _SAFE_RE.sub(" ", s or "").strip()
        s = re.sub(r"\s+", " ", s)
        return s[:80] or "기타"

    slug_to_topic: dict[str, str] = {}
    for topic_name, rec in idx.items():
        slug_to_topic[_slug_of(topic_name)] = topic_name
        slug_to_topic[topic_name] = topic_name
        if isinstance(rec, dict) and rec.get("file"):
            stem = rec["file"].rsplit(".", 1)[0]
            slug_to_topic[stem] = topic_name

    broken_re = re.compile(r"\[\[자료\s*\d+\]\]")
    affected: list[tuple[str, str, Path]] = []  # (idx_key, topic_slug, path)

    for md_file in d.glob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8")
        except Exception:
            continue
        if not broken_re.search(text):
            continue
        slug = md_file.stem
        idx_key = slug_to_topic.get(slug)
        if idx_key and idx_key in idx:
            affected.append((idx_key, slug, md_file))

    if not affected:
        return {"rebuilt": 0, "scanned": len(list(d.glob("*.md")))}

    q = _load_json(_QUEUE_PATH, [])
    if not isinstance(q, list):
        q = []
    queued_keys = {(it.get("doc_id"), it.get("topic"))
                   for it in q if isinstance(it, dict)}
    min_chars = int(_flag("WIKI_MIN_SUMMARY_CHARS", 800))
    ts = datetime.utcnow().isoformat(timespec="seconds")
    total_enqueued = 0
    topics_rebuilt: list[str] = []

    for idx_key, slug, md_path in affected:
        rec = idx.get(idx_key, {})
        doc_ids = rec.get("doc_ids") or []
        if not doc_ids:
            continue

        enqueued = 0
        for doc_id in doc_ids:
            if (doc_id, idx_key) in queued_keys:
                continue
            try:
                doc = meta.get_doc(doc_id)
            except Exception:
                continue
            if not doc:
                continue
            summary = (doc.get("summary") or "").strip()
            if len(summary) < min_chars:
                continue
            q.append({
                "doc_id": doc_id,
                "title": doc.get("title") or "",
                "summary": summary,
                "doc_type": doc.get("type") or "",
                "source": doc.get("source") or "",
                "topic": idx_key,
                "ts": ts,
            })
            queued_keys.add((doc_id, idx_key))
            enqueued += 1

        if enqueued > 0:
            rec["doc_ids"] = []
            idx[idx_key] = rec
            try:
                md_path.unlink()
            except Exception:
                pass
            total_enqueued += enqueued
            topics_rebuilt.append(idx_key)

    _atomic_write_json(_QUEUE_PATH, q)
    _atomic_write_json(_INDEX_PATH, idx)
    return {
        "rebuilt": len(topics_rebuilt),
        "topics": topics_rebuilt,
        "docs_requeued": total_enqueued,
        "scanned": len(list(d.glob("*.md"))),
    }


# ----------------------------------------------------------------------
# Page read / list (FREE — for /wiki and wiki-first answering)
# ----------------------------------------------------------------------

def _page_path(topic: str) -> Path | None:
    d = _wiki_dir()
    if d is None:
        return None
    return d / f"{_slug(topic)}.md"


def read_page(topic: str) -> str | None:
    p = _page_path(topic)
    if p is None or not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return None


def list_topics() -> list[dict]:
    """All wiki pages with light stats, newest-updated first. Reads the
    index (mtime-cached); falls back to scanning the dir if missing."""
    idx = _load_json_cached(_INDEX_PATH, {})
    out: list[dict] = []
    if isinstance(idx, dict) and idx:
        for topic, rec in idx.items():
            if not isinstance(rec, dict):
                continue
            out.append({
                "topic": topic,
                "docs": len(rec.get("doc_ids") or []),
                "updated": rec.get("updated") or "",
                "created": rec.get("created") or "",
            })
        out.sort(key=lambda r: r["updated"], reverse=True)
        return out
    d = _wiki_dir()
    if d and d.exists():
        for p in sorted(d.glob("*.md")):
            out.append({"topic": p.stem, "docs": 0, "updated": ""})
    return out


def _query_tokens(q: str) -> list[str]:
    return [t for t in re.findall(r"[\w가-힣]+", q or "") if len(t) >= 2]


def wiki_context(query: str, max_chars: int = 4000) -> dict | None:
    """P2: best synthesized page for a question, or None to fall back to
    RAG. Routing is a free, deterministic topic-name match (company/tag
    pages whose name appears in the query) — no LLM, no embedding. Returns
    {topic, text} truncated to max_chars. Conservative on purpose: a miss
    returns None so the caller's existing hybrid() retrieval still runs,
    meaning wiki-first can never answer *worse* than today, only cheaper
    when it confidently matches."""
    if not query_first_enabled():
        return None
    idx = _load_json(_INDEX_PATH, {})
    if not isinstance(idx, dict) or not idx:
        return None
    ql = (query or "").lower()
    qtokens = set(_query_tokens(ql))
    best: tuple[int, str] | None = None  # (match_strength, topic)
    for topic in idx:
        tl = topic.lower()
        if not tl or tl == "기타":
            continue
        # Substring hit (handles Korean company names) OR token overlap.
        strength = 0
        if tl in ql:
            strength = len(tl)
        else:
            ttokens = set(_query_tokens(tl))
            if ttokens and ttokens <= qtokens:
                strength = sum(len(t) for t in ttokens)
        if strength and (best is None or strength > best[0]):
            best = (strength, topic)
    if best is None:
        return None
    text = read_page(best[1])
    if not text or len(text) < 400:
        return None  # page too thin to be worth short-circuiting RAG
    return {"topic": best[1], "text": text[:max_chars]}


# ----------------------------------------------------------------------
# Nightly batch merge (the only place that spends LLM tokens)
# ----------------------------------------------------------------------

_MERGE_SYSTEM = (
    "너는 개인 지식 위키를 유지보수하는 꼼꼼한 에디터다. 기존 위키 "
    "페이지에 새로 수집된 자료 요약을 통합한다. 제공된 요약에 실제로 "
    "있는 내용만 사용하고, 절대 추측하거나 새로운 사실을 지어내지 않는다.\n\n"
    "## 가독성 원칙 (반드시 준수)\n"
    "- **간결하게**: 핵심 인사이트와 숫자 중심으로 쓴다. 교과서적 정의나 "
    "일반 용어 해설(예: ALD란, CMP란)은 생략한다.\n"
    "- **한 문단 최대 3문장**: 서로 다른 주제·관점·사실은 반드시 별도 "
    "불릿(`-`)이나 단락(빈 줄 분리)으로 나눈다. 4문장 이상을 마침표로 "
    "이어 붙이지 않는다.\n"
    "- **출처는 섹션 끝에 한 번만**: 매 문장마다 (출처: …) 반복하지 않는다. "
    "같은 자료에서 온 내용은 해당 섹션/문단 끝에 `— 출처: [[제목]]` "
    "한 번만 표기한다.\n"
    "- **계층 구조**: `##` 대주제 → `###` 소주제로 나누되, 불릿은 3단계 "
    "이상 중첩하지 않는다.\n"
    "- **핵심 수치 강조**: 매출, 점유율, 성장률, 장비 대수 등 구체적 "
    "숫자는 **볼드**로 표시한다.\n"
    "- **분량 조절**: 한 섹션이 불릿 10개를 넘기면 하위 섹션으로 분리하거나 "
    "덜 중요한 항목을 병합한다.\n"
    "- **자연스러운 문체**: 번역투·AI 상투구('결론적으로'·'요컨대'·'~라고 "
    "할 수 있다')·기계적 병렬('첫째·둘째')·과한 헤지('~인 것으로 보인다')를 "
    "피하고 담백하게 쓴다. 표현만 자연스럽게(내용·사실은 그대로).\n\n"
    "## 시계열 데이터 압축 (실적·수치 누적 시 필수)\n"
    "페이지에 분기/연도별 데이터가 쌓이면 아래 규칙으로 압축한다:\n"
    "- **최근 2~3분기**: 상세 불릿 유지 (현재 형식 그대로).\n"
    "- **그 이전 분기**: 마크다운 표로 압축. "
    "예: `| 분기 | 매출 | 영업이익 | OPM |`\n"
    "- **1년 이상 된 연간 데이터**: 한 줄 요약으로 더 압축.\n"
    "- 시간순 데이터는 **최신이 위**, 오래된 것이 아래.\n"
    "- 이 규칙의 목적: 페이지 길이를 억제하면서 장기 추이를 보존.\n"
)

# Trailing machine-readable line the model appends so we can count what
# happened (for the digest + contradiction alert) without fragile diffing.
_META_RE = re.compile(r"<!--\s*WIKI_META\s*(\{.*?\})\s*-->", re.DOTALL)
_FENCE_RE = re.compile(r"^\s*```(?:markdown)?\s*\n(.*)\n```\s*$", re.DOTALL)


def _build_merge_user(topic: str, existing: str, docs: list[dict]) -> str:
    max_page_chars = int(_flag("WIKI_MAX_PAGE_CHARS", 20000))
    existing_block = (existing or "").strip()
    if len(existing_block) > max_page_chars:
        existing_block = existing_block[:max_page_chars] + "\n…(이하 생략)"
    if not existing_block:
        existing_block = "(아직 이 토픽의 위키 페이지 없음 — 새로 작성)"
    parts = [
        f"# 토픽: {topic}\n",
        "## 기존 위키 페이지\n",
        existing_block,
        "\n\n## 새로 통합할 자료 요약\n",
    ]
    per_doc_chars = int(_flag("WIKI_DOC_SUMMARY_CHARS", 1200))
    for i, d in enumerate(docs, 1):
        s = (d.get("summary") or "").strip()[:per_doc_chars]
        parts.append(f"\n### [자료 {i}] {d.get('title','(제목없음)')}\n{s}\n")
    parts.append(
        "\n\n## 지시\n"
        "1. 기존 페이지 내용을 보존하면서 새 자료의 사실/주장을 알맞은 "
        "섹션에 통합한다. 처음이면 깔끔한 새 페이지를 만든다.\n"
        "2. **출처 표기**: 같은 자료에서 온 내용은 섹션/문단 끝에 "
        "`— 출처: [[자료의 실제 제목]]` 한 번만. "
        "**`[[자료 1]]`, `[[자료 2]]` 같은 번호 라벨은 절대 쓰지 않는다** "
        "— 반드시 `### [자료 N]` 뒤에 적힌 원래 제목을 사용한다. "
        "매 문장 반복 금지.\n"
        "3. 새 자료가 기존 주장과 **모순**되면 기존 내용을 지우지 말고 "
        "`## ⚠️ 검토 필요` 섹션에 `- 기존: … / 신규: … (출처)` 형식으로 "
        "적는다.\n"
        "4. 페이지 맨 위에 한 줄 요약(`> …`)을 유지/갱신한다.\n"
        "5. **일반 용어 해설 금지**: 업계 상식 수준의 용어 정의(ALD란, "
        "CMP란 등)는 적지 않는다. 이 회사/토픽에 고유한 정보만.\n"
        "6. **언어 규칙**: 출처 제목이든 본문이든, 한국어·영어 이외의 "
        "언어(중국어·일본어 등)는 모두 **한국어로 번역**하여 표기한다.\n"
        "7. 출력은 **페이지 마크다운 전문**만. 맨 마지막 줄에 정확히 "
        '`<!--WIKI_META {"contradictions": <정수>, "integrated": <정수>}-->` '
        "를 붙인다(통합한 자료 수, 새로 적은 모순 수).\n"
    )
    return "".join(parts)


def _parse_merge(raw: str) -> tuple[str, dict]:
    """Split model output into (clean page markdown, meta dict)."""
    text = (raw or "").strip()
    m = _FENCE_RE.match(text)
    if m:
        text = m.group(1).strip()
    meta = {"contradictions": 0, "integrated": 0}
    mm = _META_RE.search(text)
    if mm:
        try:
            parsed = json.loads(mm.group(1))
            meta["contradictions"] = int(parsed.get("contradictions", 0) or 0)
            meta["integrated"] = int(parsed.get("integrated", 0) or 0)
        except Exception:
            pass
        text = _META_RE.sub("", text).strip()
    return text + "\n", meta


def _append_docs(topic: str, existing: str, docs: list[dict]) -> dict:
    """Append new docs as dated sections — NO LLM call, ₩0 cost.
    Used when the page is below CONSOLIDATION_CHARS. The page grows
    freely; a later consolidation pass reorganises it thematically."""
    date_str = datetime.now(_KST).strftime("%Y-%m-%d")
    per_doc_chars = int(_flag("WIKI_DOC_SUMMARY_CHARS", 1200))
    parts: list[str] = []
    for d in docs:
        title = (d.get("title") or "(제목없음)").strip()
        summary = (d.get("summary") or "").strip()[:per_doc_chars]
        if not summary:
            continue
        parts.append(f"### {title}\n{summary}\n\n— 출처: [[{title}]]")
    if not parts:
        return {"topic": topic, "ok": False, "docs": len(docs),
                "contradictions": 0, "error": "no summaries to append"}
    section = f"\n\n---\n\n## 📌 {date_str} 업데이트\n\n" + "\n\n".join(parts) + "\n"
    page = (existing.rstrip() + section) if existing.strip() else (
        f"> {topic} 위키 페이지\n\n# {topic}\n" + section)
    p = _page_path(topic)
    if p is None:
        return {"topic": topic, "ok": False, "docs": len(docs),
                "contradictions": 0, "error": "no vault"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # tmp+replace: a SIGKILL mid-write used to truncate the only live
        # copy of the page, and the next append would bake the loss in.
        tmp = p.with_suffix(p.suffix + f".{os.getpid()}.tmp")
        tmp.write_text(page, encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:
        log.exception("wiki append write failed for %s", topic)
        return {"topic": topic, "ok": False, "docs": len(docs),
                "contradictions": 0, "error": str(e)[:120]}
    log.info("wiki append %s: %d docs, page now %d chars (no LLM)",
             topic, len(docs), len(page))
    return {
        "topic": topic, "ok": True, "docs": len(docs),
        "contradictions": 0, "mode": "append",
        "rel": p.relative_to(Path(config.OBSIDIAN_VAULT_PATH).resolve()).as_posix(),
    }


async def _consolidate_topic(topic: str, existing: str, docs: list[dict]) -> dict:
    """LLM consolidation — rewrites the page thematically. Runs only
    when the page exceeds CONSOLIDATION_CHARS or on first creation."""
    from ..llm.gemini import complete
    user = _build_merge_user(topic, existing, docs)
    model = _flag("WIKI_MERGE_MODEL", None) or config.ANSWER_MODEL
    max_tokens = int(_flag("WIKI_MERGE_MAX_TOKENS", 12000))
    try:
        raw = await complete(
            model=model,
            system=_MERGE_SYSTEM,
            user=user,
            max_tokens=max_tokens,
            temperature=0.2,
            purpose="wiki",
        )
    except Exception as e:
        log.warning("wiki consolidate LLM failed for %s: %s", topic, e)
        return {"topic": topic, "ok": False, "docs": len(docs),
                "contradictions": 0, "error": str(e)[:120]}
    page, meta = _parse_merge(raw)
    _NON_KOEN = re.compile(r"[^\x00-\x7F가-힣ㄱ-ㅎㅏ-ㅣ]")
    def _resolve_ref(m: re.Match) -> str:
        n = int(m.group(1))
        if 1 <= n <= len(docs):
            t = (docs[n - 1].get("title") or "").strip()
            if t and not _NON_KOEN.search(t):
                return f"[[{t}]]"
        return m.group(0)
    page = re.sub(r"\[\[자료\s*(\d+)\]\]", _resolve_ref, page)
    if len(page.strip()) < 20:
        return {"topic": topic, "ok": False, "docs": len(docs),
                "contradictions": 0, "error": "empty consolidation output"}
    p = _page_path(topic)
    if p is None:
        return {"topic": topic, "ok": False, "docs": len(docs),
                "contradictions": 0, "error": "no vault"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + f".{os.getpid()}.tmp")
        tmp.write_text(page, encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:
        log.exception("wiki consolidate write failed for %s", topic)
        return {"topic": topic, "ok": False, "docs": len(docs),
                "contradictions": 0, "error": str(e)[:120]}
    log.info("wiki consolidate %s: %d docs, page %d→%d chars (LLM)",
             topic, len(docs), len(existing), len(page))
    return {
        "topic": topic, "ok": True, "docs": len(docs),
        "contradictions": int(meta.get("contradictions", 0)),
        "mode": "consolidate",
        "rel": p.relative_to(Path(config.OBSIDIAN_VAULT_PATH).resolve()).as_posix(),
    }


async def _merge_topic(topic: str, docs: list[dict]) -> dict:
    """Route to append (free) or LLM consolidation based on page size.
    New/empty pages always get an LLM merge for a clean initial structure."""
    existing = read_page(topic) or ""
    threshold = int(_flag("WIKI_CONSOLIDATION_CHARS", 30000))
    if not existing.strip():
        return await _consolidate_topic(topic, existing, docs)
    if len(existing) >= threshold:
        return await _consolidate_topic(topic, existing, docs)
    return _append_docs(topic, existing, docs)


async def run_batch() -> dict:
    """Nightly job: drain the queue topic-by-topic, merging each into its
    wiki page. Bounded by per-run caps AND a hard daily ₩ budget; resume-
    safe (the queue is rewritten after every topic, so a crash — or a
    budget block — leaves only unprocessed topics behind). Returns a
    summary used for the Telegram digest + contradiction/budget alerts."""
    started = _now_kst_iso()
    if not enabled():
        return {"status": "disabled", "started": started}

    # Daily budget circuit breaker (KST). If today's wiki spend already
    # hit the cap, block the whole run — queued docs stay for tomorrow
    # (cost.today resets at KST midnight). budget_blocked flags the caller
    # to fire the actionable alert.
    budget = budget_krw()
    if budget > 0 and today_cost_krw() >= budget:
        summary = {"status": "budget_blocked", "budget_blocked": True,
                   "started": started, "topics": 0, "pages": 0, "docs": 0,
                   "contradictions": 0, "today_cost": today_cost_krw(),
                   "budget": budget, "remaining_in_queue": queue_size()}
        _atomic_write_json(_LASTRUN_PATH, summary)
        log.warning("wiki batch blocked at start: today ₩%.0f ≥ budget ₩%.0f",
                    summary["today_cost"], budget)
        return summary

    q = _load_json(_QUEUE_PATH, [])
    if not isinstance(q, list) or not q:
        summary = {"status": "empty", "started": started, "topics": 0,
                   "pages": 0, "docs": 0, "contradictions": 0,
                   "budget_blocked": False, "today_cost": today_cost_krw(),
                   "budget": budget, "remaining_in_queue": 0}
        _atomic_write_json(_LASTRUN_PATH, summary)
        return summary

    # Group queued docs by topic (resolve aliases so merged/renamed
    # topics land under the canonical name, not as orphan duplicates).
    by_topic: dict[str, list[dict]] = {}
    for it in q:
        if not isinstance(it, dict):
            continue
        raw = it.get("topic") or "기타"
        resolved = resolve_topic(raw) if raw != "기타" else raw
        if resolved != raw:
            it["topic"] = resolved
        by_topic.setdefault(resolved, []).append(it)

    # Drop deleted + blocked topics from the batch and clean them out of
    # the queue (blocked = generic labels like "report" that must never page).
    deleted = _load_deleted_topics()
    dropped_deleted = {t for t in by_topic
                       if t in deleted or is_topic_blocked(t)}
    for t in dropped_deleted:
        del by_topic[t]
    if dropped_deleted:
        with _QUEUE_LOCK:
            cur = _load_json(_QUEUE_PATH, [])
            if isinstance(cur, list):
                cur = [it for it in cur
                       if not (isinstance(it, dict)
                               and it.get("topic") in dropped_deleted)]
                _atomic_write_json(_QUEUE_PATH, cur)
        q = [it for it in q
             if not (isinstance(it, dict) and it.get("topic") in dropped_deleted)]

    max_topics = int(_flag("WIKI_MAX_TOPICS_PER_RUN", 25))
    max_docs = int(_flag("WIKI_MAX_DOCS_PER_TOPIC", 12))
    throttle = float(_flag("WIKI_BATCH_THROTTLE_SEC", 1.0))

    processed_doc_ids: set[str] = set()

    # Drop the catch-all "기타" bucket — unclassified docs have no
    # coherent theme and produce an unreadable mega-page.  Mark their
    # doc_ids as processed so they're removed from the queue file.
    for d in by_topic.pop("기타", []):
        did = d.get("doc_id")
        if did:
            processed_doc_ids.add(did)

    # Stable order: biggest topics first so a capped run does the most
    # impactful merges; remaining topics survive in the queue for next run.
    topics = sorted(by_topic.items(), key=lambda kv: len(kv[1]), reverse=True)

    idx = _load_json(_INDEX_PATH, {})
    if not isinstance(idx, dict):
        idx = {}

    results: list[dict] = []
    contradiction_topics: list[str] = []
    runs = 0
    budget_hit = False
    # Index records updated by THIS run. Persisted by re-applying onto a
    # fresh disk read — writing back the whole start-of-run `idx` snapshot
    # used to resurrect topics the user deleted/renamed mid-batch (the
    # loop yields between chunks, so /wiki_delete etc. can interleave).
    batch_updates: dict[str, dict] = {}

    # Phase 1: pre-filter and build task list
    task_list: list[tuple[str, list[dict]]] = []
    for topic, docs in topics:
        if len(task_list) >= max_topics:
            break
        existing_ids = set((idx.get(topic) or {}).get("doc_ids") or [])
        new_docs = [d for d in docs
                    if d.get("doc_id") not in existing_ids]
        if not new_docs:
            processed_doc_ids.update(d.get("doc_id") for d in docs)
            log.info("wiki skip %s: all %d docs already merged", topic, len(docs))
            continue
        task_list.append((topic, new_docs[:max_docs]))

    # Phase 2: process in parallel chunks (resume-safe per chunk)
    parallel = int(_flag("WIKI_PARALLEL", 3))
    for chunk_start in range(0, len(task_list), parallel):
        if budget > 0 and today_cost_krw() >= budget:
            budget_hit = True
            log.warning("wiki batch budget hit at ₩%.0f/%.0f — "
                        "%d topics left queued",
                        today_cost_krw(), budget,
                        len(task_list) - chunk_start)
            break

        chunk = task_list[chunk_start:chunk_start + parallel]
        merge_results = await asyncio.gather(
            *[_merge_topic(t, d) for t, d in chunk],
            return_exceptions=True,
        )

        for (topic, use_docs), res in zip(chunk, merge_results):
            runs += 1
            if isinstance(res, BaseException):
                log.warning("wiki merge exception for %s: %s", topic, res)
                res = {"topic": topic, "ok": False, "docs": len(use_docs),
                       "contradictions": 0, "error": str(res)[:120]}
            results.append(res)
            if res.get("ok"):
                merged_ids = [d["doc_id"] for d in use_docs]
                processed_doc_ids.update(merged_ids)
                existed = topic in idx
                rec = idx.get(topic) or {"doc_ids": []}
                seen = set(rec.get("doc_ids") or [])
                seen.update(merged_ids)
                now = _now_kst_iso()
                rec.update({
                    "file": f"{_slug(topic)}.md",
                    "title": topic,
                    "doc_ids": sorted(seen),
                    "updated": now,
                    "claims": rec.get("claims", 0) + res.get("docs", 0),
                })
                # Stamp `created` ONLY for a genuinely first-seen topic
                # (the `not existed` guard). A new topic gets created==
                # updated==now today; it KEEPS that stamp (New badge for
                # 7 days) until a later merge bumps `updated` past it.
                # Re-merging a pre-existing topic never adds `created`, so
                # a long-standing page can't be falsely flagged New — and
                # nothing strips a real newcomer's stamp on the next boot.
                if not existed and "created" not in rec:
                    rec["created"] = now
                idx[topic] = rec
                batch_updates[topic] = rec
                if res.get("contradictions", 0) > 0:
                    contradiction_topics.append(topic)
            else:
                record_failure(topic, use_docs, res.get("error", "unknown"))
                if promote_to_failed(topic):
                    log.warning("wiki topic '%s' moved to failed after %d cycles",
                                topic, _MAX_FAIL_CYCLES)

        # Persist after each chunk (resume-safety). Queue under the lock
        # (a threaded backfill may be merging items concurrently); index
        # re-applied onto a fresh disk read so mid-batch deletes/renames
        # by the user survive — a topic deleted mid-run is NOT re-written.
        with _QUEUE_LOCK:
            cur = _load_json(_QUEUE_PATH, [])
            if not isinstance(cur, list):
                cur = []
            remaining = [it for it in cur
                         if isinstance(it, dict)
                         and it.get("doc_id") not in processed_doc_ids]
            _atomic_write_json(_QUEUE_PATH, remaining)
        idx_disk = _load_json(_INDEX_PATH, {})
        if not isinstance(idx_disk, dict):
            idx_disk = {}
        deleted_now = _load_deleted_topics()
        for t, rec in batch_updates.items():
            if t in deleted_now:
                continue
            idx_disk[t] = rec
        _atomic_write_json(_INDEX_PATH, idx_disk)

        if throttle > 0:
            await asyncio.sleep(throttle)

    # Final queue cleanup: pre-filter may mark doc_ids as processed
    # (already-merged / 기타) without entering the chunk loop.
    if processed_doc_ids:
        with _QUEUE_LOCK:
            final_q = _load_json(_QUEUE_PATH, [])
            if isinstance(final_q, list):
                remaining = [it for it in final_q
                             if isinstance(it, dict)
                             and it.get("doc_id") not in processed_doc_ids]
                if len(remaining) < len(final_q):
                    _atomic_write_json(_QUEUE_PATH, remaining)

    pages_ok = sum(1 for r in results if r.get("ok"))
    if pages_ok:
        try:
            await obsidian.commit_subtree(
                "SecondBrain/Wiki",
                f"wiki: {pages_ok} pages, {len(processed_doc_ids)} docs "
                f"({datetime.now(_KST).strftime('%Y-%m-%d')})",
            )
        except Exception:
            log.exception("wiki git commit failed (pages still saved locally)")

    summary = {
        "status": "ok",
        "started": started,
        "finished": _now_kst_iso(),
        "topics": runs,
        "pages": pages_ok,
        "docs": len(processed_doc_ids),
        "contradictions": sum(r.get("contradictions", 0) for r in results),
        "contradiction_topics": contradiction_topics,
        "remaining_in_queue": queue_size(),
        "updated_topics": [r["topic"] for r in results if r.get("ok")],
        "errors": [f"{r['topic']}: {r.get('error')}"
                   for r in results if not r.get("ok")][:10],
        "budget_blocked": budget_hit,
        "today_cost": today_cost_krw(),
        "budget": budget,
    }
    _atomic_write_json(_LASTRUN_PATH, summary)
    log.info("wiki batch done: %s pages, %s docs, %s contradictions, "
             "%s left in queue (today ₩%.0f/%.0f%s)", summary["pages"],
             summary["docs"], summary["contradictions"],
             summary["remaining_in_queue"], summary["today_cost"], budget,
             " BUDGET-BLOCKED" if budget_hit else "")
    return summary


async def drain_queue(on_progress=None) -> list[dict]:
    """Loop run_batch() until the daily budget is exhausted or the queue
    is empty. ``on_progress`` is an optional async callback receiving
    each batch summary — callers use it for live Telegram updates.
    Returns the list of all batch summaries produced."""
    results: list[dict] = []
    cancelled = False
    try:
        while True:
            summary = await run_batch()
            results.append(summary)
            if on_progress:
                try:
                    await on_progress(summary)
                except Exception:
                    log.exception("drain_queue progress callback failed")
            if summary.get("budget_blocked"):
                break
            if summary.get("status") in ("disabled", "empty", "budget_blocked"):
                break
            if summary.get("remaining_in_queue", 0) == 0:
                break
            await asyncio.sleep(30)
    except asyncio.CancelledError:
        # Deploy/shutdown interrupted the drain — keep the temp budget so
        # _wiki_drain_resume picks it back up on the next startup.
        cancelled = True
        raise
    finally:
        # Any non-cancellation exit — normal completion OR a run_batch
        # error that can't make progress — reverts the cap so the override
        # never lingers on disk past the drain turn (was: only cleared on
        # the happy path, so one bad queued item froze the cap all day).
        if not cancelled:
            clear_temp_budget()
    return results


def last_run() -> dict | None:
    return _load_json(_LASTRUN_PATH, None)


# ----------------------------------------------------------------------
# Structural lint — ₩0 health scan (no LLM), Karpathy "Lint" operation.
# ----------------------------------------------------------------------

_CONTRA_MARKER = "## ⚠️ 검토 필요"   # written by _consolidate_topic on a clash


def lint(stale_days: int = 30, persist: bool = True) -> dict:
    """Zero-cost structural health scan of the wiki — NO LLM, no API,
    just an index read + one pass over each page. Surfaces drift the
    append/consolidate pipeline can't self-correct:

      • stale_singletons — 1-source topics not updated in `stale_days`
        days → merge (/wiki_dedup) or delete (/wiki_delete) candidates.
      • contradictions   — pages still carrying the `## ⚠️ 검토 필요`
        marker → need a human look (/wiki <topic>).
      • missing_pages    — topics in the index whose .md file is gone
        (index/file drift → dashboard 404s) → integrity fix needed.

    (An earlier "orphans = topics no page [[links]]" check was dropped:
    pages cite SOURCE docs via [[title]] but rarely cross-link topics,
    so it flagged ~99% of topics — pure noise, not a signal.)

    Result is persisted to wiki_lint.json so the dashboard health panel
    reads it cheaply instead of rescanning on its 60s render tick."""
    idx = _load_json(_INDEX_PATH, {})
    if not isinstance(idx, dict):
        idx = {}
    d = _wiki_dir()

    # Lexical date cutoff on the YYYY-MM-DD prefix — format-agnostic
    # between the naive-KST and isoformat timestamp writers.
    cutoff = (datetime.now(_KST) - timedelta(days=stale_days)).strftime("%Y-%m-%d")

    topic_names = [t for t in idx if isinstance(idx.get(t), dict)]

    contradiction_topics: list[str] = []
    missing_pages: list[str] = []
    pages_scanned = 0

    if d and d.exists():
        for t in topic_names:
            rec = idx[t]
            p = d / (rec.get("file") or f"{_slug(t)}.md")
            if not p.exists():
                missing_pages.append(t)
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            pages_scanned += 1
            if _CONTRA_MARKER in text:
                contradiction_topics.append(t)

    stale_singletons = []
    for t in topic_names:
        rec = idx[t]
        upd = (rec.get("updated") or "")[:10]
        if len(rec.get("doc_ids") or []) <= 1 and upd and upd < cutoff:
            stale_singletons.append({"topic": t,
                                     "docs": len(rec.get("doc_ids") or []),
                                     "updated": upd})
    stale_singletons.sort(key=lambda r: r["updated"])

    result = {
        "generated_at": _now_kst_iso(),
        "total_topics": len(topic_names),
        "pages_scanned": pages_scanned,
        "stale_days": stale_days,
        "stale_singletons": stale_singletons,
        "contradictions": sorted(contradiction_topics),
        "missing_pages": sorted(missing_pages),
    }
    if persist:
        try:
            _atomic_write_json(_LINT_PATH, result)
        except Exception:
            log.exception("wiki lint persist failed")
    return result


def last_lint() -> dict | None:
    return _load_json(_LINT_PATH, None)


def init() -> None:
    """Ensure the wiki dir exists when enabled. Safe to call always."""
    try:
        if not config.OBSIDIAN_VAULT_PATH:
            return
        d = _wiki_dir()
        if d is not None and bool(_flag("WIKI_ENABLED", False)):
            d.mkdir(parents=True, exist_ok=True)
    except Exception:
        log.exception("wiki init failed (non-fatal)")
