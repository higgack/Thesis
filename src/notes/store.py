"""Note vault + SQLite metadata for the study-notes subsystem.

- Markdown notes live in `data/notes/<slug>.md` (atomic write).
- Metadata + SRS state + recall questions + review log live in
  `data/notes.db` (SQLite, WAL + timeout=30 per CLAUDE.md hardening).

Kept fully separate from the wiki vault/index so the two subsystems
never interfere.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .. import config
from . import srs

log = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))

_VAULT = config.DATA_DIR / "notes"
_VAULT.mkdir(parents=True, exist_ok=True)
_DB_PATH = config.DATA_DIR / "notes.db"


def _now() -> str:
    return datetime.now(_KST).isoformat(timespec="seconds")


def _today() -> date:
    return datetime.now(_KST).date()


# ---------------------------------------------------------------- db ---

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_DB_PATH), timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS notes (
              id TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              source_type TEXT,
              source_ref TEXT,
              md_path TEXT,
              created TEXT,
              updated TEXT,
              cost_krw REAL DEFAULT 0,
              gen_seconds REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS note_srs (
              note_id TEXT PRIMARY KEY,
              ease REAL DEFAULT 2.5,
              interval_days INTEGER DEFAULT 0,
              reps INTEGER DEFAULT 0,
              lapses INTEGER DEFAULT 0,
              last_reviewed TEXT,
              next_due TEXT,
              FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS questions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              note_id TEXT,
              question TEXT,
              answer TEXT,
              q_type TEXT,
              FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS reviews (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              note_id TEXT,
              reviewed_at TEXT,
              grade INTEGER,
              FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_srs_due ON note_srs(next_due);
            CREATE INDEX IF NOT EXISTS idx_q_note ON questions(note_id);
            """
        )
        # Migrate older DBs that predate the cost/time columns.
        cols = {row[1] for row in c.execute("PRAGMA table_info(notes)")}
        if "cost_krw" not in cols:
            c.execute("ALTER TABLE notes ADD COLUMN cost_krw REAL DEFAULT 0")
        if "gen_seconds" not in cols:
            c.execute("ALTER TABLE notes ADD COLUMN gen_seconds REAL DEFAULT 0")


# ------------------------------------------------------------- vault ---

def _slugify(title: str) -> str:
    s = unicodedata.normalize("NFKC", title).strip()
    # Keep word chars (incl. Hangul/CJK via \w + UNICODE) and dashes.
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", "-", s).strip("-")
    return (s or "note")[:60]


def _unique_id(title: str) -> str:
    base = _slugify(title)
    cand = base
    n = 1
    with _conn() as c:
        while c.execute("SELECT 1 FROM notes WHERE id=?", (cand,)).fetchone():
            n += 1
            cand = f"{base}-{n}"
    return cand


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ------------------------------------------------------------- write ---

def save_note(note: dict) -> str:
    """Persist a synthesised note. `note` keys:
        title, source_type, source_ref, md (markdown body),
        questions: [{question, answer, q_type}]
    Writes the .md file, inserts the notes row, seeds SRS state
    (due tomorrow so it enters the next review queue), and stores the
    recall questions. Returns the note id.
    """
    init_db()
    note_id = _unique_id(note.get("title") or "note")
    md_path = _VAULT / f"{note_id}.md"
    _atomic_write_text(md_path, note.get("md") or "")

    now = _now()
    st = srs.initial_state()
    next_due = (_today() + timedelta(days=1)).isoformat()

    with _conn() as c:
        c.execute(
            "INSERT INTO notes(id,title,source_type,source_ref,md_path,"
            "created,updated,cost_krw,gen_seconds) VALUES(?,?,?,?,?,?,?,?,?)",
            (note_id, note.get("title") or note_id, note.get("source_type"),
             note.get("source_ref"), str(md_path), now, now,
             float(note.get("cost_krw") or 0.0),
             float(note.get("gen_seconds") or 0.0)),
        )
        c.execute(
            "INSERT INTO note_srs(note_id,ease,interval_days,reps,lapses,"
            "last_reviewed,next_due) VALUES(?,?,?,?,?,?,?)",
            (note_id, st["ease"], st["interval_days"], st["reps"],
             st["lapses"], None, next_due),
        )
        for q in note.get("questions") or []:
            c.execute(
                "INSERT INTO questions(note_id,question,answer,q_type) "
                "VALUES(?,?,?,?)",
                (note_id, q.get("question", ""), q.get("answer", ""),
                 q.get("q_type", "recall")),
            )
    log.info("note saved: %s (%s)", note_id, note.get("source_type"))
    return note_id


