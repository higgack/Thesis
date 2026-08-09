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
from contextlib import contextmanager
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
            # In-post link prompts: links the bot found inside a learned
            # URL's body, offered for optional follow-up ingest. links_json
            # is a list of {"url","title","desc","done"} so a missed prompt
            # survives restart and resurfaces via /pending(_links).
            c.execute("""
                CREATE TABLE IF NOT EXISTS pending_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    parent_title TEXT NOT NULL,
                    parent_url TEXT NOT NULL,
                    links_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
    except Exception:
        log.exception("pending.init failed")


@contextmanager
def _conn():
    # A plain sqlite3.Connection's own context manager only commits/
    # rolls back — it never closes. Every "with _conn() as c:" call site
    # in this module (17) was leaking a connection + WAL handle. Wrapped
    # as a real contextmanager (same pattern as kg.py's _conn).
    c = sqlite3.connect(str(_DB_PATH), timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    try:
        yield c
        c.commit()
    finally:
        c.close()


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


def add_links(chat_id: int, parent_title: str, parent_url: str,
              links: list[dict]) -> int | None:
    """Record an in-post link prompt. `links` is a list of
    {"url","title","desc"} (a "done" flag is added on write). Returns
    the row id, or None on failure."""
    try:
        norm = [{"url": l.get("url", ""), "title": l.get("title", ""),
                 "desc": l.get("desc", ""), "done": bool(l.get("done"))}
                for l in links if l.get("url")]
        if not norm:
            return None
        with _conn() as c:
            cur = c.execute(
                "INSERT INTO pending_links "
                "(chat_id, parent_title, parent_url, links_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (chat_id, parent_title, parent_url,
                 json.dumps(norm, ensure_ascii=False),
                 datetime.utcnow().isoformat(timespec="seconds")),
            )
            return cur.lastrowid
    except Exception:
        log.exception("pending.add_links failed")
        return None


def _row_to_links(r) -> dict:
    try:
        links = json.loads(r[3])
    except Exception:
        links = []
    return {"id": r[0], "chat_id": r[1], "parent_title": r[2],
            "links": links, "created_at": r[4]}


def list_links(chat_id: int | None = None, limit: int = 50) -> list[dict]:
    """Outstanding link prompts (those with ≥1 undone link)."""
    try:
        with _conn() as c:
            if chat_id is None:
                rows = c.execute(
                    "SELECT id, chat_id, parent_title, links_json, created_at "
                    "FROM pending_links ORDER BY created_at ASC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT id, chat_id, parent_title, links_json, created_at "
                    "FROM pending_links WHERE chat_id = ? "
                    "ORDER BY created_at ASC LIMIT ?", (chat_id, limit),
                ).fetchall()
        out = []
        for r in rows:
            rec = _row_to_links(r)
            if any(not l.get("done") for l in rec["links"]):
                out.append(rec)
        return out
    except Exception:
        log.exception("pending.list_links failed")
        return []


def get_links(row_id: int) -> dict | None:
    try:
        with _conn() as c:
            r = c.execute(
                "SELECT id, chat_id, parent_title, links_json, created_at "
                "FROM pending_links WHERE id = ?", (row_id,),
            ).fetchone()
        return _row_to_links(r) if r else None
    except Exception:
        log.exception("pending.get_links failed")
        return None


def set_links(row_id: int, links: list[dict]) -> bool:
    """Overwrite the links_json (used to flip per-link done flags)."""
    try:
        with _conn() as c:
            cur = c.execute(
                "UPDATE pending_links SET links_json = ? WHERE id = ?",
                (json.dumps(links, ensure_ascii=False), row_id),
            )
            return cur.rowcount > 0
    except Exception:
        log.exception("pending.set_links failed")
        return False


def delete_links(row_id: int) -> bool:
    try:
        with _conn() as c:
            cur = c.execute("DELETE FROM pending_links WHERE id = ?", (row_id,))
            return cur.rowcount > 0
    except Exception:
        log.exception("pending.delete_links failed")
        return False


def delete_all_links() -> int:
    try:
        with _conn() as c:
            cur = c.execute("DELETE FROM pending_links")
            return cur.rowcount
    except Exception:
        log.exception("pending.delete_all_links failed")
        return 0


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


def delete_all_ocr() -> int:
    """Wipe every pending OCR row. Returns the deleted count."""
    try:
        with _conn() as c:
            cur = c.execute("DELETE FROM pending_ocr")
            return cur.rowcount
    except Exception:
        log.exception("pending.delete_all_ocr failed")
        return 0


def delete_all_pro() -> int:
    """Wipe every pending Pro row. Returns the deleted count."""
    try:
        with _conn() as c:
            cur = c.execute("DELETE FROM pending_pro")
            return cur.rowcount
    except Exception:
        log.exception("pending.delete_all_pro failed")
        return 0
