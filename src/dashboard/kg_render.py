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

# Initial static-page row cap; '더 보기' fetches the rest via /kg/more
# in this same increment (browse-everything without one giant DOM dump).
_INITIAL_EDGE_LIMIT = 1200

# Cheap change-fingerprint of everything this page renders, so an
# unchanged graph costs a few ms instead of ~950ms. Measured on a
# production-sized kg.db (648k edges): the entity count alone
# (count of UNION src,dst) is 420ms and top_entities(30) is 495ms,
# against ~2.6ms for this whole fingerprint — roughly 300x cheaper
# than the work it guards. Mirrors universe_render's _pre_signature().
_LAST_SIG: str | None = None


def _pre_signature() -> str | None:
    """Fingerprint of this view's inputs — no heavy reads. None → could
    not compute, in which case the caller must fail OPEN and render.

    Covers edge inserts/deletes (COUNT+MAX(id)), ★/memo/alarm edits
    (marks.db mtime — the dashboard's mark/memo POST handlers write
    there without triggering a re-render), and the KST date so the
    'today' cost card resets at midnight. A KG spend that produces no
    edge (a failed extraction) is not covered and lags until the next
    edge lands; that is deliberate — keying on cost.db mtime would
    re-render on every unrelated LLM call in the system.
    """
    import sqlite3
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    try:
        parts: list[str] = [
            _dt.now(_tz(_td(hours=9))).strftime("%Y-%m-%d")
        ]
        kg_path = config.DATA_DIR / "kg.db"
        if not kg_path.exists():
            return None
        # Read-only URI: never take kg.db's write lock from the render
        # path (the bot writes it during ingest).
        c = sqlite3.connect(f"file:{kg_path}?mode=ro", uri=True, timeout=5)
        try:
            # Two statements on purpose. Asking for COUNT(*) and MAX(id)
            # together costs 33ms because the planner drops MAX's O(1)
            # rowid shortcut and scans a covering index for both
            # (EXPLAIN: "SCAN edges USING COVERING INDEX idx_kg_doc").
            # Split, MAX(id) becomes "SEARCH edges" at 0.2ms and the
            # pair totals 2.6ms — same answer, 12x cheaper.
            n_edges = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            max_id = c.execute(
                "SELECT COALESCE(MAX(id),0) FROM edges").fetchone()[0]
        finally:
            c.close()
        parts.append(f"{n_edges}:{max_id}")
        # kg.db mtime too, because COUNT/MAX(id) miss an in-place rewrite:
        # merge_duplicate_entities() folds entity variants by UPDATE-ing
        # src/dst on existing rows, so neither number moves and the page
        # would keep showing the pre-merge entity names (엔비디아 next to
        # NVIDIA) until some unrelated ingest changed the count.
        for p in (kg_path, kg_path.with_name("kg.db-wal")):
            try:
                parts.append(str(p.stat().st_mtime_ns))
            except OSError:
                parts.append("0")
        marks_path = config.DATA_DIR / "marks.db"
        for p in (marks_path, marks_path.with_name("marks.db-wal")):
            try:
                # st_mtime_ns, NOT int(st_mtime): whole-second resolution
                # loses a mark saved in the same second as the previous
                # render, and the change would then never be picked up.
                parts.append(str(p.stat().st_mtime_ns))
            except OSError:
                parts.append("0")
        return "-".join(parts)
    except Exception:
        return None


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


