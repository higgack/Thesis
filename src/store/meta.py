import hashlib
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from .. import config

log = logging.getLogger(__name__)


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

# Base schema: only columns + indexes that exist on every shipped
# version. Newer columns (metadata, file_hash) and their indexes are
# applied via ALTER TABLE in init() after the table is created, so
# executescript can't reference a column that's missing on a legacy
# DB and crash the whole bot startup.
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
CREATE TABLE IF NOT EXISTS ocr_cache (
    img_hash TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    ts TEXT NOT NULL
);
"""


def init():
    with _conn() as c:
        c.executescript(_SCHEMA)
        try:
            c.execute("SELECT metadata FROM documents LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE documents ADD COLUMN metadata TEXT")
        try:
            c.execute("SELECT file_hash FROM documents LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE documents ADD COLUMN file_hash TEXT")
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_docs_file_hash "
            "ON documents(file_hash)"
        )
        try:
            c.execute("SELECT body_hash FROM documents LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE documents ADD COLUMN body_hash TEXT")
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_docs_body_hash "
            "ON documents(body_hash)"
        )


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
               summary: str, obsidian_path: str | None,
               metadata: dict | None = None,
               file_hash: str | None = None,
               body_hash: str | None = None) -> None:
    import json as _json
    metadata_json = _json.dumps(metadata, ensure_ascii=False) if metadata else None
    with _conn() as c:
        c.execute(
            """INSERT INTO documents(id, source, type, title, summary,
                                     obsidian_path, ingested_at, metadata,
                                     file_hash, body_hash)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 title=excluded.title, summary=excluded.summary,
                 obsidian_path=excluded.obsidian_path,
                 metadata=excluded.metadata,
                 file_hash=excluded.file_hash,
                 body_hash=excluded.body_hash""",
            (doc_id, source, doc_type, title, summary, obsidian_path,
             datetime.utcnow().isoformat(), metadata_json, file_hash, body_hash),
        )


def ocr_cache_get(img_hash: str) -> str | None:
    """Look up a previously OCR'd page image. Same image bytes from
    different docs (recurring disclaimer / cover pages across broker
    reports) share the cached Gemini Vision output — saves the call
    cost on every repeat hit."""
    if not img_hash:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT text FROM ocr_cache WHERE img_hash=?", (img_hash,),
        ).fetchone()
        return row["text"] if row else None


def ocr_cache_put(img_hash: str, text: str) -> None:
    """Store a Vision OCR result keyed on the image bytes hash. Idempotent
    via INSERT OR REPLACE so repeat ingests don't duplicate rows."""
    if not img_hash or not text:
        return
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO ocr_cache(img_hash, text, ts) "
            "VALUES(?, ?, ?)",
            (img_hash, text, datetime.utcnow().isoformat()),
        )


def find_by_file_hash(file_hash: str) -> dict | None:
    """Lookup any doc whose file_hash matches. Bytes-identical files
    (same PDF re-uploaded under a slightly different filename, or
    learned via /upload vs orphan recovery) collide here regardless
    of source label."""
    if not file_hash:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM documents WHERE file_hash=? LIMIT 1",
            (file_hash,),
        ).fetchone()
        return dict(row) if row else None


def find_by_normalized_title(title: str) -> dict | None:
    """Title-similarity dedup — last-resort guard for cases that slip
    past file_hash + body_hash. Useful when the same article is
    reposted with light edits or arrives in multiple containers
    (PDF + tg-msg text excerpt + URL). Normalisation strips boilerplate
    prefixes / dates / punctuation; we require ≥6 normalised chars to
    avoid false-positives on generic stubs."""
    norm = _normalize_title(title or "")
    if len(norm) < 6:
        return None
    # Pull every recent doc's title and compare normalised — cheap on
    # a ~10K-doc corpus (the index hits in ms) and avoids storing yet
    # another column.
    with _conn() as c:
        rows = c.execute(
            "SELECT id, title, source FROM documents "
            "ORDER BY ingested_at DESC LIMIT 5000"
        ).fetchall()
    for row in rows:
        if _normalize_title(row["title"] or "") == norm:
            return dict(row)
    return None


def find_by_body_hash(body_hash: str) -> dict | None:
    """Cross-format content-level dedup. PDF and PPTX exports of the
    same source material have different file_hash but the extracted
    bodies normalise to the same hash, so this is the only catch-all
    for 'this is the same article reformatted'."""
    if not body_hash:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM documents WHERE body_hash=? LIMIT 1",
            (body_hash,),
        ).fetchone()
        return dict(row) if row else None


