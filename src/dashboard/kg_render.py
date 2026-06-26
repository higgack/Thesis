"""Dashboard view for the knowledge graph (kg.db) — Telegram /kg parity.

Static HTML under data/dashboard/<token>/kg/index.html: overview stats,
top entities (click to filter), and a client-side searchable list of all
relationship triples. Read-only; mirrors what `/kg` shows in Telegram.
Runs from regenerate() in the bot container (sqlite-only, lightweight).
"""
from __future__ import annotations

import html
import logging
import os
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


_CSS = """
:root{--bg:#f7f8fa;--panel:#fff;--panel-alt:#f0f2f5;--border:#e2e6ea;
--border-soft:#eef1f4;--text:#1f2937;--muted:#6b7280;--accent:#3b82f6;
--primary:#10b981;--shadow:0 1px 3px rgba(0,0,0,.06)}
[data-theme=dark]{--bg:#0f172a;--panel:#1e293b;--panel-alt:#172033;
--border:#334155;--border-soft:#1e2738;--text:#f1f5f9;--muted:#cbd5e1;
--accent:#60a5fa;--primary:#10b981;--shadow:0 1px 3px rgba(0,0,0,.4)}
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",
"Noto Sans KR",sans-serif;background:var(--bg);color:var(--text)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.layout{max-width:1000px;margin:0 auto;padding:28px 22px 80px}
header h1{font-size:22px;margin:0 0 4px}
header .sub{color:var(--muted);font-size:13px}
.nav{display:inline-flex;align-items:center;gap:3px;padding:2px 10px;
border-radius:12px;background:var(--accent);color:#fff!important;font-weight:600;
font-size:13px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
gap:12px;margin:18px 0}
.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;
padding:16px 18px;box-shadow:var(--shadow)}
.card .label{font-size:11px;color:var(--muted);margin-bottom:8px;font-weight:500}
.card .value{font-size:24px;font-weight:700}
.sec{font-size:13px;color:var(--muted);margin:24px 4px 10px;font-weight:600}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{background:var(--panel);border:1px solid var(--border);border-radius:14px;
padding:4px 11px;font-size:12.5px;cursor:pointer}
.chip:hover{border-color:var(--primary)}
.chip .deg{color:var(--muted);font-size:11px}
.controls{display:flex;gap:10px;margin:14px 0 4px}
.controls input{flex:1;background:var(--panel);border:1px solid var(--border);
color:var(--text);padding:10px 14px;border-radius:8px;font-size:14px;outline:none}
.controls input:focus{border-color:var(--primary)}
.controls .reset{background:var(--primary);border:0;color:#fff;padding:8px 18px;
border-radius:8px;cursor:pointer;font-size:13px;font-weight:600}
.edge{background:var(--panel);border:1px solid var(--border);border-radius:9px;
padding:9px 14px;margin-bottom:6px;display:flex;align-items:center;gap:8px;
flex-wrap:wrap}
.edge .s,.edge .o{font-weight:600}
.edge .r{color:var(--muted);font-style:italic;font-size:13px}
.edge .c{margin-left:auto;font-size:11px;color:var(--muted)}
.estar{cursor:pointer;background:transparent;border:0;color:var(--muted);
font-size:15px;line-height:1;padding:0 2px;transition:.12s}
.estar:hover{color:#f59e0b;transform:scale(1.15)}
.estar.on{color:#f59e0b}
.ememo{cursor:pointer;background:transparent;border:0;opacity:.45;
font-size:14px;line-height:1;padding:0 2px;transition:.12s}
.ememo:hover{opacity:1;transform:scale(1.15)}
.ememo.on{opacity:1}
.edge[data-important="1"]{border-color:rgba(245,158,11,.55);
background:rgba(245,158,11,.07)}
.edge-editor{margin:-2px 0 8px;padding:10px 12px;background:var(--panel);
border:1px solid var(--border);border-radius:10px}
.edge-editor .ent-memo{background:transparent;border:0;padding:0}
.edge .memo-preview{flex-basis:100%;margin-top:6px;padding:8px 10px;font-size:13px;
line-height:1.55;color:var(--text);white-space:pre-wrap;word-break:break-word;
background:rgba(16,185,129,.10);border:1px solid rgba(16,185,129,.4);border-radius:8px}
.ent-memos{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));
gap:10px}
.ent-memo{background:var(--panel);border:1px solid var(--border);
border-radius:10px;padding:10px 12px}
.ent-memo-h{font-weight:700;font-size:13px;margin-bottom:6px}
.ent-memo textarea{width:100%;min-height:60px;background:var(--bg);
border:1px solid var(--border);color:var(--text);border-radius:8px;
padding:8px;font-size:13px;outline:none;resize:vertical;box-sizing:border-box}
.ent-memo textarea:focus{border-color:var(--primary)}
.memo-row{display:flex;align-items:center;gap:8px;margin-top:6px}
.memo-save{background:var(--primary);border:0;color:#fff;padding:5px 14px;
border-radius:7px;cursor:pointer;font-size:12px;font-weight:600}
.memo-del{background:rgba(148,163,184,.25);border:0;color:var(--muted);padding:5px 12px;
border-radius:7px;cursor:pointer;font-size:12px;font-weight:600}
.memo-status{font-size:11px;color:var(--muted)}
.alarm-row{display:flex;align-items:center;gap:6px;margin-top:7px;flex-wrap:wrap}
.alarm-time{background:var(--bg);border:1px solid var(--border);color:var(--text);
border-radius:6px;padding:4px 6px;font-size:12px}
.alarm-set,.alarm-clear{border:0;border-radius:6px;cursor:pointer;font-size:11.5px;
padding:5px 10px;font-weight:600}
.alarm-set{background:#6366f1;color:#fff}
.alarm-clear{background:rgba(148,163,184,.25);color:var(--muted)}
.alarm-status{font-size:11px;color:#818cf8}
.controls .impfilter{background:var(--panel);border:1px solid var(--border);
color:var(--muted)}
.controls .impfilter.active{background:#f59e0b;border-color:#f59e0b;color:#fff}
.controls .memofilter{background:var(--panel);border:1px solid var(--border);
color:var(--muted)}
.controls .memofilter.active{background:#10b981;border-color:#10b981;color:#fff}
.arrow{color:var(--muted)}
mark.kw{background:#fef08a;color:inherit;border-radius:2px;padding:0 1px}
[data-theme=dark] mark.kw{background:#fbbf24;color:#0f172a}
.footer{margin-top:32px;padding:16px 0;text-align:center;font-size:11px;
color:var(--muted);border-top:1px solid var(--border-soft)}
"""

