"""Permanent-ignore list for KG relations (dashboard 🗑 = 영구 삭제).

When the user deletes a wrong relation, just removing the row isn't enough:
if the source document ever gets re-extracted (only when a doc drops to
zero edges) the same triple reappears. This store records the deleted
(src, rel, dst) so `kg.add_edges` skips it forever — same spirit as the
notes _IGNORED_FILENAMES / _IGNORED_URLS lists.

Kept in its OWN tiny SQLite db (data/kg_ignored.db), NOT kg.db, so the
dashboard SERVER process can write to it (CREATE TABLE IF NOT EXISTS is
idempotent here, like dash_queries) without grabbing a DDL write-lock on
kg.db that would contend with the bot's ingest writes.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from .. import config

log = logging.getLogger(__name__)

_DB = config.DATA_DIR / "kg_ignored.db"
_KST = timezone(timedelta(hours=9))
_SEP = "\x1f"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_DB), timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(
        "CREATE TABLE IF NOT EXISTS ignored("
        " sig TEXT PRIMARY KEY, src TEXT, rel TEXT, dst TEXT, ts TEXT)")
    return c


def sig(src: str, rel: str, dst: str) -> str:
    """Normalised signature: stripped + lowercased (catches case variants
    without over-blocking — spaces preserved so distinct entities differ)."""
    return _SEP.join([(src or "").strip().lower(),
                      (rel or "").strip().lower(),
                      (dst or "").strip().lower()])


def add(src: str, rel: str, dst: str) -> None:
    """Record a triple as permanently ignored (idempotent)."""
    s, r, d = (src or "").strip(), (rel or "").strip(), (dst or "").strip()
    if not s or not r or not d:
        return
    now = datetime.now(_KST).isoformat(timespec="seconds")
    try:
        with _conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO ignored(sig,src,rel,dst,ts) "
                "VALUES(?,?,?,?,?)", (sig(s, r, d), s, r, d, now))
            c.commit()
    except Exception:
        log.warning("kg_ignore add failed", exc_info=True)


def all_sigs() -> set:
    """Every ignored signature — loaded once per add_edges batch so the
    ingest path can skip ignored triples cheaply."""
    try:
        with _conn() as c:
            return {row[0] for row in c.execute("SELECT sig FROM ignored")}
    except Exception:
        return set()


def is_ignored(src: str, rel: str, dst: str) -> bool:
    return sig(src, rel, dst) in all_sigs()


def count() -> int:
    try:
        with _conn() as c:
            return int(c.execute("SELECT count(*) FROM ignored").fetchone()[0])
    except Exception:
        return 0
