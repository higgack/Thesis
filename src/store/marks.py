"""Shared 'important' marks store for dashboard curation.

A single tiny SQLite table maps (kind, item_id) → marked. Used by the
subsystems whose items have no natural place for an own column — wiki
pages (item_id = topic name, stable) and KG edges (item_id = edge id).
Notes and Q&A keep their own `important` column; this is only for the
others. Read from the bot's render path AND written from the separate
dashboard server process, so WAL + timeout per CLAUDE.md hardening.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager

from .. import config

log = logging.getLogger(__name__)

_DB = config.DATA_DIR / "marks.db"


@contextmanager
def _conn():
    c = sqlite3.connect(str(_DB), timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    try:
        c.execute(
            "CREATE TABLE IF NOT EXISTS marks("
            " kind TEXT NOT NULL, item_id TEXT NOT NULL,"
            " PRIMARY KEY(kind, item_id))"
        )
        yield c
        c.commit()
    finally:
        c.close()


def set_mark(kind: str, item_id: str, important: bool) -> None:
    """Add or remove an important mark for (kind, item_id)."""
    if not kind or not item_id:
        return
    try:
        with _conn() as c:
            if important:
                c.execute("INSERT OR IGNORE INTO marks(kind,item_id) "
                          "VALUES(?,?)", (kind, item_id))
            else:
                c.execute("DELETE FROM marks WHERE kind=? AND item_id=?",
                          (kind, item_id))
    except Exception:
        log.exception("marks set failed (%s/%s)", kind, item_id)


def marked(kind: str) -> set:
    """Set of item_ids marked important for `kind` (empty on any error)."""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT item_id FROM marks WHERE kind=?", (kind,)).fetchall()
        return {r[0] for r in rows}
    except Exception:
        log.exception("marks read failed (%s)", kind)
        return set()
