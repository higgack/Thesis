"""Static HTML dashboard generator.

Replaces the FastAPI/uvicorn approach because the user's network had
quirks reaching containerized dynamic servers but reaches simple
`python3 -m http.server` on adjacent ports just fine. Same pattern
the user's other working dashboards use.

Layout written under data/dashboard/:

  data/dashboard/
  ├── index.html               public root, just shows "private"
  └── <DASHBOARD_TOKEN>/
      ├── index.html           all Q&As grouped by date
      └── q-<id>.html          per-Q&A detail page

Token in the path acts as the only access gate — anyone hitting
the bare port lands on a blank "private" page; the URL with token
hands out the real archive. For a personal tool that's enough.

Regeneration cost is small (<200ms for ~1000 rows); call it after
every successful Q&A so the dashboard is always fresh.
"""
from __future__ import annotations

import html
import logging
import os
import threading
from pathlib import Path

from .. import config
from ..store import qna, cost, meta as meta_store, vector as vector_store

log = logging.getLogger(__name__)

_LOCK = threading.Lock()

_BASE_CSS = """
:root {
  --bg: #f6f8fa; --panel: #ffffff; --panel-alt: #fafbfc;
  --border: #e5e7eb; --border-soft: #f0f2f5;
  --text: #1f2937; --muted: #6b7280; --accent: #3b82f6;
  --primary: #10b981;
  --tool-brain: #ec4899; --tool-paper: #a855f7;
  --tool-web: #10b981; --tool-ingest: #f59e0b;
  --shadow: 0 1px 2px rgba(0,0,0,0.04), 0 1px 3px rgba(0,0,0,0.06);
}
[data-theme="dark"] {
  --bg: #0f1419; --panel: #1a2028; --panel-alt: #141a22;
  --border: #2a3441; --border-soft: #1f2731;
  --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff;
  --primary: #10b981;
  --tool-brain: #f472b6; --tool-paper: #c084fc;
  --tool-web: #34d399; --tool-ingest: #fbbf24;
  --shadow: 0 1px 2px rgba(0,0,0,0.5), 0 1px 3px rgba(0,0,0,0.4);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font: 14px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI",
        "Apple SD Gothic Neo", "Noto Sans KR", sans-serif;
  background: var(--bg); color: var(--text);
  transition: background-color 0.2s, color 0.2s;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
"""

# Inline at the top of <head> so the theme is applied before paint —
# avoids the white-flash on dark hours. Switches every minute so a tab
# left open across the 07:00 / 19:00 KST boundary auto-flips.
_THEME_SWITCHER_JS = """
(function(){
  function applyTheme(){
    var hourStr = new Date().toLocaleString('en-US', {
      timeZone: 'Asia/Seoul', hour: 'numeric', hour12: false
    });
    var h = parseInt(hourStr, 10);
    var dark = (h >= 19 || h < 7);
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  }
  applyTheme();
  setInterval(applyTheme, 60000);
})();
"""

