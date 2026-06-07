"""Per-call Gemini cost tracker.

Every Gemini call (text gen, embedding, web grounding) feeds its
usage_metadata through `record()`, which writes a row to SQLite and
returns the rough KRW cost. Aggregation helpers (today/period) power
the /usage display so the user can see real spend without hitting the
Google Cloud Billing API.

The KRW conversion is hard-coded to a sane default; pricing per model
mirrors Gemini 2.5 public list rates as of late 2025."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

USD_TO_KRW = 1400  # rough; ok for back-of-envelope reporting

# Gemini 2.5 bills cached input tokens at 0.25× the normal input rate
# (75% discount), for both implicit (automatic, default-on) and
# explicit caching. usage_metadata.prompt_token_count INCLUDES the
# cached tokens, so we split them out and price the cached portion
# cheaper. Without this, /usage over-reports spend on every query —
# the agent loop re-sends a stable system + tool-schema prefix that
# Gemini caches automatically across the loop's steps and same-day
# queries.
CACHED_INPUT_RATE = 0.25

# All UI-facing aggregates are bucketed by Korean local time so the
# user's "today" matches their wall clock. Storage stays in UTC ISO
# strings (ts column) for portability — only the boundaries differ.
KST = timezone(timedelta(hours=9))

# Gemini 2.5 list price ($/1M tokens). Audio input is billed higher
# than text but usage_metadata.prompt_token_count rolls them together,
# so we treat everything as text-equivalent — this slightly under-
# estimates audio-heavy days, fine for a rough indicator.
_PRICES_USD = {
    "gemini-2.5-pro":          {"in": 1.25,  "out": 10.00},
    "gemini-2.5-flash":        {"in": 0.30,  "out":  2.50},
    "gemini-2.5-flash-lite":   {"in": 0.10,  "out":  0.40},
    "gemini-embedding-001":    {"in": 0.15,  "out":  0.00},
}

_DB_PATH = config.DATA_DIR / "cost.db"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_DB_PATH))
    c.execute("""
        CREATE TABLE IF NOT EXISTS calls (
            ts TEXT NOT NULL,
            model TEXT NOT NULL,
            in_tokens INTEGER NOT NULL,
            out_tokens INTEGER NOT NULL,
            cost_krw REAL NOT NULL,
            purpose TEXT NOT NULL DEFAULT 'unknown',
            cached_tokens INTEGER NOT NULL DEFAULT 0
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts)")
    # Migrate older DBs that pre-date later columns.
    cols = {row[1] for row in c.execute("PRAGMA table_info(calls)")}
    if "purpose" not in cols:
        c.execute("ALTER TABLE calls ADD COLUMN purpose TEXT NOT NULL DEFAULT 'unknown'")
    if "cached_tokens" not in cols:
        c.execute("ALTER TABLE calls ADD COLUMN cached_tokens INTEGER NOT NULL DEFAULT 0")
    return c


def _price_krw(model: str, in_tokens: int, out_tokens: int,
               cached_tokens: int = 0) -> float:
    p = _PRICES_USD.get(model) or _PRICES_USD.get(_normalize(model))
    if not p:
        return 0.0
    # prompt_token_count already includes cached_tokens — split them so
    # the cached slice is billed at the discounted rate.
    cached = max(0, min(cached_tokens, in_tokens))
    fresh_in = in_tokens - cached
    usd = (fresh_in * p["in"]
           + cached * p["in"] * CACHED_INPUT_RATE
           + out_tokens * p["out"]) / 1_000_000
    return usd * USD_TO_KRW


def _normalize(model: str) -> str:
    """Map versioned ids ('models/gemini-2.5-flash') to base ids."""
    if model.startswith("models/"):
        model = model[len("models/"):]
    return model


def record(model: str, in_tokens: int = 0, out_tokens: int = 0,
           purpose: str = "unknown", cached_tokens: int = 0) -> float:
    """Persist one call. `purpose` is a free-form tag used for
    breakdowns ('ingest' vs 'query', etc.). `cached_tokens` is the
    cache-hit slice of `in_tokens` (billed at CACHED_INPUT_RATE).
    Returns the KRW cost it added so callers can log / surface it
    inline. Swallow all errors — billing tracking must never break a
    user request."""
    try:
        cost = _price_krw(_normalize(model), in_tokens, out_tokens,
                          cached_tokens)
        with _conn() as c:
            c.execute(
                "INSERT INTO calls(ts, model, in_tokens, out_tokens, cost_krw,"
                " purpose, cached_tokens) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (datetime.utcnow().isoformat(timespec="seconds"),
                 _normalize(model), int(in_tokens), int(out_tokens), cost,
                 purpose or "unknown", int(cached_tokens)),
            )
        return cost
    except Exception:
        log.exception("cost record failed")
        return 0.0


