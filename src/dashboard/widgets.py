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

    Checks /<token>/version quietly every 1min and reacts ONLY when this
    page's section signature actually changed (user-specified behaviour,
    2026-07-02):
      • BASELINE — the automatic refresh fires at most once per 30min
        per page, and only when idle (no scroll/key/click ~15s, nothing
        being typed, no editor open, tab visible). View state survives:
        scroll position AND filter state (search text, active toggle
        buttons, active data-tool chips) are saved → restored.
      • IN BETWEEN — a change shows the two-button prompt
        '🆕 새 내용 도착  [바로 반영] [그대로 보기]'. 바로 반영 =
        instant refresh (state preserved). 그대로 보기 = dismiss THIS
        version only — no re-prompt until a newer update arrives, but
        the 30min baseline still auto-applies it.

    page_key ∈ {"qna","notes","kg","wiki","universe"} — /version JSON keys.
    """
    k = repr(str(page_key))
    return (
        "<script>(function(){"
        "var token=location.pathname.split('/').filter(Boolean)[0]||'';"
        "var KEY=" + k + ",cur=null,dismissed=null,SK='dash_scroll_'+KEY,"
        "AK='dash_auto_'+KEY,FK='dash_filters_'+KEY,pending=false,"
        "last=Date.now(),IDLE=15000,AUTO=1800000;"
        # ── restore after OUR reload: filters FIRST (they hide/show items
        #    and change page height), then scroll to the saved spot.
        "try{var f=sessionStorage.getItem(FK);if(f){f=JSON.parse(f);"
        "sessionStorage.removeItem(FK);"
        "Object.keys(f.v||{}).forEach(function(id){"
        "var el=document.getElementById(id);if(el){el.value=f.v[id];"
        "el.dispatchEvent(new Event('input',{bubbles:true}));}});"
        "(f.on||[]).forEach(function(id){var el=document.getElementById(id);"
        "if(el&&!el.classList.contains('active')"
        "&&!el.classList.contains('on'))el.click();});"
        "(f.chips||[]).forEach(function(t){var el=document.querySelector("
        "'.chip[data-tool=\"'+t+'\"]');"
        "if(el&&!el.classList.contains('active'))el.click();});}}catch(e){}"
        "try{var s=sessionStorage.getItem(SK);"
        "if(s!==null){window.scrollTo(0,parseInt(s,10)||0);"
        "sessionStorage.removeItem(SK);}}catch(e){}"
        "['scroll','keydown','mousedown','touchstart','wheel'].forEach("
        "function(ev){window.addEventListener(ev,function(){last=Date.now();},"
        "{passive:true});});"
        # busy = actively reading/editing (incl. memo/alarm typing) → skip
        "function busy(){if(document.hidden)return true;"
        "if(Date.now()-last<IDLE)return true;"
        "if(document.querySelector('.edge-editor'))return true;"
        "var a=document.activeElement;"
        "if(a&&(a.tagName==='TEXTAREA'||a.tagName==='INPUT'))return true;"
        "return false;}"
        # save search text (#q/#search/#wiki-search), active toggle buttons
        # (by id, class active/on) and active category chips (data-tool) —
        # restored above after the reload, so the view comes back as-was.
        "function saveState(){try{var st={v:{},on:[],chips:[]};"
        "['q','search','wiki-search'].forEach(function(id){"
        "var el=document.getElementById(id);if(el&&el.value)st.v[id]=el.value;});"
        "document.querySelectorAll('.active[id],.on[id]').forEach("
        "function(el){if(el.tagName==='BUTTON')st.on.push(el.id);});"
        "document.querySelectorAll('.chip.active').forEach(function(el){"
        "if(el.dataset.tool)st.chips.push(el.dataset.tool);});"
        "sessionStorage.setItem(FK,JSON.stringify(st));}catch(e){}}"
        "function go(){try{saveState();sessionStorage.setItem(SK,String("
        "window.scrollY||window.pageYOffset||0));"
        "sessionStorage.setItem(AK,String(Date.now()));}catch(e){}"
        "location.reload();}"
        # baseline: the AUTOMATIC refresh fires at most once / 30min
        "function canAuto(){try{var t=parseInt("
        "sessionStorage.getItem(AK)||'0',10);"
        "return (Date.now()-t)>=AUTO;}catch(e){return true;}}"
        # two-button prompt; '그대로 보기' mutes THIS version only (a newer
        # update pops again; the 30min baseline still auto-applies)
        "function rmpill(){var p=document.getElementById('dash-newpill');"
        "if(p)p.remove();}"
        "function pill(){if(document.getElementById('dash-newpill'))return;"
        "var b=document.createElement('div');b.id='dash-newpill';"
        "b.style.cssText='position:fixed;left:50%;bottom:24px;"
        "transform:translateX(-50%);z-index:100000;background:#5e6ad2;"
        "color:#fff;padding:9px 14px;border-radius:20px;"
        "font:600 13px/1.2 -apple-system,BlinkMacSystemFont,sans-serif;"
        "box-shadow:0 6px 20px rgba(0,0,0,.4);display:flex;gap:8px;"
        "align-items:center';"
        "var t=document.createElement('span');"
        "t.textContent='\\uD83C\\uDD95 \\uC0C8 \\uB0B4\\uC6A9 \\uB3C4\\uCC29';"
        "var y=document.createElement('button');"
        "y.textContent='\\uBC14\\uB85C \\uBC18\\uC601';"
        "y.style.cssText='background:#fff;color:#5e6ad2;border:none;"
        "border-radius:12px;padding:5px 10px;font:700 12px/1 inherit;"
        "cursor:pointer';y.addEventListener('click',go);"
        "var n=document.createElement('button');"
        "n.textContent='\\uADF8\\uB300\\uB85C \\uBCF4\\uAE30';"
        "n.style.cssText='background:transparent;color:#dfe3ff;"
        "border:1px solid rgba(255,255,255,.5);border-radius:12px;"
        "padding:5px 10px;font:600 12px/1 inherit;cursor:pointer';"
        "n.addEventListener('click',function(){dismissed=cur;rmpill();});"
        "b.appendChild(t);b.appendChild(y);b.appendChild(n);"
        "document.body.appendChild(b);}"
        "function tick(){if(!pending)return;"
        "if(!busy()&&canAuto()){go();return;}"
        "if(dismissed!==cur)pill();}"
        "function chk(){fetch('/'+token+'/version',{cache:'no-store'})"
        ".then(function(r){return r.ok?r.json():null;})"
        ".then(function(d){if(!d||!(KEY in d))return;var v=d[KEY];"
        "if(cur===null){cur=v;return;}"
        "if(v!==cur){cur=v;pending=true;tick();}}).catch(function(){});}"
        "setInterval(chk,60000);setInterval(tick,3000);chk();"
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
