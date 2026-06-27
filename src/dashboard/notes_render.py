"""Static dashboard pages for the study-notes (체화) subsystem.

Mirrors `wiki_render.render_wiki` — `regenerate()` calls `render_notes`
each tick (in the bot process, which has notes.db access) and the
dashboard container just serves the resulting static files.

Layout written under data/dashboard/<token>/notes/:
  index.html        — 오늘 복습 큐 + 전체 노트 목록 + stats
  note-<id>.html    — 노트 본문 (Markdown→HTML, KaTeX 수식) +
                      복습 질문(클릭하여 답 펼침)

Grading (SRS update) is NOT done here — the static server can't write
notes.db safely. Self-assessment goes through the Telegram /review
inline buttons (src/notes/telegram.py), reusing the bot's callback
infra. The dashboard is the read/recall surface; Telegram is the grade
surface.

Markdown + math are rendered client-side via marked + KaTeX (jsDelivr
CDN) so note tables/headings/formulas render faithfully without a
server-side markdown dependency.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)


def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def _plain(md: str) -> str:
    """Markdown → searchable plain text for the index full-text haystack
    (drops fenced code/mermaid + markdown symbols, collapses space)."""
    s = md or ""
    s = re.sub(r"```.*?```", " ", s, flags=re.S)
    s = re.sub(r"[#>*`|]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:4000]


# 유형별 buckets: documents (pdf/ppt/word/excel/pasted text) all collapse to
# 문서; youtube and web stand alone — mirrors the three buttons the user asked
# for (PDF · 유튜브 · 웹).
def _type_bucket(source_type: str) -> str:
    st = (source_type or "").lower()
    if st == "youtube":
        return "유튜브"
    if st == "web":
        return "웹"
    if st == "text":
        return "텍스트"
    if st == "blog":
        return "블로그"
    return "문서"


_THEME_JS = """
(function(){
  function apply(){
    var h = parseInt(new Date().toLocaleString('en-US',
      {timeZone:'Asia/Seoul',hour:'numeric',hour12:false}),10);
    document.documentElement.dataset.theme = (h>=19||h<7)?'dark':'light';
  }
  apply(); setInterval(apply,60000);
})();
"""

_CSS = """
:root{--bg:#f6f8fa;--panel:#fff;--panel-alt:#fafbfc;--border:#e5e7eb;
--border-soft:#f0f2f5;--text:#1f2937;--muted:#6b7280;--accent:#3b82f6;
--primary:#10b981;--due:#f59e0b;--important:#f59e0b;--memo:#10b981;
--shadow:0 1px 3px rgba(0,0,0,.06);}
[data-theme=dark]{--bg:#0f172a;--panel:#1e293b;--panel-alt:#172033;
--border:#334155;--border-soft:#1e2738;--text:#f1f5f9;--muted:#cbd5e1;
--accent:#60a5fa;--primary:#10b981;--due:#fbbf24;--important:#fbbf24;--memo:#34d399;
--shadow:0 1px 3px rgba(0,0,0,.4);}
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",
"Apple SD Gothic Neo","Noto Sans KR",sans-serif;background:var(--bg);
color:var(--text)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.layout{max-width:900px;margin:0 auto;padding:28px 22px 80px}
main{max-width:820px;margin:0 auto;padding:28px 22px 80px}
header h1{font-size:22px;margin:0 0 4px;display:flex;align-items:center;gap:8px}
header .sub{color:var(--muted);font-size:13px}
.nav{display:inline-flex;align-items:center;gap:3px;padding:2px 10px;
border-radius:12px;background:var(--accent);color:#fff!important;font-weight:600;
font-size:13px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
gap:12px;margin:18px 0}
.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;
padding:16px 18px;box-shadow:var(--shadow)}
.card .label{font-size:11px;color:var(--muted);margin-bottom:8px;font-weight:500}
.card .value{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums}
.sec-title{font-size:13px;color:var(--muted);margin:24px 4px 10px;font-weight:600}
.fbar{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:10px 0 2px}
.fbar .flabel{font-size:11px;color:var(--muted);font-weight:700;
margin-right:4px;min-width:38px}
.fbtn{background:var(--panel);border:1px solid var(--border);color:var(--text);
font-size:12.5px;padding:5px 12px;border-radius:16px;cursor:pointer;
transition:.12s;font-weight:500}
.fbtn:hover{border-color:var(--primary)}
.fbtn.active{background:var(--primary);border-color:var(--primary);color:#fff}
.controls{display:flex;gap:10px;margin:6px 0 4px}
.controls input{flex:1;background:var(--panel);border:1px solid var(--border);
color:var(--text);padding:10px 14px;border-radius:8px;font-size:14px;outline:none;
transition:.15s}
.controls input:focus{border-color:var(--primary);
box-shadow:0 0 0 3px rgba(16,185,129,.15)}
.controls .reset{background:var(--primary);border:0;color:#fff;padding:8px 18px;
border-radius:8px;cursor:pointer;font-size:13px;font-weight:600}
.controls .reset:hover{opacity:.9}
.note-row{background:var(--panel);border:1px solid var(--border);
border-radius:10px;padding:13px 16px;margin-bottom:8px;box-shadow:var(--shadow);
display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.note-row .t{font-weight:600;flex:1;word-break:break-word}
.note-row .meta{font-size:11px;color:var(--muted);white-space:nowrap}
.note-row .snippet{flex-basis:100%;font-size:12.5px;line-height:1.55;
color:var(--muted);margin-top:4px;display:-webkit-box;-webkit-line-clamp:2;
-webkit-box-orient:vertical;overflow:hidden;word-break:break-word}
.note-row .memo-preview{flex-basis:100%;font-size:13px;line-height:1.55;
color:var(--text);margin-top:6px;padding:8px 10px;white-space:pre-wrap;
word-break:break-word;background:rgba(16,185,129,.08);
border:1px solid rgba(16,185,129,.35);border-radius:8px}
mark.kw{background:#fef08a;color:inherit;padding:0 2px;border-radius:2px}
[data-theme=dark] mark.kw{background:#fbbf24;color:#0f172a}
.ndel{cursor:pointer;background:rgba(148,163,184,.18);
border:1px solid rgba(148,163,184,.32);color:var(--muted);font-size:15px;
line-height:1;padding:5px 9px;border-radius:8px;transition:.12s}
.ndel:hover{background:rgba(239,68,68,.18);border-color:rgba(239,68,68,.55);
color:#ef4444;transform:translateY(-1px)}
[data-theme=dark] .ndel{background:rgba(71,85,105,.45);
border-color:rgba(100,116,139,.55);color:#cbd5e1}
.note-row.removing{opacity:0;transform:scale(.97);transition:.2s}
.nstar{cursor:pointer;background:transparent;border:0;color:var(--muted);
font-size:17px;line-height:1;padding:2px 4px;border-radius:6px;transition:.12s}
.nstar:hover{color:#f59e0b;transform:scale(1.15)}
.nstar.on{color:#f59e0b}
.note-memo{margin:16px 0;padding:12px 14px;background:var(--panel);
border:1px solid var(--border);border-radius:10px}
.note-memo .memo-h{font-size:12px;color:var(--muted);font-weight:600;margin-bottom:6px}
.note-memo textarea,.qa-memo textarea{width:100%;min-height:64px;background:var(--bg,#0f172a);
border:1px solid var(--border);color:var(--text);border-radius:8px;padding:9px;
font-size:13.5px;outline:none;resize:vertical;box-sizing:border-box}
.note-memo textarea:focus{border-color:var(--primary)}
.memo-row{display:flex;align-items:center;gap:8px;margin-top:7px}
.memo-save{background:var(--primary);border:0;color:#fff;padding:6px 16px;
border-radius:7px;cursor:pointer;font-size:12.5px;font-weight:600}
.memo-del{background:rgba(148,163,184,.25);border:0;color:var(--muted);padding:6px 14px;
border-radius:7px;cursor:pointer;font-size:12.5px;font-weight:600}
.memo-status{font-size:11px;color:var(--muted)}
.alarm-row{display:flex;align-items:center;gap:6px;margin-top:7px;flex-wrap:wrap}
.alarm-time{background:var(--bg,#0f172a);border:1px solid var(--border);color:var(--text);
border-radius:6px;padding:4px 6px;font-size:12px}
.alarm-set,.alarm-clear{border:0;border-radius:6px;cursor:pointer;font-size:11.5px;
padding:5px 10px;font-weight:600}
.alarm-set{background:#6366f1;color:#fff}
.alarm-clear{background:rgba(148,163,184,.25);color:var(--muted)}
.alarm-status{font-size:11px;color:#818cf8}
.note-row[data-important="1"]{border-color:rgba(245,158,11,.55);
background:rgba(245,158,11,.06)}
[data-theme=dark] .note-row[data-important="1"]{background:rgba(245,158,11,.10)}
.controls .impfilter{background:var(--panel);border:1px solid var(--border);
color:var(--muted)}
.controls .impfilter.active{background:#f59e0b;border-color:#f59e0b;color:#fff}
.controls .memofilter{background:var(--panel);border:1px solid var(--border);
color:var(--muted)}
.controls .memofilter.active{background:#10b981;border-color:#10b981;color:#fff}
.stype{font-size:10px;color:var(--muted);border:1px solid var(--border);
border-radius:8px;padding:1px 6px}
.cat{font-size:10px;font-weight:700;border-radius:8px;padding:1px 7px;
white-space:nowrap;cursor:pointer;user-select:none}
.cat:hover{filter:brightness(1.08);outline:1px solid currentColor}
.cat-주식{background:rgba(16,185,129,.15);color:#059669;
border:1px solid rgba(16,185,129,.4)}
.cat-공부{background:rgba(59,130,246,.15);color:#2563eb;
border:1px solid rgba(59,130,246,.4)}
.cat-그외{background:rgba(148,163,184,.15);color:var(--muted);
border:1px solid var(--border)}
[data-theme=dark] .cat-주식{color:#34d399}
[data-theme=dark] .cat-공부{color:#60a5fa}
.empty{text-align:center;padding:60px 20px;color:var(--muted)}
.back{color:var(--muted);font-size:13px;display:inline-block;margin-bottom:16px}
.note-body{background:var(--panel);border:1px solid var(--border);
border-radius:12px;padding:24px 28px;box-shadow:var(--shadow);line-height:1.75}
.note-body h1{font-size:22px}.note-body h2{font-size:18px;margin-top:24px;
border-bottom:1px solid var(--border-soft);padding-bottom:6px}
.note-body h3{font-size:15px;margin-top:18px}
.note-body table{border-collapse:collapse;margin:12px 0;width:100%}
.note-body th,.note-body td{border:1px solid var(--border);padding:6px 10px;
font-size:13px;text-align:left}
.note-body th{background:var(--panel-alt)}
.note-body code{background:var(--panel-alt);border-radius:4px;padding:1px 5px;
font-size:12px}
.note-body blockquote{border-left:3px solid var(--border);margin:8px 0;
padding-left:14px;color:var(--muted)}
.note-body .mermaid{text-align:center;margin:18px 0;background:var(--panel-alt);
border:1px solid var(--border-soft);border-radius:8px;padding:12px;cursor:zoom-in}
.note-body .mermaid svg{max-width:100%;height:auto}
.mmd-overlay{position:fixed;inset:0;z-index:1000;display:none;
background:rgba(0,0,0,.82);cursor:zoom-out;padding:2vh 2vw;
align-items:center;justify-content:center;overflow:auto}
.mmd-overlay.open{display:flex}
.mmd-overlay .mmd-stage{background:#fff;border-radius:10px;padding:18px;
max-width:96vw;max-height:94vh;overflow:auto;cursor:grab;
box-shadow:0 8px 40px rgba(0,0,0,.5)}
.mmd-overlay .mmd-stage svg{width:auto !important;height:85vh !important;
max-width:none !important;max-height:none !important;display:block}
.mmd-hint{position:fixed;top:14px;right:18px;z-index:1001;color:#fff;
font-size:12px;opacity:.85;font-family:-apple-system,BlinkMacSystemFont,sans-serif}
.q-sec{margin-top:24px}
.q-card{background:var(--panel);border:1px solid var(--border);
border-left:3px solid var(--primary);border-radius:10px;padding:14px 16px;
margin-bottom:10px;box-shadow:var(--shadow)}
.q-card .q{font-weight:600;cursor:pointer;display:flex;gap:8px;align-items:flex-start}
.q-card .q .qtype{font-size:9px;font-weight:700;padding:2px 7px;border-radius:10px;
background:var(--panel-alt);color:var(--muted);border:1px solid var(--border);
white-space:nowrap}
.q-card .a{margin-top:10px;padding-top:10px;border-top:1px dashed var(--border);
color:var(--text);display:none}
.q-card.open .a{display:block}
.q-card .reveal{font-size:11px;color:var(--accent);margin-top:6px}
.q-card.open .reveal{display:none}
.footer{margin-top:32px;padding:16px 0;text-align:center;font-size:11px;
color:var(--muted);border-top:1px solid var(--border-soft)}
"""

# marked (markdown) + KaTeX (math) from jsDelivr. Renders the note body
# + auto-renders $...$ / $$...$$. Recall question answers stay hidden
# until clicked (active recall).
_NOTE_JS = r"""
(function(){
  // Shared fullscreen lightbox: concept maps render small inline, so a
  // click blows the diagram up to a readable size (height-driven so wide
  // maps get big + horizontal scroll), and the wheel zooms further.
  // Click the dark backdrop (or Esc) to close.
  var _ov = null;
  function ensureOverlay(){
    if(_ov) return _ov;
    _ov = document.createElement('div');
    _ov.className = 'mmd-overlay';
    _ov.innerHTML = '<div class="mmd-hint">휠=확대/축소 · 드래그/스크롤=이동 · 바깥클릭/Esc=닫기</div>'
      + '<div class="mmd-stage"></div>';
    document.body.appendChild(_ov);
    _ov.addEventListener('click', closeOverlay);
    var stage = _ov.querySelector('.mmd-stage');
    // Clicks/drag inside the diagram must NOT close the lightbox.
    stage.addEventListener('click', function(e){ e.stopPropagation(); });
    // Drag-to-pan (in addition to scrollbars / trackpad scroll).
    var down=false, sx=0, sy=0, sl=0, st=0;
    stage.addEventListener('mousedown', function(e){
      down=true; sx=e.clientX; sy=e.clientY;
      sl=stage.scrollLeft; st=stage.scrollTop;
      stage.style.cursor='grabbing'; e.preventDefault();
    });
    window.addEventListener('mousemove', function(e){
      if(!down) return;
      stage.scrollLeft = sl-(e.clientX-sx);
      stage.scrollTop  = st-(e.clientY-sy);
    });
    window.addEventListener('mouseup', function(){
      down=false; stage.style.cursor='';
    });
    return _ov;
  }
  function openOverlay(svgHTML){
    var ov = ensureOverlay();
    var stage = ov.querySelector('.mmd-stage');
    stage.innerHTML = svgHTML;
    var svg = stage.querySelector('svg');
    var zoom = 1;
    if(svg){
      svg.style.width = 'auto';
      svg.style.maxWidth = 'none';
      svg.style.height = '85vh';
      // Wheel to zoom: scale the svg height; the stage (overflow:auto)
      // provides scroll/pan for whatever overflows.
      stage.onwheel = function(e){
        e.preventDefault();
        zoom *= (e.deltaY < 0 ? 1.15 : 0.87);
        zoom = Math.max(0.3, Math.min(zoom, 8));
        svg.style.height = (85 * zoom) + 'vh';
      };
    }
    ov.classList.add('open');
    document.body.style.overflow = 'hidden';
  }
  function closeOverlay(){
    if(!_ov) return;
    _ov.classList.remove('open');
    document.body.style.overflow = '';
  }
  document.addEventListener('keydown', function(e){
    if(e.key === 'Escape') closeOverlay();
  });

  function renderMD(){
    var el = document.getElementById('md');
    if(!el || !window.marked) return;
    // Disable GFM strikethrough. Korean number ranges use the tilde
    // (예: "15~17GWh"), and GFM treats ONE-or-two tildes as strikethrough,
    // so "6.5~8.0 ... 18~22" struck out everything between the tildes.
    // Notes never legitimately use strikethrough (synth forbids it), so
    // turn the del tokenizer off → tildes render as literal range text.
    if(window.marked.use && !window._delOff){
      try{ window.marked.use({tokenizer:{del:function(){return false;}}}); }
      catch(e){}
      window._delOff = true;
    }
    el.innerHTML = window.marked.parse(el.textContent);
    // ```mermaid code blocks → <div class="mermaid"> for diagram render
    el.querySelectorAll('pre code.language-mermaid').forEach(function(c){
      var d = document.createElement('div');
      d.className = 'mermaid';
      d.textContent = c.textContent;
      var pre = c.closest('pre');
      if(pre) pre.replaceWith(d);
    });
    if(window.mermaid){
      var dark = document.documentElement.dataset.theme === 'dark';
      window.mermaid.initialize({startOnLoad:false, securityLevel:'loose',
        theme: dark ? 'dark' : 'default'});
      // Render each diagram individually so a single bad one degrades to
      // its source text instead of mermaid's "Syntax error" bomb.
      el.querySelectorAll('.mermaid').forEach(function(node, i){
        var src = node.textContent;
        window.mermaid.render('mmd'+Date.now()+'_'+i, src).then(function(res){
          node.innerHTML = res.svg;
          node.title = '클릭하면 크게 보기';
          node.addEventListener('click', function(){ openOverlay(node.innerHTML); });
        }).catch(function(){
          var pre = document.createElement('pre');
          pre.style.whiteSpace = 'pre-wrap';
          pre.style.textAlign = 'left';
          pre.textContent = src;
          node.replaceWith(pre);
        });
      });
    }
    if(window.renderMathInElement){
      window.renderMathInElement(el,{delimiters:[
        {left:'$$',right:'$$',display:true},
        {left:'$',right:'$',display:false}]});
    }
  }
  if(window.marked){renderMD();}
  document.querySelectorAll('.q-card .q').forEach(function(q){
    q.addEventListener('click',function(){q.closest('.q-card').classList.toggle('open');});
  });
  // 📝 노트 메모 저장
  (function(){
    var box=document.querySelector('.note-memo'); if(!box)return;
    var ta=box.querySelector('textarea'), btn=box.querySelector('.memo-save');
    var del=box.querySelector('.memo-del');
    var st=box.querySelector('.memo-status'), id=box.dataset.id;
    var token=location.pathname.split('/').filter(Boolean)[0]||'';
    if(!ta||!btn||!id)return;
    function save(text,msg){
      btn.disabled=true; if(del)del.disabled=true; if(st)st.textContent='저장 중…';
      fetch('/'+token+'/memo',{method:'POST',
        headers:{'content-type':'application/json'},
        body:JSON.stringify({kind:'note',id:id,text:text})})
        .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
        .then(function(){ if(st)st.textContent=msg; })
        .catch(function(err){ if(st)st.textContent='실패: '+err.message; })
        .finally(function(){ btn.disabled=false; if(del)del.disabled=false; });
    }
    btn.addEventListener('click',function(){ save(ta.value,'저장됨 ✓'); });
    if(del)del.addEventListener('click',function(){ ta.value=''; save('','삭제됨'); });
  })();
})();
"""

_CDN = (
    "<link rel='stylesheet' "
    "href='https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css'>"
    "<script src='https://cdn.jsdelivr.net/npm/marked@12.0.0/marked.min.js'></script>"
    "<script src='https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js'></script>"
    "<script defer src='https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js'></script>"
    "<script defer src='https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/"
    "auto-render.min.js' onload='window.renderMathInElement&&"
    "document.getElementById(\"md\")&&renderMathInElement("
    "document.getElementById(\"md\"))'></script>"
)


_INDEX_JS = r"""
(function(){
  var token = location.pathname.split('/').filter(Boolean)[0] || '';
  // Live filter: typing narrows the note list by title (no bot query).
  var q = document.getElementById('q');
  var reset = document.getElementById('reset');
  function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
  function rx(s){return s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');}
  function snippet(text, query){
    if(!text||!query) return '';
    var i = text.toLowerCase().indexOf(query.toLowerCase());
    if(i<0) return '';
    var s=Math.max(0,i-80), e=Math.min(text.length,i+query.length+80);
    var slice=(s>0?'…':'')+text.slice(s,e)+(e<text.length?'…':'');
    return esc(slice).replace(new RegExp(rx(query),'gi'),
      function(m){return '<mark class="kw">'+m+'</mark>';});
  }
  // Combined filter: 유형별(data-tbucket) AND 종류별(data-cat) AND
  // full-text(title + body). Body hits get a highlighted snippet. No bot query.
  var curType='all', curCat='all', curImportant=false, curMemo=false;
  function applyFilter(){
    var t=(q?q.value:'').trim(), tl=t.toLowerCase(), shown=0;
    document.querySelectorAll('.note-row').forEach(function(row){
      var hay=row.dataset.text||'';
      var a=row.querySelector('.t'); var title=(a?a.textContent:'');
      var inTitle = !tl || title.toLowerCase().indexOf(tl)!==-1;
      var inBody  = !tl || hay.toLowerCase().indexOf(tl)!==-1;
      var okText = !tl || inTitle || inBody;
      var okType = curType==='all' || (row.dataset.tbucket||'')===curType;
      var okCat  = curCat==='all'  || (row.dataset.cat||'')===curCat;
      var okImp  = !curImportant || (row.dataset.important==='1');
      var okMemo = !curMemo || (row.dataset.hasmemo==='1');
      var ok = okText && okType && okCat && okImp && okMemo;
      row.style.display = ok ? '' : 'none';
      var old=row.querySelector('.snippet'); if(old) old.remove();
      var omp=row.querySelector('.memo-preview'); if(omp) omp.remove();
      if(ok && curMemo && row.dataset.memo){
        var mp=document.createElement('div'); mp.className='memo-preview';
        mp.textContent='📝 '+row.dataset.memo; row.appendChild(mp);
      }
      if(ok && tl && inBody){
        var h=snippet(hay,t);
        if(h){ var d=document.createElement('div'); d.className='snippet';
               d.innerHTML=h; row.appendChild(d); }
      }
      if(ok) shown++;
    });
    var c=document.getElementById('note-count'); if(c) c.textContent=shown;
  }
  function wireGroup(sel, attr, set){
    var btns=document.querySelectorAll(sel);
    btns.forEach(function(b){ b.addEventListener('click', function(){
      btns.forEach(function(x){ x.classList.remove('active'); });
      b.classList.add('active'); set(b.getAttribute(attr)); applyFilter();
    }); });
  }
  wireGroup('.ftype','data-type',function(v){ curType=v; });
  wireGroup('.fcat','data-cat',function(v){ curCat=v; });
  if(q) q.addEventListener('input', applyFilter);
  var impf = document.getElementById('impfilter');
  if(impf) impf.addEventListener('click', function(){
    curImportant = !curImportant;
    impf.classList.toggle('active', curImportant);
    applyFilter();
  });
  var memof = document.getElementById('memofilter');
  if(memof) memof.addEventListener('click', function(){
    curMemo = !curMemo;
    memof.classList.toggle('active', curMemo);
    applyFilter();
  });
  if(reset) reset.addEventListener('click', function(){
    q.value=''; curType='all'; curCat='all'; curImportant=false; curMemo=false;
    if(impf) impf.classList.remove('active');
    if(memof) memof.classList.remove('active');
    document.querySelectorAll('.fbtn').forEach(function(x){
      x.classList.toggle('active', x.getAttribute('data-type')==='all'
        || x.getAttribute('data-cat')==='all'); });
    applyFilter();
  });
  // ★ 중요 표시 토글 — optimistic update, POST persists server-side,
  // revert on failure (mirrors the category badge).
  document.querySelectorAll('.nstar').forEach(function(btn){
    btn.addEventListener('click', function(e){
      e.preventDefault(); e.stopPropagation();
      var row = btn.closest('.note-row'); var id = row && row.dataset.id;
      if(!id) return;
      var now = row.dataset.important !== '1';   // toggle target state
      row.dataset.important = now ? '1' : '0';
      btn.textContent = now ? '★' : '☆';
      btn.classList.toggle('on', now);
      applyFilter();
      fetch('/'+token+'/notes/'+encodeURIComponent(id)+'/important',
        {method:'POST', headers:{'content-type':'application/json'},
         body: JSON.stringify({important: now})})
        .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
        .catch(function(err){
          row.dataset.important = now ? '0' : '1';
          btn.textContent = now ? '☆' : '★';
          btn.classList.toggle('on', !now);
          applyFilter();
          alert('중요 표시 변경 실패: '+err.message);
        });
    });
  });
  document.querySelectorAll('.ndel').forEach(function(btn){
    btn.addEventListener('click', function(e){
      e.preventDefault(); e.stopPropagation();
      var row = btn.closest('.note-row');
      var id = row && row.dataset.id;
      if(!id) return;
      if(!confirm('이 노트를 삭제할까요?\n\n'+id)) return;
      fetch('/'+token+'/notes/'+encodeURIComponent(id), {method:'DELETE'})
        .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
        .then(function(){
          row.classList.add('removing');
          setTimeout(function(){ row.remove(); }, 220);
        })
        .catch(function(err){ alert('삭제 실패: '+err.message); });
    });
  });
  // 종류별 manual override: click the badge to cycle 주식→공부→그외.
  // Optimistic update; POST locks it server-side so auto-reclassify
  // won't overwrite. Revert on failure.
  var CATS = ['주식','공부','그외'];
  function setCat(row, badge, cat){
    row.dataset.cat = cat;
    badge.textContent = cat;
    badge.className = 'cat cat-'+cat;
    applyFilter();
  }
  document.querySelectorAll('.note-row .cat').forEach(function(badge){
    badge.addEventListener('click', function(e){
      e.preventDefault(); e.stopPropagation();
      var row = badge.closest('.note-row');
      var id = row && row.dataset.id; if(!id) return;
      var prev = row.dataset.cat || '그외';
      var next = CATS[(CATS.indexOf(prev)+1) % CATS.length];
      setCat(row, badge, next);
      fetch('/'+token+'/notes/'+encodeURIComponent(id)+'/category',
        {method:'POST', headers:{'content-type':'application/json'},
         body: JSON.stringify({cat: next})})
        .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
        .catch(function(err){ setCat(row, badge, prev); alert('변경 실패: '+err.message); });
    });
  });
})();
"""


def _head(title: str) -> str:
    return (
        "<!DOCTYPE html><html lang='ko'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_esc(title)}</title><style>{_CSS}</style>"
        f"<script>{_THEME_JS}</script>"
    )


def _render_index(token: str, notes: list[dict],
                  st: dict, cost: dict | None = None,
                  bodies: dict | None = None) -> str:
    cost = cost or {}
    bodies = bodies or {}
    try:
        from ..store import marks as _marks
        _nmemos = _marks.memos("note")
    except Exception:
        _nmemos = {}
    rows = []
    rows.append("<div class='sec-title'>전체 노트 "
                f"(<span id='note-count'>{len(notes)}</span>)</div>")
    if not notes:
        rows.append("<div class='empty'>아직 노트가 없어요. 학습 채널에 "
                    "자료를 올리면 여기에 노트로 쌓입니다.</div>")
    for n in notes:
        learned = (n.get("updated") or "")[:10]
        hay = _plain((bodies.get(n["id"]) or {}).get("md") or "")
        tbucket = _type_bucket(n.get("source_type") or "")
        cat = (n.get("category") or "").strip() or "그외"
        imp = 1 if n.get("important") else 0
        _memo_txt = (_nmemos.get(str(n["id"])) or "").strip()
        hasmemo = 1 if _memo_txt else 0
        rows.append(
            f"<div class='note-row' data-id=\"{_esc(n['id'])}\" "
            f"data-text=\"{_esc(hay)}\" data-tbucket=\"{_esc(tbucket)}\" "
            f"data-cat=\"{_esc(cat)}\" data-important=\"{imp}\" "
            f"data-hasmemo=\"{hasmemo}\" data-memo=\"{_esc(_memo_txt)}\">"
            f"<button class='nstar{' on' if imp else ''}' type='button' "
            f"title='중요 표시 토글'>{'★' if imp else '☆'}</button>"
            f"<a class='t' href='note-{_esc(n['id'])}.html'>{_esc(n['title'])}</a>"
            f"<span class='cat cat-{_esc(cat)}' title='클릭: 종류 변경 (주식↔공부↔그외)'>{_esc(cat)}</span>"
            f"<span class='stype'>{_esc(n.get('source_type') or '')}</span>"
            f"<span class='meta'>학습 {_esc(learned)}</span>"
            f"<button class='ndel' type='button' title='노트 삭제'>🗑</button>"
            f"</div>"
        )
    return "\n".join([
        _head("학습 노트"), "</head><body><div class='layout'>",
        "<header><h1>학습 노트</h1>",
        f"<div class='sub'><a class='nav' href='/{_esc(token)}/'>🧠 Archive</a> "
        f"<a class='nav' href='/{_esc(token)}/kg/'>🕸 KG</a> "
        f"<a class='nav' href='/{_esc(token)}/wiki/'>📚 Wiki</a> "
        f"<a class='nav' href='/{_esc(token)}/commands/'>📋 Commands</a></div></header>",
        "<div class='stats'>",
        f"<div class='card'><div class='label'>총 노트</div>"
        f"<div class='value'>{st.get('notes',0):,}개</div></div>",
        f"<div class='card'><div class='label'>💰 오늘 노트 비용</div>"
        f"<div class='value'>₩{cost.get('today_krw',0):,.1f}</div>"
        f"<div style='font-size:11px;color:var(--muted);margin-top:4px'>"
        f"{cost.get('today_count',0)}회 합성</div></div>",
        f"<div class='card'><div class='label'>📅 이번 달 노트 비용 "
        f"({cost.get('mtd_year','')}년 {cost.get('mtd_month','')}월)</div>"
        f"<div class='value'>₩{cost.get('mtd_krw',0):,.1f}</div>"
        f"<div style='font-size:11px;color:var(--muted);margin-top:4px'>"
        f"{cost.get('mtd_count',0)}회 합성</div></div>",
        "</div>",
        "<div class='fbar'><span class='flabel'>유형별</span>"
        "<button class='fbtn ftype active' data-type='all'>전체</button>"
        "<button class='fbtn ftype' data-type='문서'>📄 문서</button>"
        "<button class='fbtn ftype' data-type='텍스트'>📝 텍스트</button>"
        "<button class='fbtn ftype' data-type='블로그'>✍ 블로그</button>"
        "<button class='fbtn ftype' data-type='웹'>🌐 웹</button>"
        "<button class='fbtn ftype' data-type='유튜브'>▶ 유튜브</button></div>",
        "<div class='fbar'><span class='flabel'>종류별</span>"
        "<button class='fbtn fcat active' data-cat='all'>전체</button>"
        "<button class='fbtn fcat' data-cat='주식'>📈 주식</button>"
        "<button class='fbtn fcat' data-cat='공부'>📚 공부</button>"
        "<button class='fbtn fcat' data-cat='그외'>🗂 그 외</button></div>",
        "<div class='controls'><input id='q' type='text' "
        "placeholder='노트 제목·본문 검색...' autocomplete='off'>"
        "<button id='impfilter' type='button' class='reset impfilter'>"
        "★ 중요만</button>"
        "<button id='memofilter' type='button' class='reset memofilter'>"
        "📝 메모만</button>"
        "<button id='reset' type='button' class='reset'>초기화</button></div>",
        "\n".join(rows),
        "<div class='footer'>대시보드는 읽기 전용 · 🗑 = 노트 삭제</div>",
        f"<script>{_INDEX_JS}</script>",
        "</div></body></html>",
    ])


def _render_note(token: str, note: dict) -> str:
    qs = []
    for q in note.get("questions") or []:
        qs.append(
            "<div class='q-card'><div class='q'>"
            f"<span class='qtype'>{_esc(q.get('q_type') or 'recall')}</span>"
            f"<span>Q. {_esc(q.get('question'))}</span></div>"
            "<div class='reveal'>▼ 클릭하여 답 보기 (먼저 스스로 떠올려보기)</div>"
            f"<div class='a'>{_esc(q.get('answer'))}</div></div>"
        )
    q_html = ""
    if qs:
        q_html = ("<div class='q-sec'><div class='sec-title'>❓ 복습 질문 "
                  "(능동 회상)</div>" + "\n".join(qs) + "</div>")
    cost = float(note.get("cost_krw") or 0)
    gsec = float(note.get("gen_seconds") or 0)
    learned = (note.get("created") or "")[:10]
    meta = f"학습 {_esc(learned)} · 💰 ₩{cost:,.2f} · ⏱ {gsec:.0f}초"
    from . import widgets as _widgets
    try:
        from ..store import marks as _marks
        _nid = note.get("id") or ""
        _nmemo = _marks.memo("note", _nid)
        _nalarm = _marks.alarm("note", _nid)
    except Exception:
        _nmemo, _nalarm, _nid = "", {}, note.get("id") or ""
    memo_box = (
        f"<div class='note-memo' data-id=\"{_esc(note.get('id') or '')}\">"
        "<div class='memo-h'>📝 내 메모</div>"
        f"<textarea placeholder='이 노트에 대한 내 생각…'>{_esc(_nmemo)}</textarea>"
        "<div class='memo-row'><button type='button' class='memo-save'>저장</button>"
        "<button type='button' class='memo-del'>삭제</button>"
        "<span class='memo-status'></span></div>"
        + _widgets.alarm_row("note", _nid, _nalarm)
        + "</div>"
    )
    return "\n".join([
        _head(note.get("title") or "노트"), _CDN,
        f"<style>{_widgets.ALARM_CSS}</style>",
        "</head><body><main>",
        f"<a class='back' href='index.html'>← 노트 목록</a>",
        f"<div style='font-size:11px;color:var(--muted);margin-bottom:10px'>{meta}</div>",
        # marked parses the raw markdown held in #md (textContent).
        f"<div class='note-body' id='md'>{_esc(note.get('md') or '')}</div>",
        memo_box,
        q_html,
        "</main>",
        f"<script>{_NOTE_JS}</script>",
        f"<script>{_widgets.ALARM_JS}</script>",
        "</body></html>",
    ])


def render_notes(token: str) -> int:
    """Write the notes dashboard tree. Returns pages written. Never
    raises (mirrors render_wiki) so a render bug can't kill the tick."""
    if not token:
        return 0
    try:
        from ..notes import store
        from ..store import cost as cost_store
        notes = store.list_notes()
        st = store.stats()
        # Spend from cost.db (purpose=note_synth) — persistent, so deleting
        # a note never changes the cost, and a paid-but-failed synth counts.
        nc = cost_store.purpose_today_month("note_synth")
        cost = {
            "today_krw": nc["today_krw"],
            "today_count": nc["today_calls"],
            "mtd_krw": nc["month_krw"],
            "mtd_count": nc["month_calls"],
            "mtd_year": nc["year"],
            "mtd_month": nc["month"],
        }
    except Exception:
        log.exception("notes_render: store read failed")
        return 0
    if not notes:
        # Nothing to show yet — skip writing an empty tree.
        return 0

    base = Path(config.DATA_DIR) / "dashboard" / token / "notes"
    base.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()

    def _atomic(path: Path, content: str):
        tmp = path.with_name(f"{path.name}.{pid}.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)

    # Read every note once: bodies feed both the index full-text search
    # haystack and the per-note pages (one get_note per note, not two).
    full_map = {}
    for n in notes:
        f = store.get_note(n["id"])
        if f:
            full_map[n["id"]] = f

    written = 0
    try:
        _atomic(base / "index.html",
                _render_index(token, notes, st, cost, full_map))
        written += 1
        for n in notes:
            full = full_map.get(n["id"])
            if not full:
                continue
            _atomic(base / f"note-{n['id']}.html", _render_note(token, full))
            written += 1
    except Exception:
        log.exception("notes_render: write failed")
    return written