_THEME_JS = """(function(){function a(){var h=parseInt(new Date().toLocaleString(
'en-US',{timeZone:'Asia/Seoul',hour:'numeric',hour12:false}),10);
document.documentElement.dataset.theme=(h>=19||h<7)?'dark':'light';}
a();setInterval(a,60000);})();"""

_JS = r"""
(function(){
  var q=document.getElementById('q'), reset=document.getElementById('reset');
  var impf=document.getElementById('impfilter');
  var memof=document.getElementById('memofilter');
  var token=location.pathname.split('/').filter(Boolean)[0]||'';
  var curImp=false, curMemo=false;
  function apply(){
    var t=(q?q.value:'').trim(), tl=t.toLowerCase(), shown=0;
    document.querySelectorAll('.edge').forEach(function(e){
      var hay=(e.dataset.text||'').toLowerCase();
      var ok=(!tl||hay.indexOf(tl)>=0)&&(!curImp||e.dataset.important==='1')
        &&(!curMemo||e.dataset.hasmemo==='1');
      e.style.display=ok?'':'none'; if(ok)shown++;
      var omp=e.querySelector('.memo-preview'); if(omp)omp.remove();
      if(ok&&curMemo&&e.dataset.memo){
        var mp=document.createElement('div'); mp.className='memo-preview';
        mp.textContent='📝 '+e.dataset.memo; e.appendChild(mp);
      }
    });
    var c=document.getElementById('cnt'); if(c)c.textContent=shown;
  }
  if(q)q.addEventListener('input',apply);
  if(impf)impf.addEventListener('click',function(){
    curImp=!curImp; impf.classList.toggle('active',curImp); apply();});
  if(memof)memof.addEventListener('click',function(){
    curMemo=!curMemo; memof.classList.toggle('active',curMemo); apply();});
  if(reset)reset.addEventListener('click',function(){
    q.value=''; curImp=false; curMemo=false;
    if(impf)impf.classList.remove('active');
    if(memof)memof.classList.remove('active'); apply();});
  // Chip click = filter the relation list by that entity name.
  document.querySelectorAll('.chip').forEach(function(ch){
    ch.addEventListener('click',function(){
      if(q){q.value=ch.dataset.name||'';apply();
        q.scrollIntoView({block:'center'});}
    });
  });
  // 관계(엣지) ★ 토글 (kg_edge)
  document.querySelectorAll('.edge .estar').forEach(function(btn){
    btn.addEventListener('click',function(ev){
      ev.preventDefault(); ev.stopPropagation();
      var row=btn.closest('.edge'); if(!row)return;
      var id=row.dataset.edgeid; if(!id)return;
      var now=row.dataset.important!=='1';
      row.dataset.important=now?'1':'0';
      btn.textContent=now?'★':'☆'; btn.classList.toggle('on',now); apply();
      fetch('/'+token+'/mark',{method:'POST',
        headers:{'content-type':'application/json'},
        body:JSON.stringify({kind:'kg_edge',id:id,important:now})})
        .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
        .catch(function(err){
          row.dataset.important=now?'0':'1';
          btn.textContent=now?'☆':'★'; btn.classList.toggle('on',!now); apply();
          alert('중요 표시 변경 실패: '+err.message);
        });
    });
  });
  // 관계 메모 저장 (kg_edge) — 알람은 공용 ALARM_JS가 처리.
  function wireMemo(box){
    if(box.__wired)return; box.__wired=true;
    var ta=box.querySelector('textarea'), btn=box.querySelector('.memo-save');
    var del=box.querySelector('.memo-del');
    var st=box.querySelector('.memo-status'), id=box.dataset.id;
    if(!ta||!btn||!id)return;
    function save(text,msg){
      btn.disabled=true; if(del)del.disabled=true; if(st)st.textContent='저장 중…';
      fetch('/'+token+'/memo',{method:'POST',
        headers:{'content-type':'application/json'},
        body:JSON.stringify({kind:'kg_edge',id:id,text:text})})
        .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
        .then(function(){ if(st)st.textContent=msg;
          var ed=box.closest('.edge-editor'); var row=ed&&ed.__row;
          if(row){ row.dataset.hasmemo=text?'1':'0';
            var mb=row.querySelector('.ememo'); if(mb)mb.classList.toggle('on',!!text); } })
        .catch(function(err){ if(st)st.textContent='실패: '+err.message; })
        .finally(function(){ btn.disabled=false; if(del)del.disabled=false; });
    }
    btn.addEventListener('click',function(){ save(ta.value,'저장됨 ✓'); });
    if(del)del.addEventListener('click',function(){ ta.value=''; save('','삭제됨'); });
  }
  document.querySelectorAll('.ent-memo').forEach(wireMemo);
  // 📝 버튼: 관계 행 바로 아래에 메모·알람 입력칸을 펼친다(필요할 때만 생성).
  var tpl=document.getElementById('eetpl');
  document.querySelectorAll('.edge .ememo').forEach(function(btn){
    btn.addEventListener('click',function(ev){
      ev.preventDefault(); ev.stopPropagation();
      var row=btn.closest('.edge'); if(!row||!tpl)return;
      var eid=row.dataset.edgeid; if(!eid)return;
      var ed=row.nextSibling;
      if(ed&&ed.classList&&ed.classList.contains('edge-editor')){
        ed.style.display=ed.style.display==='none'?'':'none'; return;
      }
      ed=document.createElement('div'); ed.className='edge-editor'; ed.__row=row;
      ed.innerHTML=tpl.innerHTML.replace(/__EID__/g,eid);
      var ta=ed.querySelector('textarea'); if(ta)ta.value=row.dataset.memo||'';
      row.parentNode.insertBefore(ed,row.nextSibling);
      var mbox=ed.querySelector('.ent-memo'); if(mbox)wireMemo(mbox);
      var arow=ed.querySelector('.alarm-row');
      if(arow&&window.wireAlarmRow)window.wireAlarmRow(arow);
      if(ta)ta.focus();
    });
  });
})();
"""


