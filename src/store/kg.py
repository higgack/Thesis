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
    # forward_listener URL-only curation channel names — never real
    # entities. Leaked in via _notify_unsupported_urls notices before
    # the ingest-side drop pattern existed (818-edge "getfeed"
    # incident, 2026-07-20).
    "getfeed", "benineb9",
}


def is_junk_entity(e: str) -> bool:
    e = (e or "").strip()
    if len(e) < 2 or _JUNK_ENT_RE.match(e):
        return True
    low = e.lower()
    return "claude" in low or low in _ENT_STOP


# Entity canonicalization (graphify-inspired, 2026-07-29 review): without
# this, "삼성전자"/"삼성 전자"/"삼성전자(주)" extract as three distinct
# nodes with no shared edges, silently fragmenting the graph. Same
# normalization idea as wiki.py's _dedup_key for topic names, kept local
# here (not imported) so kg.py stays a self-contained, trivially-removable
# trial store per the module docstring.
_CORP_SUFFIXES = re.compile(
    r"(주식회사|\(주\)|㈜|Inc\.?|Corp\.?|Co\.,?\s*Ltd\.?|Ltd\.?)",
    re.IGNORECASE)


def _canon_key(name: str) -> str:
    k = _CORP_SUFFIXES.sub("", name or "")
    k = re.sub(r"[\s_\-·]+", "", k).lower()
    return k

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
        # norm_key → the first-seen (or merge-chosen) display string for
        # that normalized entity. New extractions resolve through this so
        # "삼성전자"/"삼성 전자" land on the same node instead of forking.
        c.execute(
            "CREATE TABLE IF NOT EXISTS entity_canon("
            " norm_key TEXT PRIMARY KEY, canonical TEXT NOT NULL)")
    _inited = True


def _resolve_entity(c: sqlite3.Connection, name: str) -> str:
    """Map an entity string to its canonical display form. First variant
    seen for a normalized key wins and is remembered; later variants
    (different spacing/corp suffix) resolve to that same string."""
    key = _canon_key(name)
    if not key:
        return name
    row = c.execute(
        "SELECT canonical FROM entity_canon WHERE norm_key=?", (key,)
    ).fetchone()
    if row:
        return row["canonical"]
    c.execute(
        "INSERT OR IGNORE INTO entity_canon(norm_key, canonical) "
        "VALUES(?,?)", (key, name))
    return name


def add_edges(doc_id: str, triples: list[dict]) -> int:
    """triples = [{src, rel, dst, confidence}]. Returns inserted count
    (duplicates ignored via the unique index)."""
    init()
    now = datetime.now(_KST).isoformat(timespec="seconds")
    # 영구 무시(대시보드 🗑) 처리된 트리플은 재추출돼도 다시 넣지 않음.
    from . import kg_ignore
    try:
        _ignored = kg_ignore.all_sigs()
    except Exception:
        _ignored = set()
    n = 0
    with _conn() as c:
        for t in triples:
            src = (t.get("src") or "").strip()
            rel = (t.get("rel") or "").strip()
            dst = (t.get("dst") or "").strip()
            if not src or not rel or not dst:
                continue
            src = _resolve_entity(c, src)
            dst = _resolve_entity(c, dst)
            if kg_ignore.sig(src, rel, dst) in _ignored:
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


