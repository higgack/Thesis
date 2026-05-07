import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from .. import config


_TITLE_NORMALIZE_RE = re.compile(r"[\s\-_.,;:()\[\]/\\!?'\"`~@#%^&*+=<>{}|]+")
_TITLE_NOISE_RE = re.compile(
    r"\b(pdf|html|aspx|docx|pptx|hwp|ko|kr|en|net|com|naver|daum|tistory)\b",
    re.IGNORECASE,
)


def _normalize_title(t: str) -> str:
    if not t:
        return ""
    t = t.lower()
    t = _TITLE_NOISE_RE.sub("", t)
    t = _TITLE_NORMALIZE_RE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

_DB_PATH = config.DATA_DIR / "meta.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    type TEXT NOT NULL,
    title TEXT,
    summary TEXT,
    obsidian_path TEXT,
    ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_docs_ingested ON documents(ingested_at DESC);
CREATE INDEX IF NOT EXISTS idx_docs_source ON documents(source);
"""


def init():
    with _conn() as c:
        c.executescript(_SCHEMA)


@contextmanager
def _conn():
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_doc(doc_id: str, source: str, doc_type: str, title: str,
               summary: str, obsidian_path: str | None) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO documents(id, source, type, title, summary, obsidian_path, ingested_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 title=excluded.title, summary=excluded.summary,
                 obsidian_path=excluded.obsidian_path""",
            (doc_id, source, doc_type, title, summary, obsidian_path,
             datetime.utcnow().isoformat()),
        )


def get_doc(doc_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        return dict(row) if row else None


def find_by_source(source: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM documents WHERE source=?", (source,)).fetchone()
        return dict(row) if row else None


def recent(limit: int = 10) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, source, type, title, ingested_at FROM documents "
            "ORDER BY ingested_at DESC LIMIT ?", (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def last_ingested_at() -> str | None:
    """ISO timestamp of the most recently ingested doc, or None if empty.
    Used by import_channel --resume to pick up where a previous run left
    off after a hang/restart."""
    with _conn() as c:
        row = c.execute(
            "SELECT MAX(ingested_at) AS last FROM documents"
        ).fetchone()
        return row["last"] if row and row["last"] else None


def search_title(substring: str, limit: int = 20) -> list[dict]:
    """Case-insensitive title substring search. Returns full doc rows so
    callers don't need a follow-up get_doc per result."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, source, type, title, summary, obsidian_path, ingested_at "
            "FROM documents "
            "WHERE title LIKE ? COLLATE NOCASE "
            "ORDER BY ingested_at DESC LIMIT ?",
            (f"%{substring}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]


def find_duplicates() -> list[list[dict]]:
    """Group docs whose normalized titles match. Each returned list has 2+
    docs sharing essentially the same title."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, source, type, title, summary, ingested_at "
            "FROM documents"
        ).fetchall()
    groups: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(r)
        key = _normalize_title(d.get("title") or "")
        if len(key) < 4:
            continue
        groups.setdefault(key, []).append(d)
    return [g for g in groups.values() if len(g) >= 2]


def find_noise(min_summary_chars: int = 200) -> list[dict]:
    """Return text-type docs likely to be noise: short summaries, often
    accidental hashtags / questions saved by mistake."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, source, type, title, summary FROM documents "
            "WHERE type = 'text' "
            "AND length(coalesce(summary, '')) < ? "
            "ORDER BY ingested_at DESC",
            (min_summary_chars,),
        ).fetchall()
        return [dict(r) for r in rows]


def count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM documents").fetchone()[0]


def delete(doc_id: str) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        return cur.rowcount > 0