def record_resp(model: str, resp, purpose: str = "unknown") -> float:
    """Convenience wrapper — pull token counts off a Gemini response."""
    um = getattr(resp, "usage_metadata", None)
    if not um:
        return 0.0
    in_tok = getattr(um, "prompt_token_count", 0) or 0
    out_tok = getattr(um, "candidates_token_count", 0) or 0
    cached = getattr(um, "cached_content_token_count", 0) or 0
    # Gemini 2.5 thinking models bill reasoning tokens at the OUTPUT
    # rate, but report them in `thoughts_token_count` — a field NOT
    # included in candidates_token_count. Without folding it in, /usage
    # and the dashboard under-report every flash/pro answer + /deep call
    # (often by 30-60% of the visible output on reasoning-heavy turns).
    thoughts = getattr(um, "thoughts_token_count", 0) or 0
    out_tok += thoughts
    return record(model, in_tok, out_tok, purpose, cached_tokens=cached)


def _since(start_iso: str) -> dict:
    with _conn() as c:
        cur = c.execute(
            "SELECT model, SUM(in_tokens), SUM(out_tokens), SUM(cost_krw),"
            "       COUNT(*), SUM(cached_tokens) FROM calls "
            "WHERE ts >= ? GROUP BY model",
            (start_iso,),
        )
        rows = cur.fetchall()
        cur2 = c.execute(
            "SELECT purpose, SUM(cost_krw), COUNT(*) FROM calls "
            "WHERE ts >= ? GROUP BY purpose",
            (start_iso,),
        )
        purpose_rows = cur2.fetchall()
    by_model = {}
    total = 0.0
    calls = 0
    total_in = 0
    total_cached = 0
    for model, in_tok, out_tok, cost, n, cached_tok in rows:
        by_model[model] = {
            "in": int(in_tok or 0),
            "out": int(out_tok or 0),
            "cost": float(cost or 0.0),
            "calls": int(n or 0),
            "cached": int(cached_tok or 0),
        }
        total += float(cost or 0.0)
        calls += int(n or 0)
        total_in += int(in_tok or 0)
        total_cached += int(cached_tok or 0)
    by_purpose = {
        (purpose or "unknown"): {
            "cost": float(cost or 0.0),
            "calls": int(n or 0),
        }
        for purpose, cost, n in purpose_rows
    }
    return {"by_model": by_model, "by_purpose": by_purpose,
            "total_krw": total, "calls": calls,
            "total_in": total_in, "total_cached": total_cached}


def _kst_day_start_utc(d) -> datetime:
    """KST midnight on date `d` expressed in UTC, naive (matches the
    ts column which is UTC ISO without offset)."""
    kst_midnight = datetime.combine(d, time.min, tzinfo=KST)
    return kst_midnight.astimezone(timezone.utc).replace(tzinfo=None)


def today_krw() -> dict:
    """KST today (00:00 KST → now)."""
    today = datetime.now(KST).date()
    start = _kst_day_start_utc(today)
    return _since(start.isoformat(timespec="seconds"))


def period_krw(days: int) -> dict:
    """Last `days` complete KST days plus today, e.g. days=7 → last
    7 KST days inclusive of today."""
    today = datetime.now(KST).date()
    start = _kst_day_start_utc(today - timedelta(days=days - 1))
    return _since(start.isoformat(timespec="seconds"))


def month_to_date_krw() -> dict:
    """Current calendar month in KST: 1st 00:00 KST → now."""
    today = datetime.now(KST).date()
    first = today.replace(day=1)
    start = _kst_day_start_utc(first)
    out = _since(start.isoformat(timespec="seconds"))
    out["year"] = today.year
    out["month"] = today.month
    out["day"] = today.day
    return out


def all_time_krw() -> dict:
    """All-time cumulative cost breakdown."""
    return _since("2000-01-01T00:00:00")


def daily_breakdown(days: int = 7) -> list[dict]:
    """Per-day KRW totals for the last `days` KST days (newest first).

    Each row is a KST-local date with everything that fell within
    that 24-hour window. Empty days show ₩0 / 0 calls so gaps are
    visible rather than missing."""
    today = datetime.now(KST).date()
    days_list = [today - timedelta(days=i) for i in range(days)]
    seen: dict[str, tuple[float, int]] = {}
    with _conn() as c:
        for d in days_list:
            start = _kst_day_start_utc(d).isoformat(timespec="seconds")
            end = _kst_day_start_utc(d + timedelta(days=1)).isoformat(timespec="seconds")
            row = c.execute(
                "SELECT SUM(cost_krw), COUNT(*) FROM calls "
                "WHERE ts >= ? AND ts < ?",
                (start, end),
            ).fetchone()
            seen[d.isoformat()] = (float(row[0] or 0.0), int(row[1] or 0))
    return [
        {"date": d.isoformat(),
         "cost": seen[d.isoformat()][0],
         "calls": seen[d.isoformat()][1]}
        for d in days_list
    ]