def record_review(note_id: str, grade: int) -> dict | None:
    """Apply a self-grade: advance SRS state, set timestamps, log the
    review. Returns the new SRS row (or None if the note is unknown)."""
    init_db()
    with _conn() as c:
        row = c.execute(
            "SELECT ease,interval_days,reps,lapses FROM note_srs "
            "WHERE note_id=?", (note_id,)).fetchone()
        if row is None:
            return None
        new = srs.schedule(dict(row), int(grade))
        now = _now()
        next_due = (_today() + timedelta(days=new["interval_days"])).isoformat()
        c.execute(
            "UPDATE note_srs SET ease=?,interval_days=?,reps=?,lapses=?,"
            "last_reviewed=?,next_due=? WHERE note_id=?",
            (new["ease"], new["interval_days"], new["reps"], new["lapses"],
             now, next_due, note_id),
        )
        c.execute("UPDATE notes SET updated=? WHERE id=?", (now, note_id))
        c.execute(
            "INSERT INTO reviews(note_id,reviewed_at,grade) VALUES(?,?,?)",
            (note_id, now, int(grade)),
        )
        new["next_due"] = next_due
        new["last_reviewed"] = now
        return new


# -------------------------------------------------------------- read ---

def get_note(note_id: str) -> dict | None:
    init_db()
    with _conn() as c:
        n = c.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        if n is None:
            return None
        srs_row = c.execute(
            "SELECT * FROM note_srs WHERE note_id=?", (note_id,)).fetchone()
        qs = c.execute(
            "SELECT question,answer,q_type FROM questions WHERE note_id=?",
            (note_id,)).fetchall()
    md = ""
    try:
        md = Path(n["md_path"]).read_text(encoding="utf-8")
    except Exception:
        log.warning("note md missing: %s", n["md_path"])
    out = dict(n)
    out["md"] = md
    out["srs"] = dict(srs_row) if srs_row else None
    out["questions"] = [dict(q) for q in qs]
    return out


def list_notes() -> list[dict]:
    init_db()
    with _conn() as c:
        rows = c.execute(
            "SELECT n.id,n.title,n.source_type,n.source_ref,n.updated,"
            "s.last_reviewed,s.next_due,s.reps "
            "FROM notes n LEFT JOIN note_srs s ON s.note_id=n.id "
            "ORDER BY n.updated DESC").fetchall()
    return [dict(r) for r in rows]


def due_notes(on: date | None = None) -> list[dict]:
    """Notes whose next_due <= today — the review queue."""
    init_db()
    cutoff = (on or _today()).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT n.rowid AS rowid,n.id,n.title,n.source_type,"
            "s.next_due,s.reps,s.lapses "
            "FROM notes n JOIN note_srs s ON s.note_id=n.id "
            "WHERE s.next_due<=? ORDER BY s.next_due ASC",
            (cutoff,)).fetchall()
    return [dict(r) for r in rows]


def id_for_rowid(rowid: int) -> str | None:
    """Resolve a note's stable slug id from its integer rowid — used to
    keep Telegram callback_data short (slug ids can be long/multibyte)."""
    init_db()
    with _conn() as c:
        row = c.execute("SELECT id FROM notes WHERE rowid=?",
                        (int(rowid),)).fetchone()
    return row["id"] if row else None


def stats() -> dict:
    init_db()
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        due = len(due_notes())
        reviews = c.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    return {"notes": total, "due_today": due, "total_reviews": reviews}