def compute_body_hash(body: str) -> str:
    """Normalised hash of the first 4000 characters of an extracted
    document body. Whitespace collapsed and lowercased so trivial
    formatting differences (PPT line breaks vs PDF paragraph breaks)
    don't escape dedup. Returns '' if the body is empty / too short
    to be meaningful."""
    if not body:
        return ""
    norm = " ".join(body.lower().split())[:4000]
    if len(norm) < 200:
        # Too short to be a confident match — short tg-msg bodies use
        # text_hash dedup instead.
        return ""
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def get_doc(doc_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        return dict(row) if row else None


def find_by_source(source: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM documents WHERE source=?", (source,)).fetchone()
        return dict(row) if row else None


def find_text_by_hash(hash8: str) -> dict | None:
    """Content-hash lookup for text docs. Source labels follow the
    `<prefix>:<hash8>` shape (e.g. `tg-msg:3418:35747abd`), but the
    forward-listener re-forwarding the same upstream message produces
    a fresh msg_id every time — so two forwards of identical text get
    two source labels that differ only in the middle segment and slip
    past find_by_source(). Searching by the trailing hash catches that
    case cheaply and short-circuits the summary + embed work."""
    if not hash8:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM documents "
            "WHERE type='text' AND source LIKE ? "
            "ORDER BY ingested_at ASC LIMIT 1",
            (f"%:{hash8}",),
        ).fetchone()
        return dict(row) if row else None


def find_by_filename(filename: str) -> dict | None:
    """Filename-level lookup. Matches both `tg-doc:<uniq>:<filename>`
    (live Telegram doc) and `local:<filename>` (orphan recovery) so
    files learned under one source label still dedupe when seen via
    the other path. Used by the retry queue / load filter to avoid
    cycling already-ingested files."""
    if not filename:
        return None
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM documents "
            "WHERE source LIKE ? OR source = ? LIMIT 1",
            (f"tg-doc:%:{filename}", f"local:{filename}"),
        ).fetchone()
        return dict(row) if row else None


_FORWARD_DIGEST_RE = re.compile(
    # Auto-aggregator emojis followed by a year — captures the daily
    # rollup pattern these forwarded summaries use. User-written notes
    # don't start with this combo.
    r"^\s*[📋📰📊📈]\s*\d{4}년|"
    # Plain '한국 목표가 ... 상승여력' / '한국 신고가' style stock
    # screener forwards (no emoji prefix).
    r"\d{4}년\s*\d{1,2}월\s*\d{1,2}일.*"
    r"(요약|한국\s*목표가|한국\s*신고가|상승여력|신저가|급등주|급락주)|"
    r"채널\s*요약|"
    r"Substack\s*요약|"
    # Bare-timestamp 'cards' that the forwarder sometimes drops.
    r"^\s*📅",
    re.IGNORECASE,
)


def find_forwarded_digests() -> list[dict]:
    """Return every doc that looks like an auto-forwarded channel
    digest: source comes from a Telegram message text ingest
    (`tg-msg:%`) AND the title matches the bracket/emoji digest
    pattern. User-written long pastes use the same source prefix but
    don't match the title shape, so this leaves them alone."""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT id, source, type, title, summary, ingested_at "
                "FROM documents WHERE source LIKE 'tg-msg:%'"
            ).fetchall()
    except Exception:
        log.exception("find_forwarded_digests failed")
        return []
    out: list[dict] = []
    for row in rows:
        d = dict(row)
        title = (d.get("title") or "").strip()
        if title and _FORWARD_DIGEST_RE.search(title):
            out.append(d)
    return out


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
    callers don't need a follow-up get_doc per result. Includes metadata
    JSON when present."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, source, type, title, summary, obsidian_path, "
            "       ingested_at, metadata "
            "FROM documents "
            "WHERE title LIKE ? COLLATE NOCASE "
            "ORDER BY ingested_at DESC LIMIT ?",
            (f"%{substring}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]


def search_broad(substring: str, limit: int = 30) -> list[dict]:
    """Wider net than search_title — also looks in summary and metadata
    JSON (which holds company name + tags + report_date). Used by /find
    so a query like '몰리브덴' or '데이터센터 전력' matches even when the
    keyword sits in the body rather than the title. Title matches are
    sorted first; everything else falls back to ingested_at DESC."""
    pat = f"%{substring}%"
    with _conn() as c:
        rows = c.execute(
            "SELECT id, source, type, title, summary, obsidian_path, "
            "       ingested_at, metadata, "
            "       CASE WHEN title LIKE ? COLLATE NOCASE THEN 0 ELSE 1 END AS rank "
            "FROM documents "
            "WHERE title    LIKE ? COLLATE NOCASE "
            "   OR summary  LIKE ? COLLATE NOCASE "
            "   OR metadata LIKE ? COLLATE NOCASE "
            "ORDER BY rank ASC, ingested_at DESC "
            "LIMIT ?",
            (pat, pat, pat, pat, limit),
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


def usage_stats() -> dict:
    """Aggregate stats for the /usage command: totals, ingest velocity,
    type breakdown, latest doc."""
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        last_24h = c.execute(
            "SELECT COUNT(*) FROM documents "
            "WHERE ingested_at >= datetime('now', '-1 day')"
        ).fetchone()[0]
        last_7d = c.execute(
            "SELECT COUNT(*) FROM documents "
            "WHERE ingested_at >= datetime('now', '-7 days')"
        ).fetchone()[0]
        last_30d = c.execute(
            "SELECT COUNT(*) FROM documents "
            "WHERE ingested_at >= datetime('now', '-30 days')"
        ).fetchone()[0]
        type_rows = c.execute(
            "SELECT type, COUNT(*) c FROM documents "
            "GROUP BY type ORDER BY c DESC"
        ).fetchall()
        latest = c.execute(
            "SELECT title, ingested_at FROM documents "
            "ORDER BY ingested_at DESC LIMIT 1"
        ).fetchone()
    return {
        "total": total,
        "last_24h": last_24h,
        "last_7d": last_7d,
        "last_30d": last_30d,
        "types": [(r["type"], r["c"]) for r in type_rows],
        "latest_title": latest["title"] if latest else "(none)",
        "latest_at": latest["ingested_at"] if latest else "",
    }


def delete(doc_id: str) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        return cur.rowcount > 0
