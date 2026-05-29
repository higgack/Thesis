import hashlib
import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
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
        # body_signature: hash of the leading ~300 chars of the body.
        # Lets find_by_normalized_title distinguish docs that share a
        # title but lead with different opening paragraphs (e.g. 같은
        # 회사·같은 분기 실적발표 글이 '장전 실적'과 '장후 실적' 두 버전으로
        # 들어올 때). body_hash already differs but title-norm dedup
        # used to fire regardless; now it cross-checks signatures.
        try:
            c.execute("SELECT body_signature FROM documents LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE documents ADD COLUMN body_signature TEXT")
        # Chunk-level embedding cache ([B] cost cut, 2026-05): maps a
        # SHA1 of normalised chunk text to the Chroma id of the first
        # chunk that carried it. When a future doc produces the same
        # chunk (recurring disclaimer / template / signature block),
        # vector.add_chunks pulls the existing embedding out of Chroma
        # instead of paying Gemini to recompute. Cross-doc only —
        # within-doc duplicates still re-embed once (rare anyway).
        c.execute(
            "CREATE TABLE IF NOT EXISTS chunk_embed_cache("
            " chunk_hash TEXT PRIMARY KEY,"
            " chroma_id TEXT NOT NULL,"
            " ts TEXT NOT NULL)"
        )
        # Summary cache (cost cut, 2026-05): maps a hash of the summarize
        # inputs (doc_type + skip_meta + hint + title + body) to the
        # produced (summary, metadata). A retry of a doc that failed at
        # the embedding stage — or a later re-ingest of byte-identical
        # content — reuses the summary instead of paying the Lite model
        # to regenerate it. Same content → same output, so quality is
        # unchanged. Bounded by a 30-day TTL pruned on write.
        c.execute(
            "CREATE TABLE IF NOT EXISTS summary_cache("
            " cache_key TEXT PRIMARY KEY,"
            " summary TEXT NOT NULL,"
            " metadata_json TEXT NOT NULL,"
            " ts TEXT NOT NULL)"
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
               body_hash: str | None = None,
               body_signature: str | None = None) -> None:
    import json as _json
    metadata_json = _json.dumps(metadata, ensure_ascii=False) if metadata else None
    with _conn() as c:
        c.execute(
            """INSERT INTO documents(id, source, type, title, summary,
                                     obsidian_path, ingested_at, metadata,
                                     file_hash, body_hash, body_signature)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 title=excluded.title, summary=excluded.summary,
                 obsidian_path=excluded.obsidian_path,
                 metadata=excluded.metadata,
                 file_hash=excluded.file_hash,
                 body_hash=excluded.body_hash,
                 body_signature=excluded.body_signature""",
            (doc_id, source, doc_type, title, summary, obsidian_path,
             datetime.utcnow().isoformat(), metadata_json, file_hash,
             body_hash, body_signature),
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


def chunk_embed_lookup(hashes: list[str]) -> dict[str, str]:
    """Bulk lookup chunk_hash → chroma_id for already-embedded chunks.
    Used by vector.add_chunks to skip Gemini embedding calls on
    chunks whose text has been seen in any earlier doc."""
    if not hashes:
        return {}
    with _conn() as c:
        placeholders = ",".join("?" * len(hashes))
        rows = c.execute(
            f"SELECT chunk_hash, chroma_id FROM chunk_embed_cache "
            f"WHERE chunk_hash IN ({placeholders})",
            hashes,
        ).fetchall()
    return {r["chunk_hash"]: r["chroma_id"] for r in rows}


def chunk_embed_remember(mapping: list[tuple[str, str]]) -> None:
    """Persist (chunk_hash, chroma_id) pairs after their initial
    embedding. INSERT OR IGNORE so concurrent ingests with the same
    chunk don't fight."""
    if not mapping:
        return
    ts = datetime.utcnow().isoformat()
    with _conn() as c:
        c.executemany(
            "INSERT OR IGNORE INTO chunk_embed_cache(chunk_hash, chroma_id, ts) "
            "VALUES(?, ?, ?)",
            [(h, cid, ts) for h, cid in mapping],
        )


_SUMMARY_CACHE_TTL_DAYS = 30


def summary_cache_get(cache_key: str) -> dict | None:
    """Return {'summary': str, 'metadata': dict} for a previously
    computed summarize result, or None on miss / corrupt row. Used to
    skip the Lite summary call on retries and identical re-ingests."""
    if not cache_key:
        return None
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT summary, metadata_json FROM summary_cache "
                "WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
        if not row:
            return None
        try:
            metadata = json.loads(row["metadata_json"]) or {}
        except Exception:
            metadata = {}
        return {"summary": row["summary"] or "", "metadata": metadata}
    except Exception:
        log.exception("summary_cache_get failed")
        return None


def summary_cache_remember(cache_key: str, summary: str,
                           metadata: dict | None) -> None:
    """Persist a summarize result keyed by its input hash. Prunes rows
    older than the TTL on each write so the table stays bounded.
    Swallows all errors — the cache is an optimisation, never a
    correctness dependency."""
    if not cache_key or not summary:
        return
    try:
        ts = datetime.utcnow()
        with _conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO summary_cache"
                "(cache_key, summary, metadata_json, ts) VALUES(?, ?, ?, ?)",
                (cache_key, summary,
                 json.dumps(metadata or {}, ensure_ascii=False),
                 ts.isoformat()),
            )
            cutoff = (ts - timedelta(days=_SUMMARY_CACHE_TTL_DAYS)).isoformat()
            c.execute("DELETE FROM summary_cache WHERE ts < ?", (cutoff,))
    except Exception:
        log.exception("summary_cache_remember failed")


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