_INDEX_CSS = _BASE_CSS + """
.layout { max-width: 1100px; margin: 0 auto; padding: 28px 22px 80px; }
header { margin-bottom: 22px; }
header h1 {
  font-size: 22px; margin: 0 0 4px 0; color: var(--text);
  display: flex; align-items: center; gap: 8px;
}
header .sub { color: var(--muted); font-size: 13px; }

.stats {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(170px,1fr));
  gap: 12px; margin-bottom: 22px;
}
.stat-card {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 12px; padding: 16px 18px;
  box-shadow: var(--shadow);
}
.stat-card .label {
  font-size: 11px; color: var(--muted);
  letter-spacing: 0.3px; margin-bottom: 8px;
  display: flex; align-items: center; gap: 6px;
  font-weight: 500;
}
.stat-card .value {
  font-size: 24px; font-weight: 700; color: var(--text);
  font-variant-numeric: tabular-nums;
}
.stat-card .sub {
  font-size: 11px; color: var(--muted); margin-top: 4px;
}

.controls {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 12px; padding: 14px 16px; margin-bottom: 18px;
  box-shadow: var(--shadow);
}
.controls .search-row {
  display: flex; gap: 10px; margin-bottom: 12px;
}
.controls input[type=text] {
  flex: 1; background: var(--panel-alt); border: 1px solid var(--border);
  color: var(--text); padding: 9px 13px; border-radius: 8px;
  font-size: 14px; outline: none; transition: 0.15s;
}
.controls input[type=text]:focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px rgba(16,185,129,0.15);
}
.controls button.reset {
  background: var(--primary); border: 0;
  color: #fff; padding: 8px 18px; border-radius: 8px;
  cursor: pointer; font-size: 13px; font-weight: 600;
}
.controls button.reset:hover { background: #059669; }
.chips { display: flex; gap: 8px; flex-wrap: wrap; }
.chip {
  cursor: pointer; user-select: none;
  padding: 6px 14px; border-radius: 16px;
  font-size: 12px; font-weight: 600;
  background: var(--panel); border: 1px solid var(--border);
  color: var(--muted); transition: 0.1s;
}
.chip:hover { color: var(--text); border-color: var(--muted); }
.chip.active { color: #fff; }
.chip.brain.active  { background: var(--tool-brain); border-color: var(--tool-brain); }
.chip.paper.active  { background: var(--tool-paper); border-color: var(--tool-paper); }
.chip.web.active    { background: var(--tool-web); border-color: var(--tool-web); }
.chip.ingest.active { background: var(--tool-ingest); border-color: var(--tool-ingest); }

.summary-line {
  font-size: 12px; color: var(--muted); margin: 16px 4px 8px;
}

.day-section {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 12px; margin-bottom: 12px; overflow: hidden;
  box-shadow: var(--shadow);
}
.day-section > summary {
  cursor: pointer; list-style: none; padding: 14px 18px;
  display: flex; justify-content: space-between; align-items: center;
  font-weight: 600;
}
.day-section > summary::-webkit-details-marker { display: none; }
.day-section > summary::before {
  content: "▸ "; color: var(--muted); margin-right: 4px; font-size: 11px;
}
.day-section[open] > summary::before { content: "▾ "; }
.day-section .day-count { color: var(--muted); font-weight: 400; font-size: 12px; }
.day-body { padding: 0 18px 14px; }

.qna-card {
  background: var(--panel-alt); border: 1px solid var(--border-soft);
  border-radius: 10px; padding: 14px 16px; margin-bottom: 8px;
  transition: 0.1s;
}
.qna-card:hover { border-color: var(--border); }
.qna-card details { background: transparent; border: 0; padding: 0; }
.qna-card summary { cursor: pointer; list-style: none; outline: none; }
.qna-card summary::-webkit-details-marker { display: none; }
.qna-card .row1 {
  display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
  font-size: 11px; color: var(--muted); margin-bottom: 8px;
}
.qna-card .question {
  font-size: 15px; font-weight: 600; color: var(--text);
  word-break: break-word;
}
.qna-card .question a { color: inherit; }
.tool {
  display: inline-block; padding: 2px 9px; border-radius: 12px;
  font-size: 11px; font-weight: 600;
}
.tool-brain  { background: rgba(236,72,153,0.12); color: var(--tool-brain); }
.tool-paper  { background: rgba(168,85,247,0.12); color: var(--tool-paper); }
.tool-web    { background: rgba(16,185,129,0.12); color: var(--tool-web); }
.tool-ingest { background: rgba(245,158,11,0.12); color: var(--tool-ingest); }
.tool-other  { background: rgba(107,114,128,0.10); color: var(--muted); }
.warning {
  background: rgba(245,158,11,0.10); border-left: 3px solid var(--tool-ingest);
  padding: 8px 12px; font-size: 12px; margin: 10px 0;
  border-radius: 0 6px 6px 0; color: #78350f;
}
.answer {
  white-space: pre-wrap; word-break: break-word; line-height: 1.7;
  margin-top: 12px; color: #374151;
  background: var(--panel); border: 1px solid var(--border-soft);
  border-radius: 8px; padding: 14px 16px;
}
.sources {
  margin-top: 12px; padding-top: 12px;
  border-top: 1px dashed var(--border); font-size: 12px;
  color: var(--muted);
}
.sources li { padding: 2px 0; }

.qna-card.hidden, .day-section.hidden { display: none; }
"""

