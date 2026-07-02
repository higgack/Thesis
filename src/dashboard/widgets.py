"""Shared dashboard widgets — currently the alarm setter row, used by all
four item surfaces (notes / Q&A / wiki / KG).

Two ways to set an alarm (all KST):
  • 매일: a time (HH:MM) → fires daily until acked.
  • 특정일: a single typed datetime `MM.DD.HH:MM` (e.g. 06.26.04:30 =
    6월 26일 04:30) → starts firing on that date, daily until acked. The year
    is resolved client-side (this year, or next year if the date already
    passed). No calendar picker.

The wiring JS is generic: it reads kind + item_id off each .alarm-row's data
attributes, so one ALARM_JS block works on every page (no per-surface JS).
Values rendered into attributes are validated time/date formats; item_id is
HTML-escaped for the attribute. Over the wire the date is still YYYY-MM-DD
(server unchanged); only the input UI uses the MM.DD.HH:MM shorthand.
"""
from __future__ import annotations

import html as _html


def live_reload_js(page_key: str) -> str:
    """A tiny <script> that makes an open dashboard page surface new content
    WITHOUT a manual refresh and WITHOUT disrupting active work.

    It polls /<token>/version (a cheap per-section count) and reacts ONLY
    when this page's section count actually changed. Two-speed response
    (user request, 2026-07-02):
      • the "🔄 새 내용 — 보기" pill appears PROMPTLY on any change —
        click = instant manual refresh, never throttled;
      • the AUTOMATIC reload fires at most once per 30 minutes per page
        (sessionStorage stamp survives the reload), and still only when
        idle (no scroll/key/click ~15s, no editor open, tab visible) —
        so an open page refreshes itself on a calm cadence instead of
        yanking on every KG tick.
    Scroll position is preserved across the reload.

    page_key ∈ {"qna","notes","kg","wiki","universe"} — /version JSON keys.
    """
    k = repr(str(page_key))
    return (
        "<script>(function(){"
        "var token=location.pathname.split('/').filter(Boolean)[0]||'';"
        "var KEY=" + k + ",cur=null,SK='dash_scroll_'+KEY,"
        "AK='dash_auto_'+KEY,pending=false,"
        "last=Date.now(),IDLE=15000,AUTO=1800000;"
        # restore scroll if THIS script triggered the last reload
        "try{var s=sessionStorage.getItem(SK);"
        "if(s!==null){window.scrollTo(0,parseInt(s,10)||0);"
        "sessionStorage.removeItem(SK);}}catch(e){}"
        "['scroll','keydown','mousedown','touchstart','wheel'].forEach("
        "function(ev){window.addEventListener(ev,function(){last=Date.now();},"
        "{passive:true});});"
        # busy = actively reading/editing → don't yank the page
        "function busy(){if(document.hidden)return true;"
        "if(Date.now()-last<IDLE)return true;"
        "if(document.querySelector('.edge-editor'))return true;"
        "var a=document.activeElement;"
        "if(a&&(a.tagName==='TEXTAREA'||a.tagName==='INPUT'))return true;"
        "return false;}"
        "function go(){try{sessionStorage.setItem(SK,String("
        "window.scrollY||window.pageYOffset||0));"
        "sessionStorage.setItem(AK,String(Date.now()));}catch(e){}"
        "location.reload();}"
        # auto-reload throttle: at most once / 30min per page (manual pill
        # clicks also stamp — the content is fresh either way)
        "function canAuto(){try{var t=parseInt("
        "sessionStorage.getItem(AK)||'0',10);"
        "return (Date.now()-t)>=AUTO;}catch(e){return true;}}"
        "function pill(){if(document.getElementById('dash-newpill'))return;"
        "var b=document.createElement('div');b.id='dash-newpill';"
        "b.textContent='\\uD83D\\uDD04 \\uC0C8 \\uB0B4\\uC6A9 \\u2014 \\uBCF4\\uAE30';"
        "b.style.cssText='position:fixed;left:50%;bottom:24px;"
        "transform:translateX(-50%);z-index:100000;background:#5e6ad2;"
        "color:#fff;padding:9px 16px;border-radius:20px;cursor:pointer;"
        "font:600 13px/1.2 -apple-system,BlinkMacSystemFont,sans-serif;"
        "box-shadow:0 6px 20px rgba(0,0,0,.4)';"
        "b.addEventListener('click',go);document.body.appendChild(b);}"
        "function tick(){if(pending){if(!busy()&&canAuto())go();else pill();}}"
        "function chk(){fetch('/'+token+'/version',{cache:'no-store'})"
        ".then(function(r){return r.ok?r.json():null;})"
        ".then(function(d){if(!d||!(KEY in d))return;var v=d[KEY];"
        "if(cur===null){cur=v;return;}"
        "if(v!==cur){cur=v;pending=true;tick();}}).catch(function(){});}"
        "setInterval(chk,12000);setInterval(tick,3000);chk();"
        "})();</script>"
    )