def find_by_normalized_title(title: str,
                              body_signature: str | None = None) -> dict | None:
    """Title-similarity dedup — last-resort guard for cases that slip
    past file_hash + body_hash. Useful when the same article is
    reposted with light edits or arrives in multiple containers
    (PDF + tg-msg text excerpt + URL). Normalisation strips boilerplate
    prefixes / dates / punctuation; we require ≥6 normalised chars to
    avoid false-positives on generic stubs.

    `body_signature` (optional) lets the caller distinguish docs with
    identical titles but different opening paragraphs — e.g. the same
    earnings event covered as both pre- and post-market commentary
    from the same channel. When both the new doc and the candidate row
    carry signatures, mismatched signatures skip the row. Rows ingested
    before the body_signature column existed have NULL there and fall
    back to title-only matching (conservative — caller can /forget and
    re-forward to re-index under the new logic)."""
    norm = _normalize_title(title or "")
    if len(norm) < 6:
        return None
    # Pull every recent doc's title and compare normalised — cheap on
    # a ~10K-doc corpus (the index hits in ms) and avoids storing yet
    # another column.
    with _conn() as c:
        rows = c.execute(
            "SELECT id, title, source, body_signature FROM documents "
            "ORDER BY ingested_at DESC LIMIT 5000"
        ).fetchall()
    for row in rows:
        if _normalize_title(row["title"] or "") != norm:
            continue
        row_sig = row["body_signature"]
        # Skip when the new doc has a signature and the row's doesn't
        # match (either differs or is NULL because the row predates
        # the body_signature column). body_hash already differed by
        # the time we got here, so a missing reference signature
        # means we have no positive evidence these are the same doc
        # — default to allowing the new ingest. Legacy data gets
        # slightly more duplicates rather than false-positive dedup.
        if body_signature and body_signature != row_sig:
            continue
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


def compute_body_signature(body: str) -> str:
    """Short prefix hash for tie-breaking title-norm dedup. Hashes the
    first 300 chars of the normalised body — small enough that two
    different opening paragraphs reliably yield different signatures,
    large enough that one-character typos or date corrections still
    collide (i.e. correctly dedup as 'same article, light edits').
    Returns '' if the body is too short to be meaningful."""
    if not body:
        return ""
    norm = " ".join(body.lower().split())[:300]
    if len(norm) < 100:
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