_DETAIL_CSS = _BASE_CSS + """
main { max-width: 820px; margin: 0 auto; padding: 28px 22px 80px; }
.back {
  color: var(--muted); font-size: 13px;
  display: inline-block; margin-bottom: 18px;
}
.meta {
  color: var(--muted); font-size: 12px; margin-bottom: 8px;
  display: flex; gap: 12px; flex-wrap: wrap;
}
h1 {
  font-size: 22px; line-height: 1.4; margin: 0 0 24px 0;
}
.answer {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 12px; padding: 22px 26px;
  white-space: pre-wrap; word-break: break-word;
  line-height: 1.75; color: #374151;
  box-shadow: var(--shadow);
}
.warning {
  background: rgba(245,158,11,0.10); border-left: 3px solid var(--tool-ingest);
  padding: 10px 14px; font-size: 13px; margin-bottom: 16px;
  border-radius: 0 6px 6px 0; color: #78350f;
}
.sources {
  margin-top: 24px; padding: 18px 22px;
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 12px; font-size: 13px;
  box-shadow: var(--shadow);
}
.sources h3 { margin: 0 0 10px 0; font-size: 13px; color: var(--muted); }
.sources ul { margin: 0; padding-left: 18px; color: #374151; }
.sources li { padding: 3px 0; }
.tool {
  display: inline-block; padding: 3px 10px; border-radius: 12px;
  font-size: 11px; font-weight: 600; background: rgba(107,114,128,0.10);
  color: var(--muted);
}
"""



def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def _tool_class(name: str) -> str:
    n = name.lower()
    if "brain" in n or "compare" in n or "recent" in n:
        return "tool tool-brain"
    if "paper" in n:
        return "tool tool-paper"
    if "web" in n:
        return "tool tool-web"
    if "ingest" in n:
        return "tool tool-ingest"
    return "tool tool-other"


def _tool_emoji(name: str) -> str:
    n = name.lower()
    if "brain" in n or "compare" in n or "recent" in n:
        return "🧠"
    if "paper" in n:
        return "📄"
    if "web" in n:
        return "🌐"
    if "ingest" in n:
        return "📥"
    return "🔧"


_INDEX_JS = """
(function(){
  var search = document.getElementById('q');
  var resetBtn = document.getElementById('reset');
  var chips = document.querySelectorAll('.chip');
  var cards = document.querySelectorAll('.qna-card');
  var sections = document.querySelectorAll('.day-section');
  var counter = document.getElementById('count');

  // Active filters: empty set = all tools allowed.
  var activeTools = new Set();

  function apply(){
    var q = (search.value || '').trim().toLowerCase();
    var visible = 0;
    cards.forEach(function(c){
      var text = c.dataset.text || '';
      var tools = (c.dataset.tools || '').split(' ');
      var matchesQuery = !q || text.indexOf(q) !== -1;
      var matchesTool = activeTools.size === 0 ||
        tools.some(function(t){ return activeTools.has(t); });
      var show = matchesQuery && matchesTool;
      c.classList.toggle('hidden', !show);
      if (show) visible++;
    });
    sections.forEach(function(s){
      var anyVisible = s.querySelectorAll('.qna-card:not(.hidden)').length > 0;
      s.classList.toggle('hidden', !anyVisible);
    });
    if (counter) counter.textContent = visible;
  }

  search.addEventListener('input', apply);
  resetBtn.addEventListener('click', function(){
    search.value = '';
    activeTools.clear();
    chips.forEach(function(c){ c.classList.remove('active'); });
    apply();
  });
  chips.forEach(function(c){
    c.addEventListener('click', function(){
      var tool = c.dataset.tool;
      if (activeTools.has(tool)){
        activeTools.delete(tool);
        c.classList.remove('active');
      } else {
        activeTools.add(tool);
        c.classList.add('active');
      }
      apply();
    });
  });
})();
"""


def _card_data_text(it: dict) -> str:
    """Searchable haystack for the JS filter — lowercased, ascii-safe."""
    parts = [
        it.get("question") or "",
        it.get("answer") or "",
        " ".join(it.get("tools") or []),
        " ".join(it.get("sources") or []),
    ]
    return _esc(" ".join(parts).lower())[:5000]


def _card_data_tools(it: dict) -> str:
    """Space-separated bucket names so the JS filter can match by group."""
    buckets = set()
    for t in (it.get("tools") or []):
        n = t.lower()
        if "brain" in n or "compare" in n or "recent" in n:
            buckets.add("brain")
        elif "paper" in n:
            buckets.add("paper")
        elif "web" in n:
            buckets.add("web")
        elif "ingest" in n:
            buckets.add("ingest")
        else:
            buckets.add("other")
    return " ".join(sorted(buckets))


def _kst_day(ts_iso: str) -> str:
    """Convert a UTC ISO timestamp ('2026-05-08T15:33:00') to its
    KST calendar date string. Falls back to the leading 10 chars on
    anything unparseable."""
    from datetime import datetime, timedelta, timezone
    if not ts_iso:
        return ""
    try:
        dt = datetime.fromisoformat(ts_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=9))).date().isoformat()
    except Exception:
        return ts_iso[:10]


