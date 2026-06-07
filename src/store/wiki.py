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
from datetime import datetime
from pathlib import Path

from .. import config
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
    return float(_flag("WIKI_DAILY_BUDGET_KRW", 2000))


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


def _split_multi_topic(raw: str) -> list[str]:
    """Split a multi-company/topic string into individual names.
    Handles: '삼성전자, SK하이닉스' / '삼성전자 SK하이닉스' /
    '넥스틴, 파크시스템스, 인텍플러스'."""
    parts = re.split(r"[,;/·]|\s{2,}", raw)
    if len(parts) == 1:
        tokens = raw.split()
        if len(tokens) >= 2 and all(len(t) >= 2 for t in tokens):
            parts = tokens
    return [p.strip() for p in parts if p.strip()]


def topics_for(metadata: dict | None, title: str) -> list[str]:
    """Return a LIST of wiki topics for a doc. Multi-company docs get
    routed to multiple pages (one queue entry per company)."""
    md = metadata or {}
    company = (md.get("company") or "").strip()
    if company:
        split = _split_multi_topic(company)
        if len(split) >= 2:
            return split
        return [company]
    tags = md.get("tags") or []
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, str) and t.strip():
                return [t.strip()]
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
        min_chars = int(_flag("WIKI_MIN_SUMMARY_CHARS", 600))
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


def decompose_merged_topic(topic: str) -> dict:
    """Delete a merged multi-company page and re-enqueue its docs so they
    get routed to individual company pages. Returns stats."""
    from . import meta
    idx = _load_json(_INDEX_PATH, {})
    if not isinstance(idx, dict) or topic not in idx:
        return {"error": f"토픽 '{topic}' 인덱스에 없음"}
    rec = idx[topic]
    doc_ids = rec.get("doc_ids") or []
    if not doc_ids:
        return {"error": "doc_ids 비어있음"}

    q = _load_json(_QUEUE_PATH, [])
    if not isinstance(q, list):
        q = []
    queued_keys = {
        (it.get("doc_id"), it.get("topic"))
        for it in q if isinstance(it, dict)
    }
    min_chars = int(_flag("WIKI_MIN_SUMMARY_CHARS", 600))
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


def backfill(docs: list[dict]) -> dict:
    """Enqueue EXISTING corpus docs (from meta.docs_since) into the wiki
    queue so the nightly batch will synthesize them too — not just new
    ingests. Skips docs already in a wiki page, already queued, or below
    the importance gate. Spends ₩0 (enqueue only); the nightly batch does
    the actual merging under the daily budget + per-run caps, so a huge
    backfill simply drains over several capped nights. Returns counts."""
    res = {"enqueued": 0, "skipped_wikied": 0, "skipped_gate": 0,
           "skipped_queued": 0, "total": len(docs)}
    if not enabled():
        res["error"] = "wiki disabled"
        return res
    min_chars = int(_flag("WIKI_MIN_SUMMARY_CHARS", 600))
    wikied = _wikied_doc_ids()
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
        summary = (d.get("summary") or "").strip()
        if len(summary) < min_chars:
            res["skipped_gate"] += 1
            continue
        tlist = topics_for(d.get("metadata"), d.get("title") or "")
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
    max_page_chars = int(_flag("WIKI_MAX_PAGE_CHARS", 6000))
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
        "`— 출처: [[자료제목]]` 한 번만. 매 문장 반복 금지.\n"
        "3. 새 자료가 기존 주장과 **모순**되면 기존 내용을 지우지 말고 "
        "`## ⚠️ 검토 필요` 섹션에 `- 기존: … / 신규: … (출처)` 형식으로 "
        "적는다.\n"
        "4. 페이지 맨 위에 한 줄 요약(`> …`)을 유지/갱신한다.\n"
        "5. **일반 용어 해설 금지**: 업계 상식 수준의 용어 정의(ALD란, "
        "CMP란 등)는 적지 않는다. 이 회사/토픽에 고유한 정보만.\n"
        "6. 출력은 **페이지 마크다운 전문**만. 맨 마지막 줄에 정확히 "
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
    max_tokens = int(_flag("WIKI_MERGE_MAX_TOKENS", 3000))
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
    started = datetime.utcnow().isoformat(timespec="seconds")
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

    # Group queued docs by topic.
    by_topic: dict[str, list[dict]] = {}
    for it in q:
        if not isinstance(it, dict):
            continue
        by_topic.setdefault(it.get("topic") or "기타", []).append(it)

    max_topics = int(_flag("WIKI_MAX_TOPICS_PER_RUN", 25))
    max_docs = int(_flag("WIKI_MAX_DOCS_PER_TOPIC", 6))
    throttle = float(_flag("WIKI_BATCH_THROTTLE_SEC", 1.0))

    # Stable order: biggest topics first so a capped run does the most
    # impactful merges; remaining topics survive in the queue for next run.
    topics = sorted(by_topic.items(), key=lambda kv: len(kv[1]), reverse=True)

    idx = _load_json(_INDEX_PATH, {})
    if not isinstance(idx, dict):
        idx = {}

    processed_doc_ids: set[str] = set()
    results: list[dict] = []
    contradiction_topics: list[str] = []
    runs = 0
    budget_hit = False

    for topic, docs in topics:
        if runs >= max_topics:
            break
        # Spend accrues during the run — stop the moment we cross the daily
        # cap so we never blow far past it (overshoot ≤ one merge, ~₩13).
        if budget > 0 and today_cost_krw() >= budget:
            budget_hit = True
            log.warning("wiki batch budget hit mid-run at ₩%.0f/%.0f — "
                        "stopping (%d topics left queued)",
                        today_cost_krw(), budget, len(topics) - runs)
            break
        runs += 1
        use_docs = docs[:max_docs]
        res = await _merge_topic(topic, use_docs)
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
                "updated": datetime.utcnow().isoformat(timespec="seconds"),
                "claims": rec.get("claims", 0) + res.get("docs", 0),
            })
            idx[topic] = rec
            if res.get("contradictions", 0) > 0:
                contradiction_topics.append(topic)
            # Persist progress after each topic (resume-safety): drop the
            # processed docs from the queue and save the index now, so a
            # crash mid-run never re-merges or loses work. Re-load the
            # queue from disk first (not the start-of-run snapshot) so docs
            # ingested *during* the batch — the merge awaits yield the loop,
            # letting enqueue() run — survive instead of being clobbered.
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

    # Commit the updated wiki subtree to the vault git repo (versioning +
    # one-command rollback). Reuses obsidian's lock + push circuit-breaker.
    pages_ok = sum(1 for r in results if r.get("ok"))
    if pages_ok:
        try:
            await obsidian.commit_subtree(
                "SecondBrain/Wiki",
                f"wiki: {pages_ok} pages, {len(processed_doc_ids)} docs "
                f"({datetime.utcnow().date().isoformat()})",
            )
        except Exception:
            log.exception("wiki git commit failed (pages still saved locally)")

    summary = {
        "status": "ok",
        "started": started,
        "finished": datetime.utcnow().isoformat(timespec="seconds"),
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