def render_kg(token: str) -> int:
    """Write data/dashboard/<token>/kg/index.html. Returns 1 if written,
    0 when the graph is empty / on error. Never raises."""
    if not token:
        return 0
    try:
        from ..store import kg
        st = kg.stats()
        if not st.get("edges"):
            return 0
        tops = kg.top_entities(30)
        edges = kg.all_edges(3000)
    except Exception:
        log.exception("kg_render: store read failed")
        return 0
    # KG-specific spend (purpose=kg_extract) — same persistent cost.db
    # source the notes/archive cards use, so deleting kg.db never changes it.
    try:
        from ..store import cost as _cost
        nc = _cost.purpose_today_month("kg_extract")
    except Exception:
        nc = {"today_krw": 0, "today_calls": 0, "mtd_krw": 0,
              "month_krw": 0, "month_calls": 0, "year": "", "month": ""}
    today_krw = nc.get("today_krw", 0)
    today_calls = nc.get("today_calls", 0)
    mtd_krw = nc.get("month_krw", nc.get("mtd_krw", 0))
    mtd_calls = nc.get("month_calls", 0)
    mtd_y = nc.get("year", "")
    mtd_m = nc.get("month", "")

    # 중요 표시(★)·메모·알람은 관계(엣지) 기준 — 특정 사실(예: 실적발표일)에
    # 직접 ★/메모/알람을 다는 게 실용적이라(사용자 요청). item_id = 엣지 id.
    from . import widgets as _widgets
    try:
        from ..store import marks as _marks
        _edge_imp = _marks.marked("kg_edge")
        _edge_memos = _marks.memos("kg_edge")
        _edge_alarms = _marks.alarm_map("kg_edge")
    except Exception:
        _edge_imp, _edge_memos, _edge_alarms = set(), {}, {}

    chips = "".join(
        f"<span class='chip' data-name=\"{_esc(t['name'])}\">"
        f"{_esc(t['name'])} <span class='deg'>{t['deg']}</span></span>"
        for t in tops)

    _edge_text = {}
    rows = []
    for e in edges:
        hay = f"{e['src']} {e['rel']} {e['dst']}"
        c = e.get("confidence") or 0
        eid = str(e.get("id") or "")
        imp = 1 if eid in _edge_imp else 0
        _memo_txt = (_edge_memos.get(eid) or "").strip()
        hasmemo = 1 if _memo_txt else 0
        _edge_text[eid] = f"{e['src']} —{e['rel']}→ {e['dst']}"
        rows.append(
            f"<div class='edge' data-edgeid=\"{_esc(eid)}\" "
            f"data-text=\"{_esc(hay)}\" data-important=\"{imp}\" "
            f"data-hasmemo=\"{hasmemo}\" data-memo=\"{_esc(_memo_txt)}\">"
            f"<button type='button' class='estar{' on' if imp else ''}' "
            f"title='중요 표시 토글'>{'★' if imp else '☆'}</button>"
            f"<button type='button' class='ememo{' on' if hasmemo else ''}' "
            f"title='메모·알람'>📝</button>"
            f"<span class='s'>{_esc(e['src'])}</span>"
            f"<span class='arrow'>—</span>"
            f"<span class='r'>{_esc(e['rel'])}</span>"
            f"<span class='arrow'>→</span>"
            f"<span class='o'>{_esc(e['dst'])}</span>"
            f"<span class='c'>{c:.2f}</span></div>")

    # 📝 중요 관계 메모·알람 — ★ 했거나 메모/알람이 있는 관계별 박스.
    memo_ids = sorted(set(_edge_imp) | set(_edge_memos.keys())
                      | set(_edge_alarms.keys()))
    memo_boxes = []
    for eid in memo_ids:
        label = _edge_text.get(eid, eid)
        txt = _edge_memos.get(eid, "")
        memo_boxes.append(
            f"<div class='ent-memo' data-id=\"{_esc(eid)}\">"
            f"<div class='ent-memo-h'>🔗 {_esc(label)}</div>"
            f"<textarea placeholder='이 관계에 대한 내 생각…'>{_esc(txt)}</textarea>"
            f"<div class='memo-row'><button type='button' class='memo-save'>"
            f"저장</button><button type='button' class='memo-del'>삭제</button>"
            f"<span class='memo-status'></span></div>"
            + _widgets.alarm_row("kg_edge", eid, _edge_alarms.get(eid, {}))
            + "</div>")
    memo_section = ""
    if memo_boxes:
        memo_section = (
            "<div class='sec'>📝 중요 관계 메모 · 알람</div>"
            f"<div class='ent-memos'>{''.join(memo_boxes)}</div>")

    page = "\n".join([
        "<!DOCTYPE html><html lang='ko'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>🕸 지식그래프</title>",
        f"<style>{_CSS}{_widgets.ALARM_CSS}</style>"
        f"<script>{_THEME_JS}</script></head><body>",
        "<div class='layout'>",
        "<header><h1>🕸 지식그래프</h1>",
        f"<div class='sub'><a class='nav' href='/{_esc(token)}/'>🧠 Archive</a> "
        f"<a class='nav' href='/{_esc(token)}/wiki/'>📚 Wiki</a> "
        f"<a class='nav' href='/{_esc(token)}/notes/'>학습 노트</a> "
        f"<a class='nav' href='/{_esc(token)}/commands/'>📋 Commands</a></div>"
        "</header>",
        "<div class='stats'>",
        f"<div class='card'><div class='label'>🕸 관계(엣지)</div>"
        f"<div class='value'>{st['edges']:,}</div></div>",
        f"<div class='card'><div class='label'>📍 개체</div>"
        f"<div class='value'>{st['entities']:,}</div></div>",
        f"<div class='card'><div class='label'>📄 문서</div>"
        f"<div class='value'>{st['docs']:,}</div></div>",
        f"<div class='card'><div class='label'>💰 오늘 KG 비용</div>"
        f"<div class='value'>₩{today_krw:,.1f}</div>"
        f"<div style='font-size:11px;color:var(--muted);margin-top:4px'>"
        f"{today_calls}콜 추출</div></div>",
        f"<div class='card'><div class='label'>📅 이번 달 KG 비용"
        f"{f' ({mtd_y}년 {mtd_m}월)' if mtd_y else ''}</div>"
        f"<div class='value'>₩{mtd_krw:,.1f}</div>"
        f"<div style='font-size:11px;color:var(--muted);margin-top:4px'>"
        f"{mtd_calls}콜 추출</div></div>",
        "</div>",
        "<div class='sec'>주요 개체 (연결수) — 클릭하면 관계 필터</div>",
        f"<div class='chips'>{chips}</div>",
        memo_section,
        "<div class='controls'><input id='q' type='text' "
        "placeholder='개체·관계 검색...' autocomplete='off'>"
        "<button id='impfilter' type='button' class='reset impfilter'>"
        "★ 중요만</button>"
        "<button id='memofilter' type='button' class='reset memofilter'>"
        "📝 메모만</button>"
        "<button id='reset' type='button' class='reset'>초기화</button></div>",
        f"<div class='sec'>관계 (<span id='cnt'>{len(edges)}</span>) — "
        "☆ 중요 표시 · 📝 눌러 메모·알람 작성</div>",
        "\n".join(rows),
        "<template id='eetpl'>"
        "<div class='ent-memo' data-id=\"__EID__\">"
        "<textarea placeholder='이 관계에 대한 내 생각…'></textarea>"
        "<div class='memo-row'><button type='button' class='memo-save'>저장</button>"
        "<button type='button' class='memo-del'>삭제</button>"
        "<span class='memo-status'></span></div>"
        + _widgets.alarm_row("kg_edge", "__EID__", {})
        + "</div></template>",
        "<div class='footer'>읽기 전용 · 텔레그램 /kg 와 동일 데이터 · "
        "백그라운드 자동 축적</div>",
        f"<script>{_JS}</script>",
        f"<script>{_widgets.ALARM_JS}</script>",
        "</div></body></html>",
    ])

    base = Path(config.DATA_DIR) / "dashboard" / token / "kg"
    base.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    tmp = base / f"index.html.{pid}.tmp"
    tmp.write_text(page, encoding="utf-8")
    os.replace(tmp, base / "index.html")
    return 1