def ingested_filename_set() -> set[str]:
    """Return every filename already represented in `documents`,
    derived from `tg-doc:<uniq>:<filename>` and `local:<filename>`
    sources.

    Built in ONE table scan so the retry-queue load filter can dedupe
    N candidates against a Python set instead of issuing N separate
    find_by_filename() LIKE-scan queries at startup (each opening its
    own connection). Same matching semantics as find_by_filename."""
    names: set[str] = set()
    with _conn() as c:
        rows = c.execute(
            "SELECT source FROM documents "
            "WHERE source LIKE 'tg-doc:%' OR source LIKE 'local:%'"
        ).fetchall()
    for r in rows:
        src = r["source"] or ""
        if src.startswith("local:"):
            names.add(src[len("local:"):])
        elif src.startswith("tg-doc:"):
            # tg-doc:<uniq>:<filename> — filename may itself contain ':'
            parts = src.split(":", 2)
            if len(parts) == 3 and parts[2]:
                names.add(parts[2])
    return names


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
    JSON (which holds company name + tags + report_date).

    Multi-token AND matching when the query contains whitespace: each
    space-separated token must appear somewhere in title / summary /
    metadata. This lets '미래 반도체' (with space) find the company
    '미래반도체' (no space) — both '미래' and '반도체' are substrings
    of the compound — and lets '데이터센터 전력' find docs that
    mention both terms anywhere. Title hits across more tokens rank
    higher; ties break by ingested_at DESC."""
    substring = (substring or "").strip()
    if not substring:
        return []
    tokens = substring.split()

    with _conn() as c:
        if len(tokens) == 1:
            # Single-token substring match. Sort purely by recency —
            # title-vs-body relevance ranking was confusing in practice
            # (오래된 글이 위로 오면서 최신 매칭이 묻혔다).
            pat = f"%{substring}%"
            rows = c.execute(
                "SELECT id, source, type, title, summary, obsidian_path, "
                "       ingested_at, metadata "
                "FROM documents "
                "WHERE title    LIKE ? COLLATE NOCASE "
                "   OR summary  LIKE ? COLLATE NOCASE "
                "   OR metadata LIKE ? COLLATE NOCASE "
                "ORDER BY ingested_at DESC "
                "LIMIT ?",
                (pat, pat, pat, limit),
            ).fetchall()
            return [dict(r) for r in rows]

        # Multi-token AND: every token must hit somewhere. Sort by
        # recency only (title-hit ranking removed for consistency
        # with the single-token branch).
        where_parts: list[str] = []
        where_vals: list[str] = []
        for tok in tokens:
            pat = f"%{tok}%"
            where_parts.append(
                "(title LIKE ? COLLATE NOCASE "
                "OR summary LIKE ? COLLATE NOCASE "
                "OR metadata LIKE ? COLLATE NOCASE)"
            )
            where_vals.extend([pat, pat, pat])
        where_sql = " AND ".join(where_parts)
        rows = c.execute(
            f"SELECT id, source, type, title, summary, obsidian_path, "
            f"       ingested_at, metadata "
            f"FROM documents WHERE {where_sql} "
            f"ORDER BY ingested_at DESC LIMIT ?",
            (*where_vals, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def find_duplicates() -> list[list[dict]]:
    """Group docs whose normalized titles AND body_signatures both
    match. Two docs sharing a title but with different signatures
    (e.g. same company's separate DART disclosures) are treated as
    legitimately distinct and won't surface in /dedupe. Legacy rows
    with NULL body_signature bucket only with other NULL rows of
    the same title — keeps pre-fix duplicates discoverable without
    grouping them into signature-bearing rows on weak evidence."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, source, type, title, summary, ingested_at, "
            "       body_signature "
            "FROM documents"
        ).fetchall()
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        d = dict(r)
        key_title = _normalize_title(d.get("title") or "")
        if len(key_title) < 4:
            continue
        key = (key_title, d.get("body_signature") or "")
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
