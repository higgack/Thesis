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
_FAILED_PATH = config.DATA_DIR / "wiki_failed.json"
_ALIASES_PATH = config.DATA_DIR / "wiki_aliases.json"
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


def budget_exceeded() -> bool:
    """True when today's (KST) wiki spend has reached the daily cap.
    0 disables the cap."""
    b = budget_krw()
    return b > 0 and today_cost_krw() >= b


# ----------------------------------------------------------------------
# Atomic JSON state (matches the repo's tmp→replace + .bak convention)
# ----------------------------------------------------------------------

def _atomic_write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    bak = path.with_suffix(path.suffix + ".bak")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    if path.exists():
        try:
            os.replace(path, bak)
        except OSError:
            pass
    os.replace(tmp, path)


def _load_json(path: Path, default):
    if not path.exists():
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


def _split_multi_topic(raw: str) -> list[str]:
    """Split a multi-company/topic string into individual names.
    Handles: '삼성전자, SK하이닉스' / '삼성전자 SK하이닉스' /
    '넥스틴, 파크시스템스, 인텍플러스'.
    Corporate suffixes (Inc, Corp, Ltd, …) are merged back with the
    preceding token so 'Broadcom Inc' stays as one entity."""
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
            parts = merged if len(merged) >= 2 else [raw]
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
            if topic == "기타":
                continue
            if (doc_id, topic) in queued_keys:
                continue
            q.append({
                "doc_id": doc_id,
                "title": title,
                "summary": summary,
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
    q = _load_json(_QUEUE_PATH, [])
    return len(q) if isinstance(q, list) else 0


def pending_list() -> list[dict]:
    """Queue contents grouped by topic for /wiki_pending display."""
    q = _load_json(_QUEUE_PATH, [])
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
    """After _MAX_FAIL_CYCLES, move topic's docs from queue to failed."""
    failed = _load_failed()
    rec = next((f for f in failed if f.get("topic") == topic), None)
    if not rec or rec.get("cycles", 0) < _MAX_FAIL_CYCLES:
        return False
    q = _load_json(_QUEUE_PATH, [])
    if not isinstance(q, list):
        return False
    remaining = [it for it in q
                 if not (isinstance(it, dict) and it.get("topic") == topic)]
    removed = len(q) - len(remaining)
    if removed > 0:
        rec["doc_count"] = removed
        rec["promoted"] = True
        _atomic_write_json(_QUEUE_PATH, remaining)
        _save_failed(failed)
    return removed > 0


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
    q = _load_json(_QUEUE_PATH, [])
    if not isinstance(q, list):
        q = []
    queued_keys = {(it.get("doc_id"), it.get("topic"))
                   for it in q if isinstance(it, dict)}
    ts = datetime.utcnow().isoformat(timespec="seconds")
    requeued = 0
    for doc_id in doc_ids:
        if (doc_id, topic) in queued_keys:
            continue
        try:
            d = meta.get_doc(doc_id)
        except Exception:
            continue
        if not d:
            continue
        q.append({
            "doc_id": doc_id,
            "title": d.get("title") or "",
            "summary": (d.get("summary") or "").strip(),
            "doc_type": d.get("type") or "",
            "source": d.get("source") or "",
            "topic": topic,
            "ts": ts,
        })
        queued_keys.add((doc_id, topic))
        requeued += 1
    _atomic_write_json(_QUEUE_PATH, q)
    new_failed = [f for f in failed if f.get("topic") != topic]
    _save_failed(new_failed)
    return {"retried": topic, "requeued": requeued,
            "note": f"{requeued}건 큐 복귀 — 다음 배치(/wiki_run)에서 재시도"}


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

    q = _load_json(_QUEUE_PATH, [])
    if not isinstance(q, list):
        q = []
    q = [it for it in q if not (isinstance(it, dict) and it.get("topic") == topic)]
    queued_keys = {
        (it.get("doc_id"), it.get("topic"))
        for it in q if isinstance(it, dict)
    }
    min_chars = int(_flag("WIKI_MIN_SUMMARY_CHARS", 800))
    ts = datetime.utcnow().isoformat(timespec="seconds")
    enqueued = 0
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
            if (doc_id, t) in queued_keys:
                continue
            q.append({
                "doc_id": doc_id,
                "title": d.get("title") or "",
                "summary": summary,
                "doc_type": d.get("type") or "",
                "source": d.get("source") or "",
                "topic": t,
                "ts": ts,
            })
            queued_keys.add((doc_id, t))
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
    """Load alias map: {alias_name → canonical_topic}."""
    d = _load_json(_ALIASES_PATH, {})
    return d if isinstance(d, dict) else {}


def _save_alias(alias: str, canonical: str) -> None:
    """Record an alias so future ingests auto-route."""
    aliases = _load_aliases()
    aliases[alias] = canonical
    _atomic_write_json(_ALIASES_PATH, aliases)


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
}


def resolve_topic(proposed: str) -> str:
    """Map a proposed topic name to an existing canonical topic.
    Checked at ingest time so duplicates are prevented, not just detected.
    Order: exact alias → CJK translate → dedup-key → substring → passthrough."""
    # 1) Exact alias hit (covers Korean↔English pairs set by merge)
    aliases = _load_aliases()
    if proposed in aliases:
        return aliases[proposed]

    # 1b) CJK (Chinese/Japanese) → Korean/English translation
    if _CJK_RE.search(proposed):
        translated = _CJK_TRANSLATE.get(proposed.strip())
        if translated:
            _save_alias(proposed, translated)
            return resolve_topic(translated)

    # 2) Dedup-key match against existing index topics
    idx = _load_json(_INDEX_PATH, {})
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

    q = _load_json(_QUEUE_PATH, [])
    if not isinstance(q, list):
        q = []
    for it in q:
        if isinstance(it, dict) and it.get("topic") == absorb:
            it["topic"] = keep
    queued_keys = {(it.get("doc_id"), it.get("topic"))
                   for it in q if isinstance(it, dict)}
    from . import meta
    enqueued = 0
    for doc_id in new_ids:
        if (doc_id, keep) in queued_keys:
            continue
        try:
            d = meta.get_doc(doc_id)
        except Exception:
            continue
        if not d:
            continue
        summary = (d.get("summary") or "").strip()
        q.append({
            "doc_id": doc_id,
            "title": d.get("title") or "",
            "summary": summary,
            "doc_type": d.get("type") or "",
            "source": d.get("source") or "",
            "topic": keep,
            "ts": _now_kst_iso(),
        })
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
    if old_name not in idx:
        return {"error": f"토픽 '{old_name}' 인덱스에 없음"}
    if new_name in idx:
        return {"error": f"토픽 '{new_name}' 이미 존재 — /wiki_dedup merge 사용"}

    rec = idx.pop(old_name)
    new_file = f"{_slug(new_name)}.md"
    old_path = _page_path(old_name)
    rec["file"] = new_file
    rec["title"] = new_name
    idx[new_name] = rec
    _atomic_write_json(_INDEX_PATH, idx)

    renamed_file = False
    d = _wiki_dir()
    if d and old_path and old_path.exists():
        try:
            old_path.rename(d / new_file)
            renamed_file = True
        except Exception:
            pass

    q = _load_json(_QUEUE_PATH, [])
    remapped = 0
    if isinstance(q, list):
        for it in q:
            if isinstance(it, dict) and it.get("topic") == old_name:
                it["topic"] = new_name
                remapped += 1
        if remapped:
            _atomic_write_json(_QUEUE_PATH, q)

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

    q = _load_json(_QUEUE_PATH, [])
    removed_queue = 0
    if isinstance(q, list):
        new_q = [it for it in q
                 if not (isinstance(it, dict) and it.get("topic") == topic)]
        removed_queue = len(q) - len(new_q)
        if removed_queue:
            _atomic_write_json(_QUEUE_PATH, new_q)

    if not rec and removed_queue == 0:
        return {"error": f"토픽 '{topic}' 인덱스/큐에 없음"}
    return {"ok": True, "topic": topic, "deleted_file": deleted_file,
            "removed_queue": removed_queue, "had_index": rec is not None}


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
    wikied = _wikied_doc_ids()
    failed_ids = _failed_doc_ids()
    q = _load_json(_QUEUE_PATH, [])
    if not isinstance(q, list):
        q = []
    queued_ids = {it.get("doc_id") for it in q if isinstance(it, dict)}
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
                 if t != "기타"]
        if not tlist:
            res["skipped_gate"] += 1
            continue
        ts = datetime.utcnow().isoformat(timespec="seconds")
        for topic in tlist:
            q.append({
                "doc_id": doc_id,
                "title": d.get("title") or "",
                "summary": summary,
                "doc_type": d.get("type") or d.get("doc_type") or "",
                "source": d.get("source") or "",
                "topic": topic,
                "ts": ts,
            })
        queued_ids.add(doc_id)
        res["enqueued"] += 1
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
    index (cheap); falls back to scanning the dir if the index is missing."""
    idx = _load_json(_INDEX_PATH, {})
    out: list[dict] = []
    if isinstance(idx, dict) and idx:
        for topic, rec in idx.items():
            if not isinstance(rec, dict):
                continue
            out.append({
                "topic": topic,
                "docs": len(rec.get("doc_ids") or []),
                "updated": rec.get("updated") or "",
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
    "덜 중요한 항목을 병합한다.\n\n"
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


async def _merge_topic(topic: str, docs: list[dict]) -> dict:
    """One LLM merge for one topic. Returns a result row; never raises
    (a single bad topic must not abort the run)."""
    from ..llm.gemini import complete
    existing = read_page(topic) or ""
    user = _build_merge_user(topic, existing, docs)
    model = _flag("WIKI_MERGE_MODEL", None) or config.ANSWER_MODEL
    max_tokens = int(_flag("WIKI_MERGE_MAX_TOKENS", 8000))
    try:
        raw = await complete(
            model=model,
            system=_MERGE_SYSTEM,
            user=user,
            max_tokens=max_tokens,
            temperature=0.2,
            purpose="wiki",          # ← cost.db tags wiki spend separately
        )
    except Exception as e:
        log.warning("wiki merge LLM failed for %s: %s", topic, e)
        return {"topic": topic, "ok": False, "docs": len(docs),
                "contradictions": 0, "error": str(e)[:120]}
    page, meta = _parse_merge(raw)
    # Post-process: resolve "자료 N" labels → actual document titles.
    # The LLM sometimes writes [[자료 2]] instead of the real title.
    # Skip substitution if the title contains non-Korean/English chars
    # (CJK etc.) — the LLM was instructed to translate, so its organic
    # citations are already in Korean; forcing the raw title would
    # reintroduce the foreign text.
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
                "contradictions": 0, "error": "empty merge output"}
    p = _page_path(topic)
    if p is None:
        return {"topic": topic, "ok": False, "docs": len(docs),
                "contradictions": 0, "error": "no vault"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(page, encoding="utf-8")
    except Exception as e:
        log.exception("wiki page write failed for %s", topic)
        return {"topic": topic, "ok": False, "docs": len(docs),
                "contradictions": 0, "error": str(e)[:120]}
    return {
        "topic": topic, "ok": True, "docs": len(docs),
        "contradictions": int(meta.get("contradictions", 0)),
        "rel": p.relative_to(Path(config.OBSIDIAN_VAULT_PATH).resolve()).as_posix(),
    }


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
                rec = idx.get(topic) or {"doc_ids": []}
                seen = set(rec.get("doc_ids") or [])
                seen.update(merged_ids)
                rec.update({
                    "file": f"{_slug(topic)}.md",
                    "title": topic,
                    "doc_ids": sorted(seen),
                    "updated": _now_kst_iso(),
                    "claims": rec.get("claims", 0) + res.get("docs", 0),
                })
                idx[topic] = rec
                if res.get("contradictions", 0) > 0:
                    contradiction_topics.append(topic)
            else:
                record_failure(topic, use_docs, res.get("error", "unknown"))
                if promote_to_failed(topic):
                    log.warning("wiki topic '%s' moved to failed after %d cycles",
                                topic, _MAX_FAIL_CYCLES)

        # Persist after each chunk (resume-safety)
        cur = _load_json(_QUEUE_PATH, [])
        if not isinstance(cur, list):
            cur = []
        remaining = [it for it in cur
                     if isinstance(it, dict)
                     and it.get("doc_id") not in processed_doc_ids]
        _atomic_write_json(_QUEUE_PATH, remaining)
        _atomic_write_json(_INDEX_PATH, idx)

        if throttle > 0:
            await asyncio.sleep(throttle)

    # Final queue cleanup: pre-filter may mark doc_ids as processed
    # (already-merged / 기타) without entering the chunk loop.
    if processed_doc_ids:
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
    return results


def last_run() -> dict | None:
    return _load_json(_LASTRUN_PATH, None)


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
