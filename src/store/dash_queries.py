"""Dashboard query queue — lets the web search box ask the bot.

The dashboard container is a thin stdlib HTTP server capped at 200 MB; it
cannot run the agent itself (RAG + rerank + Pro synthesis would OOM, and
the LLM clients are cold there). So a query typed into the dashboard
search box is parked here as a row, the bot — which already has the agent
warm and is memory-sized, with its own pressure gate — drains it in a
job_queue tick, runs `agent.run()` (paid) or a read-only command (free),
and writes the answer back. The browser polls until the row is done.

Cross-container by design: the dashboard WRITES requests, the bot WRITES
answers, both over the shared ./data volume. WAL + timeout=30 (matching
every other SQLite site in this project) keeps the two writers from
locking each other out.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

_DB_PATH = config.DATA_DIR / "dash_queries.db"
_KST = timezone(timedelta(hours=9))

_inited = False


def _now() -> str:
    return datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S")


def _init_once(c: sqlite3.Connection) -> None:
    # DDL once per process (not per connection) — this module is polled
    # every few seconds by both the dashboard AND bot processes.
    global _inited
    if _inited:
        return
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            query TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            kind TEXT,
            answer TEXT,
            sources TEXT,
            error TEXT,
            done_ts TEXT
        )
        """
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_dq_status ON queries(status)")
    # Migrate DBs that predate the column (both containers open this file).
    cols = {row[1] for row in c.execute("PRAGMA table_info(queries)")}
    if "pro_count" not in cols:
        # >0 means the run tripped the Pro-confirmation gate (that many
        # documents) and was answered on Flash anyway. The browser turns
        # it into a "Pro로 다시 답변" button, so the user can take the
        # ~₩150 upgrade or just keep the Flash answer already on screen —
        # the choice Telegram gets from inline buttons (2026-09-06).
        try:
            c.execute("ALTER TABLE queries ADD COLUMN pro_count INTEGER "
                      "NOT NULL DEFAULT 0")
        except sqlite3.OperationalError as e:
            # Two processes open this file (dashboard + bot), so both can
            # read PRAGMA table_info before either ALTERs and the loser
            # gets "duplicate column name". Harmless — the column exists,
            # which is all we wanted. Anything else is a real failure.
            if "duplicate column" not in str(e).lower():
                raise
    _inited = True


@contextmanager
def _conn():
    # A plain sqlite3.Connection's own context manager only commits,
    # never closes — every "with _conn() as c:" call site here was
    # leaking a connection + WAL handle, on both the dashboard's and the
    # bot's polling ticks. Wrapped as a real contextmanager (kg.py
    # pattern).
    c = sqlite3.connect(str(_DB_PATH), timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    _init_once(c)
    try:
        yield c
        c.commit()
    finally:
        c.close()


def enqueue(query: str) -> int:
    """Insert a pending request, return its id. The dashboard hands the
    id back to the browser for polling."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO queries(ts, query, status) VALUES (?, ?, 'pending')",
            (_now(), (query or "").strip()),
        )
        return int(cur.lastrowid)


def claim_pending(limit: int = 1) -> list[dict]:
    """Atomically flip up to `limit` pending rows to 'running' and return
    them. The conditional UPDATE (status still 'pending') makes the claim
    a no-double-run even if two ticks overlap."""
    out: list[dict] = []
    with _conn() as c:
        rows = c.execute(
            "SELECT id, query FROM queries WHERE status='pending' "
            "ORDER BY id ASC LIMIT ?",
            (int(limit),),
        ).fetchall()
        for qid, q in rows:
            cur = c.execute(
                "UPDATE queries SET status='running' "
                "WHERE id=? AND status='pending'",
                (qid,),
            )
            if cur.rowcount == 1:
                out.append({"id": int(qid), "query": q or ""})
    return out


def release(qid: int) -> None:
    """Put a claimed row back to pending (e.g. deferred under memory
    pressure) so a later tick retries it."""
    with _conn() as c:
        c.execute(
            "UPDATE queries SET status='pending' WHERE id=? AND status='running'",
            (int(qid),),
        )


def complete(qid: int, answer: str = "", sources: list[str] | None = None,
             kind: str = "qa", error: str | None = None,
             pro_count: int = 0) -> None:
    """Mark a row done (or 'error' when `error` is set) with its result.
    `pro_count` >0 records that the Pro gate fired — see _init_once."""
    with _conn() as c:
        c.execute(
            "UPDATE queries SET status=?, kind=?, answer=?, sources=?, "
            "error=?, pro_count=?, done_ts=? WHERE id=?",
            (
                "error" if error else "done",
                kind,
                answer or "",
                json.dumps(sources or [], ensure_ascii=False),
                error,
                int(pro_count or 0),
                _now(),
                int(qid),
            ),
        )


def get(qid: int) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT id, status, query, kind, answer, sources, error, "
            "       COALESCE(pro_count, 0) "
            "FROM queries WHERE id=?",
            (int(qid),),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "status": row[1],
        "query": row[2] or "",
        "kind": row[3] or "",
        "answer": row[4] or "",
        "sources": json.loads(row[5] or "[]"),
        "error": row[6] or "",
        "pro_count": int(row[7] or 0),
    }


def recent_count(seconds: int) -> int:
    """How many requests arrived in the last `seconds` — the dashboard
    uses this to reject floods before they ever reach the bot (each Q&A
    is a real Gemini spend)."""
    cutoff = (datetime.now(_KST) - timedelta(seconds=int(seconds))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM queries WHERE ts >= ?", (cutoff,)
        ).fetchone()
    return int(row[0] or 0)


def purge_old(hours: int = 6) -> int:
    """Drop finished rows older than `hours` so the table stays tiny.
    Returns rows deleted."""
    cutoff = (datetime.now(_KST) - timedelta(hours=int(hours))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM queries WHERE status IN ('done','error') "
            "AND done_ts IS NOT NULL AND done_ts < ?",
            (cutoff,),
        )
        return cur.rowcount