def alarm_row(kind: str, item_id: str, cur: dict | None = None) -> str:
    """HTML for the alarm setter. `cur` = {'hhmm','date'} (current alarm)."""
    cur = cur or {}
    hhmm = (cur.get("hhmm") or "").strip()
    date = (cur.get("date") or "").strip()
    daily_v = hhmm if (hhmm and not date) else ""
    # specific-day input echoes MM.DD.HH:MM built from the stored YYYY-MM-DD.
    dt_v = ""
    if date and hhmm and len(date) == 10:
        dt_v = f"{date[5:7]}.{date[8:10]}.{hhmm}"
    if not hhmm:
        status = ""
    elif date:
        status = f"{date[5:7]}.{date[8:10]} {hhmm} KST부터"
    else:
        status = f"매일 {hhmm} KST"
    kid = _html.escape(str(item_id), quote=True)
    return (
        f"<div class='alarm-row' data-kind='{_html.escape(kind)}' data-id=\"{kid}\">"
        "<span class='alm-l'>⏰ 매일</span>"
        f"<input type='time' class='alarm-time' value='{daily_v}'>"
        "<button type='button' class='alarm-set'>설정</button>"
        "<button type='button' class='alarm-clear'>해제</button>"
        "<span class='alm-l'>· 📅 특정일</span>"
        f"<input type='text' class='alarm-dt' placeholder='06.26.04:30' maxlength='11' value='{dt_v}'>"
        "<button type='button' class='alarm-setdt'>설정</button>"
        "<button type='button' class='alarm-clear'>해제</button>"
        f"<span class='alarm-status'>{status}</span></div>"
    )


ALARM_JS = r"""
(function(){
  var token=location.pathname.split('/').filter(Boolean)[0]||'';
  var DTR=/^(\d{2})\.(\d{2})\.([01]\d|2[0-3]):([0-5]\d)$/;
  function parseDT(s){
    var m=DTR.exec((s||'').trim()); if(!m)return null;
    var mo=+m[1], da=+m[2]; if(mo<1||mo>12||da<1||da>31)return null;
    var now=new Date(), yr=now.getFullYear();
    var cand=new Date(yr, mo-1, da, +m[3], +m[4]);
    if(cand.getTime() < now.getTime()-60000) yr+=1;
    return {date:yr+'-'+m[1]+'-'+m[2], hhmm:m[3]+':'+m[4]};
  }
  function wire(row){
    var kind=row.dataset.kind, id=row.dataset.id;
    if(!kind||!id||row.__wired)return;
    row.__wired=true;
    var atime=row.querySelector('.alarm-time'), aset=row.querySelector('.alarm-set');
    var adt=row.querySelector('.alarm-dt'), asetd=row.querySelector('.alarm-setdt');
    var aclrs=row.querySelectorAll('.alarm-clear'), ast=row.querySelector('.alarm-status');
    function post(hhmm,date){
      fetch('/'+token+'/alarm',{method:'POST',
        headers:{'content-type':'application/json'},
        body:JSON.stringify({kind:kind,id:id,hhmm:hhmm,date:date})})
        .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
        .then(function(){ if(ast)ast.textContent=!hhmm?'해제됨':
          (date?(date.slice(5).replace('-','.')+' '+hhmm+' KST부터'):('매일 '+hhmm+' KST')); })
        .catch(function(e){ if(ast)ast.textContent='실패: '+e.message; });
    }
    if(aset)aset.addEventListener('click',function(e){e.preventDefault();e.stopPropagation();
      if(atime&&atime.value)post(atime.value,''); else if(ast)ast.textContent='시간을 넣어줘';});
    if(asetd)asetd.addEventListener('click',function(e){e.preventDefault();e.stopPropagation();
      var p=parseDT(adt&&adt.value);
      if(p)post(p.hhmm,p.date);
      else if(ast)ast.textContent='형식 확인 (예 06.26.04:30)';});
    aclrs.forEach(function(aclr){aclr.addEventListener('click',function(e){
      e.preventDefault();e.stopPropagation();
      if(atime)atime.value=''; if(adt)adt.value=''; post('','');});});
  }
  window.wireAlarmRow=wire;
  document.querySelectorAll('.alarm-row').forEach(wire);
})();
"""


ALARM_CSS = """
.alarm-row{display:flex;align-items:center;gap:6px;margin-top:8px;flex-wrap:wrap}
.alarm-row .alm-l{color:var(--muted);font-size:11px}
.alarm-row input{background:var(--panel);border:1px solid var(--border);
color:var(--text);border-radius:6px;padding:4px 6px;font-size:12px}
.alarm-row input.alarm-dt{width:108px}
.alarm-row button{border:0;border-radius:6px;cursor:pointer;font-size:11.5px;
padding:5px 10px;font-weight:600}
.alarm-set,.alarm-setdt{background:#6366f1;color:#fff}
.alarm-clear{background:rgba(148,163,184,.25);color:var(--muted)}
.alarm-status{font-size:11px;color:#818cf8}
"""
