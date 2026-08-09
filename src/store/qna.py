"""Permanent Q&A archive.

Every successful agent reply is appended here so the user can browse
past analyses long after the rolling chat memory has expired. Schema
is intentionally narrow — one row per turn, JSON for the structured
bits — so the dashboard can read it with raw SQL."""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from .. import config

log = logging.getLogger(__name__)

_DB_PATH = config.DATA_DIR / "qna.db"

_inited = False


def _init_once(c: sqlite3.Connection) -> None:
    # DDL/migration once per process, not on every connection open —
    # record() fires on every successful Q&A turn.
    global _inited
    if _inited:
        return
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
            warning TEXT,
            important INTEGER DEFAULT 0
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_qna_ts ON qna(ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_qna_chat ON qna(chat_id)")
    # Migrate older DBs that predate the important flag.
    cols = {r[1] for r in c.execute("PRAGMA table_info(qna)")}
    if "important" not in cols:
        c.execute("ALTER TABLE qna ADD COLUMN important INTEGER DEFAULT 0")
    _inited = True


@contextmanager
def _conn():
    # See cost.py's _conn for why this must be a real contextmanager: a
    # plain sqlite3.Connection's own context manager only commits, never
    # closes — every "with _conn() as c:" call here was leaking a
    # connection on every Q&A turn (record()) and every dashboard poll
    # (recent()).
    c = sqlite3.connect(str(_DB_PATH), timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    _init_once(c)
    try:
        yield c
        c.commit()
    finally:
        c.close()


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
            "tools", "model", "warning", "important")
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
                "model, warning, important FROM qna "
                "WHERE question LIKE ? OR answer LIKE ? "
                "ORDER BY ts DESC LIMIT ? OFFSET ?",
                (like, like, int(limit), int(offset)),
            )
        else:
            cur = c.execute(
                "SELECT id, ts, question, answer, sources, tools, "
                "model, warning, important FROM qna "
                "ORDER BY ts DESC LIMIT ? OFFSET ?",
                (int(limit), int(offset)),
            )
        return [_row_to_dict(r) for r in cur.fetchall()]


def get(qna_id: int) -> dict | None:
    with _conn() as c:
        cur = c.execute(
            "SELECT id, ts, question, answer, sources, tools, "
            "model, warning, important FROM qna WHERE id = ?",
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


def set_important(qna_id: int, important: bool) -> int:
    """Toggle a Q&A row's 중요(important) flag — dashboard ✓ curation.
    Returns rows affected."""
    try:
        with _conn() as c:
            cur = c.execute("UPDATE qna SET important=? WHERE id=?",
                            (1 if important else 0, int(qna_id)))
            return cur.rowcount
    except Exception:
        log.exception("qna set_important failed")
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
