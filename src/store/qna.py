"""Permanent Q&A archive.

Every successful agent reply is appended here so the user can browse
past analyses long after the rolling chat memory has expired. Schema
is intentionally narrow — one row per turn, JSON for the structured
bits — so the dashboard can read it with raw SQL."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime

from .. import config

log = logging.getLogger(__name__)

_DB_PATH = config.DATA_DIR / "qna.db"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_DB_PATH), timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""
        CREATE TABLE IF NOT EXISTS qna (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            sources TEXT NOT NULL DEFAULT '[]',
            tools TEXT NOT NULL DEFAULT '[]',
            model TEXT,
            warning TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_qna_ts ON qna(ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_qna_chat ON qna(chat_id)")
    return c


def record(chat_id: int, question: str, answer: str,
           sources: list[str] | None = None,
           tools: list[str] | None = None,
           model: str | None = None,
           warning: str | None = None) -> None:
    """Persist one Q&A turn. Errors are swallowed — archiving must
    never block a user reply."""
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO qna(ts, chat_id, question, answer, "
                "sources, tools, model, warning) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.utcnow().isoformat(timespec="seconds"),
                    int(chat_id),
                    question or "",
                    answer or "",
                    json.dumps(sources or [], ensure_ascii=False),
                    json.dumps(tools or [], ensure_ascii=False),
                    model,
                    warning,
                ),
            )
    except Exception:
        log.exception("qna record failed")


def _row_to_dict(row: tuple) -> dict:
    cols = ("id", "ts", "question", "answer", "sources",
            "tools", "model", "warning")
    d = dict(zip(cols, row))
    try:
        d["sources"] = json.loads(d["sources"] or "[]")
    except Exception:
        d["sources"] = []
    try:
        d["tools"] = json.loads(d["tools"] or "[]")
    except Exception:
        d["tools"] = []
    return d


def recent(limit: int = 100, offset: int = 0,
           search: str | None = None) -> list[dict]:
    """Newest first. `search` does case-insensitive substring match
    over both question and answer."""
    with _conn() as c:
        if search:
            like = f"%{search}%"
            cur = c.execute(
                "SELECT id, ts, question, answer, sources, tools, "
                "model, warning FROM qna "
                "WHERE question LIKE ? OR answer LIKE ? "
                "ORDER BY ts DESC LIMIT ? OFFSET ?",
                (like, like, int(limit), int(offset)),
            )
        else:
            cur = c.execute(
                "SELECT id, ts, question, answer, sources, tools, "
                "model, warning FROM qna "
                "ORDER BY ts DESC LIMIT ? OFFSET ?",
                (int(limit), int(offset)),
            )
        return [_row_to_dict(r) for r in cur.fetchall()]


def get(qna_id: int) -> dict | None:
    with _conn() as c:
        cur = c.execute(
            "SELECT id, ts, question, answer, sources, tools, "
            "model, warning FROM qna WHERE id = ?",
            (int(qna_id),),
        )
        row = cur.fetchone()
        return _row_to_dict(row) if row else None


def count() -> int:
    with _conn() as c:
        cur = c.execute("SELECT COUNT(*) FROM qna")
        return int(cur.fetchone()[0])


def delete(qna_id: int) -> int:
    """Drop one Q&A row by primary key. Returns rows affected."""
    try:
        with _conn() as c:
            cur = c.execute("DELETE FROM qna WHERE id = ?", (int(qna_id),))
            return cur.rowcount
    except Exception:
        log.exception("qna delete failed")
        return 0


def purge_expired() -> int:
    """Drop junk rows left by Pro-confirmation timeouts — empty question,
    model='expired'. They aren't real Q&As, clutter the dashboard, and
    used to reappear on refresh (deleted from the DB but re-rendered from
    a stale static page). Idempotent — safe to call on every startup."""
    try:
        with _conn() as c:
            cur = c.execute("DELETE FROM qna WHERE model = 'expired'")
            return cur.rowcount
    except Exception:
        log.exception("qna purge_expired failed")
        return 0


def delete_search(keyword: str) -> int:
    """Bulk-drop rows whose question or answer contains `keyword`
    (case-insensitive substring). Returns rows affected."""
    if not keyword:
        return 0
    like = f"%{keyword}%"
    try:
        with _conn() as c:
            cur = c.execute(
                "DELETE FROM qna WHERE question LIKE ? OR answer LIKE ?",
                (like, like),
            )
            return cur.rowcount
    except Exception:
        log.exception("qna delete_search failed")
        return 0


def date_buckets(limit_days: int = 60) -> list[dict]:
    """Return [{date, count}] newest first — drives the calendar
    sidebar on the dashboard."""
    with _conn() as c:
        cur = c.execute(
            "SELECT substr(ts, 1, 10) AS d, COUNT(*) "
            "FROM qna GROUP BY d ORDER BY d DESC LIMIT ?",
            (int(limit_days),),
        )
        return [{"date": r[0], "count": int(r[1])} for r in cur.fetchall()]
