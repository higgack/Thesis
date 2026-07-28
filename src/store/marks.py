"""Shared 'important' marks store for dashboard curation.

A single tiny SQLite table maps (kind, item_id) → marked. Used by the
subsystems whose items have no natural place for an own column — wiki
pages (item_id = topic name, stable) and KG edges (item_id = edge id).
Notes and Q&A keep their own `important` column; this is only for the
others. Read from the bot's render path AND written from the separate
dashboard server process, so WAL + timeout per CLAUDE.md hardening.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from contextlib import contextmanager

from .. import config


def alarm_token(kind: str, item_id: str) -> str:
    """Short stable token for an alarm's Telegram callback_data (the raw
    item_id — e.g. a Korean wiki topic — can exceed Telegram's 64-byte
    callback limit, so we never put it in the button)."""
    return hashlib.sha1(f"{kind}|{item_id}".encode("utf-8")).hexdigest()[:16]

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
        c.execute(
            "CREATE TABLE IF NOT EXISTS memos("
            " kind TEXT NOT NULL, item_id TEXT NOT NULL,"
            " text TEXT NOT NULL, updated TEXT,"
            " PRIMARY KEY(kind, item_id))"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS alarms("
            " kind TEXT NOT NULL, item_id TEXT NOT NULL,"
            " hhmm TEXT NOT NULL, start_date TEXT DEFAULT '',"
            " acked INTEGER DEFAULT 0,"
            " last_fired TEXT, PRIMARY KEY(kind, item_id))"
        )
        # Migrate alarms created before start_date (one-time date alarms).
        acols = {r[1] for r in c.execute("PRAGMA table_info(alarms)")}
        if "start_date" not in acols:
            c.execute("ALTER TABLE alarms ADD COLUMN start_date TEXT DEFAULT ''")
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


def set_memo(kind: str, item_id: str, text: str) -> None:
    """Upsert a personal memo for (kind, item_id). Empty text deletes it."""
    if not kind or not item_id:
        return
    text = (text or "").strip()
    try:
        with _conn() as c:
            if text:
                from datetime import datetime, timezone, timedelta
                now = datetime.now(timezone(timedelta(hours=9))).isoformat(
                    timespec="seconds")
                c.execute(
                    "INSERT INTO memos(kind,item_id,text,updated) VALUES(?,?,?,?)"
                    " ON CONFLICT(kind,item_id) DO UPDATE SET text=?,updated=?",
                    (kind, item_id, text, now, text, now))
            else:
                c.execute("DELETE FROM memos WHERE kind=? AND item_id=?",
                          (kind, item_id))
    except Exception:
        log.exception("memo set failed (%s/%s)", kind, item_id)


def memo(kind: str, item_id: str) -> str:
    """Single memo text for (kind, item_id), '' if none / error."""
    if not kind or not item_id:
        return ""
    try:
        with _conn() as c:
            r = c.execute("SELECT text FROM memos WHERE kind=? AND item_id=?",
                          (kind, item_id)).fetchone()
        return r[0] if r else ""
    except Exception:
        return ""


def memos(kind: str) -> dict:
    """Map item_id → memo text for `kind` (empty on any error)."""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT item_id, text FROM memos WHERE kind=?", (kind,)).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        log.exception("memos read failed (%s)", kind)
        return {}


# ---- alarms (daily Telegram reminder until acked) -------------------------

def set_alarm(kind: str, item_id: str, hhmm: str, start_date: str = "") -> None:
    """Set/replace a KST alarm at HH:MM. start_date='' → fires daily.
    start_date=YYYY-MM-DD → starts firing on that date (daily until acked).
    Resets acked + last_fired so it fires fresh. Empty hhmm clears it."""
    if not kind or not item_id:
        return
    hhmm = (hhmm or "").strip()
    start_date = (start_date or "").strip()
    try:
        with _conn() as c:
            if hhmm:
                c.execute(
                    "INSERT INTO alarms(kind,item_id,hhmm,start_date,acked,last_fired)"
                    " VALUES(?,?,?,?,0,'')"
                    " ON CONFLICT(kind,item_id) DO UPDATE SET"
                    " hhmm=?,start_date=?,acked=0,last_fired=''",
                    (kind, item_id, hhmm, start_date, hhmm, start_date))
            else:
                c.execute("DELETE FROM alarms WHERE kind=? AND item_id=?",
                          (kind, item_id))
    except Exception:
        log.exception("set_alarm failed (%s/%s)", kind, item_id)


def clear_alarm(kind: str, item_id: str) -> None:
    set_alarm(kind, item_id, "")


def alarm_map(kind: str) -> dict:
    """item_id → {'hhmm','date'} for `kind` (to pre-fill the dashboard)."""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT item_id, hhmm, COALESCE(start_date,'') FROM alarms"
                " WHERE kind=?", (kind,)).fetchall()
        return {r[0]: {"hhmm": r[1], "date": r[2]} for r in rows}
    except Exception:
        return {}


def alarm(kind: str, item_id: str) -> dict:
    """{'hhmm','date'} for one item's alarm; {'hhmm':'','date':''} if none."""
    if not kind or not item_id:
        return {"hhmm": "", "date": ""}
    try:
        with _conn() as c:
            r = c.execute(
                "SELECT hhmm, COALESCE(start_date,'') FROM alarms"
                " WHERE kind=? AND item_id=?", (kind, item_id)).fetchone()
        return {"hhmm": r[0], "date": r[1]} if r else {"hhmm": "", "date": ""}
    except Exception:
        return {"hhmm": "", "date": ""}


def alarms_due(now_hhmm: str, today: str) -> list:
    """[(kind,item_id)] for alarms whose time has arrived today (hhmm <=
    now), not yet acked, and not already fired today. Using <= (not ==)
    means a delayed tick or a bot restart still catches the alarm instead
    of silently skipping the whole day; the per-day last_fired guard keeps
    it to one fire."""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT kind,item_id FROM alarms WHERE hhmm<=? AND acked=0"
                " AND COALESCE(last_fired,'')<>?"
                " AND COALESCE(start_date,'')<=?",
                (now_hhmm, today, today)).fetchall()
        return [(r[0], r[1]) for r in rows]
    except Exception:
        log.exception("alarms_due failed")
        return []


def mark_alarm_fired(kind: str, item_id: str, today: str) -> None:
    try:
        with _conn() as c:
            c.execute("UPDATE alarms SET last_fired=? WHERE kind=? AND item_id=?",
                      (today, kind, item_id))
    except Exception:
        log.exception("mark_alarm_fired failed")


def alarm_by_token(token: str) -> tuple | None:
    """Resolve a callback token back to (kind, item_id) by scanning the
    (small) alarms table. None if not found."""
    if not token:
        return None
    try:
        with _conn() as c:
            rows = c.execute("SELECT kind,item_id FROM alarms").fetchall()
        for k, i in rows:
            if alarm_token(k, i) == token:
                return (k, i)
    except Exception:
        log.exception("alarm_by_token failed")
    return None


def ack_alarm(kind: str, item_id: str) -> bool:
    """Mark an alarm acknowledged → stops future fires. Returns True if a
    previously-unacked alarm was flipped."""
    try:
        with _conn() as c:
            cur = c.execute(
                "UPDATE alarms SET acked=1 WHERE kind=? AND item_id=? AND acked=0",
                (kind, item_id))
            return cur.rowcount > 0
    except Exception:
        log.exception("ack_alarm failed")
        return False
