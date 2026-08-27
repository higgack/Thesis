"""🌌 두뇌 UNIVERSE — the whole knowledge base as one interactive map.

Static HTML under data/dashboard/<token>/universe/index.html: every LLM-wiki
topic is a star (size = source-doc count), edges come from two signals —
  • KG relations whose src/dst entities both resolve to wiki topics (solid)
  • topics that share source documents (dashed)
Click a star → side panel with the wiki page link, its source docs, related
study notes and KG relations. Search, keyword chips, zoom/pan, random jump.

Inspired by the "모소밤부 UNIVERSE" blog map (D3 force graph over a static
payload) — same architecture as the rest of this dashboard: the bot process
regenerates a self-contained file; the stdlib server just serves it. D3 from
the CDN like the reference page (dashboard is online-only anyway).

Colors validated for the dark surface with the dataviz palette checker:
node #6b78e3 / selection accent #b58a2f — all four checks pass (lightness
band, chroma, CVD ΔE 110, contrast). Link kinds are NOT color-alone:
solid (KG) vs dashed (shared-doc) stroke patterns carry the identity.

Rebuild is skipped when the payload signature hasn't changed, so the 15s
regenerate tick doesn't rewrite a ~300KB file for nothing.
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import logging
import os
import re
import time
from collections import defaultdict
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

_LAST_SIG: str | None = None
# Cheap pre-gate signature (wiki index mtime + kg/notes row counts). The
# full payload build reads thousands of meta.db/kg.db/notes.db rows; doing
# that every 15s regenerate tick — from the bot AND the dashboard process —
# adds constant read pressure to the hottest DBs for a page whose content
# rarely changed. Skip the heavy reads entirely unless an input moved.
_LAST_PRE: str | None = None
# Per-rebuild timings, for the log line in render_universe(). There was no
# instrumentation here at all, so "how long does the universe rebuild take
# on the live graph" could not be answered from the logs (2026-08-26).
_MS: dict[str, float] = {}
# Log the full timing at INFO the first time after boot (that is the
# baseline number someone actually wants) and any time a rebuild is slow.
# In between it is DEBUG: during an ingest burst the kg row count changes
# every tick, so this path can run every 15s and should not narrate.
_TIMING_LOGGED = False
_SLOW_REBUILD_MS = 1500.0

# Validated dark-mode palette (see module docstring).
_C_NODE = "#6b78e3"
_C_ACCENT = "#b58a2f"

_SRC_CAP = 30      # source docs listed per topic panel
_NOTE_CAP = 8      # related notes per topic panel
_REL_CAP = 12      # KG relations per topic panel
_DEG_CAP = 8       # keep at most this many links per node (hairball guard)
# Global link cap. Raised 600 → 1500 with the sample below: at 2,124 nodes
# that is still an average degree near 1.4, and _DEG_CAP keeps any single
# hub from eating the budget, so this buys density without a hairball.
_LINK_CAP = 1500
_DOC_FANOUT_MAX = 6  # a doc in more topics than this is too generic to link
# Topic<->topic links are derived from a SAMPLE of the KG, not all of it:
# at 705k edges a full pass would be far too slow for a 15s render tick.
# The footer states the sample size so the map is not mistaken for the
# complete graph.
#
# This sample now feeds ONLY the click-panel relation lists (36 relations
# per topic at most) — the map's links come from the exact SQL join in
# kg.topic_pair_counts(). 30,000 was sized for link-building and measured
# 1,830ms live once it had no reason to be that big; 12,000 keeps panels
# just as full at a proportionally smaller read. order='recent' (id DESC)
# stays: it is sort-free and makes the "최신" wording true.
_KG_EDGE_SAMPLE = 12_000

# Topics that are real wiki pages but meaningless as stars on a relatedness
# map, so they are dropped from this view only (their wiki pages are
# untouched).
#   "기타" — the catch-all bucket for docs no topic could be derived for.
#     Every other consumer already refuses to treat it as a topic:
#     wiki.enqueue() skips it, wiki.run_batch() drops it ("unclassified
#     docs have no coherent theme and produce an unreadable mega-page"),
#     and wiki_render has it in its own _SKIP_TOPICS. This view read
#     wiki_index.json directly and missed that rule, which made the bucket
#     the largest node in the graph and the #1 keyword chip (2,768 docs,
#     about half of all wiki sources).
#   The rest — generic genre/market-bucket words, not subjects (리포트,
#     코스피, 투자, 경제, 주식, 매크로, 금리, …). They accumulate hundreds
#     of unrelated docs and hub to everything, which is noise on a map
#     whose whole job is showing what relates to what. Hand-picked by the
#     user from the live topic list (2026-08-26); extending it is a
#     one-line edit. The footer deliberately does NOT list these — the
#     user asked for the disclosure to go.
_SKIP_TOPICS = (
    "기타", "분석", "보고서", "뉴스", "실적", "신고가",
    "리포트", "외교", "코스피", "공시", "투자", "경제", "증시", "주식",
    "거시경제", "정치", "인터뷰", "매크로", "기술", "금리", "PNG", "특허",
    # 2차 선별 (2026-08-26, 전체 목록을 훑은 사용자 선정 42종)
    "수출", "미국증시", "목표주가", "주가",
    "프로그래밍", "트레이딩", "투자 전략", "음악", "경제 분석",
    "혁신", "펀드", "코스닥", "주식시장", "주", "자기계발", "연구",
    "역사", "시황", "시장 동향", "교육",
    "한양대학교", "특징주", "차트", "증시일정", "증시 동향", "증권",
    "주소록", "종교", "제품", "전략", "시장 분석", "수학", "세법",
    "성균관대학교", "산업 분석", "미국 주식", "데이터 분석", "국제정세",
    "고려대학교", "경제사", "경제 지표",
    "행복", "한국개발연구원",
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _esc(s) -> str:
    return _html.escape(str(s if s is not None else ""), quote=True)


def _build_payload() -> dict | None:
    """Assemble {nodes, links, total_docs} from wiki index + KG + notes +
    meta. Returns None when the wiki index is missing/empty (nothing to
    draw — the page simply isn't generated yet)."""
    from ..store import kg
    from ..store import meta as meta_store
    from ..store import wiki as wiki_store
    from ..notes import store as notes_store
    from .wiki_render import _topic_filename

    idx_path = config.DATA_DIR / "wiki_index.json"
    if not idx_path.exists():
        return None
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception:
        log.warning("universe: wiki_index.json unreadable")
        return None
    if not isinstance(idx, dict) or not idx:
        return None

    try:
        aliases = wiki_store._load_aliases()
    except Exception:
        aliases = {}

    # entity-name → canonical topic key (topic names, titles, aliases)
    norm2key: dict[str, str] = {}
    topics: dict[str, dict] = {}
    for key, rec in idx.items():
        if not isinstance(rec, dict):
            continue
        if key in _SKIP_TOPICS:
            continue
        topics[key] = rec
        norm2key.setdefault(_norm(key), key)
        title = rec.get("title") or ""
        if title:
            norm2key.setdefault(_norm(title), key)
    for alias, canonical in aliases.items():
        if canonical in topics:
            norm2key.setdefault(_norm(alias), canonical)

    # ---- KG signal, from two different queries on purpose
    # LINKS come from an exact SQL aggregation over the WHOLE graph. The
    # map exists to show relatedness, and sampling was terrible at that:
    # only ~2% of edges have a wiki topic at both ends, so 30,000 sampled
    # rows yielded ~640 pairs where the exact join finds ~15,000 — and the
    # join does its per-edge work inside SQLite, which releases the GIL,
    # instead of in a Python loop that holds it.
    kg_pairs: dict[tuple[str, str], int] = {}
    _t_pairs = time.monotonic()
    try:
        for a, b, w in kg.topic_pair_counts(norm2key):
            kg_pairs[(a, b)] = w
    except Exception:
        log.warning("universe: kg topic pairs unavailable", exc_info=True)
    _MS["pairs"] = (time.monotonic() - _t_pairs) * 1000.0
    _MS["n_pairs"] = float(len(kg_pairs))
    # PANEL RELATIONS still come from the recent-edge sample. A panel shows
    # at most _REL_CAP*3 relations for one clicked topic, so a recent slice
    # is the right shape for it, and it costs ~120ms.
    rels: dict[str, list] = defaultdict(list)
    _t_sample = time.monotonic()
    try:
        edges = kg.all_edges(limit=_KG_EDGE_SAMPLE, order="recent")
    except Exception:
        log.warning("universe: kg edges unavailable", exc_info=True)
        edges = []
    for e in edges:
        a = norm2key.get(_norm(e.get("src", "")))
        b = norm2key.get(_norm(e.get("dst", "")))
        if a and len(rels[a]) < _REL_CAP * 3:
            rels[a].append(e)
        if b and b != a and len(rels[b]) < _REL_CAP * 3:
            rels[b].append(e)
    _MS["sample"] = (time.monotonic() - _t_sample) * 1000.0

    # ---- shared-doc signal + doc collection for panels
    doc2topics: dict[str, list[str]] = defaultdict(list)
    all_doc_ids: set[str] = set()
    for key, rec in topics.items():
        for did in (rec.get("doc_ids") or []):
            doc2topics[did].append(key)
            all_doc_ids.add(did)
    doc_pairs: dict[tuple[str, str], int] = defaultdict(int)
    for did, tlist in doc2topics.items():
        if 1 < len(tlist) <= _DOC_FANOUT_MAX:
            ts = sorted(set(tlist))
            for i in range(len(ts)):
                for j in range(i + 1, len(ts)):
                    doc_pairs[(ts[i], ts[j])] += 1

    # ---- doc metadata (chunked IN() — sqlite has a ~999 placeholder cap)
    docs: dict[str, dict] = {}
    ids = list(all_doc_ids)
    _t_docs = time.monotonic()
    try:
        for i in range(0, len(ids), 500):
            docs.update(meta_store.get_docs_batch(ids[i:i + 500]))
    except Exception:
        log.warning("universe: doc batch fetch failed", exc_info=True)
    _MS["docs"] = (time.monotonic() - _t_docs) * 1000.0

    # ---- notes attached to topics, provenance first
    # Pass 1 (exact): a note whose source_ref equals one of the topic's
    # own source documents. notes.source_ref and documents.source are the
    # same kind of label (a URL, or an uploaded file's name), so this is a
    # real join rather than a guess — when a PDF was both archived and
    # noted, the note belongs to exactly the topics that PDF built.
    # Pass 2 (fallback): the old title-substring scan, used only to top up
    # a topic the exact pass left short.
    # The previous third condition, `note.category == topic_key`, is gone:
    # 'AI' and '반도체' are category names AND wiki topics, so every note in
    # those categories attached to that one star regardless of subject —
    # coincidence of naming, not relevance (2026-08-26).
    try:
        notes = notes_store.list_notes()
    except Exception:
        notes = []
    nnotes = [(n, _norm(n.get("title") or "")) for n in notes]
    notes_by_src: dict[str, list] = defaultdict(list)
    for _n in notes:
        _ref = (_n.get("source_ref") or "").strip()
        # "study-text" is the shared constant ref for pasted plain text —
        # it identifies no source and would glue unrelated notes together.
        if _ref and _ref != "study-text":
            notes_by_src[_ref].append(_n)

    # Relation strings dominate the page: measured at 59% of the payload,
    # because each node repeats full entity/relation names and 삼성전자 or
    # 의존 recur across hundreds of nodes. Intern them into one vocabulary
    # and store each relation as [src_i, rel_i, dst_i, conf] — about a
    # third of the bytes of {"s":…,"r":…,"d":…,"c":…}. Insertion order is
    # deterministic (topics are iterated sorted), so the payload signature
    # stays stable across rebuilds with unchanged data.
    vocab: list[str] = []
    vocab_idx: dict[str, int] = {}

    def _vi(text: str) -> int:
        t = text or ""
        i = vocab_idx.get(t)
        if i is None:
            i = len(vocab)
            vocab_idx[t] = i
            vocab.append(t)
        return i

    nodes = []
    _t_nodes = time.monotonic()
    for key, rec in sorted(topics.items()):
        doc_ids = rec.get("doc_ids") or []
        stem = (rec.get("file") or "").rsplit(".", 1)[0] or key
        srcs = []
        for did in doc_ids:
            d = docs.get(did)
            if not d:
                continue
            u = (d.get("source") or "").strip()
            srcs.append({
                "t": (d.get("title") or "(무제)").strip()[:90],
                "u": u if u.startswith("http") else "",
                "d": (d.get("ingested_at") or "")[:10],
            })
        srcs.sort(key=lambda s: s["d"], reverse=True)

        nkey = _norm(key)
        ntitle = _norm(rec.get("title") or "")
        my_notes = []
        seen_notes: set = set()

        def _add_note(n) -> None:
            if n["id"] in seen_notes or len(my_notes) >= _NOTE_CAP:
                return
            seen_notes.add(n["id"])
            my_notes.append({
                "t": (n.get("title") or "")[:80],
                "u": f"../notes/note-{n['id']}.html",
                "c": n.get("category") or "",
            })

        # pass 1 — same source document as this topic
        if notes_by_src:
            for did in doc_ids:
                if len(my_notes) >= _NOTE_CAP:
                    break
                d = docs.get(did)
                src_label = (d or {}).get("source") or ""
                for n in notes_by_src.get(src_label.strip(), ()):
                    _add_note(n)
        # pass 2 — title substring, only to fill remaining slots
        if len(my_notes) < _NOTE_CAP:
            for n, nt in nnotes:
                if len(my_notes) >= _NOTE_CAP:
                    break
                if (nkey and nkey in nt) or (ntitle and ntitle in nt):
                    _add_note(n)

        my_rels = sorted(rels.get(key, []),
                         key=lambda e: e.get("confidence") or 0,
                         reverse=True)[:_REL_CAP]

        nodes.append({
            "id": key,
            "label": rec.get("title") or key,
            "docs": len(doc_ids),
            "up": (rec.get("updated") or "")[:10],
            "url": f"../wiki/{_topic_filename(stem)}",
            "srcs": srcs[:_SRC_CAP],
            "notes": my_notes,
            "rels": [[_vi(e["src"]), _vi(e["rel"]), _vi(e["dst"]),
                      round(e.get("confidence") or 0, 2)]
                     for e in my_rels],
            "al": " ".join(a for a, c in aliases.items() if c == key)[:200],
        })

    if not nodes:
        return None

    _MS["nodes"] = (time.monotonic() - _t_nodes) * 1000.0

    # ---- merge + prune links (weight-desc, per-node degree cap)
    cand = [{"s": a, "t": b, "k": "kg", "w": w}
            for (a, b), w in kg_pairs.items()]
    for (a, b), w in doc_pairs.items():
        if (a, b) not in kg_pairs:
            cand.append({"s": a, "t": b, "k": "doc", "w": w})
    cand.sort(key=lambda l: l["w"], reverse=True)
    deg: dict[str, int] = defaultdict(int)
    links = []
    for l in cand:
        if len(links) >= _LINK_CAP:
            break
        if deg[l["s"]] >= _DEG_CAP and deg[l["t"]] >= _DEG_CAP:
            continue
        deg[l["s"]] += 1
        deg[l["t"]] += 1
        links.append(l)

    # COUNT(*) directly, NOT kg.stats() — stats() also computes the
    # distinct-entity count, whose UNION over src+dst measures 420ms on a
    # 648k-edge graph, while this count is ~2ms. Read-only URI so the
    # render never takes kg.db's write lock.
    kg_total = 0
    try:
        import sqlite3 as _sq
        _kgp = config.DATA_DIR / "kg.db"
        if _kgp.exists():
            _c = _sq.connect(f"file:{_kgp}?mode=ro", uri=True, timeout=5)
            try:
                kg_total = int(_c.execute(
                    "SELECT COUNT(*) FROM edges").fetchone()[0] or 0)
            finally:
                _c.close()
    except Exception:
        kg_total = 0
    # Which vocabulary words resolve to a topic that is actually a star on
    # this map. Lets the panel's 지식그래프 관계 list turn 심텍 into a jump
    # to the 심텍 star instead of dead text — the point being to keep
    # following the graph (사용자 요청 2026-08-26). Only mapped words are
    # listed, and node_keys guards against pointing at a topic that was
    # skipped or has no node.
    node_keys = {n["id"] for n in nodes}
    vtopic = {}
    for i, word in enumerate(vocab):
        k = norm2key.get(_norm(word))
        if k and k in node_keys:
            vtopic[str(i)] = k
    return {"nodes": nodes, "links": links, "total_docs": len(all_doc_ids),
            "vocab": vocab, "vtopic": vtopic,
            "kg_sampled": len(edges), "kg_total": kg_total}


def _render_html(payload: dict, token: str) -> str:
    from . import widgets as _widgets
    from .kg_render import _THEME_JS  # same 19~07 KST dark switch as every page
    tok = _esc(token)
    # "</" would terminate the <script> block mid-JSON.
    data = json.dumps(payload, ensure_ascii=False,
                      separators=(",", ":")).replace("</", "<\\/")
    n_topics = len(payload["nodes"])
    n_links = len(payload["links"])
    n_docs = payload["total_docs"]
    return """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🌌 두뇌 UNIVERSE</title>
<link rel="preconnect" href="https://cdnjs.cloudflare.com">
<script>%(theme_js)s</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
<style>
*{box-sizing:border-box}
/* Light by default; [data-theme=dark] flips 19~07 KST like every other
   dashboard page (same _THEME_JS). Palette passes the dataviz checks on
   BOTH surfaces. --lk/--lkdoc/--nodeline drive link + node-outline colors
   so the graph re-skins with the theme. */
:root{--bg:#f2f3f8;--panel:#ffffff;--ink:#23262f;--sub:#697086;--line:#d9dde9;
  --chrome:rgba(242,243,248,.94);--field:#ffffff;--chip:#ffffff;--hov:#eceef7;
  --lk:#7a84cf;--lkdoc:#a9b0cc;--nodeline:#4d59c4;
  --node:%(C_NODE)s;--accent:%(C_ACCENT)s}
[data-theme=dark]{--bg:#0d1117;--panel:#161b27;--ink:#e6e9f2;--sub:#8b93a8;
  --line:#262c3d;--chrome:rgba(13,17,23,.94);--field:#0f141f;--chip:#121826;
  --hov:#1b2233;--lk:#7c88cf;--lkdoc:#586285;--nodeline:#9aa4ee}
html,body{margin:0;height:100%%;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic",sans-serif;overflow:hidden;
  transition:background-color .3s,color .3s}
#app{position:fixed;inset:0;display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:10px;padding:10px 16px;
  background:var(--chrome);border-bottom:1px solid var(--line);z-index:20}
header h1{font-size:15px;margin:0;font-weight:800;white-space:nowrap}
#search{flex:1;max-width:340px;background:var(--field);border:1px solid var(--line);
  color:var(--ink);border-radius:999px;padding:8px 15px;font-size:13px;outline:none}
#search:focus{border-color:var(--node)}
.nav{white-space:nowrap;text-decoration:none;font-size:12.5px;font-weight:600;
  padding:7px 12px;border-radius:999px;border:1px solid var(--line);
  background:var(--chip);color:var(--sub)}
.nav:hover{border-color:var(--node);color:var(--ink)}
#stage{flex:1;position:relative;overflow:hidden}
svg#svg{width:100%%;height:100%%;display:block;cursor:grab;touch-action:none}
.lk{stroke:var(--lk);fill:none;shape-rendering:optimizeSpeed}.lk.doc{stroke:var(--lkdoc);stroke-dasharray:4 4}
.lk.hot{stroke:var(--accent)}
/* Selection styling lives in CSS so picking a star costs O(its degree)
   DOM writes instead of one pass over every node and every link. With
   2,124 nodes and 1,500 links that was ~8,750 writes per click. */
g.sel .lk{opacity:.15}
g.sel .lk.hot{opacity:.95}
.node-c.sel circle{stroke:var(--accent)!important;stroke-width:3}
.node-c{cursor:pointer}
.lbl{text-rendering:optimizeSpeed;paint-order:stroke;stroke:var(--bg);stroke-width:3px;fill:var(--ink);
  font-weight:600;pointer-events:none;user-select:none}
/* One row, clipped: fillKwbar() appends chips until the next one would
   overflow and stops there, so the bar always shows the most chips that
   fit THIS screen without wrapping over the map (wrapping to three rows
   buried the stars under chips — 사용자 피드백 2026-08-26). */
#kwbar{position:absolute;top:10px;left:50%%;transform:translateX(-50%%);z-index:12;
  display:flex;flex-wrap:nowrap;gap:6px;max-width:92%%;overflow:hidden;
  padding:4px}
#kwbar::-webkit-scrollbar{display:none}
.kw{white-space:nowrap;cursor:pointer;font-size:12px;font-weight:600;padding:6px 12px;
  border-radius:999px;background:var(--chrome);border:1px solid var(--line);color:var(--sub)}
.kw:hover{border-color:var(--node);color:var(--ink)}
#panel{position:absolute;top:0;right:0;height:100%%;width:380px;max-width:88vw;
  background:var(--panel);border-left:1px solid var(--line);transform:translateX(100%%);
  transition:transform .25s ease;z-index:30;display:flex;flex-direction:column}
#panel.open{transform:translateX(0)}
#panel .ph{padding:14px 16px 11px;border-bottom:1px solid var(--line)}
#panel .ptitle{font-size:17px;font-weight:800;margin:0 40px 4px 0;color:var(--ink)}
#panel .pmeta{font-size:11.5px;color:var(--sub)}
#pclose{position:absolute;top:12px;right:12px;background:var(--chip);
  border:1px solid var(--line);color:var(--sub);border-radius:8px;width:28px;height:28px;
  font-size:14px;cursor:pointer}
#panel .plist{flex:1;overflow-y:auto;padding:10px 12px 30px}
.sec{font-size:11px;font-weight:800;color:var(--sub);letter-spacing:.06em;
  margin:14px 4px 6px;text-transform:uppercase}
.pitem{display:block;text-decoration:none;color:var(--ink);padding:7px 9px;
  border-radius:9px;border-left:2px solid transparent;font-size:13px;line-height:1.45}
.pitem:hover{background:var(--hov);border-left-color:var(--node)}
.pitem .pd{font-size:10.5px;color:var(--sub);margin-top:2px}
.conns{display:flex;flex-wrap:wrap;gap:5px;padding:2px 4px 4px}
.conn{display:inline-flex;align-items:center;gap:5px;cursor:pointer;
  font:inherit;font-size:12px;color:var(--ink);background:var(--hov);
  border:1px solid var(--nodeline);border-radius:999px;padding:4px 9px}
.conn:hover{border-color:var(--node);background:var(--chrome)}
.conn .cw{font-size:10px;color:var(--sub);font-weight:700}
/* dashed border mirrors the map's dashed shared-doc link, so the two
   kinds are told apart by shape here as well as by colour */
.conn.doc{border-style:dashed}
.rel{font-size:12px;color:var(--sub);padding:5px 9px;line-height:1.5}
.rel b{color:var(--ink);font-weight:600}
.rel .rl{font:inherit;font-weight:600;color:var(--ink);background:none;
  border:0;border-bottom:1px dashed var(--nodeline);padding:0;cursor:pointer}
.rel .rl:hover{color:var(--accent);border-bottom-color:var(--accent)}
.rel .rr{color:var(--node);font-weight:700}
.wikibtn{display:block;text-align:center;margin:10px 4px 0;padding:10px;
  border-radius:9px;background:var(--node);color:#0d1117;font-weight:800;
  font-size:13.5px;text-decoration:none}
#foot{position:absolute;left:14px;bottom:12px;font-size:11px;color:var(--sub);
  z-index:10;background:var(--chrome);padding:6px 10px;border-radius:8px;
  border:1px solid var(--line);line-height:1.6}
#foot .sw{display:inline-block;width:14px;height:0;border-top:2px solid var(--lk);
  vertical-align:middle;margin:0 3px}
#foot .sw.dash{border-top-style:dashed;border-top-color:var(--lkdoc)}
#ctrls{position:absolute;right:14px;bottom:12px;z-index:10;display:flex;gap:6px}
#ctrls button{background:var(--chrome);border:1px solid var(--line);
  color:var(--ink);width:34px;height:34px;border-radius:8px;font-size:15px;cursor:pointer}
#ctrls button:hover{border-color:var(--node)}
#tip{position:absolute;pointer-events:none;z-index:40;background:var(--panel);
  border:1px solid var(--line);color:var(--ink);font-size:12px;padding:6px 10px;
  border-radius:8px;opacity:0;transition:opacity .12s;max-width:260px}
@media(max-width:760px){#panel{width:100%%;max-width:100%%;height:66%%;top:auto;bottom:0;
  border-left:none;border-top:1px solid var(--line);border-radius:16px 16px 0 0;
  transform:translateY(100%%)}#panel.open{transform:translateY(0)}
  header h1 span{display:none}}
</style></head><body>
<div id="app">
<header>
  <h1>🌌 두뇌 <span style="color:var(--node)">UNIVERSE</span></h1>
  <input id="search" placeholder="🔍 토픽 검색">
  <a class="nav" href="/%(tok)s/">🧠 Archive</a>
  <a class="nav" href="/%(tok)s/wiki/">📚 Wiki</a>
  <a class="nav" href="/%(tok)s/kg/">🕸 KG</a>
  <a class="nav" href="/%(tok)s/notes/">📒 Note</a>
  <a class="nav" href="/%(tok)s/commands/">📋 Commands</a>
  <a class="nav" href="https://echodiary-eng.vercel.app/" target="_blank" rel="noopener">📅 Daily</a>
</header>
<div id="stage">
  <svg id="svg"></svg>
  <div id="kwbar"></div>
  <div id="panel"><button id="pclose">×</button>
    <div class="ph"><h2 class="ptitle" id="ptitle"></h2><div class="pmeta" id="pmeta"></div></div>
    <div class="plist" id="plist"></div></div>
  <div id="foot"><b>%(n_topics)s</b>개 토픽 · <b>%(n_links)s</b>개 연결 · 문서 <b>%(n_docs)s</b>편<br>
    ● 크기=문서 수 · <span class="sw"></span>KG 관계 · <span class="sw dash"></span>공유 문서<br>
    <span style="opacity:.72">연결선은 KG 관계 %(kg_total)s개 전수 기준</span></div>
  <div id="ctrls"><button id="dice" title="랜덤">🎲</button><button id="zin">+</button>
    <button id="zout">−</button><button id="zfit">⤢</button></div>
  <div id="tip"></div>
</div>
</div>
<script>const PAYLOAD=%(data)s;</script>
<script>
const stage=document.getElementById('stage'),svg=d3.select('#svg'),
  tip=document.getElementById('tip');
let W=stage.clientWidth,H=stage.clientHeight;
const NODES=PAYLOAD.nodes.map(d=>Object.assign({},d));
const byId={};NODES.forEach(n=>byId[n.id]=n);
const VOCAB=PAYLOAD.vocab||[];
const VTOPIC=PAYLOAD.vtopic||{};
const LINKS=PAYLOAD.links.filter(l=>byId[l.s]&&byId[l.t])
  .map(l=>({source:l.s,target:l.t,k:l.k,w:l.w}));
// Adjacency for the panel's 연결된 토픽 list. Built from PAYLOAD.links
// while s/t are still plain ids — d3.forceLink rewrites LINKS' source/
// target into node objects, so reading it after the simulation starts
// would need a different shape. Same data the map draws, so the list and
// the lines can never disagree.
const ADJ={};
PAYLOAD.links.forEach(l=>{
  if(!byId[l.s]||!byId[l.t])return;
  (ADJ[l.s]=ADJ[l.s]||[]).push({id:l.t,w:l.w,k:l.k});
  (ADJ[l.t]=ADJ[l.t]||[]).push({id:l.s,w:l.w,k:l.k});});
Object.keys(ADJ).forEach(k=>ADJ[k].sort((a,b)=>b.w-a.w));
let panelJump=[];   // topic keys the open panel can jump to (numeric refs)
const R=d=>Math.min(34,7+Math.sqrt(d.docs||1)*2.4);
const FS=d=>Math.max(10,Math.min(15,9+Math.sqrt(d.docs||1)));
const g=svg.append('g');
const zoom=d3.zoom().scaleExtent([.15,4])
  .on('zoom',ev=>g.attr('transform',ev.transform));
svg.call(zoom);
const lk=g.selectAll('line').data(LINKS).join('line')
  .attr('class',l=>'lk'+(l.k==='doc'?' doc':''))
  .attr('stroke-width',l=>Math.min(4.5,1.3+l.w*.55)).attr('opacity',.8);
const nd=g.selectAll('g.node-c').data(NODES).join('g').attr('class','node-c');
nd.append('circle').attr('r',R).attr('fill','var(--node)')
  .attr('fill-opacity',.85).style('stroke','var(--nodeline)').attr('stroke-width',1);
nd.append('text').attr('class','lbl').attr('text-anchor','middle')
  .attr('dy',d=>R(d)+13).style('font-size',d=>FS(d)+'px')
  .text(d=>d.label.length>16?d.label.slice(0,15)+'…':d.label);
const sim=d3.forceSimulation(NODES)
  .force('link',d3.forceLink(LINKS).id(d=>d.id)
    .distance(l=>110-Math.min(40,l.w*6)).strength(l=>Math.min(.5,.15+l.w*.05)))
  .force('charge',d3.forceManyBody().strength(-320))
  .force('collide',d3.forceCollide().radius(d=>R(d)+16))
  .force('x',d3.forceX(W/2).strength(.04))
  .force('y',d3.forceY(H/2).strength(.04));
function ticked(){
  lk.attr('x1',l=>l.source.x).attr('y1',l=>l.source.y)
    .attr('x2',l=>l.target.x).attr('y2',l=>l.target.y);
  nd.attr('transform',d=>`translate(${d.x},${d.y})`);
}
sim.on('tick',ticked);
// Settle the layout BEFORE first paint: the multi-second "stars floating
// into place" intro read as page slowness. ~200 synchronous ticks of a
// few-hundred-node graph is <150ms, then the map appears fully formed.
sim.stop();
const _t0=performance.now();
for(let i=0;i<200;i++)sim.tick();
ticked();
// id -> DOM element, so selecting a star touches only the elements that
// actually change instead of re-walking every node and link.
const NODEEL={},LINKEL={};
nd.each(function(d){NODEEL[d.id]=this;});
lk.each(function(l){
  const a=l.source.id!==undefined?l.source.id:l.source;
  const b=l.target.id!==undefined?l.target.id:l.target;
  (LINKEL[a]=LINKEL[a]||[]).push(this);
  (LINKEL[b]=LINKEL[b]||[]).push(this);});
console.log(`universe: ${NODES.length} nodes, ${LINKS.length} links, `
  +`layout ${Math.round(performance.now()-_t0)}ms`);
nd.call(d3.drag()
  .on('start',(ev,d)=>{if(!ev.active)sim.alphaTarget(.25).restart();d.fx=d.x;d.fy=d.y;})
  .on('drag',(ev,d)=>{d.fx=ev.x;d.fy=ev.y;})
  .on('end',(ev,d)=>{if(!ev.active)sim.alphaTarget(0);d.fx=null;d.fy=null;}));
// ---- panel
const panel=document.getElementById('panel');
let selected=null;
function esc(s){const d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}
function openPanel(d){
  // spotlight: only the previously- and newly-selected elements change.
  // The dimming of everything else is one class on the container, and
  // stroke-width no longer moves — the old hot/normal widths differed by
  // half a pixel while the accent colour already carries the signal.
  applySel(d.id);
  document.getElementById('ptitle').textContent=d.label;
  document.getElementById('pmeta').textContent=
    `문서 ${d.docs}편`+(d.up?` · 갱신 ${d.up}`:'');
  let h=`<a class="wikibtn" href="${d.url}">📚 위키 페이지 열기</a>`;
  // Which topics this star is actually wired to. Until now the only way
  // to read that was to trace the lines by eye, which is the one thing
  // the map is for. Weight-desc, click to jump.
  // One jump table per panel render, shared by the chips below and by the
  // clickable entity names in the relation list. Buttons carry its numeric
  // index: esc() escapes < > & but NOT quotes, so a topic name is never
  // safe to drop into an attribute.
  panelJump=[];
  const jump=k=>{const i=panelJump.indexOf(k);
    if(i>=0)return i;panelJump.push(k);return panelJump.length-1;};
  const conns=(ADJ[d.id]||[]).filter(c=>byId[c.id]);
  if(conns.length){
    h+=`<div class="sec">🔗 연결된 토픽 (${conns.length})</div>`
      +`<div class="conns">`;
    conns.forEach(c=>{
      const t=c.k==='doc'?'공유 문서':'KG 관계';
      h+=`<button type="button" class="conn${c.k==='doc'?' doc':''}"`
        +` data-goto="${jump(c.id)}" title="${t} · 가중치 ${c.w}">`
        +`${esc(byId[c.id].label)}<span class="cw">${c.w}</span></button>`;});
    h+=`</div>`;}
  if(d.rels&&d.rels.length){h+=`<div class="sec">🕸 지식그래프 관계</div>`;
    // r is [src_i, rel_i, dst_i, conf] into PAYLOAD.vocab — see the
    // interning note in _build_payload.
    // An endpoint that is itself a star becomes a jump, so the graph can
    // be walked from the relation list too: 반도체 → 심텍 → onward.
    const ent=i=>{const k=VTOPIC[i];
      return (k&&byId[k])
        ? `<button type="button" class="rl" data-goto="${jump(k)}"`
          +` title="${esc(byId[k].label)} 토픽으로 이동">`
          +`${esc(VOCAB[i])}</button>`
        : `<b>${esc(VOCAB[i])}</b>`;};
    d.rels.forEach(r=>{h+=`<div class="rel">${ent(r[0])} <span class="rr">—${esc(VOCAB[r[1]])}→</span> ${ent(r[2])}</div>`;});}
  if(d.notes&&d.notes.length){h+=`<div class="sec">📒 관련 학습노트</div>`;
    d.notes.forEach(n=>{h+=`<a class="pitem" href="${n.u}">${esc(n.t)}${n.c?`<div class="pd">${esc(n.c)}</div>`:''}</a>`;});}
  if(d.srcs&&d.srcs.length){h+=`<div class="sec">📄 소스 문서 (${d.docs}편${d.srcs.length<d.docs?', 최근 '+d.srcs.length:''})</div>`;
    d.srcs.forEach(s=>{h+=s.u
      ?`<a class="pitem" href="${s.u}" target="_blank" rel="noopener">${esc(s.t)}<div class="pd">${esc(s.d)}</div></a>`
      :`<div class="pitem">${esc(s.t)}<div class="pd">${esc(s.d)}</div></div>`;});}
  document.getElementById('plist').innerHTML=h;
  document.getElementById('plist').scrollTop=0;
  panel.classList.add('open');
}
function applySel(id){
  if(selected){
    const prev=NODEEL[selected];
    if(prev)prev.classList.remove('sel');
    (LINKEL[selected]||[]).forEach(e=>e.classList.remove('hot'));}
  selected=id;
  if(id){
    const cur=NODEEL[id];
    if(cur)cur.classList.add('sel');
    (LINKEL[id]||[]).forEach(e=>e.classList.add('hot'));}
  g.node().classList.toggle('sel',!!id);}
function clearSel(){panel.classList.remove('open');applySel(null);
  applyFilter();}
document.getElementById('pclose').onclick=clearSel;
nd.on('click',(ev,d)=>{ev.stopPropagation();openPanel(d);});
nd.on('dblclick',(ev,d)=>{location.href=d.url;});
// ---- tooltip
nd.on('mousemove',(ev,d)=>{tip.style.opacity=1;
  tip.innerHTML=`<b>${esc(d.label)}</b><br>문서 ${d.docs}편 — 클릭해 펼치기`;
  const r=stage.getBoundingClientRect();
  tip.style.left=Math.min(r.width-270,ev.clientX-r.left+14)+'px';
  tip.style.top=(ev.clientY-r.top+12)+'px';})
  .on('mouseleave',()=>tip.style.opacity=0);
// ---- search (dim non-matching; Enter focuses first match)
const q=document.getElementById('search');
let filterDirty=false;
function applyFilter(){
  const v=(q.value||'').trim().toLowerCase();
  if(!v){
    // 흐림이 적용된 적 없으면 되돌릴 것도 없다 — 배경 클릭(clearSel)마다
    // 전 노드·전 링크에 opacity 를 다시 쓰던 3,600회 DOM 작업을 생략
    // (저사양에서 클릭이 굼뜨던 원인 중 하나, 2026-08-27).
    if(filterDirty){nd.attr('opacity',1);lk.attr('opacity',.8);filterDirty=false;}
    return;}
  filterDirty=true;
  // NOTE: while a star is selected, `g.sel .lk` in CSS outranks the link
  // opacity written below — same precedence the old inline spotlight had,
  // and clearSel() re-runs this once the selection drops.
  const m=d=>(d.label+' '+d.id+' '+(d.al||'')).toLowerCase().includes(v);
  // 매치만 밝히면 연결선이 어두운 별로 사라져 "뭐랑 연결됐는지"가 안 보인다.
  // 매치 + 1-hop 이웃까지 함께 밝히고, 매치에 닿는 링크는 살린다.
  const hit=new Set();NODES.forEach(d=>{if(m(d))hit.add(d.id);});
  const nbr=new Set();
  LINKS.forEach(l=>{const s=hit.has(l.source.id),t=hit.has(l.target.id);
    if(s&&!t)nbr.add(l.target.id);if(t&&!s)nbr.add(l.source.id);});
  nd.attr('opacity',d=>hit.has(d.id)?1:nbr.has(d.id)?.8:.12);
  lk.attr('opacity',l=>(hit.has(l.source.id)||hit.has(l.target.id))?.85:.05);
}
// Debounced: applyFilter walks every node and every link and then writes
// an opacity to each — ~7,000 operations. Running that per keystroke made
// typing in the search box lag once the link count grew.
let _fT=null;
q.addEventListener('input',()=>{
  clearTimeout(_fT);_fT=setTimeout(applyFilter,120);});
q.addEventListener('keydown',ev=>{if(ev.key!=='Enter')return;
  const v=(q.value||'').trim().toLowerCase();if(!v)return;
  const hit=NODES.find(d=>(d.label+' '+d.id+' '+(d.al||'')).toLowerCase().includes(v));
  if(hit){focusNode(hit);openPanel(hit);}});
// Delegated: #plist's innerHTML is rebuilt on every open, so per-chip
// listeners would be lost. Registered once.
document.getElementById('plist').addEventListener('click',ev=>{
  const b=ev.target.closest('button[data-goto]');
  if(!b)return;
  const n=byId[panelJump[+b.dataset.goto]];
  if(n){focusNode(n);openPanel(n);}});
function focusNode(d){
  // 250ms: 전환 프레임마다 5,700개 SVG 요소를 다시 그리므로 지속시간이
  // 곧 저사양 기기의 멈춤 시간이다. 짧게, 그러나 순간이동은 아니게.
  svg.transition().duration(250).call(zoom.transform,
    d3.zoomIdentity.translate(W/2-d.x*1.4,H/2-d.y*1.4).scale(1.4));}
// ---- chips: top topics by doc count (same measure as star size).
// Up to 20 candidates, but only as many as fit one row on this screen:
// append until the bar overflows, then drop the one that overflowed.
// Re-fitted on resize, so the "best count" tracks the window instead of
// being a hardcoded guess.
const kwbar=document.getElementById('kwbar');
const KWCAND=NODES.slice().sort((a,b)=>b.docs-a.docs).slice(0,20);
function fillKwbar(){
  kwbar.textContent='';
  for(const d of KWCAND){
    const b=document.createElement('button');b.className='kw';
    b.textContent=`${d.label} ${d.docs}`;
    b.onclick=()=>{focusNode(d);openPanel(d);};
    kwbar.appendChild(b);
    if(kwbar.scrollWidth>kwbar.clientWidth){b.remove();break;}
  }
}
fillKwbar();
let _kwT=null;
addEventListener('resize',()=>{clearTimeout(_kwT);_kwT=setTimeout(fillKwbar,150);});
// ---- controls
function fit(){
  if(!NODES.length)return;
  // 칩 바가 stage 위에 떠 있으므로, 그 아래 영역에만 지도를 맞춘다 —
  // 상단 별들이 키워드 칩 뒤로 숨던 문제 (사용자 요청 2026-08-27).
  const bar=document.getElementById('kwbar');
  const TOP=bar?bar.offsetTop+bar.offsetHeight+12:60;
  const AH=Math.max(120,H-TOP);
  const xs=NODES.map(d=>d.x),ys=NODES.map(d=>d.y);
  const x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys),y1=Math.max(...ys);
  const s=Math.min(2,.88/Math.max((x1-x0)/W,(y1-y0)/AH,.01));
  svg.transition().duration(500).call(zoom.transform,
    d3.zoomIdentity.translate(W/2-s*(x0+x1)/2,TOP+AH/2-s*(y0+y1)/2).scale(s));}
document.getElementById('zin').onclick=()=>svg.transition().call(zoom.scaleBy,1.35);
document.getElementById('zout').onclick=()=>svg.transition().call(zoom.scaleBy,1/1.35);
document.getElementById('zfit').onclick=fit;
document.getElementById('dice').onclick=()=>{
  const d=NODES[Math.floor(Math.random()*NODES.length)];focusNode(d);openPanel(d);};
svg.on('click',clearSel);
fit();  // positions are pre-settled — frame the whole map immediately
addEventListener('resize',()=>{W=stage.clientWidth;H=stage.clientHeight;
  sim.force('x',d3.forceX(W/2).strength(.04))
     .force('y',d3.forceY(H/2).strength(.04));sim.alpha(.2).restart();});
</script>
%(reload_js)s
</body></html>""" % {
        "C_NODE": _C_NODE, "C_ACCENT": _C_ACCENT, "tok": tok,
        "n_topics": n_topics, "n_links": n_links, "n_docs": n_docs,
        "kg_total": f'{payload.get("kg_total", 0):,}',
        "data": data, "reload_js": _widgets.live_reload_js("universe"),
        "theme_js": _THEME_JS,
    }


def _pre_signature() -> str | None:
    """File-stat + row-count fingerprint of the universe's inputs — no
    heavy reads. None → couldn't compute (fail open, do the full build)."""
    import sqlite3
    try:
        idx_path = config.DATA_DIR / "wiki_index.json"
        parts = [str(idx_path.stat().st_mtime_ns) if idx_path.exists()
                 else "0"]
        for db, table in (("kg.db", "edges"), ("notes.db", "notes"),
                          ("meta.db", "documents")):
            p = config.DATA_DIR / db
            if not p.exists():
                parts.append("0")
                continue
            c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
            try:
                parts.append(str(c.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]))
            finally:
                c.close()
        return "-".join(parts)
    except Exception:
        return None


