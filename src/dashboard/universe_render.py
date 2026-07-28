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

# Validated dark-mode palette (see module docstring).
_C_NODE = "#6b78e3"
_C_ACCENT = "#b58a2f"

_SRC_CAP = 30      # source docs listed per topic panel
_NOTE_CAP = 8      # related notes per topic panel
_REL_CAP = 12      # KG relations per topic panel
_DEG_CAP = 8       # keep at most this many links per node (hairball guard)
_LINK_CAP = 600    # global link cap
_DOC_FANOUT_MAX = 6  # a doc in more topics than this is too generic to link


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
        topics[key] = rec
        norm2key.setdefault(_norm(key), key)
        title = rec.get("title") or ""
        if title:
            norm2key.setdefault(_norm(title), key)
    for alias, canonical in aliases.items():
        if canonical in topics:
            norm2key.setdefault(_norm(alias), canonical)

    # ---- KG signal: topic↔topic relation counts + per-topic relations
    kg_pairs: dict[tuple[str, str], int] = defaultdict(int)
    rels: dict[str, list] = defaultdict(list)
    try:
        edges = kg.all_edges(limit=3000)
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
        if a and b and a != b:
            kg_pairs[tuple(sorted((a, b)))] += 1

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
    try:
        for i in range(0, len(ids), 500):
            docs.update(meta_store.get_docs_batch(ids[i:i + 500]))
    except Exception:
        log.warning("universe: doc batch fetch failed", exc_info=True)

    # ---- notes matched by title/category (cheap substring pass)
    try:
        notes = notes_store.list_notes()
    except Exception:
        notes = []
    nnotes = [(n, _norm(n.get("title") or "")) for n in notes]

    nodes = []
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
        for n, nt in nnotes:
            if len(my_notes) >= _NOTE_CAP:
                break
            if (n.get("category") or "") == key or \
                    (nkey and nkey in nt) or (ntitle and ntitle in nt):
                my_notes.append({
                    "t": (n.get("title") or "")[:80],
                    "u": f"../notes/note-{n['id']}.html",
                    "c": n.get("category") or "",
                })

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
            "rels": [{"s": e["src"], "r": e["rel"], "d": e["dst"],
                      "c": round(e.get("confidence") or 0, 2)}
                     for e in my_rels],
            "al": " ".join(a for a, c in aliases.items() if c == key)[:200],
        })

    if not nodes:
        return None

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

    return {"nodes": nodes, "links": links, "total_docs": len(all_doc_ids)}


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
.lk{stroke:var(--lk);fill:none}.lk.doc{stroke:var(--lkdoc);stroke-dasharray:4 4}
.lk.hot{stroke:var(--accent)}
.node-c{cursor:pointer}
.lbl{paint-order:stroke;stroke:var(--bg);stroke-width:3px;fill:var(--ink);
  font-weight:600;pointer-events:none;user-select:none}
#kwbar{position:absolute;top:10px;left:50%%;transform:translateX(-50%%);z-index:12;
  display:flex;gap:6px;max-width:92%%;overflow-x:auto;padding:4px;scrollbar-width:none}
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
.rel{font-size:12px;color:var(--sub);padding:5px 9px;line-height:1.5}
.rel b{color:var(--ink);font-weight:600}
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
  <a class="nav" href="/%(tok)s/notes/">📒 노트</a>
  <a class="nav" href="/%(tok)s/commands/">📋 Commands</a>
</header>
<div id="stage">
  <svg id="svg"></svg>
  <div id="kwbar"></div>
  <div id="panel"><button id="pclose">×</button>
    <div class="ph"><h2 class="ptitle" id="ptitle"></h2><div class="pmeta" id="pmeta"></div></div>
    <div class="plist" id="plist"></div></div>
  <div id="foot"><b>%(n_topics)s</b>개 토픽 · <b>%(n_links)s</b>개 연결 · 문서 <b>%(n_docs)s</b>편<br>
    ● 크기=문서 수 · <span class="sw"></span>KG 관계 · <span class="sw dash"></span>공유 문서</div>
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
const LINKS=PAYLOAD.links.filter(l=>byId[l.s]&&byId[l.t])
  .map(l=>({source:l.s,target:l.t,k:l.k,w:l.w}));
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
for(let i=0;i<200;i++)sim.tick();
ticked();
nd.call(d3.drag()
  .on('start',(ev,d)=>{if(!ev.active)sim.alphaTarget(.25).restart();d.fx=d.x;d.fy=d.y;})
  .on('drag',(ev,d)=>{d.fx=ev.x;d.fy=ev.y;})
  .on('end',(ev,d)=>{if(!ev.active)sim.alphaTarget(0);d.fx=null;d.fy=null;}));