def merge_duplicate_entities() -> int:
    """One-time-per-boot cleanup (same pattern as purge_junk): canonicalize
    existing edges whose src/dst are spacing/corp-suffix variants of the
    same entity ('삼성전자' vs '삼성 전자' vs '삼성전자(주)') so they share
    one graph node instead of forking into disconnected duplicates.
    New extractions since 2026-07-29 already resolve through
    _resolve_entity at insert time; this sweeps up anything from before
    that (or anything add_edges hasn't seen yet). Idempotent — safe to
    run every boot. Returns edge-reference rows rewritten.

    Commits ONE canon group (norm_key) per transaction instead of the
    whole sweep at once — a single multi-thousand-row transaction would
    hold the write lock long enough to push concurrent add_edges() past
    its 30s busy_timeout, and add_edges() swallows that OperationalError
    silently (bare except), which was a real data-loss risk on any boot
    that overlapped with active ingest."""
    init()
    with _conn() as c:
        names = [r[0] for r in c.execute(
            "SELECT e FROM (SELECT src AS e FROM edges "
            "UNION SELECT dst AS e FROM edges)")]
    groups: dict[str, list[str]] = {}
    for name in names:
        key = _canon_key(name)
        if key:
            groups.setdefault(key, []).append(name)
    merged = 0
    for key, variants in groups.items():
        with _conn() as c:
            if len(variants) == 1:
                c.execute(
                    "INSERT OR IGNORE INTO entity_canon(norm_key, canonical)"
                    " VALUES(?,?)", (key, variants[0]))
                continue
            counts = {
                v: c.execute(
                    "SELECT count(*) FROM edges WHERE src=? OR dst=?",
                    (v, v)).fetchone()[0]
                for v in variants
            }
            # Most-used variant wins; shortest string breaks ties (usually
            # the unadorned form, e.g. "삼성전자" over "삼성전자(주)").
            canonical = sorted(variants, key=lambda v: (-counts[v], len(v)))[0]
            c.execute(
                "INSERT INTO entity_canon(norm_key, canonical) VALUES(?,?) "
                "ON CONFLICT(norm_key) DO UPDATE SET canonical=excluded.canonical",
                (key, canonical))
            var_set = set(variants)
            placeholders = ",".join("?" * len(variants))
            rows = c.execute(
                f"SELECT id, src, dst FROM edges "
                f"WHERE src IN ({placeholders}) OR dst IN ({placeholders})",
                variants + variants).fetchall()
            # One UPDATE per row covering BOTH columns — a row whose src
            # AND dst are both variants of the same entity (self-referencing
            # edge) used to be visited twice (once per column) as two
            # separate UPDATEs; if the second collided with an existing
            # canonical triple, the DELETE fallback discarded the row even
            # though the first UPDATE had already landed correctly.
            for row in rows:
                new_src = canonical if row["src"] in var_set else row["src"]
                new_dst = canonical if row["dst"] in var_set else row["dst"]
                if new_src == row["src"] and new_dst == row["dst"]:
                    continue
                try:
                    c.execute(
                        "UPDATE edges SET src=?, dst=? WHERE id=?",
                        (new_src, new_dst, row["id"]))
                    merged += 1
                except sqlite3.IntegrityError:
                    # Canonical form of this exact triple already exists
                    # (from the same doc) — this row is now a pure
                    # duplicate, drop it instead of erroring.
                    c.execute(
                        "DELETE FROM edges WHERE id=?", (row["id"],))
    if merged:
        log.info("kg merge_duplicate_entities: canonicalized %d refs", merged)
    return merged


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
                "SELECT id,src,rel,dst,confidence,doc_id,ts FROM edges "
                "WHERE src=? OR dst=? ORDER BY confidence DESC, ts DESC LIMIT ?",
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
                f"SELECT id,src,rel,dst,confidence,doc_id,ts FROM edges "
                f"WHERE id IN ({ph})", iids).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def delete_edge(edge_id) -> dict | None:
    """Delete one edge by id — powers the dashboard 🗑. READ/WRITE but does
    NOT call init(): this runs in the dashboard SERVER process and init()'s
    CREATE TABLE/INDEX would grab a write-lock that contends with the bot
    writing kg.db during ingest → 'database is locked'. The table already
    exists.

    Returns the deleted {src, rel, dst} (so the caller can add it to the
    permanent-ignore list), or None if the id was bad / not found / error."""
    try:
        eid = int(edge_id)
    except (TypeError, ValueError):
        return None
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT src,rel,dst FROM edges WHERE id=?", (eid,)).fetchone()
            if not row:
                return None
            c.execute("DELETE FROM edges WHERE id=?", (eid,))
        return {"src": row["src"], "rel": row["rel"], "dst": row["dst"]}
    except Exception:
        return None


def all_edges(limit: int = 3000, order: str = "conf",
             offset: int = 0) -> list[dict]:
    """Every edge for the dashboard KG view. order='conf' (default,
    confidence-desc — Universe page) or 'date' (ts-desc — KG page 최신순
    default, so the 1200-row cap keeps truly-recent low-confidence edges
    instead of only the highest-confidence subset). offset supports the
    KG page's '더 보기' pagination past the initial page."""
    init()
    order_sql = ("ts DESC, confidence DESC" if order == "date"
                 else "confidence DESC, ts DESC")
    with _conn() as c:
        rows = c.execute(
            f"SELECT id,src,rel,dst,confidence,doc_id,ts FROM edges "
            f"ORDER BY {order_sql} LIMIT ? OFFSET ?", (limit, offset)).fetchall()
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