def render_universe(token: str) -> int:
    """Write data/dashboard/<token>/universe/index.html. Returns 1 if
    (re)written, 0 when skipped (no data / inputs unchanged)."""
    global _LAST_SIG, _LAST_PRE
    out_file = (Path(config.DATA_DIR) / "dashboard" / token
                / "universe" / "index.html")
    _t0 = time.monotonic()
    pre = _pre_signature()
    if pre is not None and pre == _LAST_PRE and out_file.exists():
        return 0
    payload = _build_payload()
    if payload is None:
        return 0
    _LAST_PRE = pre
    _t_emit = time.monotonic()
    sig = hashlib.sha1(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    base = Path(config.DATA_DIR) / "dashboard" / token / "universe"
    out = base / "index.html"
    if sig == _LAST_SIG and out.exists():
        return 0
    base.mkdir(parents=True, exist_ok=True)
    html_text = _render_html(payload, token)
    pid = os.getpid()
    tmp = base / f"index.html.{pid}.tmp"
    tmp.write_text(html_text, encoding="utf-8")
    os.replace(tmp, out)
    _LAST_SIG = sig
    _MS["emit"] = (time.monotonic() - _t_emit) * 1000.0
    global _TIMING_LOGGED
    total = (time.monotonic() - _t0) * 1000.0
    # Phase breakdown, because the first live rebuild came in at 13.1s
    # against a ~0.5s synthetic estimate with 8s of it unattributed —
    # docs = meta.db batch fetch, nodes = per-topic assembly incl. the
    # note-matching passes, emit = signature dumps + HTML render + write.
    msg = ("universe rebuilt in %.0fms (pairs %.0fms → %d, sample %.0fms, "
           "docs %.0fms, nodes %.0fms, emit %.0fms, %d nodes, %d links, "
           "page %.0fKB)")
    args = (total, _MS.get("pairs", 0.0), int(_MS.get("n_pairs", 0)),
            _MS.get("sample", 0.0), _MS.get("docs", 0.0),
            _MS.get("nodes", 0.0), _MS.get("emit", 0.0),
            len(payload["nodes"]), len(payload["links"]),
            len(html_text) / 1024.0)
    if total >= _SLOW_REBUILD_MS or not _TIMING_LOGGED:
        log.info(msg, *args)
        _TIMING_LOGGED = True
    else:
        log.debug(msg, *args)
    return 1
