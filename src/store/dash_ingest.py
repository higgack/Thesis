"""Ingest request queue — lets the browser extension push a URL into the
bot's learning pipeline (RAG or 학습노트) with one click.

Same cross-container contract as dash_queries: the dashboard server (a
thin 200 MB stdlib HTTP container) cannot run the ingest pipeline itself
(chroma + Gemini → OOM, clients cold), so a click is parked here as a
row. The bot — warm clients, memory-sized, pressure-gated — drains it in
a job_queue tick, routes by `target` to pipeline.ingest_url (RAG) or
notes.channel.ingest_url (학습노트), and writes the result back. The
extension may poll the status endpoint to show ✓/✗.

WAL + timeout=30 (matching every other SQLite site here) keeps the two
writers from locking each other out.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

_DB_PATH = config.DATA_DIR / "dash_ingest.db"
_KST = timezone(timedelta(hours=9))

# Only these targets are accepted; anything else is coerced to 'rag'.
TARGETS = ("rag", "note")

_inited = False


def _now() -> str:
    return datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S")


def _init_once(c: sqlite3.Connection) -> None:
    # DDL once per process (not per connection) — polled every few
    # seconds by both the dashboard AND bot processes.
    global _inited
    if _inited:
        return
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS ingests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            url TEXT NOT NULL,
            target TEXT NOT NULL DEFAULT 'rag',
            status TEXT NOT NULL DEFAULT 'pending',
            result TEXT,
            error TEXT,
            done_ts TEXT
        )
        """
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_di_status ON ingests(status)")
    _inited = True


@contextmanager
def _conn():
    # A plain sqlite3.Connection's own context manager only commits,
    # never closes — every "with _conn() as c:" call site here was
    # leaking a connection + WAL handle. Wrapped as a real
    # contextmanager (kg.py pattern).
    c = sqlite3.connect(str(_DB_PATH), timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    _init_once(c)
    try:
        yield c
        c.commit()
    finally:
        c.close()


def enqueue(url: str, target: str = "rag") -> int:
    """Insert a pending ingest, return its id for browser polling."""
    target = target if target in TARGETS else "rag"
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO ingests(ts, url, target, status) "
            "VALUES (?, ?, ?, 'pending')",
            (_now(), (url or "").strip(), target),
        )
        return int(cur.lastrowid)


def has_active(url: str, target: str = "rag") -> bool:
    """True if an identical (url, target) is already pending or running —
    lets the server skip duplicate enqueues from accidental multi-clicks
    (the same video pressed 🧠/📒 several times)."""
    target = target if target in TARGETS else "rag"
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM ingests WHERE url=? AND target=? "
            "AND status IN ('pending','running') LIMIT 1",
            ((url or "").strip(), target)).fetchone()
    return row is not None


def reset_stale_running() -> int:
    """On bot startup, flip orphaned 'running' rows (claimed just before a
    crash/restart, never completed) back to 'pending' so the worker retries
    them instead of leaving them stuck forever. Safe to call before the
    worker job starts. Returns the number reset."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE ingests SET status='pending' WHERE status='running'")
        return cur.rowcount


def claim_pending(limit: int = 1) -> list[dict]:
    """Atomically flip up to `limit` pending rows to 'running' and return
    them. The conditional UPDATE makes the claim a no-double-run even if
    two ticks overlap."""
    out: list[dict] = []
    with _conn() as c:
        rows = c.execute(
            "SELECT id, url, target FROM ingests WHERE status='pending' "
            "ORDER BY id ASC LIMIT ?",
            (int(limit),),
        ).fetchall()
        for rid, url, target in rows:
            cur = c.execute(
                "UPDATE ingests SET status='running' "
                "WHERE id=? AND status='pending'",
                (rid,),
            )
            if cur.rowcount == 1:
                out.append({"id": int(rid), "url": url or "",
                            "target": target or "rag"})
    return out


def release(rid: int) -> None:
    """Put a claimed row back to pending (e.g. deferred under memory
    pressure) so a later tick retries it."""
    with _conn() as c:
        c.execute(
            "UPDATE ingests SET status='pending' "
            "WHERE id=? AND status='running'",
            (int(rid),),
        )


def complete(rid: int, status: str = "done", result: str = "",
             error: str | None = None) -> None:
    """Mark a row done/duplicate/empty/error with a short result string."""
    with _conn() as c:
        c.execute(
            "UPDATE ingests SET status=?, result=?, error=?, done_ts=? "
            "WHERE id=?",
            ("error" if error else (status or "done"),
             result or "", error, _now(), int(rid)),
        )


def get(rid: int) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT id, status, url, target, result, error "
            "FROM ingests WHERE id=?",
            (int(rid),),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "status": row[1], "url": row[2] or "",
        "target": row[3] or "rag", "result": row[4] or "",
        "error": row[5] or "",
    }


def recent_count(seconds: int) -> int:
    """How many ingests arrived in the last `seconds` — the dashboard
    uses this to reject floods before they ever reach the bot (each
    ingest is real Gemini spend)."""
    cutoff = (datetime.now(_KST) - timedelta(seconds=int(seconds))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM ingests WHERE ts >= ?", (cutoff,)
        ).fetchone()
    return int(row[0] or 0)


def purge_old(hours: int = 12) -> int:
    """Drop finished rows older than `hours` so the table stays tiny."""
    cutoff = (datetime.now(_KST) - timedelta(hours=int(hours))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM ingests WHERE status NOT IN ('pending','running') "
            "AND done_ts IS NOT NULL AND done_ts < ?",
            (cutoff,),
        )
        return cur.rowcount
