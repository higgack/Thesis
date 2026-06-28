"""Knowledge-graph trial store (kg-gen inspired).

Atomic factual triples (src — relation → dst) extracted from documents,
kept in their OWN SQLite DB (data/kg.db) so the trial is isolated and
trivially removable. If it proves useful it folds into the Phase 2 wiki
fact-table. SQLite-only, no GPU, no new heavy deps — extraction reuses the
existing Gemini stack (see agent/kg_extract.py).
"""
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .. import config

_TOK_RE = re.compile(r"[\w가-힣]+", re.UNICODE)

# Junk-entity check (kept here, in the dependency-light store, so both the
# extractor and purge_junk share ONE definition without importing the
# heavy gemini stack). Drops: pure numbers/% ("0.0%"), Claude/agent
# metadata (anything containing "claude"), and generic standalone terms
# (exact-match — specific multiword names like "미국 달러 지수" survive).
_JUNK_ENT_RE = re.compile(r"^[\d.,%\-+~/()\s]+$")
_ENT_STOP = {
    "claude", "claude tag", "claudetag", "@claude", "tag", "ai", "llm",
    "gpt", "정부", "중국", "미국", "유럽", "한국", "일본", "세계", "전세계",
    "글로벌", "시장", "기업", "회사", "업계", "산업", "국가", "지역",
    "전체", "기타", "관련", "내용",
}


def is_junk_entity(e: str) -> bool:
    e = (e or "").strip()
    if len(e) < 2 or _JUNK_ENT_RE.match(e):
        return True
    low = e.lower()
    return "claude" in low or low in _ENT_STOP

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


_inited = False


def init() -> None:
    # Run the DDL once per process. Every read path used to call init() →
    # repeated CREATE…IF NOT EXISTS each grabs a brief write lock, which
    # contends with concurrent ingest writes (and across processes) →
    # 'database is locked'. After the first success it's a no-op.
    global _inited
    if _inited:
        return
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
    _inited = True


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


def purge_junk() -> int:
    """Delete edges whose subject/object is a junk entity (Claude/agent
    metadata, generic terms, numbers) per the extractor's stoplist —
    one-time cleanup of edges added before the filter. Returns rows removed."""
    init()
    with _conn() as c:
        rows = c.execute("SELECT id, src, dst FROM edges").fetchall()
        bad = [r["id"] for r in rows
               if is_junk_entity(r["src"]) or is_junk_entity(r["dst"])]
        for i in range(0, len(bad), 500):
            ids = bad[i:i + 500]
            c.execute(
                f"DELETE FROM edges WHERE id IN ({','.join('?' * len(ids))})",
                ids)
    if bad:
        log.info("kg purge_junk: removed %d junk edges", len(bad))
    return len(bad)


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


def edges_for_entity(name: str, limit: int = 1200) -> list[dict]:
    """All edges where src OR dst == name exactly (the chip's full degree
    set), confidence-desc. Powers the dashboard 'click a 주요 개체 chip →
    see ALL its relations' (not just the top-3000-by-confidence subset).

    READ-ONLY — must NOT call init(). This runs in the dashboard SERVER
    process; init()'s CREATE TABLE/INDEX is a write-lock that collides with
    the bot writing kg.db during ingest → 'database is locked'. The table
    already exists (created by the bot), so a plain SELECT is enough; if it
    somehow doesn't, we return [] gracefully."""
    if not name:
        return []
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT id,src,rel,dst,confidence,doc_id FROM edges "
                "WHERE src=? OR dst=? ORDER BY confidence DESC LIMIT ?",
                (name, name, int(limit))).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def edges_by_ids(ids) -> list[dict]:
    """Fetch specific edges by id (read-only, no init/DDL). Used to keep
    ★/메모/알람-표시된 엣지를 초기 렌더 상위-N 밖이어도 포함시키기 위함
    (그래야 '중요만/메모만' 필터가 안 깨짐)."""
    iids = []
    for i in ids:
        try:
            iids.append(int(i))
        except (TypeError, ValueError):
            continue
    if not iids:
        return []
    try:
        with _conn() as c:
            ph = ",".join("?" * len(iids))
            rows = c.execute(
                f"SELECT id,src,rel,dst,confidence,doc_id FROM edges "
                f"WHERE id IN ({ph})", iids).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def all_edges(limit: int = 3000) -> list[dict]:
    """Every edge (confidence-desc) for the dashboard KG view."""
    init()
    with _conn() as c:
        rows = c.execute(
            "SELECT id,src,rel,dst,confidence,doc_id FROM edges "
            "ORDER BY confidence DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def edge_by_id(edge_id) -> dict | None:
    """Single edge by primary key (for dashboard memo/alarm cards)."""
    init()
    try:
        eid = int(edge_id)
    except (TypeError, ValueError):
        return None
    with _conn() as c:
        r = c.execute(
            "SELECT id,src,rel,dst,confidence,doc_id FROM edges WHERE id=?",
            (eid,)).fetchone()
    return dict(r) if r else None


def context_for(query: str, limit: int = 12) -> list[dict]:
    """High-confidence edges whose subject/object overlaps the query's
    tokens — injected into answers so the LLM sees relevant relationships.
    Returns [] when the graph is empty or nothing matches (₩0, local).

    Matching is slug-insensitive (spaces stripped + lowercased on both
    sides) so stored variants like "삼성 전기"/"SAMSUNG" still match a
    "삼성전기"/"samsung" query — recall without touching stored data.
    Read-time only; no entity canonicalization is written back (that would
    risk over-merging distinct fine-grained entities)."""
    init()
    toks = [t for t in _TOK_RE.findall(query or "") if len(t) >= 2][:8]
    if not toks:
        return []
    clause = " OR ".join(
        ["REPLACE(LOWER(src),' ','') LIKE ? "
         "OR REPLACE(LOWER(dst),' ','') LIKE ?"] * len(toks))
    params: list = []
    for t in toks:
        s = "%" + t.lower().replace(" ", "") + "%"
        params += [s, s]
    params.append(int(limit))
    with _conn() as c:
        try:
            rows = c.execute(
                f"SELECT src,rel,dst,confidence FROM edges WHERE ({clause}) "
                "AND confidence >= 0.6 ORDER BY confidence DESC LIMIT ?",
                params).fetchall()
        except sqlite3.OperationalError:
            return []
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
