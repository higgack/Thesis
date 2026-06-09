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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

_DB_PATH = config.DATA_DIR / "dash_queries.db"
_KST = timezone(timedelta(hours=9))


def _now() -> str:
    return datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_DB_PATH), timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
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
    return c


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
             kind: str = "qa", error: str | None = None) -> None:
    """Mark a row done (or 'error' when `error` is set) with its result."""
    with _conn() as c:
        c.execute(
            "UPDATE queries SET status=?, kind=?, answer=?, sources=?, "
            "error=?, done_ts=? WHERE id=?",
            (
                "error" if error else "done",
                kind,
                answer or "",
                json.dumps(sources or [], ensure_ascii=False),
                error,
                _now(),
                int(qid),
            ),
        )


def get(qid: int) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT id, status, query, kind, answer, sources, error "
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
