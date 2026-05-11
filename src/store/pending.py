"""Pending decision store — items the user was asked to confirm
(inline Pro / OCR extend buttons) but didn't tap within the 10-min
TTL.

Persisted to sqlite so they survive restart. Surfaces via the
/pending family of commands so the user can review and decide
asynchronously — perfect for batch ingest (orphan recovery firing
many OCR prompts) or for prompts that fire while the user is asleep.
"""
import json
import logging
import sqlite3
from datetime import datetime

from .. import config

log = logging.getLogger(__name__)

_DB_PATH = config.DATA_DIR / "pending.db"


def init() -> None:
    """Create tables if missing. Called once at process start."""
    try:
        with _conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS pending_ocr (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    doc_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    pdf_path TEXT NOT NULL,
                    applied_pages INTEGER NOT NULL,
                    total_pages INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS pending_pro (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    question TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
    except Exception:
        log.exception("pending.init failed")


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(str(_DB_PATH))


def add_ocr(chat_id: int, doc_id: str, title: str, pdf_path: str,
            applied_pages: int, total_pages: int) -> int | None:
    """Record a missed OCR-extend prompt. Returns the row id."""
    try:
        with _conn() as c:
            cur = c.execute(
                "INSERT INTO pending_ocr "
                "(chat_id, doc_id, title, pdf_path, applied_pages, total_pages, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat_id, doc_id, title, pdf_path, applied_pages, total_pages,
                 datetime.utcnow().isoformat(timespec="seconds")),
            )
            return cur.lastrowid
    except Exception:
        log.exception("pending.add_ocr failed")
        return None


def add_pro(chat_id: int, question: str, count: int) -> int | None:
    """Record a missed Pro-confirmation prompt. On /pending_pro <N>
    we replay the question with deep=True so a fresh compare_papers
    + Pro synthesis runs from scratch (~₩0.05 embedding overhead)."""
    try:
        with _conn() as c:
            cur = c.execute(
                "INSERT INTO pending_pro "
                "(chat_id, question, count, created_at) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, question, count,
                 datetime.utcnow().isoformat(timespec="seconds")),
            )
            return cur.lastrowid
    except Exception:
        log.exception("pending.add_pro failed")
        return None


def list_ocr(chat_id: int | None = None, limit: int = 50) -> list[dict]:
    try:
        with _conn() as c:
            if chat_id is None:
                rows = c.execute(
                    "SELECT id, chat_id, doc_id, title, pdf_path, applied_pages, "
                    "total_pages, created_at FROM pending_ocr "
                    "ORDER BY created_at ASC LIMIT ?", (limit,),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT id, chat_id, doc_id, title, pdf_path, applied_pages, "
                    "total_pages, created_at FROM pending_ocr "
                    "WHERE chat_id = ? ORDER BY created_at ASC LIMIT ?",
                    (chat_id, limit),
                ).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r[0], "chat_id": r[1], "doc_id": r[2], "title": r[3],
                "pdf_path": r[4], "applied_pages": r[5], "total_pages": r[6],
                "created_at": r[7],
            })
        return out
    except Exception:
        log.exception("pending.list_ocr failed")
        return []


def list_pro(chat_id: int | None = None, limit: int = 50) -> list[dict]:
    try:
        with _conn() as c:
            if chat_id is None:
                rows = c.execute(
                    "SELECT id, chat_id, question, count, created_at "
                    "FROM pending_pro ORDER BY created_at ASC LIMIT ?", (limit,),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT id, chat_id, question, count, created_at "
                    "FROM pending_pro WHERE chat_id = ? "
                    "ORDER BY created_at ASC LIMIT ?", (chat_id, limit),
                ).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r[0], "chat_id": r[1], "question": r[2],
                "count": r[3], "created_at": r[4],
            })
        return out
    except Exception:
        log.exception("pending.list_pro failed")
        return []


def get_ocr(row_id: int) -> dict | None:
    items = []
    try:
        with _conn() as c:
            r = c.execute(
                "SELECT id, chat_id, doc_id, title, pdf_path, applied_pages, "
                "total_pages, created_at FROM pending_ocr WHERE id = ?",
                (row_id,),
            ).fetchone()
            if not r:
                return None
            return {
                "id": r[0], "chat_id": r[1], "doc_id": r[2], "title": r[3],
                "pdf_path": r[4], "applied_pages": r[5], "total_pages": r[6],
                "created_at": r[7],
            }
    except Exception:
        log.exception("pending.get_ocr failed")
        return None


def get_pro(row_id: int) -> dict | None:
    try:
        with _conn() as c:
            r = c.execute(
                "SELECT id, chat_id, question, count, created_at "
                "FROM pending_pro WHERE id = ?", (row_id,),
            ).fetchone()
            if not r:
                return None
            return {
                "id": r[0], "chat_id": r[1], "question": r[2],
                "count": r[3], "created_at": r[4],
            }
    except Exception:
        log.exception("pending.get_pro failed")
        return None


def delete_ocr(row_id: int) -> bool:
    try:
        with _conn() as c:
            cur = c.execute("DELETE FROM pending_ocr WHERE id = ?", (row_id,))
            return cur.rowcount > 0
    except Exception:
        log.exception("pending.delete_ocr failed")
        return False


def delete_pro(row_id: int) -> bool:
    try:
        with _conn() as c:
            cur = c.execute("DELETE FROM pending_pro WHERE id = ?", (row_id,))
            return cur.rowcount > 0
    except Exception:
        log.exception("pending.delete_pro failed")
        return False