def _kst_hhmm(ts_iso: str) -> str:
    from datetime import datetime, timedelta, timezone
    if not ts_iso:
        return ""
    try:
        dt = datetime.fromisoformat(ts_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=9))).strftime("%H:%M")
    except Exception:
        return ts_iso[11:16]


def _render_index(rows: list[dict], stats: dict) -> str:
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        day = _kst_day(r.get("ts") or "")
        grouped.setdefault(day, []).append(r)
    days_sorted = sorted(grouped.keys(), reverse=True)

    today_calls = stats.get("today_calls", 0)
    mtd_day = stats.get("mtd_day", 1) or 1
    avg_daily = (stats["mtd_krw"] / mtd_day) if stats.get("mtd_krw") else 0

    parts = [
        "<!DOCTYPE html><html lang='ko'><head>",
        "<meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>🧠 Second Brain Archive</title>",
        f"<style>{_INDEX_CSS}</style>",
        f"<script>{_THEME_SWITCHER_JS}</script>",
        "</head><body><div class='layout'>",
        "<header>",
        "<h1>🧠 Second Brain Archive</h1>",
        "<div class='sub'>카드 클릭 시 답변 펼침 · 검색창에서 키워드 필터 · 칩으로 도구별 필터</div>",
        "</header>",

        "<div class='stats'>",
        "<div class='stat-card'>",
        "<div class='label'>📊 총 Q&amp;A</div>",
        f"<div class='value'>{stats['total_qna']:,}건</div>",
        f"<div class='sub'>{len(days_sorted)}일에 걸쳐 누적</div>",
        "</div>",
        "<div class='stat-card'>",
        "<div class='label'>📚 학습 자료</div>",
        f"<div class='value'>{stats['docs']:,}개</div>",
        f"<div class='sub'>청크 {stats['chunks']:,}개</div>",
        "</div>",
        "<div class='stat-card'>",
        "<div class='label'>💰 오늘 비용</div>",
        f"<div class='value'>₩{stats['today_krw']:,.0f}</div>",
        f"<div class='sub'>{today_calls}콜</div>",
        "</div>",
        "<div class='stat-card'>",
        f"<div class='label'>📅 이번 달 ({stats['mtd_year']}년 {stats['mtd_month']}월)</div>",
        f"<div class='value'>₩{stats['mtd_krw']:,.0f}</div>",
        f"<div class='sub'>{mtd_day}일차 · 일평균 ₩{avg_daily:,.0f}</div>",
        "</div>",
        "</div>",

        "<div class='controls'>",
        "<div class='search-row'>",
        "<input id='q' type='text' placeholder='질문 / 답변 / 출처 검색...' autocomplete='off'>",
        "<button id='reset' class='reset' type='button'>초기화</button>",
        "</div>",
        "<div class='chips'>",
        "<span class='chip brain' data-tool='brain'>🧠 brain</span>",
        "<span class='chip paper' data-tool='paper'>📄 papers</span>",
        "<span class='chip web' data-tool='web'>🌐 web</span>",
        "<span class='chip ingest' data-tool='ingest'>📥 ingest</span>",
        "</div>",
        "</div>",

        f"<div class='summary-line'>총 <span id='count'>{len(rows)}</span>건의 Q&amp;A 기록</div>",
    ]

    if not rows:
        parts.append(
            "<div style='text-align:center;padding:80px 20px;color:var(--muted)'>"
            "아직 저장된 Q&amp;A가 없어요. 봇에 질문 한 번 던지면 여기에 쌓입니다."
            "</div>"
        )

    for day in days_sorted:
        items = grouped[day]
        parts.append(
            f"<details class='day-section' open>"
            f"<summary>📅 {_esc(day)}<span class='day-count'>{len(items)}건</span></summary>"
            f"<div class='day-body'>"
        )
        for it in items:
            tools = it.get("tools") or []
            tool_chips = "".join(
                f"<span class='{_tool_class(t)}'>{_tool_emoji(t)} {_esc(t)}</span>"
                for t in tools
            )
            warn = (
                f"<div class='warning'>{_esc(it.get('warning'))}</div>"
                if it.get("warning") else ""
            )
            sources_html = ""
            srcs = it.get("sources") or []
            if srcs:
                lis = "".join(f"<li>{_esc(s)}</li>" for s in srcs)
                sources_html = (
                    f"<div class='sources'>📚 출처 {len(srcs)}개<ul>{lis}</ul></div>"
                )
            model_chip = (
                f"<span style='margin-left:auto'>{_esc(it.get('model'))}</span>"
                if it.get("model") else ""
            )
            data_text = _card_data_text(it)
            data_tools = _card_data_tools(it)
            parts.append(
                f"<div class='qna-card' data-text=\"{data_text}\" data-tools=\"{data_tools}\">"
                "<details><summary>"
                "<div class='row1'>"
                f"<span>{_esc(_kst_hhmm(it['ts']))}</span>"
                f"{tool_chips}{model_chip}"
                "</div>"
                "<div class='question'>"
                f"<a href='q-{int(it['id'])}.html'>Q. {_esc(it['question'])}</a>"
                "</div></summary>"
                f"{warn}"
                f"<div class='answer'>{_esc(it['answer'])}</div>"
                f"{sources_html}"
                "</details></div>"
            )
        parts.append("</div></details>")

    parts.append(f"<script>{_INDEX_JS}</script>")
    parts.append("</div></body></html>")
    return "\n".join(parts)


