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
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)


def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


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
--primary:#10b981;--due:#f59e0b;--shadow:0 1px 3px rgba(0,0,0,.06);}
[data-theme=dark]{--bg:#0f172a;--panel:#1e293b;--panel-alt:#172033;
--border:#334155;--border-soft:#1e2738;--text:#f1f5f9;--muted:#cbd5e1;
--accent:#60a5fa;--primary:#10b981;--due:#fbbf24;
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
.note-row{background:var(--panel);border:1px solid var(--border);
border-radius:10px;padding:13px 16px;margin-bottom:8px;box-shadow:var(--shadow);
display:flex;align-items:center;gap:12px}
.note-row.due{border-left:3px solid var(--due)}
.note-row .t{font-weight:600;flex:1;word-break:break-word}
.note-row .meta{font-size:11px;color:var(--muted);white-space:nowrap}
.badge{display:inline-block;font-size:10px;font-weight:700;padding:2px 8px;
border-radius:10px;background:rgba(245,158,11,.15);color:#d97706}
[data-theme=dark] .badge{color:#fbbf24}
.stype{font-size:10px;color:var(--muted);border:1px solid var(--border);
border-radius:8px;padding:1px 6px}
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
.grade-hint{margin-top:20px;padding:14px 16px;background:var(--panel-alt);
border:1px solid var(--border);border-radius:10px;font-size:13px;color:var(--muted)}
.footer{margin-top:32px;padding:16px 0;text-align:center;font-size:11px;
color:var(--muted);border-top:1px solid var(--border-soft)}
"""

# marked (markdown) + KaTeX (math) from jsDelivr. Renders the note body
# + auto-renders $...$ / $$...$$. Recall question answers stay hidden
# until clicked (active recall).
_NOTE_JS = r"""
(function(){
  function renderMD(){
    var el = document.getElementById('md');
    if(!el || !window.marked) return;
    el.innerHTML = window.marked.parse(el.textContent);
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
})();
"""

_CDN = (
    "<link rel='stylesheet' "
    "href='https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css'>"
    "<script src='https://cdn.jsdelivr.net/npm/marked@12.0.0/marked.min.js'></script>"
    "<script defer src='https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js'></script>"
    "<script defer src='https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/"
    "auto-render.min.js' onload='window.renderMathInElement&&"
    "document.getElementById(\"md\")&&renderMathInElement("
    "document.getElementById(\"md\"))'></script>"
)


def _head(title: str) -> str:
    return (
        "<!DOCTYPE html><html lang='ko'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_esc(title)}</title><style>{_CSS}</style>"
        f"<script>{_THEME_JS}</script>"
    )


def _render_index(token: str, notes: list[dict], due: list[dict],
                  st: dict, cost: dict | None = None) -> str:
    cost = cost or {}
    rows = []
    if due:
        rows.append("<div class='sec-title'>🔁 오늘 복습할 노트 "
                    f"({len(due)})</div>")
        for n in due:
            rows.append(
                f"<div class='note-row due'><span class='badge'>복습</span>"
                f"<a class='t' href='note-{_esc(n['id'])}.html'>{_esc(n['title'])}</a>"
                f"<span class='stype'>{_esc(n.get('source_type') or '')}</span></div>"
            )
    rows.append(f"<div class='sec-title'>📒 전체 노트 ({len(notes)})</div>")
    if not notes:
        rows.append("<div class='empty'>아직 노트가 없어요. 학습 채널에 "
                    "자료를 올리면 여기에 노트로 쌓입니다.</div>")
    for n in notes:
        nd = n.get("next_due") or ""
        lr = n.get("last_reviewed") or "—"
        rows.append(
            f"<div class='note-row'>"
            f"<a class='t' href='note-{_esc(n['id'])}.html'>{_esc(n['title'])}</a>"
            f"<span class='stype'>{_esc(n.get('source_type') or '')}</span>"
            f"<span class='meta'>다음복습 {_esc(nd)} · 마지막 {_esc(lr[:10])}</span>"
            f"</div>"
        )
    return "\n".join([
        _head("📒 학습 노트"), "</head><body><div class='layout'>",
        "<header><h1>📒 학습 노트 <span style='font-size:13px;color:var(--muted)'>"
        "(체화)</span></h1>",
        f"<div class='sub'><a class='nav' href='/{_esc(token)}/'>🧠 Archive</a> "
        f"<a class='nav' href='/{_esc(token)}/wiki/'>📚 Wiki</a></div></header>",
        "<div class='stats'>",
        f"<div class='card'><div class='label'>📒 총 노트</div>"
        f"<div class='value'>{st.get('notes',0):,}개</div></div>",
        f"<div class='card'><div class='label'>🔁 오늘 복습</div>"
        f"<div class='value'>{st.get('due_today',0):,}개</div></div>",
        f"<div class='card'><div class='label'>✅ 누적 복습</div>"
        f"<div class='value'>{st.get('total_reviews',0):,}회</div></div>",
        f"<div class='card'><div class='label'>💰 오늘 비용</div>"
        f"<div class='value'>₩{cost.get('today_krw',0):,.0f}</div>"
        f"<div style='font-size:11px;color:var(--muted);margin-top:4px'>"
        f"{cost.get('today_calls',0)}콜</div></div>",
        f"<div class='card'><div class='label'>📅 이번 달 비용 "
        f"({cost.get('mtd_year','')}년 {cost.get('mtd_month','')}월)</div>"
        f"<div class='value'>₩{cost.get('mtd_krw',0):,.0f}</div>"
        f"<div style='font-size:11px;color:var(--muted);margin-top:4px'>"
        f"{cost.get('mtd_day','')}일차</div></div>",
        "</div>",
        "\n".join(rows),
        "<div class='footer'>채점은 텔레그램 <code>/review</code>에서 · "
        "대시보드는 읽기·복습 전용</div>",
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
    srs = note.get("srs") or {}
    cost = float(note.get("cost_krw") or 0)
    gsec = float(note.get("gen_seconds") or 0)
    meta = (f"다음복습 {_esc(srs.get('next_due') or '—')} · "
            f"복습 {_esc(srs.get('reps') or 0)}회 · "
            f"마지막 {_esc((srs.get('last_reviewed') or '—')[:10])} · "
            f"💰 ₩{cost:,.2f} · ⏱ {gsec:.0f}초")
    return "\n".join([
        _head(note.get("title") or "노트"), _CDN,
        "</head><body><main>",
        f"<a class='back' href='index.html'>← 노트 목록</a>",
        f"<div style='font-size:11px;color:var(--muted);margin-bottom:10px'>{meta}</div>",
        # marked parses the raw markdown held in #md (textContent).
        f"<div class='note-body' id='md'>{_esc(note.get('md') or '')}</div>",
        q_html,
        "<div class='grade-hint'>📲 복습 후 <b>텔레그램 /review</b>에서 "
        "[😵 까먹음 / 🤔 가물가물 / ✅ 기억함] 으로 자가평가하면 다음 복습일이 "
        "자동 조정돼요.</div>",
        "</main>",
        f"<script>{_NOTE_JS}</script>",
        "</body></html>",
    ])


def render_notes(token: str) -> int:
    """Write the notes dashboard tree. Returns pages written. Never
    raises (mirrors render_wiki) so a render bug can't kill the tick."""
    if not token:
        return 0
    try:
        from ..notes import store, recall
        from ..store import cost as cost_store
        notes = store.list_notes()
        due = store.due_notes()
        st = recall.stats()
        _today = cost_store.today_krw()
        _mtd = cost_store.month_to_date_krw()
        cost = {
            "today_krw": _today.get("total_krw", 0),
            "today_calls": _today.get("calls", 0),
            "mtd_krw": _mtd.get("total_krw", 0),
            "mtd_year": _mtd.get("year", ""),
            "mtd_month": _mtd.get("month", ""),
            "mtd_day": _mtd.get("day", ""),
        }
    except Exception:
        log.exception("notes_render: store read failed")
        return 0
    if not notes and not due:
        # Nothing to show yet — skip writing an empty tree.
        return 0

    base = Path(config.DATA_DIR) / "dashboard" / token / "notes"
    base.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()

    def _atomic(path: Path, content: str):
        tmp = path.with_name(f"{path.name}.{pid}.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)

    written = 0
    try:
        _atomic(base / "index.html",
                _render_index(token, notes, due, st, cost))
        written += 1
        for n in notes:
            full = store.get_note(n["id"])
            if not full:
                continue
            _atomic(base / f"note-{n['id']}.html", _render_note(token, full))
            written += 1
    except Exception:
        log.exception("notes_render: write failed")
    return written
