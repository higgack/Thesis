"""Knowledge-graph trial store (kg-gen inspired).

Atomic factual triples (src — relation → dst) extracted from documents,
kept in their OWN SQLite DB (data/kg.db) so the trial is isolated and
trivially removable. If it proves useful it folds into the Phase 2 wiki
fact-table. SQLite-only, no GPU, no new heavy deps — extraction reuses the
existing Gemini stack (see agent/kg_extract.py).
"""
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .. import config

log = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))
_DB = config.DATA_DIR / "kg.db"


@contextmanager
def _conn():
    c = sqlite3.connect(_DB, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    with _conn() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS edges("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " src TEXT NOT NULL, rel TEXT NOT NULL, dst TEXT NOT NULL,"
            " doc_id TEXT, confidence REAL DEFAULT 0.5, ts TEXT)"
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_kg_src ON edges(src)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_kg_dst ON edges(dst)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_kg_doc ON edges(doc_id)")
        # Same triple from the same doc shouldn't pile up on re-extract.
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_kg_uniq "
                  "ON edges(src, rel, dst, doc_id)")


def add_edges(doc_id: str, triples: list[dict]) -> int:
    """triples = [{src, rel, dst, confidence}]. Returns inserted count
    (duplicates ignored via the unique index)."""
    init()
    now = datetime.now(_KST).isoformat(timespec="seconds")
    n = 0
    with _conn() as c:
        for t in triples:
            src = (t.get("src") or "").strip()
            rel = (t.get("rel") or "").strip()
            dst = (t.get("dst") or "").strip()
            if not src or not rel or not dst:
                continue
            try:
                cur = c.execute(
                    "INSERT OR IGNORE INTO edges(src,rel,dst,doc_id,"
                    "confidence,ts) VALUES(?,?,?,?,?,?)",
                    (src, rel, dst, doc_id,
                     float(t.get("confidence") or 0.5), now))
                n += cur.rowcount
            except Exception:
                pass
    return n


def clear_doc(doc_id: str) -> None:
    init()
    with _conn() as c:
        c.execute("DELETE FROM edges WHERE doc_id=?", (doc_id,))


def docs_with_edges() -> set:
    """doc_ids that already have extracted edges — so /kg_extract skips
    re-processing them."""
    init()
    with _conn() as c:
        return {r[0] for r in c.execute(
            "SELECT DISTINCT doc_id FROM edges WHERE doc_id IS NOT NULL")}


def neighbors(entity: str, limit: int = 40) -> list[dict]:
    """Edges where `entity` appears as subject or object (substring)."""
    init()
    like = f"%{entity}%"
    with _conn() as c:
        rows = c.execute(
            "SELECT src,rel,dst,confidence,doc_id FROM edges "
            "WHERE src LIKE ? OR dst LIKE ? "
            "ORDER BY confidence DESC LIMIT ?",
            (like, like, limit)).fetchall()
    return [dict(r) for r in rows]


def top_entities(limit: int = 15) -> list[dict]:
    init()
    with _conn() as c:
        rows = c.execute(
            "SELECT e AS name, count(*) AS deg FROM ("
            " SELECT src AS e FROM edges UNION ALL SELECT dst AS e FROM edges"
            ") GROUP BY e ORDER BY deg DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict:
    init()
    with _conn() as c:
        edges = c.execute("SELECT count(*) FROM edges").fetchone()[0]
        docs = c.execute(
            "SELECT count(DISTINCT doc_id) FROM edges").fetchone()[0]
        ents = c.execute(
            "SELECT count(*) FROM (SELECT src FROM edges "
            "UNION SELECT dst FROM edges)").fetchone()[0]
    return {"edges": edges, "docs": docs, "entities": ents}