def _render_detail(item: dict, token_dir: str) -> str:
    tools = item.get("tools") or []
    tool_chips = "".join(
        f"<span class='tool'>{_tool_emoji(t)} {_esc(t)}</span>" for t in tools
    )
    warn = (
        f"<div class='warning'>{_esc(item.get('warning'))}</div>"
        if item.get("warning") else ""
    )
    sources_html = ""
    srcs = item.get("sources") or []
    if srcs:
        lis = "".join(f"<li>{_esc(s)}</li>" for s in srcs)
        sources_html = (
            f"<div class='sources'><h3>📚 출처</h3><ul>{lis}</ul></div>"
        )
    model_chip = (
        f"<span>{_esc(item.get('model'))}</span>"
        if item.get("model") else ""
    )
    return "\n".join([
        "<!DOCTYPE html><html lang='ko'><head>",
        "<meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>Q&A · {int(item['id'])}</title>",
        f"<style>{_DETAIL_CSS}</style>",
        f"<script>{_THEME_SWITCHER_JS}</script>",
        "</head><body><main>",
        "<a class='back' href='index.html'>← 목록으로</a>",
        "<div class='meta'>",
        f"<span>{_esc(_kst_day(item['ts']))} {_esc(_kst_hhmm(item['ts']))}</span>",
        model_chip,
        tool_chips,
        "</div>",
        f"<h1>{_esc(item['question'])}</h1>",
        warn,
        f"<div class='answer'>{_esc(item['answer'])}</div>",
        sources_html,
        "</main></body></html>",
    ])


_PUBLIC_INDEX = """<!DOCTYPE html><html lang='ko'><head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>private</title>
<style>body{background:#0f1419;color:#8b949e;font:14px sans-serif;
text-align:center;padding:40vh 20px}</style>
</head><body>private</body></html>
"""


def regenerate() -> None:
    """Rewrite the static HTML tree under data/dashboard/.
    Returns silently — never raises so a render bug can't kill ingest."""
    token = (os.getenv("DASHBOARD_TOKEN", "") or "").strip()
    if not token:
        log.warning("DASHBOARD_TOKEN unset; static dashboard skipped")
        return
    try:
        with _LOCK:
            base = Path(config.DATA_DIR) / "dashboard"
            target = base / token
            target.mkdir(parents=True, exist_ok=True)
            (base / "index.html").write_text(_PUBLIC_INDEX, encoding="utf-8")
            rows = qna.recent(limit=2000)
            today = cost.today_krw()
            mtd = cost.month_to_date_krw()
            stats = {
                "total_qna": qna.count(),
                "docs": meta_store.count(),
                "chunks": vector_store.chunk_count(),
                "today_krw": today["total_krw"],
                "today_calls": today["calls"],
                "mtd_krw": mtd["total_krw"],
                "mtd_year": mtd["year"],
                "mtd_month": mtd["month"],
                "mtd_day": mtd["day"],
            }
            (target / "index.html").write_text(
                _render_index(rows, stats), encoding="utf-8"
            )
            for it in rows:
                fname = target / f"q-{int(it['id'])}.html"
                fname.write_text(_render_detail(it, token), encoding="utf-8")
    except Exception:
        log.exception("dashboard regenerate failed")