_CSS = """
/* Linear-style (DESIGN.md): thin borders, indigo #5e6ad2.
   No web-font @import (렌더 차단 제거) — Inter 로컬 있으면 사용, 없으면 system. */
:root{--bg:#f7f8f9;--panel:#fff;--panel-alt:#f0f1f3;--border:#e8e8ea;
--border-input:#e0e1e4;--border-soft:#eef0f2;--text:#282a30;--heading:#16171a;
--muted:#8a8f98;--accent:#5e6ad2;--accent-hover:#515dc4;
--primary:#5e6ad2;--important:#f5a623;--memo:#2faf6a;--danger:#e5484d;
--shadow:0 1px 2px rgba(0,0,0,.03)}
[data-theme=dark]{--bg:#0b0c0e;--panel:#141518;--panel-alt:#1c1d21;
--border:#26272b;--border-input:#2a2c31;--border-soft:#1f2024;--text:#e2e3e6;
--heading:#f7f8f8;--muted:#8a8f98;--accent:#7c84e8;--accent-hover:#9aa2f0;
--primary:#5e6ad2;--important:#f5a623;--memo:#3fbf7a;--danger:#f2555a;
--shadow:none}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 'Inter',-apple-system,BlinkMacSystemFont,"Segoe UI",
"Noto Sans KR",sans-serif;-webkit-font-smoothing:antialiased;
background:var(--bg);color:var(--text);transition:background-color .3s,color .3s}
h1,h2,h3{color:var(--heading);letter-spacing:-0.014em}
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
flex-wrap:wrap;
/* 1200 edge cards render at once — skip offscreen layout/paint (weak-PC
   fix); search/filter JS is unaffected. */
content-visibility:auto;contain-intrinsic-size:auto 48px}
.edge .s,.edge .o{font-weight:600}
.edge .r{color:var(--muted);font-style:italic;font-size:13px}
.edge .c{margin-left:auto;font-size:11px;color:var(--muted)}
.edge .edate{font-size:11px;color:var(--muted);opacity:.85}
.edge .esrc{flex-basis:100%;font-size:11.5px;color:var(--muted);
text-decoration:none;margin-top:2px;display:block;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
a.esrc:hover{color:var(--primary);text-decoration:underline}
.estar{cursor:pointer;background:transparent;border:0;color:var(--muted);
font-size:15px;line-height:1;padding:0 2px;transition:.12s}
.estar:hover{color:var(--important);transform:scale(1.15)}
.estar.on{color:var(--important)}
.ememo{cursor:pointer;background:transparent;border:0;opacity:.45;
font-size:14px;line-height:1;padding:0 2px;transition:.12s}
.ememo:hover{opacity:1;transform:scale(1.15)}
.ememo.on{opacity:1}
.edel{cursor:pointer;background:transparent;border:0;opacity:.8;
font-size:15px;line-height:1;padding:0 4px;margin-left:2px;transition:.12s}
.edel:hover{opacity:1;transform:scale(1.2);filter:drop-shadow(0 0 3px var(--danger))}
.edge.removing{opacity:0;transform:translateX(10px);transition:opacity .2s,transform .2s}
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
.controls .impfilter.active{background:var(--important);border-color:var(--important);color:#fff}
.controls .memofilter{background:var(--panel);border:1px solid var(--border);
color:var(--muted)}
.controls .memofilter.active{background:var(--memo);border-color:var(--memo);color:#fff}
.controls.filters{flex-wrap:wrap;align-items:center;margin:0 0 4px}
.controls .flabel{font-size:12px;color:var(--muted);font-weight:600}
.controls .fsel{background:var(--panel);border:1px solid var(--border);
color:var(--text);padding:7px 10px;border-radius:8px;font-size:13px;
cursor:pointer;outline:none}
.controls .fsel:hover:not(:disabled){border-color:var(--primary)}
.controls .fsel:disabled{opacity:.45;cursor:default}
.controls .fsel.on{border-color:var(--primary);color:var(--primary);
font-weight:600}
.controls .fhint{font-size:11.5px;color:var(--muted);margin-left:auto}
.controls .sortbtn{background:var(--panel);border:1px solid var(--border);
color:var(--muted);white-space:nowrap}
.controls .sortbtn:hover{border-color:var(--primary)}
.controls .sortbtn.active{background:var(--primary);border-color:var(--primary);color:#fff}
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
  var sortbtn=document.getElementById('sortbtn');
  var morebtn=document.getElementById('morebtn');
  var moreOrigText=morebtn?morebtn.textContent:'';
  var moreOrigOffset=morebtn?morebtn.dataset.offset:'0';
  var listEl=document.getElementById('kg-list');
  var origList=listEl?listEl.innerHTML:'';
  var tpl=document.getElementById('eetpl');
  var token=location.pathname.split('/').filter(Boolean)[0]||'';
  var curImp=false, curMemo=false, curSort='date';
  var fy=document.getElementById('fy'), fm=document.getElementById('fm');
  var fd=document.getElementById('fd'), fc=document.getElementById('fc');
  var fhint=document.getElementById('fhint');
  function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
  // data-ts is a KST ISO stamp ("2026-08-20T13:45:12+09:00"); older rows
  // may carry just "2026-08-20". Fixed-offset slicing handles both.
  function tsY(e){return (e.dataset.ts||'').slice(0,4);}
  function tsM(e){return (e.dataset.ts||'').slice(5,7);}
  function tsD(e){return (e.dataset.ts||'').slice(8,10);}
  function setOpts(sel, vals, allLabel, keep){
    if(!sel)return;
    var prev=keep?sel.value:'';
    sel.innerHTML='<option value="">'+allLabel+'</option>'
      +vals.map(function(v){return '<option value="'+v+'">'+v+'</option>';}).join('');
    sel.value=(prev&&vals.indexOf(prev)>=0)?prev:'';
    sel.disabled=vals.length===0;
    sel.classList.toggle('on', !!sel.value);
  }
  function uniqSorted(arr){
    var seen={}, out=[];
    arr.forEach(function(v){if(v&&!seen[v]){seen[v]=1;out.push(v);}});
    return out.sort().reverse();          // newest first, like the list
  }
  // Repopulate 연/월/일 from the rows currently in the DOM. Months are
  // limited to the chosen year and days to the chosen month, so a
  // combination that has no relations is never offered.
  function rebuildDateOpts(){
    var rows=Array.prototype.slice.call(document.querySelectorAll('.edge'));
    setOpts(fy, uniqSorted(rows.map(tsY)), '연도 전체', true);
    var y=fy?fy.value:'';
    var mRows=y?rows.filter(function(e){return tsY(e)===y;}):rows;
    setOpts(fm, y?uniqSorted(mRows.map(tsM)):[], '월 전체', true);
    var m=fm?fm.value:'';
    var dRows=m?mRows.filter(function(e){return tsM(e)===m;}):mRows;
    setOpts(fd, (y&&m)?uniqSorted(dRows.map(tsD)):[], '일 전체', true);
  }
  function apply(){
    var t=(q?q.value:'').trim(), tl=t.toLowerCase(), shown=0;
    var y=fy?fy.value:'', m=fm?fm.value:'', d=fd?fd.value:'';
    var minc=fc&&fc.value?parseFloat(fc.value):null;
    [fy,fm,fd,fc].forEach(function(s){if(s)s.classList.toggle('on',!!s.value);});
    document.querySelectorAll('.edge').forEach(function(e){
      var hay=(e.dataset.text||'').toLowerCase();
      var ok=(!tl||hay.indexOf(tl)>=0)&&(!curImp||e.dataset.important==='1')
        &&(!curMemo||e.dataset.hasmemo==='1')
        &&(!y||tsY(e)===y)&&(!m||tsM(e)===m)&&(!d||tsD(e)===d)
        &&(minc===null||(parseFloat(e.dataset.conf)||0)>=minc-1e-9);
      e.style.display=ok?'':'none'; if(ok)shown++;
      var omp=e.querySelector('.memo-preview'); if(omp)omp.remove();
      if(ok&&curMemo&&e.dataset.memo){
        var mp=document.createElement('div'); mp.className='memo-preview';
        mp.textContent='📝 '+e.dataset.memo; e.appendChild(mp);
      }
    });
    var c=document.getElementById('cnt'); if(c)c.textContent=shown;
    // Filtering only sees rows already in the DOM. While '더 보기' is
    // still offering more, say so — otherwise an empty result reads as
    // "no such data" when it really means "not loaded yet".
    if(fhint){
      var filtering=!!(y||m||d||minc!==null);
      var more=morebtn&&morebtn.style.display!=='none';
      fhint.textContent=(filtering&&more)
        ? '※ 현재 불러온 행 기준 — 전체를 보려면 아래 ‘더 보기’' : '';
    }
  }
  function onDateChange(level){
    if(level==='y'&&fm){fm.value='';}
    if((level==='y'||level==='m')&&fd){fd.value='';}
    rebuildDateOpts(); apply();
  }
  if(fy)fy.addEventListener('change',function(){onDateChange('y');});
  if(fm)fm.addEventListener('change',function(){onDateChange('m');});
  if(fd)fd.addEventListener('change',apply);
  if(fc)fc.addEventListener('change',apply);
  rebuildDateOpts();
  // 정렬: 'conf'=신뢰도순(동점이면 최신순) / 'date'=최신순(동점이면 신뢰도순).
  // 열려있는 인라인 편집기는 위치가 꼬이므로 정렬 전에 닫는다(미저장 입력 폐기).
  function sortList(mode){
    if(!listEl)return;
    var eds=listEl.querySelectorAll('.edge-editor');
    eds.forEach(function(x){x.remove();});
    var rows=Array.prototype.slice.call(listEl.querySelectorAll('.edge'));
    rows.sort(function(a,b){
      var ca=parseFloat(a.dataset.conf)||0, cb=parseFloat(b.dataset.conf)||0;
      var ta=a.dataset.ts||'', tb=b.dataset.ts||'';
      if(mode==='date'){
        if(ta!==tb)return ta<tb?1:-1;        // 최신 먼저
        return cb-ca;
      }
      if(ca!==cb)return cb-ca;                // 신뢰도 높은 것 먼저
      return ta<tb?1:(ta>tb?-1:0);           // 동점이면 최신 먼저
    });
    rows.forEach(function(r){listEl.appendChild(r);});
  }
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
  // ★ 토글 (이벤트 위임에서 호출). 3000행에 리스너를 다는 대신 listEl 1개로.
  function toggleStar(row, star){
    var id=row.dataset.edgeid; if(!id)return;
    var now=row.dataset.important!=='1';
    row.dataset.important=now?'1':'0';
    star.textContent=now?'★':'☆'; star.classList.toggle('on',now); apply();
    fetch('/'+token+'/mark',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({kind:'kg_edge',id:id,important:now})})
      .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
      .catch(function(err){
        row.dataset.important=now?'0':'1';
        star.textContent=now?'☆':'★'; star.classList.toggle('on',!now); apply();
        alert('중요 표시 변경 실패: '+err.message);
      });
  }
  // 📝 편집기 열기 (이벤트 위임에서 호출).
  function openEditor(row){
    if(!tpl)return;
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
    if(arow){
      var hh=row.dataset.ahhmm||'', dt=row.dataset.adate||'';
      if(hh&&!dt){var ti=arow.querySelector('.alarm-time'); if(ti)ti.value=hh;}
      if(hh&&dt&&dt.length===10){var di=arow.querySelector('.alarm-dt');
        if(di)di.value=dt.slice(5,7)+'.'+dt.slice(8,10)+'.'+hh;}
      var ast=arow.querySelector('.alarm-status');
      if(ast)ast.textContent=hh?(dt?(dt.slice(5,7)+'.'+dt.slice(8,10)+' '+hh+' KST부터')
        :('매일 '+hh+' KST')):'';
      if(window.wireAlarmRow)window.wireAlarmRow(arow);
    }
    function edEmpty(){
      var t=ed.querySelector('textarea');
      var at=ed.querySelector('.alarm-time'), adt=ed.querySelector('.alarm-dt');
      var hasMemo=t&&t.value.trim();
      var hasAlarm=(at&&at.value)||(adt&&adt.value)||(row.dataset.ahhmm||'');
      return !hasMemo && !hasAlarm;
    }
    ed.addEventListener('click',function(ev){
      var t=ev.target; if(!t||!t.classList)return;
      if(t.classList.contains('memo-del')||t.classList.contains('alarm-clear')){
        if(edEmpty()){ ev.stopImmediatePropagation(); ev.preventDefault(); ed.remove(); }
      }
    },true);
    if(ta)ta.focus();
  }
  // 🗑 관계 삭제 (이벤트 위임에서 호출). 확인 후 서버 삭제 → 행 제거.
  function deleteEdge(row){
    var id=row.dataset.edgeid; if(!id)return;
    if(!confirm('이 관계를 영구 삭제할까요?\n(재학습돼도 다시 생기지 않습니다)\n\n'+(row.dataset.text||'')))return;
    fetch('/'+token+'/kg/'+encodeURIComponent(id)+'/delete',{method:'POST'})
      .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
      .then(function(){
        var ed=row.nextSibling;
        if(ed&&ed.classList&&ed.classList.contains('edge-editor'))ed.remove();
        row.classList.add('removing');
        setTimeout(function(){
          row.remove();
          var c=document.getElementById('cnt');
          if(c){var n=parseInt(c.textContent,10); if(!isNaN(n)&&n>0)c.textContent=n-1;}
        },200);
      })
      .catch(function(err){alert('삭제 실패: '+err.message);});
  }
  // JSON 엣지 → 행 HTML (Python 렌더와 동일 구조; 동적 로드용).
  function renderEdge(e){
    var eid=esc(e.id), imp=e.important?1:0;
    var memo=e.memo||'', hm=memo.trim()?1:0;
    var src_html='';
    if(e.src_title){
      var ttl=e.src_title.length>50?e.src_title.slice(0,50)+'…':e.src_title;
      if((e.src_url||'').indexOf('http')===0)
        src_html="<a class='esrc' href=\""+esc(e.src_url)+"\" target='_blank' rel='noopener' title='출처 문서 원문 열기'>📰 "+esc(ttl)+"</a>";
      else src_html="<span class='esrc' title='출처 문서'>📰 "+esc(ttl)+"</span>";
    }
    var c=(typeof e.c==='number'?e.c:(parseFloat(e.c)||0)).toFixed(2);
    var ld=(e.ts||'').slice(0,10);
    var date_html=ld?"<span class='edate' title='학습된 날짜'>📅 "+esc(ld)+"</span>":'';
    return "<div class='edge' data-edgeid=\""+eid+"\" data-text=\""+esc(e.src+' '+e.rel+' '+e.dst)+"\""
      +" data-important=\""+imp+"\" data-conf=\""+c+"\" data-ts=\""+esc(e.ts||'')+"\""
      +" data-hasmemo=\""+hm+"\" data-memo=\""+esc(memo)+"\""
      +" data-ahhmm=\""+esc(e.ahhmm||'')+"\" data-adate=\""+esc(e.adate||'')+"\">"
      +"<button type='button' class='estar"+(imp?' on':'')+"' title='중요 표시 토글'>"+(imp?'★':'☆')+"</button>"
      +"<button type='button' class='ememo"+(hm?' on':'')+"' title='메모·알람'>📝</button>"
      +"<span class='s'>"+esc(e.src)+"</span><span class='arrow'>—</span>"
      +"<span class='r'>"+esc(e.rel)+"</span><span class='arrow'>→</span>"
      +"<span class='o'>"+esc(e.dst)+"</span><span class='c'>"+c+"</span>"+date_html
      +"<button type='button' class='edel' title='이 관계 영구 삭제 (재학습돼도 안 생김)'>🗑</button>"+src_html+"</div>";
  }
  // 칩 클릭 = 그 개체의 전체 관계를 서버에서 받아와 표시(상위 3000 한계 우회).
  function loadEntity(name){
    if(!name||!listEl)return;
    listEl.innerHTML="<div style='color:var(--muted);padding:14px'>불러오는 중…</div>";
    fetch('/'+token+'/kg/entity?e='+encodeURIComponent(name))
      .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
      .then(function(d){
        var edges=d.edges||[];
        listEl.innerHTML=edges.length?edges.map(renderEdge).join('')
          :"<div style='color:var(--muted);padding:14px'>관계 없음</div>";
        // 위임 리스너라 재-와이어링 불필요(listEl은 유지됨).
        if(curSort!=='conf')sortList(curSort);
        if(q)q.value=name; rebuildDateOpts(); apply();
        if(listEl.scrollIntoView)listEl.scrollIntoView({block:'start'});
      })
      .catch(function(err){
        listEl.innerHTML="<div style='color:var(--danger,#e5484d);padding:14px'>불러오기 실패: "+esc(err.message)+"</div>";});
  }
  if(q)q.addEventListener('input',apply);
  if(impf)impf.addEventListener('click',function(){
    curImp=!curImp; impf.classList.toggle('active',curImp); apply();});
  if(memof)memof.addEventListener('click',function(){
    curMemo=!curMemo; memof.classList.toggle('active',curMemo); apply();});
  if(sortbtn)sortbtn.addEventListener('click',function(){
    curSort=curSort==='conf'?'date':'conf';
    sortbtn.textContent=curSort==='conf'?'↕ 신뢰도순':'↕ 최신순';
    sortbtn.classList.toggle('active',curSort==='date');
    sortList(curSort); apply();});
  if(reset)reset.addEventListener('click',function(){
    if(listEl){ listEl.innerHTML=origList; }   // 위임이라 재-와이어링 불필요
    if(q)q.value=''; curImp=false; curMemo=false; curSort='date';
    if(impf)impf.classList.remove('active');
    if(memof)memof.classList.remove('active');
    if(sortbtn){sortbtn.textContent='↕ 최신순'; sortbtn.classList.add('active');}
    if(morebtn){ morebtn.style.display=''; morebtn.disabled=false;
      morebtn.textContent=moreOrigText; morebtn.dataset.offset=moreOrigOffset; }
    if(fy)fy.value=''; if(fm)fm.value=''; if(fd)fd.value=''; if(fc)fc.value='';
    rebuildDateOpts();
    apply();});
  // '더 보기': 정적 페이지의 초기 캡(_INITIAL_EDGE_LIMIT) 이후를 이어서
  // /kg/more로 가져와 renderEdge()로 붙인다(loadEntity와 같은 렌더 경로).
  // offset은 초기 페이지가 렌더된 순서('date', 서버 렌더 기준)에 대한 커서이므로
  // order는 항상 'date'로 고정 — curSort로 보내면 오프셋이 다른 정렬 순서를
  // 가리키게 돼 행이 누락되거나 중복된다. 화면상의 정렬은 sortList()로 별도 적용.
  if(morebtn)morebtn.addEventListener('click',function(){
    var off=parseInt(morebtn.dataset.offset,10)||0;
    morebtn.disabled=true; morebtn.textContent='불러오는 중…';
    fetch('/'+token+'/kg/more?offset='+off+'&limit=5000&order=date')
      .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
      .then(function(d){
        var edges=d.edges||[];
        if(edges.length&&listEl){
          listEl.insertAdjacentHTML('beforeend', edges.map(renderEdge).join(''));
          if(curSort!=='date')sortList(curSort);
        }
        morebtn.dataset.offset=d.next_offset;
        var loaded=Math.min(d.next_offset,d.total);
        if(d.has_more){
          morebtn.textContent='더 보기 ('+loaded.toLocaleString()+' / '+d.total.toLocaleString()+')';
          morebtn.disabled=false;
        } else {
          morebtn.textContent='전체 로드됨 ('+d.total.toLocaleString()+')';
          morebtn.style.display='none';
        }
        // Newly loaded rows can carry dates the selects never listed,
        // so refresh the 연/월/일 options before re-filtering.
        rebuildDateOpts();
        apply();  // 새로 붙은 행에도 현재 검색/필터 적용
      })
      .catch(function(err){
        morebtn.disabled=false; morebtn.textContent='불러오기 실패 — 다시 시도';
      });
  });
  document.querySelectorAll('.chip').forEach(function(ch){
    ch.addEventListener('click',function(){ loadEntity(ch.dataset.name||''); });
  });
  // 이벤트 위임: 3000행마다 리스너 다는 대신 listEl 1개에서 ★/📝 클릭 처리.
  if(listEl)listEl.addEventListener('click',function(ev){
    var t=ev.target; if(!t||!t.closest)return;
    var star=t.closest('.estar');
    if(star&&listEl.contains(star)){var r=star.closest('.edge'); if(r){
      ev.preventDefault(); ev.stopPropagation(); toggleStar(r,star);} return;}
    var mbtn=t.closest('.ememo');
    if(mbtn&&listEl.contains(mbtn)){var r2=mbtn.closest('.edge'); if(r2){
      ev.preventDefault(); ev.stopPropagation(); openEditor(r2);} return;}
    var dbtn=t.closest('.edel');
    if(dbtn&&listEl.contains(dbtn)){var r3=dbtn.closest('.edge'); if(r3){
      ev.preventDefault(); ev.stopPropagation(); deleteEdge(r3);} return;}
  });
})();
"""