// ---- panel
const panel=document.getElementById('panel');
let selected=null;
function esc(s){const d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}
function openPanel(d){
  selected=d.id;
  nd.select('circle').style('stroke',n=>n.id===d.id?'var(--accent)':'var(--nodeline)')
    .attr('stroke-width',n=>n.id===d.id?3:1);
  // spotlight the selected star's links; push the rest back
  lk.classed('hot',l=>l.source.id===d.id||l.target.id===d.id)
    .attr('opacity',l=>(l.source.id===d.id||l.target.id===d.id)?.95:.15)
    .attr('stroke-width',l=>(l.source.id===d.id||l.target.id===d.id)
      ?Math.min(5,1.8+l.w*.55):Math.min(4.5,1.3+l.w*.55));
  document.getElementById('ptitle').textContent=d.label;
  document.getElementById('pmeta').textContent=
    `문서 ${d.docs}편`+(d.up?` · 갱신 ${d.up}`:'');
  let h=`<a class="wikibtn" href="${d.url}">📚 위키 페이지 열기</a>`;
  if(d.rels&&d.rels.length){h+=`<div class="sec">🕸 지식그래프 관계</div>`;
    d.rels.forEach(r=>{h+=`<div class="rel"><b>${esc(r.s)}</b> <span class="rr">—${esc(r.r)}→</span> <b>${esc(r.d)}</b></div>`;});}
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
function clearSel(){panel.classList.remove('open');selected=null;
  nd.select('circle').style('stroke','var(--nodeline)').attr('stroke-width',1);
  lk.classed('hot',false)
    .attr('stroke-width',l=>Math.min(4.5,1.3+l.w*.55));
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
function applyFilter(){
  const v=(q.value||'').trim().toLowerCase();
  if(!v){nd.attr('opacity',1);lk.attr('opacity',.8);return;}
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
q.addEventListener('input',applyFilter);
q.addEventListener('keydown',ev=>{if(ev.key!=='Enter')return;
  const v=(q.value||'').trim().toLowerCase();if(!v)return;
  const hit=NODES.find(d=>(d.label+' '+d.id+' '+(d.al||'')).toLowerCase().includes(v));
  if(hit){focusNode(hit);openPanel(hit);}});
function focusNode(d){
  svg.transition().duration(600).call(zoom.transform,
    d3.zoomIdentity.translate(W/2-d.x*1.4,H/2-d.y*1.4).scale(1.4));}
// ---- chips: top 10 topics by docs
const kwbar=document.getElementById('kwbar');
NODES.slice().sort((a,b)=>b.docs-a.docs).slice(0,10).forEach(d=>{
  const b=document.createElement('button');b.className='kw';
  b.textContent=`${d.label} ${d.docs}`;
  b.onclick=()=>{focusNode(d);openPanel(d);};kwbar.appendChild(b);});
// ---- controls
function fit(){
  if(!NODES.length)return;
  const xs=NODES.map(d=>d.x),ys=NODES.map(d=>d.y);
  const x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys),y1=Math.max(...ys);
  const s=Math.min(2,.88/Math.max((x1-x0)/W,(y1-y0)/H,.01));
  svg.transition().duration(500).call(zoom.transform,
    d3.zoomIdentity.translate(W/2-s*(x0+x1)/2,H/2-s*(y0+y1)/2).scale(s));}
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
        "data": data, "reload_js": _widgets.live_reload_js("universe"),
        "theme_js": _THEME_JS,
    }


def _pre_signature() -> str | None:
    """File-stat + row-count fingerprint of the universe's inputs — no
    heavy reads. None → couldn't compute (fail open, do the full build)."""
    import sqlite3
    try:
        idx_path = config.DATA_DIR / "wiki_index.json"
        parts = [str(int(idx_path.stat().st_mtime)) if idx_path.exists()
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
    pre = _pre_signature()
    if pre is not None and pre == _LAST_PRE and out_file.exists():
        return 0
    payload = _build_payload()
    if payload is None:
        return 0
    _LAST_PRE = pre
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
    return 1
