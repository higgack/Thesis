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