def render_kg(token: str) -> int:
    """Write data/dashboard/<token>/kg/index.html. Returns 1 if written,
    0 when the graph is empty / unchanged / on error. Never raises.

    Self-gating (2026-08-20): this is the most expensive render in the
    dashboard (~950ms of SQL per call on a 648k-edge graph), and it had
    no change detection of its own — regenerate()'s outer gate was the
    only thing stopping it from running every 15s tick. That outer gate
    keyed on (total_qna, doc_count), which this page does not display,
    so it both let the page go stale and was the wrong place for the
    decision. The cheap fingerprint below is ~500x cheaper than the work
    it guards, so the caller can now invoke this every tick and get both
    freshness and a lower cost than the outer gate delivered."""
    global _LAST_SIG
    if not token:
        return 0
    out_file = Path(config.DATA_DIR) / "dashboard" / token / "kg" / "index.html"
    sig = _pre_signature()
    # Fail open on sig=None (couldn't fingerprint) so a signature bug can
    # never freeze the page permanently.
    if sig is not None and sig == _LAST_SIG and out_file.exists():
        return 0
    try:
        from ..store import kg
        st = kg.stats()
        if not st.get("edges"):
            return 0
        tops = kg.top_entities(30)
        # 초기엔 상위 _INITIAL_EDGE_LIMIT개만(최신순) 렌더 → DOM 경량화.
        # 나머지는 '더 보기' 버튼이 /kg/more로 이어서 가져온다(전체 스캔은
        # 칩 클릭(개체 전체) / 검색으로도 가능). ★·메모·알람 표시가 있어도
        # 순서·1페이지 포함 여부는 그대로 최신순/신뢰도순을 따른다 — 오래된
        # 중요 엣지를 앞으로 끌어올리지 않음(사용자 요청, 2026-07-24).
        edges = kg.all_edges(_INITIAL_EDGE_LIMIT, order="date")
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

    # 기본 정렬: 최신순(ts 내림차순), 동점이면 신뢰도 내림차순. reverse=True가
    # 두 키 모두 내림차순으로 적용 → ts desc, confidence desc.
    try:
        edges.sort(key=lambda e: (e.get("ts") or "", e.get("confidence") or 0),
                   reverse=True)
    except Exception:
        pass

    # 출처 문서: 각 트리플은 추출 원본 문서(doc_id)를 안다 → 근거 확인용으로
    # 관계 행에 "📄 제목"을 표시(URL이면 원문 링크). doc_id→문서 일괄 조회.
    try:
        from ..store import meta as _meta
        _doc_ids = list({e.get("doc_id") for e in edges if e.get("doc_id")})
        _docs = _meta.get_docs_batch(_doc_ids) if _doc_ids else {}
    except Exception:
        _docs = {}

    chips = "".join(
        f"<span class='chip' data-name=\"{_esc(t['name'])}\">"
        f"{_esc(t['name'])} <span class='deg'>{t['deg']}</span></span>"
        for t in tops)

    rows = []
    for e in edges:
        hay = f"{e['src']} {e['rel']} {e['dst']}"
        c = e.get("confidence") or 0
        _ldate = (e.get("ts") or "")[:10]  # 학습된 날짜(추출 시각)
        eid = str(e.get("id") or "")
        imp = 1 if eid in _edge_imp else 0
        _memo_txt = (_edge_memos.get(eid) or "").strip()
        hasmemo = 1 if _memo_txt else 0
        _al = _edge_alarms.get(eid, {})
        # 출처 문서(있으면): URL이면 원문 링크, 아니면 제목만.
        _doc = _docs.get(e.get("doc_id") or "")
        src_html = ""
        if _doc:
            _dtitle = (_doc.get("title") or "").strip() or "출처 문서"
            if len(_dtitle) > 50:
                _dtitle = _dtitle[:50] + "…"
            _dsrc = (_doc.get("source") or "").strip()
            if _dsrc.startswith("http"):
                src_html = (
                    f"<a class='esrc' href=\"{_esc(_dsrc)}\" target='_blank' "
                    f"rel='noopener' title='출처 문서 원문 열기'>📰 {_esc(_dtitle)}</a>")
            else:
                src_html = (
                    f"<span class='esrc' title='출처 문서'>📰 {_esc(_dtitle)}</span>")
        rows.append(
            f"<div class='edge' data-edgeid=\"{_esc(eid)}\" "
            f"data-text=\"{_esc(hay)}\" data-important=\"{imp}\" "
            f"data-conf=\"{c:.2f}\" data-ts=\"{_esc(e.get('ts') or '')}\" "
            f"data-hasmemo=\"{hasmemo}\" data-memo=\"{_esc(_memo_txt)}\" "
            f"data-ahhmm=\"{_esc(_al.get('hhmm','') or '')}\" "
            f"data-adate=\"{_esc(_al.get('date','') or '')}\">"
            f"<button type='button' class='estar{' on' if imp else ''}' "
            f"title='중요 표시 토글'>{'★' if imp else '☆'}</button>"
            f"<button type='button' class='ememo{' on' if hasmemo else ''}' "
            f"title='메모·알람'>📝</button>"
            f"<span class='s'>{_esc(e['src'])}</span>"
            f"<span class='arrow'>—</span>"
            f"<span class='r'>{_esc(e['rel'])}</span>"
            f"<span class='arrow'>→</span>"
            f"<span class='o'>{_esc(e['dst'])}</span>"
            f"<span class='c'>{c:.2f}</span>"
            + (f"<span class='edate' title='학습된 날짜'>📅 {_esc(_ldate)}</span>"
               if _ldate else "")
            + "<button type='button' class='edel' title='이 관계 영구 삭제 (재학습돼도 안 생김)'>🗑</button>"
            + f"{src_html}</div>")

    # 메모·알람 편집은 각 관계 행의 📝 버튼으로 그 자리에서(인라인) 한다.
    # 별도 그리드 섹션은 노트 화면처럼 '메모만' 필터 + 행 미리보기로 대체.
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
        f"<a class='nav' href='/{_esc(token)}/universe/'>🌌 Universe</a> "
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
        f"{mtd_calls}콜 추출 · 누적 ₩{nc.get('total_krw', 0):,.0f} "
        f"({nc.get('total_calls', 0):,}콜)</div></div>",
        "</div>",
        "<div class='sec'>주요 개체 (연결수) — 클릭하면 그 개체의 전체 관계</div>",
        f"<div class='chips'>{chips}</div>",
        "<div class='controls'><input id='q' type='text' "
        "placeholder='개체·관계 검색...' autocomplete='off'>"
        "<button id='impfilter' type='button' class='reset impfilter'>"
        "★ 중요만</button>"
        "<button id='memofilter' type='button' class='reset memofilter'>"
        "📝 메모만</button>"
        "<button id='sortbtn' type='button' class='reset sortbtn active' "
        "title='정렬 기준 전환'>↕ 최신순</button>"
        "<button id='reset' type='button' class='reset'>초기화</button></div>",
        # 2nd control row: date drill-down (연 → 월 → 일, each populated
        # from the dates actually present) + a minimum-confidence cut.
        # Options are derived from the loaded rows, so a month/day that
        # has no relations never appears as a dead choice.
        "<div class='controls filters'>"
        "<span class='flabel'>📅 날짜</span>"
        "<select id='fy' class='fsel'><option value=''>연도 전체</option></select>"
        "<select id='fm' class='fsel' disabled>"
        "<option value=''>월 전체</option></select>"
        "<select id='fd' class='fsel' disabled>"
        "<option value=''>일 전체</option></select>"
        "<span class='flabel' style='margin-left:10px'>🎯 신뢰도</span>"
        "<select id='fc' class='fsel'>"
        "<option value=''>전체</option>"
        "<option value='0.95'>0.95 이상</option>"
        "<option value='0.9'>0.90 이상</option>"
        "<option value='0.8'>0.80 이상</option>"
        "<option value='0.7'>0.70 이상</option>"
        "<option value='0.5'>0.50 이상</option>"
        "</select>"
        "<span id='fhint' class='fhint'></span>"
        "</div>",
        f"<div class='sec'>관계 (<span id='cnt'>{len(edges)}</span>) — "
        "☆ 중요 표시 · 📝 메모·알람 · 🗑 영구 삭제(재학습돼도 안 생김)</div>",
        f"<div id='kg-list'>{chr(10).join(rows)}</div>",
        "<div id='more-wrap' style='text-align:center;margin:14px 0'>"
        f"<button id='morebtn' type='button' class='reset' "
        f"data-offset='{_INITIAL_EDGE_LIMIT}' data-order='date'>"
        f"더 보기 ({min(_INITIAL_EDGE_LIMIT, st['edges']):,} / "
        f"{st['edges']:,})</button></div>",
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
        _widgets.live_reload_js("kg"),
        "</div></body></html>",
    ])

    base = Path(config.DATA_DIR) / "dashboard" / token / "kg"
    base.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    tmp = base / f"index.html.{pid}.tmp"
    tmp.write_text(page, encoding="utf-8")
    os.replace(tmp, base / "index.html")
    # Only after a successful write — a crash mid-render must not mark
    # this signature as rendered.
    _LAST_SIG = sig
    return 1
