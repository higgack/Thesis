import sqlite3
from contextlib import contextmanager
from datetime import datetime
from .. import config

_DB_PATH = config.DATA_DIR / "meta.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    type TEXT NOT NULL,
    title TEXT,
    summary TEXT,
    notion_page_id TEXT,
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
               summary: str, notion_page_id: str | None) -> None:
    with _conn() as c:
        c.execute(
            """INSERT INTO documents(id, source, type, title, summary, notion_page_id, ingested_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 title=excluded.title, summary=excluded.summary,
                 notion_page_id=excluded.notion_page_id""",
            (doc_id, source, doc_type, title, summary, notion_page_id,
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


def count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM documents").fetchone()[0]


def delete(doc_id: str) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        return cur.rowcount > 0
