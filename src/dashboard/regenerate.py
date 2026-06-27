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
  --important: #f59e0b; --memo: #10b981;
  --warning-text: #78350f;
  --tool-brain: #ec4899; --tool-paper: #a855f7;
  --tool-patent: #3b82f6; --tool-report: #14b8a6;
  --tool-web: #10b981; --tool-ingest: #f59e0b;
  --shadow: 0 1px 2px rgba(0,0,0,0.04), 0 1px 3px rgba(0,0,0,0.06);
}
[data-theme="dark"] {
  --bg: #0f172a; --panel: #1e293b; --panel-alt: #172033;
  --border: #334155; --border-soft: #1e2738;
  --text: #f1f5f9; --muted: #cbd5e1; --accent: #60a5fa;
  --primary: #10b981;
  --important: #fbbf24; --memo: #34d399;
  --warning-text: #fcd34d;
  --tool-brain: #f472b6; --tool-paper: #c084fc;
  --tool-patent: #60a5fa; --tool-report: #2dd4bf;
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

/* Search-keyword highlight (Noah-style yellow). Used on the
   index card body when the search input matches, and on the
   detail page when the URL carries a ?kw= param. */
mark.kw-highlight {
  background: #fef08a; color: inherit;
  padding: 0 2px; border-radius: 2px;
  box-shadow: 0 0 0 1px rgba(0,0,0,0.06);
}
[data-theme="dark"] mark.kw-highlight {
  background: #fbbf24; color: #0f172a;
}
mark.kw-highlight.kw-current {
  outline: 2px solid #f59e0b; outline-offset: 1px;
}
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
.nav-shortcut {
  display: inline-flex; align-items: center; gap: 3px;
  padding: 2px 10px; border-radius: 12px;
  background: var(--accent); color: #fff !important;
  font-weight: 600; font-size: 13px;
  text-decoration: none !important;
  transition: opacity 0.15s;
}
.nav-shortcut:hover { opacity: 0.85; }

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
.chip.patent.active { background: var(--tool-patent); border-color: var(--tool-patent); }
.chip.report.active { background: var(--tool-report); border-color: var(--tool-report); }
.chip.web.active    { background: var(--tool-web); border-color: var(--tool-web); }
.chip.ingest.active { background: var(--tool-ingest); border-color: var(--tool-ingest); }

.ask-hint { font-size: 11.5px; color: var(--muted); margin: 0 2px 12px; }
.ask-hint code {
  background: var(--panel-alt); border: 1px solid var(--border);
  border-radius: 5px; padding: 1px 5px; font-size: 11px;
}

.ask-panel {
  background: var(--panel); border: 1px solid var(--border);
  border-left: 3px solid var(--primary);
  border-radius: 12px; padding: 16px 18px; margin: 4px 0 16px;
  box-shadow: var(--shadow);
}
.ask-panel.hidden { display: none; }
.ask-panel .ask-q {
  font-size: 13px; font-weight: 700; color: var(--text);
  margin-bottom: 10px; display: flex; align-items: center; gap: 8px;
}
.ask-panel .ask-q .ask-close {
  margin-left: auto; cursor: pointer; color: var(--muted);
  font-weight: 400; font-size: 16px; line-height: 1;
}
.ask-panel .ask-q .ask-kind {
  font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 10px;
  background: var(--panel-alt); color: var(--muted); border: 1px solid var(--border);
}
.ask-panel .ask-body {
  font-size: 13.5px; line-height: 1.7; color: var(--text);
  white-space: pre-wrap; word-break: break-word;
}
.ask-panel .ask-body code {
  background: var(--panel-alt); border-radius: 4px; padding: 1px 4px; font-size: 12px;
}
.ask-panel .ask-sources {
  margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--border);
  font-size: 12px; color: var(--muted);
}
.ask-panel .ask-sources b { color: var(--text); }
.ask-panel .ask-spinner { color: var(--muted); font-size: 13px; }
.ask-panel .ask-err { color: #ef4444; font-size: 13px; white-space: pre-wrap; }

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

/* Month wrapper around the per-day sections so the user can collapse
   an entire month with one click. Slightly heavier border than the
   day rows so the nesting reads at a glance. */
.month-section {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 14px; margin-bottom: 16px; overflow: hidden;
  box-shadow: var(--shadow);
}
.month-section > summary {
  cursor: pointer; list-style: none; padding: 16px 20px;
  display: flex; justify-content: space-between; align-items: center;
  font-weight: 700; font-size: 15px;
  background: var(--panel-alt);
  border-bottom: 1px solid var(--border-soft);
}
.month-section > summary::-webkit-details-marker { display: none; }
.month-section > summary::before {
  content: "▸ "; color: var(--muted); margin-right: 4px; font-size: 12px;
}
.month-section[open] > summary::before { content: "▾ "; }
.month-section .month-count { color: var(--muted); font-weight: 400; font-size: 13px; }
.month-body { padding: 12px 14px 4px; }
/* Inside a month wrapper the day-section already has a panel
   background — drop its own border so the nested look stays clean. */
.month-section .day-section { background: var(--panel-alt); }

.qna-card {
  background: var(--panel-alt); border: 1px solid var(--border-soft);
  border-radius: 10px; padding: 14px 16px; margin-bottom: 8px;
  transition: 0.1s;
}
.qna-card:hover { border-color: var(--border); }
/* Card-level accent: extend the per-chip tool colours to the whole card's
   left border so categories (brain/web/patent/…) are scannable at a glance. */
.qna-card.acc-brain  { border-left: 3px solid var(--tool-brain); }
.qna-card.acc-paper  { border-left: 3px solid var(--tool-paper); }
.qna-card.acc-patent { border-left: 3px solid var(--tool-patent); }
.qna-card.acc-report { border-left: 3px solid var(--tool-report); }
.qna-card.acc-web    { border-left: 3px solid var(--tool-web); }
.qna-card.acc-ingest { border-left: 3px solid var(--tool-ingest); }
/* Keyboard navigation focus ring (j/k). Indigo — distinct from every tool
   colour so it reads clearly as "this is the keyboard-selected card". */
.qna-card.kbd-focus  { box-shadow: 0 0 0 2px #6366f1; border-color: #6366f1; }
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
.qna-card .snippet {
  margin-top: 6px;
  font-size: 13px; line-height: 1.55;
  color: var(--muted);
  word-break: break-word;
  display: -webkit-box;
  -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}
.qna-card .snippet mark.kw-highlight { color: inherit; }
.tool {
  display: inline-block; padding: 2px 9px; border-radius: 12px;
  font-size: 11px; font-weight: 600;
}
.tool-brain  { background: rgba(236,72,153,0.12); color: var(--tool-brain); }
.tool-paper  { background: rgba(168,85,247,0.12); color: var(--tool-paper); }
.tool-patent { background: rgba(59,130,246,0.12); color: var(--tool-patent); }
.tool-report { background: rgba(20,184,166,0.12); color: var(--tool-report); }
.tool-web    { background: rgba(16,185,129,0.12); color: var(--tool-web); }
.tool-ingest { background: rgba(245,158,11,0.12); color: var(--tool-ingest); }
.tool-other  { background: rgba(107,114,128,0.10); color: var(--muted); }
.warning {
  background: rgba(245,158,11,0.10); border-left: 3px solid var(--tool-ingest);
  padding: 8px 12px; font-size: 12px; margin: 10px 0;
  border-radius: 0 6px 6px 0; color: var(--warning-text);
}
.answer {
  white-space: pre-wrap; word-break: break-word; line-height: 1.7;
  margin-top: 12px; color: var(--text);
  background: var(--panel); border: 1px solid var(--border-soft);
  border-radius: 8px; padding: 14px 16px;
}
.sources {
  margin-top: 12px; padding-top: 12px;
  border-top: 1px dashed var(--border); font-size: 12px;
  color: var(--muted);
}
.sources li { padding: 2px 0; }
.source-link {
  text-decoration: none; margin-left: 4px;
  font-size: 12px; opacity: 0.65; transition: opacity 0.12s;
}
.source-link:hover { opacity: 1; }

.qna-card.hidden, .day-section.hidden, .month-section.hidden { display: none; }

.del-btn {
  /* Tactile button look like the NOAH dashboard — slate-tinted rounded
     square so the trash icon reads as an actionable control instead
     of a stray emoji floating on the card edge. Stays neutral grey
     until hover, then flips to red so accidental hovers don't scream
     destructive. */
  cursor: pointer;
  background: rgba(148, 163, 184, 0.18);  /* slate-400 18% */
  border: 1px solid rgba(148, 163, 184, 0.32);
  color: var(--muted);
  font-size: 16px;
  line-height: 1;
  padding: 6px 9px;
  border-radius: 8px;
  margin-left: 8px;
  transition: background-color 0.12s, border-color 0.12s, color 0.12s,
              transform 0.12s;
}
.del-btn:hover {
  background: rgba(239, 68, 68, 0.18);
  border-color: rgba(239, 68, 68, 0.55);
  color: #ef4444;
  transform: translateY(-1px);
}
.del-btn:active { transform: translateY(0); }
[data-theme="dark"] .del-btn {
  background: rgba(71, 85, 105, 0.45);  /* slate-700 45% */
  border-color: rgba(100, 116, 139, 0.55);
  color: #cbd5e1;
}
[data-theme="dark"] .del-btn:hover {
  background: rgba(239, 68, 68, 0.25);
  border-color: rgba(239, 68, 68, 0.7);
  color: #fca5a5;
}
.qna-card.removing {
  opacity: 0; transform: scale(0.95);
  transition: opacity 0.25s, transform 0.25s;
}
.star-btn {
  cursor: pointer; background: transparent; border: 0; color: var(--muted);
  font-size: 16px; line-height: 1; padding: 2px 5px; border-radius: 6px;
  transition: 0.12s;
}
.star-btn:hover { color: var(--important); transform: scale(1.15); }
.star-btn.on { color: var(--important); }
.qna-card[data-important="1"] {
  border-color: rgba(245, 158, 11, 0.5);
  background: rgba(245, 158, 11, 0.06);
}
.qna-card .memo-preview { margin-top: 8px; padding: 8px 10px; font-size: 13px;
  line-height: 1.55; color: var(--text); white-space: pre-wrap; word-break: break-word;
  background: rgba(16,185,129,0.08); border: 1px solid rgba(16,185,129,0.35);
  border-radius: 8px; }
.qa-memo { margin-top: 10px; padding-top: 8px; border-top: 1px dashed var(--border); }
.qa-memo-h { font-size: 11px; color: var(--muted); font-weight: 600; margin-bottom: 5px; }
.qa-memo textarea { width: 100%; min-height: 54px; background: var(--bg);
  border: 1px solid var(--border); color: var(--text); border-radius: 8px;
  padding: 8px; font-size: 13px; outline: none; resize: vertical; box-sizing: border-box; }
.qa-memo textarea:focus { border-color: var(--primary); }
.memo-row { display: flex; align-items: center; gap: 8px; margin-top: 6px; }
.memo-save { background: var(--primary); border: 0; color: #fff; padding: 5px 14px;
  border-radius: 7px; cursor: pointer; font-size: 12px; font-weight: 600; }
.memo-del { background: rgba(148,163,184,.25); border: 0; color: var(--muted);
  padding: 5px 12px; border-radius: 7px; cursor: pointer; font-size: 12px; font-weight: 600; }
.memo-status { font-size: 11px; color: var(--muted); }
.alarm-row { display: flex; align-items: center; gap: 6px; margin-top: 7px; flex-wrap: wrap; }
.alarm-time { background: var(--bg,#0f172a); border: 1px solid var(--border); color: var(--text);
  border-radius: 6px; padding: 4px 6px; font-size: 12px; }
.alarm-set, .alarm-clear { border: 0; border-radius: 6px; cursor: pointer; font-size: 11.5px;
  padding: 5px 10px; font-weight: 600; }
.alarm-set { background: #6366f1; color: #fff; }
.alarm-clear { background: rgba(148,163,184,.25); color: var(--muted); }
.alarm-status { font-size: 11px; color: #818cf8; }
.controls button.impfilter { background: var(--panel); border: 1px solid var(--border); color: var(--muted); }
.controls button.impfilter.active { background: var(--important); border-color: var(--important); color: #fff; }
.controls button.memofilter { background: var(--panel); border: 1px solid var(--border); color: var(--muted); }
.controls button.memofilter.active { background: var(--memo); border-color: var(--memo); color: #fff; }

.dash-cmd-link {
  color: var(--primary) !important; cursor: pointer;
  text-decoration: underline; text-underline-offset: 2px;
  font-family: monospace; font-weight: 600;
}
.dash-cmd-link:hover { opacity: 0.8; }

.ans-bl { white-space: normal; margin: 6px 0; padding-left: 1.5em; list-style: disc; }
.ans-bl li { margin: 3px 0; }

.footer {
  margin-top: 32px; padding: 16px 0; text-align: center;
  font-size: 11px; color: var(--muted);
  border-top: 1px solid var(--border-soft);
}
"""

_DETAIL_JS = r"""
(function(){
  // Pull ?kw= off the URL; if present, highlight all matches in the
  // answer body and smooth-scroll the first one into view. Same
  // shape as the index-page highlighter so the two stay visually
  // consistent.
  var params = new URLSearchParams(location.search);
  var kw = params.get('kw');
  if (!kw) return;
  var answer = document.querySelector('.answer');
  if (!answer) return;
  function escapeRegex(s){
    return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }
  var regex = new RegExp(escapeRegex(kw), 'gi');
  var walker = document.createTreeWalker(answer, NodeFilter.SHOW_TEXT, null);
  var nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  var firstMark = null;
  nodes.forEach(function(node){
    var text = node.nodeValue;
    if (!regex.test(text)) return;
    regex.lastIndex = 0;
    var frag = document.createDocumentFragment();
    var last = 0, m;
    while ((m = regex.exec(text)) !== null){
      if (m[0].length === 0){ regex.lastIndex++; continue; }
      if (m.index > last)
        frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      var mk = document.createElement('mark');
      mk.className = 'kw-highlight';
      mk.textContent = m[0];
      if (!firstMark){
        mk.classList.add('kw-current');
        firstMark = mk;
      }
      frag.appendChild(mk);
      last = m.index + m[0].length;
    }
    if (last < text.length)
      frag.appendChild(document.createTextNode(text.slice(last)));
    node.parentNode.replaceChild(frag, node);
  });
  if (firstMark){
    setTimeout(function(){
      firstMark.scrollIntoView({behavior: 'smooth', block: 'center'});
    }, 80);
  }
})();
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
  line-height: 1.75; color: var(--text);
  box-shadow: var(--shadow);
}
.warning {
  background: rgba(245,158,11,0.10); border-left: 3px solid var(--tool-ingest);
  padding: 10px 14px; font-size: 13px; margin-bottom: 16px;
  border-radius: 0 6px 6px 0; color: var(--warning-text);
}
.sources {
  margin-top: 24px; padding: 18px 22px;
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 12px; font-size: 13px;
  box-shadow: var(--shadow);
}
.sources h3 { margin: 0 0 10px 0; font-size: 13px; color: var(--muted); }
.sources ul { margin: 0; padding-left: 18px; color: var(--text); }
.sources li { padding: 3px 0; }
.tool {
  display: inline-block; padding: 3px 10px; border-radius: 12px;
  font-size: 11px; font-weight: 600; background: rgba(107,114,128,0.10);
  color: var(--muted);
}
.dash-cmd-link {
  color: var(--primary) !important; cursor: pointer;
  text-decoration: underline; text-underline-offset: 2px;
  font-family: monospace; font-weight: 600;
}
.dash-cmd-link:hover { opacity: 0.8; }

.ans-bl { white-space: normal; margin: 6px 0; padding-left: 1.5em; list-style: disc; }
.ans-bl li { margin: 3px 0; }
"""



def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


# Title→URL map, populated once per regenerate() via meta.title_url_map()
# so _source_li does a dict lookup instead of a per-source SQL LIKE.
# The old per-call search_title() fan-out (N sources × full-table LIKE,
# every 60 s) pegged a worker thread at ~100% CPU around the clock.
_SOURCE_URL_CACHE: dict[str, str] = {}


def _source_li(title: str) -> str:
    """Render a single 출처 entry. Resolves the doc's source URL from
    the per-regenerate _SOURCE_URL_CACHE (built once via
    meta.title_url_map) — when the source is an http(s) URL the title
    becomes a clickable link, otherwise plain text. Resolving at render
    time lets historical Q&As pick up newly-known URLs without a
    migration, but the lookup is now O(1) dict access, not a SQL scan."""
    safe_title = _esc(title)
    url = _SOURCE_URL_CACHE.get((title or "").strip(), "")
    if not url:
        return f"<li>{safe_title}</li>"
    safe_url = _esc(url)
    return (
        f"<li>{safe_title} "
        f"<a href='{safe_url}' target='_blank' rel='noopener' "
        f"class='source-link' title='원본 열기'>🔗</a></li>"
    )


def _tool_bucket(name: str) -> str:
    """Single bucket name for chip filter + colour. Order matters —
    `compare_papers` must hit brain before paper, `research_data` must
    hit data before paper. Keep this in sync with _card_data_tools and
    the chip HTML in _index_html."""
    n = name.lower()
    if "brain" in n or "compare" in n or "recent" in n:
        return "brain"
    if "patent" in n or "citing" in n or n.startswith("kipris_"):
        # all patent flavours: search_patents (EPO), search_company_patents
        # (KIPRIS), get_patent_detail, get_citing_patents,
        # search_kr_patents_kisti, get_kr_patent_detail,
        # get_kr_patent_citations, and the 8 verified kipris_* commands
        # (search / pub / reg / inventor / status / family / claims /
        # priority) which don't carry "patent" in the tool_name.
        return "patent"
    if ("report" in n or "rnd_projects" in n
            or "related_content" in n or "classification" in n
            or "govt_reports" in n or "agency_rnd" in n
            or "rnd_outcomes" in n or "rnd_issues" in n):
        # KR R&D bucket: ScienceON reports + NTIS projects /
        # classification recommend / related content / outcomes /
        # govt reports / agency stats / issues.
        return "report"
    if "paper" in n:
        return "paper"
    if "web" in n:
        return "web"
    if "ingest" in n:
        return "ingest"
    return "other"


_TOOL_EMOJI_MAP = {
    "brain": "🧠", "paper": "📄", "patent": "⚖️",
    "report": "📑", "web": "🌐", "ingest": "📥",
}


def _tool_class(name: str) -> str:
    return f"tool tool-{_tool_bucket(name)}"


def _tool_emoji(name: str) -> str:
    return _TOOL_EMOJI_MAP.get(_tool_bucket(name), "🔧")


_INDEX_JS = r"""
(function(){
  var search = document.getElementById('q');
  var resetBtn = document.getElementById('reset');
  var chips = document.querySelectorAll('.chip');
  var cards = document.querySelectorAll('.qna-card');
  var sections = document.querySelectorAll('.day-section');
  var months = document.querySelectorAll('.month-section');
  var counter = document.getElementById('count');

  var activeTools = new Set();
  var curImportant = false;
  var curMemo = false;

  function escapeRegex(s){
    return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  function escapeHtml(s){
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // Build a single-line (max 2-line clamp via CSS) preview of the
  // body around the FIRST keyword match. Adds ellipses on whichever
  // side was truncated, then re-highlights matches inside the slice.
  // Centred on the first hit so the user always sees their term in
  // context even if the body is several KB.
  function buildSnippet(text, query){
    if (!text || !query) return '';
    var lower = text.toLowerCase();
    var qLower = query.toLowerCase();
    var idx = lower.indexOf(qLower);
    if (idx < 0) return '';
    var qLen = query.length;
    var contextChars = 80;  // ~80 chars on each side of the match
    var start = Math.max(0, idx - contextChars);
    var end = Math.min(text.length, idx + qLen + contextChars);
    var head = start > 0 ? '…' : '';
    var tail = end < text.length ? '…' : '';
    var slice = text.slice(start, end);
    var regex = new RegExp(escapeRegex(query), 'gi');
    var body = escapeHtml(slice).replace(regex, function(m){
      return '<mark class="kw-highlight">' + m + '</mark>';
    });
    return head + body + tail;
  }

  function highlightTextNodes(root, regex){
    if (!root || !regex) return 0;
    var matches = 0;
    var walker = document.createTreeWalker(
      root, NodeFilter.SHOW_TEXT,
      { acceptNode: function(n){
          // Skip text already inside a <mark> or inside <script>/<style>.
          var p = n.parentNode;
          while (p && p !== root){
            var tag = (p.tagName || '').toLowerCase();
            if (tag === 'mark' || tag === 'script' || tag === 'style')
              return NodeFilter.FILTER_REJECT;
            p = p.parentNode;
          }
          return regex.test(n.nodeValue)
            ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
        } });
    var nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach(function(node){
      var text = node.nodeValue;
      regex.lastIndex = 0;
      var frag = document.createDocumentFragment();
      var last = 0, m;
      while ((m = regex.exec(text)) !== null){
        if (m[0].length === 0){ regex.lastIndex++; continue; }
        if (m.index > last)
          frag.appendChild(document.createTextNode(text.slice(last, m.index)));
        var mk = document.createElement('mark');
        mk.className = 'kw-highlight';
        mk.textContent = m[0];
        frag.appendChild(mk);
        last = m.index + m[0].length;
        matches++;
      }
      if (last < text.length)
        frag.appendChild(document.createTextNode(text.slice(last)));
      if (last > 0) node.parentNode.replaceChild(frag, node);
    });
    return matches;
  }

  function clearHighlights(root){
    if (!root) return;
    var marks = root.querySelectorAll('mark.kw-highlight');
    marks.forEach(function(m){
      m.parentNode.replaceChild(document.createTextNode(m.textContent), m);
    });
    root.normalize();
  }

  function apply(){
    var q = (search.value || '').trim();
    var qLower = q.toLowerCase();
    var visible = 0;
    var regex = q ? new RegExp(escapeRegex(q), 'gi') : null;
    cards.forEach(function(c){
      if (c.classList.contains('removed')) return;
      var text = c.dataset.text || '';
      var tools = (c.dataset.tools || '').split(' ');
      var matchesQuery = !qLower || text.indexOf(qLower) !== -1;
      var matchesTool = activeTools.size === 0 ||
        tools.some(function(t){ return activeTools.has(t); });
      var matchesImp = !curImportant || (c.dataset.important === '1');
      var matchesMemo = !curMemo || (c.dataset.hasmemo === '1');
      var show = matchesQuery && matchesTool && matchesImp && matchesMemo;
      c.classList.toggle('hidden', !show);
      // Clear stale highlights + snippet preview from previous query.
      clearHighlights(c);
      var oldSnip = c.querySelector('.snippet');
      if (oldSnip) oldSnip.remove();
      // 메모만 필터 ON → 카드 밑에 메모 내용 미리보기(안 열어봐도 보이게).
      var oldMp = c.querySelector(':scope > .memo-preview');
      if (oldMp) oldMp.remove();
      if (show && curMemo && c.dataset.memo){
        var mp = document.createElement('div');
        mp.className = 'memo-preview';
        mp.textContent = '📝 ' + c.dataset.memo;
        c.appendChild(mp);
      }
      if (show && regex){
        var qEl = c.querySelector('.question');
        var aEl = c.querySelector('.answer');
        // Highlight in-place inside both question and (still-folded)
        // answer. The answer DOM stays hidden under <details> until
        // the user clicks the card open — at that point the highlights
        // are already there waiting.
        highlightTextNodes(qEl, regex);
        highlightTextNodes(aEl, regex);
        // Body preview: ±80 chars around the first match, only when
        // the keyword actually appears in the body (so cards that
        // only matched on the question don't get a redundant snippet).
        if (aEl){
          var snipHTML = buildSnippet(aEl.textContent, q);
          if (snipHTML && qEl && qEl.parentNode){
            var snip = document.createElement('div');
            snip.className = 'snippet';
            snip.innerHTML = snipHTML;
            qEl.parentNode.insertBefore(snip, qEl.nextSibling);
          }
        }
      }
      if (show) visible++;
    });
    sections.forEach(function(s){
      var anyVisible = s.querySelectorAll('.qna-card:not(.hidden):not(.removed)').length > 0;
      s.classList.toggle('hidden', !anyVisible);
    });
    // Hide a month when every day inside is hidden — keeps the filter
    // output compact instead of leaving empty month wrappers behind.
    months.forEach(function(m){
      var anyDay = m.querySelectorAll('.day-section:not(.hidden)').length > 0;
      m.classList.toggle('hidden', !anyDay);
    });
    if (counter) counter.textContent = visible;
  }

  search.addEventListener('input', apply);
  var impFilterBtn = document.getElementById('impfilter');
  if (impFilterBtn) impFilterBtn.addEventListener('click', function(){
    curImportant = !curImportant;
    impFilterBtn.classList.toggle('active', curImportant);
    apply();
  });
  var memoFilterBtn = document.getElementById('memofilter');
  if (memoFilterBtn) memoFilterBtn.addEventListener('click', function(){
    curMemo = !curMemo;
    memoFilterBtn.classList.toggle('active', curMemo);
    apply();
  });
  resetBtn.addEventListener('click', function(){
    search.value = '';
    activeTools.clear();
    chips.forEach(function(c){ c.classList.remove('active'); });
    curImportant = false;
    curMemo = false;
    if (impFilterBtn) impFilterBtn.classList.remove('active');
    if (memoFilterBtn) memoFilterBtn.classList.remove('active');
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

  // Propagate the current search query to the detail page URL so the
  // q-N.html page can jump to the matching keyword in the full answer.
  cards.forEach(function(c){
    var link = c.querySelector('.question a');
    if (!link) return;
    link.addEventListener('click', function(e){
      var q = (search.value || '').trim();
      if (!q) return;  // no query, default link works fine
      var base = link.getAttribute('href');
      if (!base || base.indexOf('?') !== -1) return;
      e.preventDefault();
      location.href = base + '?kw=' + encodeURIComponent(q);
    });
  });

  // Per-card delete: token comes from the URL path so the same value
  // gates both viewing and editing.
  var token = location.pathname.split('/').filter(Boolean)[0] || '';
  document.querySelectorAll('.del-btn').forEach(function(btn){
    btn.addEventListener('click', function(e){
      e.preventDefault();
      e.stopPropagation();
      var id = btn.dataset.id;
      if (!id) return;
      if (!confirm('Q&A #' + id + ' 삭제할까요?')) return;
      fetch('/' + token + '/q-' + id, {method:'DELETE'})
        .then(function(r){
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(function(){
          var card = btn.closest('.qna-card');
          if (!card) return;
          card.classList.add('removing');
          setTimeout(function(){
            card.classList.add('removed');
            card.classList.add('hidden');
            apply();
          }, 250);
        })
        .catch(function(err){
          alert('삭제 실패: ' + err.message);
        });
    });
  });

  // ★ 중요 표시 토글 — optimistic, POST persists server-side, revert on fail.
  document.querySelectorAll('.star-btn').forEach(function(btn){
    btn.addEventListener('click', function(e){
      e.preventDefault(); e.stopPropagation();
      var id = btn.dataset.id; if (!id) return;
      var card = btn.closest('.qna-card'); if (!card) return;
      var now = card.dataset.important !== '1';
      card.dataset.important = now ? '1' : '0';
      btn.textContent = now ? '★' : '☆';
      btn.classList.toggle('on', now);
      apply();
      fetch('/' + token + '/q-' + id + '/important',
        {method:'POST', headers:{'content-type':'application/json'},
         body: JSON.stringify({important: now})})
        .then(function(r){ if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
        .catch(function(err){
          card.dataset.important = now ? '0' : '1';
          btn.textContent = now ? '☆' : '★';
          btn.classList.toggle('on', !now);
          apply();
          alert('중요 표시 변경 실패: ' + err.message);
        });
    });
  });

  // 📝 Q&A 메모 저장 + ⏰ 알람
  document.querySelectorAll('.qa-memo').forEach(function(box){
    var ta=box.querySelector('textarea'), btn=box.querySelector('.memo-save');
    var del=box.querySelector('.memo-del');
    var st=box.querySelector('.memo-status'), id=box.dataset.id;
    if(!ta||!btn||!id)return;
    function save(text,msg){
      btn.disabled=true; if(del)del.disabled=true; if(st)st.textContent='저장 중…';
      fetch('/'+token+'/memo',{method:'POST',
        headers:{'content-type':'application/json'},
        body:JSON.stringify({kind:'qna',id:id,text:text})})
        .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
        .then(function(){ if(st)st.textContent=msg; })
        .catch(function(err){ if(st)st.textContent='실패: '+err.message; })
        .finally(function(){ btn.disabled=false; if(del)del.disabled=false; });
    }
    btn.addEventListener('click',function(e){
      e.preventDefault(); e.stopPropagation(); save(ta.value,'저장됨 ✓'); });
    if(del)del.addEventListener('click',function(e){
      e.preventDefault(); e.stopPropagation(); ta.value=''; save('','삭제됨'); });
  });

  // ── Keyboard navigation: j/k move between visible cards, Enter
  //    expands the focused card inline. Ignored while typing in the
  //    search box so it never fights the bot-query Enter handler.
  var kbdIdx = -1;
  function visibleCards(){
    return Array.prototype.filter.call(cards, function(c){
      return !c.classList.contains('hidden') && c.offsetParent !== null;
    });
  }
  function focusCard(list, idx){
    for (var i = 0; i < list.length; i++) list[i].classList.remove('kbd-focus');
    if (idx < 0 || idx >= list.length) return;
    list[idx].classList.add('kbd-focus');
    list[idx].scrollIntoView({block:'center', behavior:'smooth'});
  }
  document.addEventListener('keydown', function(e){
    var tag = (document.activeElement && document.activeElement.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var list = visibleCards();
    if (!list.length) return;
    if (e.key === 'j'){
      e.preventDefault();
      kbdIdx = kbdIdx < 0 ? 0 : Math.min(kbdIdx + 1, list.length - 1);
      focusCard(list, kbdIdx);
    } else if (e.key === 'k'){
      e.preventDefault();
      kbdIdx = kbdIdx < 0 ? 0 : Math.max(kbdIdx - 1, 0);
      focusCard(list, kbdIdx);
    } else if (e.key === 'Enter' && kbdIdx >= 0 && kbdIdx < list.length){
      e.preventDefault();
      var d = list[kbdIdx].querySelector('details');
      if (d) d.open = !d.open;
    }
  });

  // ── Ask the bot from the search box ──────────────────────────────
  // Typing filters the archive live (apply(), above). Pressing Enter
  // instead SENDS the text to the bot: natural language → a paid Gemini
  // Q&A; a "/command" → a free read-only lookup. The request is parked
  // in dash_queries.db; the bot drains it and we poll for the answer.
  var askPanel = document.getElementById('ask-panel');
  var askPoll = null;
  var askDeadline = 0;
  var askHistory = [];

  function askEsc(s){
    return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }
  function askRenderBody(s){
    var h = askEsc(s);
    h = h.replace(/`([^`]+)`/g, '<code>$1</code>');
    h = h.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
    var lines = h.split('\n'), out = [], inUl = false;
    for (var i = 0; i < lines.length; i++){
      var t = lines[i].replace(/^\s+/, '');
      if (/^[•·] /.test(t)){
        if (!inUl){ out.push('<ul class="ans-bl">'); inUl = true; }
        out.push('<li>' + t.substring(2) + '</li>');
      } else {
        if (inUl){ out.push('</ul>'); inUl = false; }
        out.push(lines[i]);
      }
    }
    if (inUl) out.push('</ul>');
    return out.join('\n');
  }
  function askClose(){
    if (askPoll){ clearInterval(askPoll); askPoll = null; }
    askPanel.classList.add('hidden');
    askPanel.innerHTML = '';
    askHistory.length = 0;
  }
  function askHeader(qText, kindChip){
    return "<div class='ask-q'><span>💬 " + askEsc(qText) + "</span>" +
      (kindChip || '') +
      "<span class='ask-close' title='닫기'>✕</span></div>";
  }
  function askWire(){
    var x = askPanel.querySelector('.ask-close');
    if (x) x.addEventListener('click', askClose);
  }
  function askSpinner(qText, isCmd){
    askPanel.innerHTML = askHeader(qText) + "<div class='ask-spinner'>" +
      (isCmd ? '⏳ 명령 실행 중…'
             : '🤔 생각 중… (자료 검색·답변 생성, 수십 초 걸릴 수 있어요)') +
      "</div>";
    askPanel.classList.remove('hidden');
    askWire();
  }
  function askDone(qText, data){
    var kindChip = "<span class='ask-kind'>" +
      (data.kind === 'command' ? '명령어' : 'Q&amp;A') + "</span>";
    var body;
    if (data.error){
      body = "<div class='ask-err'>⚠️ " + askEsc(data.error) + "</div>";
    } else {
      // Command output is the bot's own Telegram-HTML (a safe <b>/<i>/
      // <code>/<a> subset) — render as-is so tags format instead of
      // showing raw. Q&A answers are plain text + light markdown, so
      // escape first, then apply **bold** / `code`.
      var inner = (data.kind === 'command')
        ? data.answer
        : askRenderBody(data.answer);
      body = "<div class='ask-body'>" + inner + "</div>";
      if (data.sources && data.sources.length){
        var lis = data.sources.map(function(s){
          return '<li>' + askEsc(s) + '</li>'; }).join('');
        body += "<div class='ask-sources'><b>📚 출처</b><ul>" + lis + "</ul></div>";
      }
    }
    askPanel.innerHTML = askHeader(qText, kindChip) + body;
    askPanel.classList.remove('hidden');
    askWire();
  }
  function askSubmit(q){
    if (askPoll){ clearInterval(askPoll); askPoll = null; }
    if (!askPanel.classList.contains('hidden') && askPanel.innerHTML.trim()){
      askHistory.push(askPanel.innerHTML);
    }
    askSpinner(q, q.charAt(0) === '/');
    fetch('/' + token + '/ask', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({q: q})
    }).then(function(r){
      return r.json().then(function(j){ return {ok: r.ok, j: j}; });
    }).then(function(res){
      if (!res.ok || !res.j || !res.j.id){
        askDone(q, {error: (res.j && res.j.error) || '요청 실패'});
        return;
      }
      var id = res.j.id;
      askDeadline = Date.now() + 11 * 60 * 1000;
      askPoll = setInterval(function(){
        if (Date.now() > askDeadline){
          clearInterval(askPoll); askPoll = null;
          askDone(q, {error: '시간 초과. 다시 시도해주세요.'});
          return;
        }
        fetch('/' + token + '/ask/' + id).then(function(r){
          return r.json();
        }).then(function(d){
          if (d.status === 'done' || d.status === 'error'){
            clearInterval(askPoll); askPoll = null;
            askDone(q, d);
          }
        }).catch(function(){ /* transient poll error — keep trying */ });
      }, 1200);
    }).catch(function(err){
      askDone(q, {error: '네트워크 오류: ' + err.message});
    });
  }
  if (search){
    search.addEventListener('keydown', function(e){
      if (e.key === 'Enter'){
        var q = (search.value || '').trim();
        if (!q) return;
        e.preventDefault();
        askSubmit(q);
      }
    });
  }
  window._dashAskSubmit = askSubmit;
  window._dashAskBack = function(){
    if (askHistory.length > 0){
      askPanel.innerHTML = askHistory.pop();
      askPanel.classList.remove('hidden');
      askWire();
    } else {
      askClose();
    }
  };

  // Auto-run command from URL param: ?cmd=/show_xxx
  var params = new URLSearchParams(location.search);
  var autoCmd = params.get('cmd');
  if (autoCmd){
    history.replaceState(null, '', location.pathname);
    setTimeout(function(){ askSubmit(autoCmd); }, 200);
  }
})();
"""

_LINKIFY_JS = r"""
(function(){
  var token = location.pathname.split('/').filter(Boolean)[0] || '';
  var isDetail = /\/q-\d+\.html/.test(location.pathname);
  var isCommands = /\/commands\//.test(location.pathname);
  var showRe = /\/show_[a-f0-9]{6,32}\b/g;
  var cmdRe = /\/([a-z_]+)(?:\s|$)/;

  function linkify(el){
    if (!el) return;
    var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {
      acceptNode: function(n){
        var p = n.parentNode;
        if (p && (p.tagName === 'A' || p.tagName === 'CODE')) return NodeFilter.FILTER_REJECT;
        return showRe.test(n.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      }
    });
    var nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    showRe.lastIndex = 0;
    nodes.forEach(function(node){
      var text = node.nodeValue;
      var re = /\/show_[a-f0-9]{6,32}/g;
      var frag = document.createDocumentFragment();
      var last = 0, m;
      while ((m = re.exec(text)) !== null){
        if (m.index > last)
          frag.appendChild(document.createTextNode(text.slice(last, m.index)));
        var a = document.createElement('a');
        a.href = '#';
        a.className = 'dash-cmd-link';
        a.textContent = m[0];
        a.dataset.cmd = m[0];
        frag.appendChild(a);
        last = m.index + m[0].length;
      }
      if (last < text.length)
        frag.appendChild(document.createTextNode(text.slice(last)));
      if (last > 0) node.parentNode.replaceChild(frag, node);
    });

    // "처음으로" → back to index
    var walker2 = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {
      acceptNode: function(n){
        return /처음으로/.test(n.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      }
    });
    var tnodes = [];
    while (walker2.nextNode()) tnodes.push(walker2.currentNode);
    tnodes.forEach(function(node){
      var text = node.nodeValue;
      var idx = text.indexOf('처음으로');
      if (idx < 0) return;
      var frag = document.createDocumentFragment();
      if (idx > 0) frag.appendChild(document.createTextNode(text.slice(0, idx)));
      var a = document.createElement('a');
      if (!isDetail && !isCommands){
        a.href = '#';
        a.dataset.back = '1';
      } else {
        a.href = '/' + token + '/';
      }
      a.className = 'dash-cmd-link';
      a.textContent = '처음으로';
      frag.appendChild(a);
      var rest = idx + 4;
      if (rest < text.length) frag.appendChild(document.createTextNode(text.slice(rest)));
      node.parentNode.replaceChild(frag, node);
    });
  }

  document.querySelectorAll('.answer').forEach(linkify);
  document.querySelectorAll('.ask-body').forEach(linkify);

  document.addEventListener('click', function(e){
    var link = e.target.closest('.dash-cmd-link');
    if (!link) return;
    if (link.dataset.back){
      e.preventDefault();
      if (window._dashAskBack) window._dashAskBack();
      return;
    }
    if (!link.dataset.cmd) return;
    e.preventDefault();
    var cmd = link.dataset.cmd;
    if (!isDetail && !isCommands && window._dashAskSubmit){
      window._dashAskSubmit(cmd);
    } else {
      location.href = '/' + token + '/?cmd=' + encodeURIComponent(cmd);
    }
  });

  // Also linkify ask panel results when they appear
  var observer = new MutationObserver(function(muts){
    muts.forEach(function(m){
      m.addedNodes.forEach(function(n){
        if (n.nodeType === 1){
          var bodies = n.querySelectorAll ? n.querySelectorAll('.ask-body') : [];
          bodies.forEach(linkify);
          if (n.classList && n.classList.contains('ask-body')) linkify(n);
        }
      });
    });
  });
  var askP = document.getElementById('ask-panel');
  if (askP) observer.observe(askP, {childList: true, subtree: true});
})();
"""


def _format_bullets(escaped: str) -> str:
    """Convert •/· bullet lines in HTML-escaped text to <ul> for
    proper hanging indentation on the dashboard."""
    lines = escaped.split('\n')
    out: list[str] = []
    in_ul = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith('• ') or stripped.startswith('· '):
            if not in_ul:
                out.append('<ul class="ans-bl">')
                in_ul = True
            out.append(f'<li>{stripped[2:]}</li>')
        else:
            if in_ul:
                out.append('</ul>')
                in_ul = False
            out.append(line)
    if in_ul:
        out.append('</ul>')
    return '\n'.join(out)


def _card_data_text(it: dict) -> str:
    """Searchable haystack for the JS filter — lowercased, ascii-safe.
    Intentionally limited to question + answer only. Including tool
    names and source titles in the haystack caused confusing matches
    where a card surfaced because the keyword appeared in a referenced
    document's title (not the visible Q&A text), so the user couldn't
    see why it matched. Tool-based filtering happens through the
    chips instead."""
    parts = [
        it.get("question") or "",
        it.get("answer") or "",
    ]
    return _esc(" ".join(parts).lower())[:5000]


def _card_data_tools(it: dict) -> str:
    """Space-separated bucket names so the JS filter can match by group."""
    buckets = {_tool_bucket(t) for t in (it.get("tools") or [])}
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


def _render_index(rows: list[dict], stats: dict, token: str = "") -> str:
    from . import widgets as _widgets
    try:
        from ..store import marks as _marks
        _qmemos = _marks.memos("qna")
        _qalarms = _marks.alarm_map("qna")
    except Exception:
        _qmemos, _qalarms = {}, {}
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
        f"<style>{_INDEX_CSS}{_widgets.ALARM_CSS}</style>",
        f"<script>{_THEME_SWITCHER_JS}</script>",
        "</head><body><div class='layout'>",
        "<header>",
        "<h1>🧠 Second Brain Archive</h1>",
        "<div class='sub'>카드 클릭 시 전체 리포트 · "
        f"<a href='/{token}/kg/' class='nav-shortcut'>🕸 KG</a> "
        f"<a href='/{token}/wiki/' class='nav-shortcut'>📚 Wiki</a> "
        f"<a href='/{token}/notes/' class='nav-shortcut'>학습 노트</a> "
        f"<a href='/{token}/commands/' class='nav-shortcut'>📋 Commands</a>"
        "</div>",
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
        "<input id='q' type='text' placeholder='질문 / 답변 / 출처 검색...  (Enter = 봇에게 질문 · /명령어 가능)' autocomplete='off'>",
        "<button id='impfilter' class='reset impfilter' type='button'>★ 중요만</button>",
        "<button id='memofilter' class='reset memofilter' type='button'>📝 메모만</button>",
        "<button id='reset' class='reset' type='button'>초기화</button>",
        "</div>",
        "<div class='ask-hint'>타이핑 = 기록 필터 · <b>Enter = 봇에게 질문</b> "
        "(자연어 또는 <code>/usage</code>·<code>/wiki</code> 같은 조회 명령)</div>",
        "<div class='chips'>",
        "<span class='chip brain' data-tool='brain'>🧠 brain</span>",
        "<span class='chip paper' data-tool='paper'>📄 papers</span>",
        "<span class='chip patent' data-tool='patent'>⚖️ patents</span>",
        "<span class='chip report' data-tool='report'>📑 R&amp;D</span>",
        "<span class='chip web' data-tool='web'>🌐 web</span>",
        "<span class='chip ingest' data-tool='ingest'>📥 ingest</span>",
        "</div>",
        "</div>",

        "<div id='ask-panel' class='ask-panel hidden'></div>",

        f"<div class='summary-line'>총 <span id='count'>{len(rows)}</span>건의 Q&amp;A 기록</div>",
    ]

    if not rows:
        parts.append(
            "<div style='text-align:center;padding:80px 20px;color:var(--muted)'>"
            "아직 저장된 Q&amp;A가 없어요. 봇에 질문 한 번 던지면 여기에 쌓입니다."
            "</div>"
        )

    # Group the per-day sections by KST year-month (YYYY-MM, sliced from
    # the day key) so the dashboard can collapse a whole month with one
    # click — useful once the archive crosses several months and the
    # default-open day list grows unwieldy.
    from itertools import groupby
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    _cur_month = _dt.now(_tz(_td(hours=9))).strftime("%Y-%m")
    def _month_of(day: str) -> str:
        return day[:7] if len(day) >= 7 else day
    months_iter = groupby(days_sorted, key=_month_of)
    for month, day_iter in months_iter:
        month_days = list(day_iter)
        month_count = sum(len(grouped[d]) for d in month_days)
        is_current = (month == _cur_month)
        m_open = " open" if is_current else ""
        parts.append(
            f"<details class='month-section'{m_open} data-month='{_esc(month)}'>"
            f"<summary>📅 {_esc(month)}<span class='month-count'>{month_count}건</span></summary>"
            f"<div class='month-body'>"
        )
        for day in month_days:
            items = grouped[day]
            d_open = " open" if is_current else ""
            parts.append(
                f"<details class='day-section'{d_open}>"
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
                    lis = "".join(_source_li(s) for s in srcs)
                    sources_html = (
                        f"<div class='sources'>📚 출처 {len(srcs)}개<ul>{lis}</ul></div>"
                    )
                model_chip = (
                    f"<span style='margin-left:auto'>{_esc(it.get('model'))}</span>"
                    if it.get("model") else ""
                )
                data_text = _card_data_text(it)
                data_tools = _card_data_tools(it)
                # Card accent class from the primary (first) tool's bucket.
                acc = f" acc-{_tool_bucket(tools[0])}" if tools else ""
                del_btn = (
                    f"<button type='button' class='del-btn' "
                    f"data-id='{int(it['id'])}' title='이 Q&A 삭제'>🗑</button>"
                )
                imp = 1 if it.get("important") else 0
                _qmemo_txt = (_qmemos.get(str(it['id'])) or "").strip()
                hasmemo = 1 if _qmemo_txt else 0
                star_btn = (
                    f"<button type='button' class='star-btn{' on' if imp else ''}' "
                    f"data-id='{int(it['id'])}' title='중요 표시 토글'>"
                    f"{'★' if imp else '☆'}</button>"
                )
                parts.append(
                    f"<div class='qna-card{acc}' data-text=\"{data_text}\" "
                    f"data-tools=\"{data_tools}\" data-important=\"{imp}\" "
                    f"data-hasmemo=\"{hasmemo}\" data-memo=\"{_esc(_qmemo_txt)}\">"
                    "<details><summary>"
                    "<div class='row1'>"
                    f"<span>{_esc(_kst_hhmm(it['ts']))}</span>"
                    f"{tool_chips}{model_chip}{star_btn}{del_btn}"
                    "</div>"
                    "<div class='question'>"
                    f"<a href='q-{int(it['id'])}.html'>Q. {_esc(it['question'])}</a>"
                    "</div></summary>"
                    f"{warn}"
                    f"<div class='answer'>{_format_bullets(_esc(it['answer']))}</div>"
                    f"{sources_html}"
                    f"<div class='qa-memo' data-id='{int(it['id'])}'>"
                    "<div class='qa-memo-h'>📝 내 메모</div>"
                    "<textarea placeholder='이 Q&A에 대한 내 생각…'>"
                    f"{_esc(_qmemos.get(str(it['id']), ''))}</textarea>"
                    "<div class='memo-row'>"
                    "<button type='button' class='memo-save'>저장</button>"
                    "<button type='button' class='memo-del'>삭제</button>"
                    "<span class='memo-status'></span></div>"
                    + _widgets.alarm_row("qna", str(it['id']),
                                         _qalarms.get(str(it['id']), {}))
                    + "</div>"
                    "</details></div>"
                )
            parts.append("</div></details>")  # close day-section
        parts.append("</div></details>")      # close month-section

    parts.append(
        f"<div class='footer'>생성: {stats['generated_at']} · "
        f"{stats['total_qna']:,}건 누적 · 무제한 보관</div>"
    )
    parts.append(f"<script>{_INDEX_JS}</script>")
    parts.append(f"<script>{_LINKIFY_JS}</script>")
    parts.append(f"<script>{_widgets.ALARM_JS}</script>")
    parts.append("</div></body></html>")
    return "\n".join(parts)


_QA_MEMO_CSS = """
.qa-memo { margin-top: 18px; padding-top: 14px; border-top: 1px dashed var(--border); }
.qa-memo-h { font-size: 12px; color: var(--muted); font-weight: 600; margin-bottom: 6px; }
.qa-memo textarea { width: 100%; min-height: 60px; background: var(--bg);
  border: 1px solid var(--border); color: var(--text); border-radius: 8px;
  padding: 9px; font-size: 13px; outline: none; resize: vertical; box-sizing: border-box; }
.qa-memo textarea:focus { border-color: var(--primary); }
.memo-row { display: flex; align-items: center; gap: 8px; margin-top: 6px; }
.memo-save { background: var(--primary); border: 0; color: #fff; padding: 5px 14px;
  border-radius: 7px; cursor: pointer; font-size: 12px; font-weight: 600; }
.memo-del { background: rgba(148,163,184,.25); border: 0; color: var(--muted);
  padding: 5px 12px; border-radius: 7px; cursor: pointer; font-size: 12px; font-weight: 600; }
.memo-status { font-size: 11px; color: var(--muted); }
"""


def _render_detail(item: dict, token_dir: str) -> str:
    from . import widgets as _widgets
    _qid = str(item["id"])
    try:
        from ..store import marks as _marks
        _dmemo = _marks.memo("qna", _qid)
        _dalarm = _marks.alarm("qna", _qid)
    except Exception:
        _dmemo, _dalarm = "", {}
    memo_box = (
        f"<div class='qa-memo' data-id='{_qid}'>"
        "<div class='qa-memo-h'>📝 내 메모</div>"
        f"<textarea placeholder='이 Q&A에 대한 내 생각…'>{_esc(_dmemo)}</textarea>"
        "<div class='memo-row'><button type='button' class='memo-save'>저장</button>"
        "<button type='button' class='memo-del'>삭제</button>"
        "<span class='memo-status'></span></div>"
        + _widgets.alarm_row("qna", _qid, _dalarm)
        + "</div>"
    )
    _memo_js = (
        "(function(){var box=document.querySelector('.qa-memo');if(!box)return;"
        "var ta=box.querySelector('textarea'),btn=box.querySelector('.memo-save'),"
        "del=box.querySelector('.memo-del'),"
        "st=box.querySelector('.memo-status'),id=box.dataset.id,"
        "token=location.pathname.split('/').filter(Boolean)[0]||'';"
        "if(!ta||!btn||!id)return;"
        "function save(text,msg){btn.disabled=true;if(del)del.disabled=true;"
        "if(st)st.textContent='저장 중…';"
        "fetch('/'+token+'/memo',{method:'POST',"
        "headers:{'content-type':'application/json'},"
        "body:JSON.stringify({kind:'qna',id:id,text:text})})"
        ".then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})"
        ".then(function(){if(st)st.textContent=msg;})"
        ".catch(function(e){if(st)st.textContent='실패: '+e.message;})"
        ".finally(function(){btn.disabled=false;if(del)del.disabled=false;});}"
        "btn.addEventListener('click',function(){save(ta.value,'저장됨 ✓');});"
        "if(del)del.addEventListener('click',function(){ta.value='';save('','삭제됨');});"
        "})();"
    )
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
        lis = "".join(_source_li(s) for s in srcs)
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
        f"<style>{_DETAIL_CSS}{_widgets.ALARM_CSS}{_QA_MEMO_CSS}</style>",
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
        f"<div class='answer'>{_format_bullets(_esc(item['answer']))}</div>",
        sources_html,
        memo_box,
        "</main>",
        f"<script>{_DETAIL_JS}</script>",
        f"<script>{_LINKIFY_JS}</script>",
        f"<script>{_memo_js}</script>",
        f"<script>{_widgets.ALARM_JS}</script>",
        "</body></html>",
    ])


_PUBLIC_INDEX = """<!DOCTYPE html><html lang='ko'><head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>private</title>
<style>body{background:#0f1419;color:#8b949e;font:14px sans-serif;
text-align:center;padding:40vh 20px}</style>
</head><body>private</body></html>
"""


_COMMANDS_CSS = _BASE_CSS + """
.layout { max-width: 960px; margin: 0 auto; padding: 28px 22px 80px; }
header { margin-bottom: 22px; }
header h1 {
  font-size: 22px; margin: 0 0 4px 0; color: var(--text);
  display: flex; align-items: center; gap: 8px;
}
header .sub { color: var(--muted); font-size: 13px; }
.nav-shortcut {
  display: inline-flex; align-items: center; gap: 3px;
  padding: 2px 10px; border-radius: 12px;
  background: var(--accent); color: #fff !important;
  font-weight: 600; font-size: 13px;
  text-decoration: none !important;
  transition: opacity 0.15s;
}
.nav-shortcut:hover { opacity: 0.85; }
.toc {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 12px; padding: 16px 20px; margin-bottom: 22px;
  box-shadow: var(--shadow);
}
.toc h2 { font-size: 14px; margin: 0 0 10px; color: var(--text); }
.toc ul { margin: 0; padding-left: 18px; }
.toc li { padding: 2px 0; }
.toc a { font-size: 13px; }
.cmd-section {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 12px; margin-bottom: 16px; overflow: hidden;
  box-shadow: var(--shadow);
}
.cmd-section > summary {
  cursor: pointer; list-style: none; padding: 16px 20px;
  display: flex; justify-content: space-between; align-items: center;
  font-weight: 700; font-size: 15px;
  background: var(--panel-alt);
  border-bottom: 1px solid var(--border-soft);
}
.cmd-section > summary::-webkit-details-marker { display: none; }
.cmd-section > summary::before {
  content: "▸ "; color: var(--muted); margin-right: 4px; font-size: 12px;
}
.cmd-section[open] > summary::before { content: "▾ "; }
.cmd-section .section-count {
  color: var(--muted); font-weight: 400; font-size: 13px;
}
.cmd-body { padding: 16px 20px; }
.cmd-entry {
  background: var(--panel-alt); border: 1px solid var(--border-soft);
  border-radius: 10px; padding: 14px 16px; margin-bottom: 10px;
}
.cmd-entry:last-child { margin-bottom: 0; }
.cmd-name {
  font-size: 14px; font-weight: 700; color: var(--primary);
  font-family: monospace;
}
.cmd-desc {
  font-size: 13px; line-height: 1.65; color: var(--text);
  margin-top: 6px; white-space: pre-wrap; word-break: break-word;
}
.cmd-desc code {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 4px; padding: 1px 5px; font-size: 12px;
}
.cmd-badge {
  display: inline-block; font-size: 10px; font-weight: 700;
  padding: 2px 8px; border-radius: 10px; margin-left: 6px;
  vertical-align: middle;
}
.cmd-badge.paid {
  background: rgba(245,158,11,0.15); color: #d97706;
  border: 1px solid rgba(245,158,11,0.3);
}
[data-theme="dark"] .cmd-badge.paid {
  color: #fbbf24;
}
.cmd-badge.mutation {
  background: rgba(239,68,68,0.12); color: #dc2626;
  border: 1px solid rgba(239,68,68,0.25);
}
[data-theme="dark"] .cmd-badge.mutation {
  color: #fca5a5;
}
.cmd-badge.free {
  background: rgba(16,185,129,0.12); color: var(--primary);
  border: 1px solid rgba(16,185,129,0.25);
}
.cmd-try-link {
  color: var(--primary); text-decoration: none;
  border-bottom: 1px dashed currentColor;
  padding-bottom: 1px;
}
.cmd-try-link:hover { opacity: 0.75; text-decoration: none; }
.guide-entry > summary {
  cursor: pointer; list-style: none;
}
.guide-entry > summary::-webkit-details-marker { display: none; }
.guide-hint {
  color: var(--accent); font-size: 12px; font-weight: 600; margin-top: 8px;
}
.guide-entry[open] .guide-hint { display: none; }
.guide-content {
  margin-top: 12px; padding: 16px 18px;
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 8px; font-size: 13px; line-height: 1.7;
  white-space: pre-wrap; word-break: break-word;
}
.guide-content .cmd-try-link { font-size: 13px; }
.guide-content .cmd-badge { font-size: 9px; padding: 1px 6px; vertical-align: middle; }
.footer {
  margin-top: 32px; padding: 16px 0; text-align: center;
  font-size: 11px; color: var(--muted);
  border-top: 1px solid var(--border-soft);
}
"""


def _linkify_guide_cmds(guide_html: str, token: str,
                        paid_commands: frozenset | None = None) -> str:
    """Make /command patterns in guide Telegram HTML clickable.

    Handles both plain ``<b>/cmd args</b>`` and numbered
    ``<b>N) /cmd args · /cmd2 args</b>``.  Paid commands get an inline
    cost badge when *paid_commands* is provided."""
    import re as _re
    paid = paid_commands or frozenset()

    def _cmd_badge(cmd: str) -> str:
        cb = cmd.lstrip("/")
        if cb in paid:
            c = _COST_HINTS.get(cb, _COST_DEFAULT)
            return f" <span class='cmd-badge paid'>{c}</span>"
        return ""

    def _repl_bold(m):
        inner = m.group(1)
        def _repl_cmd(cm):
            raw = cm.group(0)
            if raw.endswith("*"):
                return raw
            link = (f"<a href='/{html.escape(token)}/?cmd={raw}' "
                    f"class='cmd-try-link'>{raw}</a>")
            return link + _cmd_badge(raw)
        return "<b>" + _re.sub(r'/[a-z]\w+\*?', _repl_cmd, inner) + "</b>"

    return _re.sub(r'<b>([^<]*/[a-z]\w+[^<]*)</b>', _repl_bold, guide_html)


_COST_HINTS: dict[str, str] = {
    "deep": "~₩50",
    "compare_papers": "~₩20",
    "web_search": "~₩10",
    "search_my_brain": "~₩10",
    "ingest_url": "~₩10-50",
    "eval": "~₩50+",
    "eval_seed": "~₩50+",
    "wiki_run": "~₩55/토픽",
    "wiki_drain": "~₩55/토픽",
    "wiki_backfill": "₩0 적재",
}
_COST_DEFAULT = "~₩5"


def _render_commands_page(token: str, lookup_guide: str,
                          paid_commands: frozenset,
                          mutation_commands: frozenset,
                          guides: dict[str, str] | None = None) -> str:
    """Render the command reference HTML page from _LOOKUP_GUIDE_TEXT.

    The guide text is Telegram HTML (safe subset: <b>, <code>, <i>, <a>
    plus &lt; &gt; &amp; entities). Descriptions pass through as-is
    since the tag set is already safe for browser rendering."""
    import re as _re
    guides = guides or {}

    section_re = _re.compile(r'═+\s*\n<b>(.+?)</b>\s*\n═+')
    cmd_re = _re.compile(r'<b>(/\w[\w_]*(?:\s+[^<]*)?)</b>')

    sections: list[dict] = []
    raw_sections = section_re.split(lookup_guide)
    i = 1
    while i < len(raw_sections) - 1:
        title = raw_sections[i].strip()
        body = raw_sections[i + 1].strip()
        i += 2
        entries: list[dict] = []
        cmd_parts = cmd_re.split(body)
        j = 1
        while j < len(cmd_parts) - 1:
            cmd_sig = cmd_parts[j].strip()
            desc = cmd_parts[j + 1].strip()
            desc = _re.sub(r'\s*·\s*$', '', desc)
            desc = _re.sub(r'\n\s*•\s*$', '', desc)
            desc = _re.sub(r'^\s*•\s*', '', desc)
            desc = desc.strip()
            first_tok = cmd_sig.split()[0]
            cmd_base = first_tok.lstrip("/").split("&")[0]
            if (_re.search(r'[가-힣]', cmd_sig)
                    and '&lt;' not in cmd_sig
                    and '[' not in cmd_sig
                    and '·' not in cmd_sig):
                j += 2
                continue
            if not desc:
                j += 2
                continue
            is_paid = cmd_base in paid_commands
            is_mutation = cmd_base in mutation_commands
            entries.append({
                "signature": cmd_sig,
                "description": desc,
                "cmd_base": cmd_base,
                "is_paid": is_paid,
                "is_mutation": is_mutation,
            })
            j += 2
        non_cmd_preamble = cmd_parts[0].strip() if cmd_parts else ""
        if entries or non_cmd_preamble:
            sections.append({
                "title": title,
                "preamble": non_cmd_preamble,
                "entries": entries,
            })

    toc_items = []
    for idx, sec in enumerate(sections):
        anchor = f"sec-{idx}"
        sec["anchor"] = anchor
        n = len(sec["entries"])
        toc_items.append(
            f"<li><a href='#{anchor}'>{sec['title']}</a> "
            f"<span style='color:var(--muted);font-size:11px'>"
            f"({n})</span></li>"
        )

    parts = [
        "<!DOCTYPE html><html lang='ko'><head>",
        "<meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>📋 명령어 레퍼런스</title>",
        f"<style>{_COMMANDS_CSS}</style>",
        f"<script>{_THEME_SWITCHER_JS}</script>",
        "</head><body><div class='layout'>",
        "<header>",
        "<h1>📋 명령어 레퍼런스</h1>",
        "<div class='sub'>전체 명령어 상세 가이드 · "
        f"<a href='/{token}/' class='nav-shortcut'>🧠 Archive</a> "
        f"<a href='/{token}/kg/' class='nav-shortcut'>🕸 KG</a> "
        f"<a href='/{token}/wiki/' class='nav-shortcut'>📚 Wiki</a> "
        f"<a href='/{token}/notes/' class='nav-shortcut'>학습 노트</a>"
        "</div>",
        "</header>",
        "<div class='toc'>",
        "<h2>📑 목차</h2>",
        f"<ul>{''.join(toc_items)}</ul>",
        "</div>",
    ]

    for sec in sections:
        paid_n = sum(1 for e in sec["entries"] if e["is_paid"])
        mut_n = sum(1 for e in sec["entries"] if e["is_mutation"])
        count_label = f"{len(sec['entries'])}개"
        tags = []
        if paid_n:
            tags.append(f"💰 {paid_n}")
        if mut_n:
            tags.append(f"⚠️ {mut_n}")
        if tags:
            count_label += " · " + " · ".join(tags)
        parts.append(
            f"<details class='cmd-section' open id='{sec['anchor']}'>"
            f"<summary>{sec['title']}"
            f"<span class='section-count'>{count_label}</span></summary>"
            f"<div class='cmd-body'>"
        )
        if sec.get("preamble"):
            parts.append(
                f"<div class='cmd-desc' style='margin-bottom:12px'>"
                f"{sec['preamble']}</div>"
            )
        for entry in sec["entries"]:
            cb = entry["cmd_base"]
            cost = _COST_HINTS.get(cb, _COST_DEFAULT)
            badge = ""
            if entry["is_paid"] and entry["is_mutation"]:
                badge = (f"<span class='cmd-badge paid'>💰 {cost}</span>"
                         "<span class='cmd-badge mutation'>⚠️ 변경</span>")
            elif entry["is_paid"]:
                badge = f"<span class='cmd-badge paid'>💰 {cost}</span>"
            elif entry["is_mutation"]:
                badge = "<span class='cmd-badge mutation'>⚠️ 변경</span>"
            else:
                badge = "<span class='cmd-badge free'>무료</span>"
            sig = entry['signature']
            ftok = sig.split()[0]
            rest_sig = sig[len(ftok):]
            guide_key = cb
            guide_html = guides.get(guide_key, "")
            if guide_html:
                linked_guide = _linkify_guide_cmds(
                    guide_html, token, paid_commands=paid_commands)
                parts.append(
                    f"<details class='cmd-entry guide-entry'>"
                    f"<summary>"
                    f"<div class='cmd-name'>{ftok}{rest_sig}{badge}</div>"
                    f"<div class='cmd-desc'>{entry['description']}</div>"
                    f"<div class='guide-hint'>📖 클릭하여 가이드 펼치기</div>"
                    f"</summary>"
                    f"<div class='guide-content'>{linked_guide}</div>"
                    f"</details>"
                )
            elif cb == "guide_lookup":
                parts.append(
                    f"<div class='cmd-entry'>"
                    f"<div class='cmd-name'>{ftok}{rest_sig}{badge}</div>"
                    f"<div class='cmd-desc'>{entry['description']} "
                    f"(이 페이지)</div>"
                    f"</div>"
                )
            else:
                cmd_href = f"/{_esc(token)}/?cmd={ftok}"
                parts.append(
                    f"<div class='cmd-entry'>"
                    f"<div class='cmd-name'>"
                    f"<a href='{cmd_href}' class='cmd-try-link' title='실행해보기'>{ftok}</a>"
                    f"{rest_sig}{badge}</div>"
                    f"<div class='cmd-desc'>{entry['description']}</div>"
                    f"</div>"
                )
        parts.append("</div></details>")

    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    gen_at = _dt.now(_tz(_td(hours=9))).strftime("%Y-%m-%d %H:%M")
    total_cmds = sum(len(s["entries"]) for s in sections)
    parts.append(
        f"<div class='footer'>생성: {gen_at} · "
        f"{total_cmds}개 명령어</div>"
    )
    parts.append("</div></body></html>")
    return "\n".join(parts)


# Set False after the first successful run per process so we do ONE
# full detail-page rebuild on (re)start (propagating any template/token
# change) and then only emit new pages incrementally.
_FIRST_RUN = True


def regenerate() -> None:
    """Rewrite the static HTML tree under data/dashboard/.
    Returns silently — never raises so a render bug can't kill ingest.

    MUST be called off the event loop (callers use asyncio.to_thread):
    it does SQLite reads + a Chroma count over the full corpus + HTML
    file writes. Running it on the loop froze the bot for ~60s every
    tick.

    Two cost fixes vs the old version:
      • Non-blocking lock — overlapping ticks (scheduled + post-command)
        skip instead of piling up or blocking the caller.
      • Incremental detail pages — a Q&A detail page is immutable once
        recorded, so after the first full build per process we only
        write detail files that don't exist yet. The old code rewrote
        ALL ~2000 detail files every 60s tick, which is what pegged the
        loop."""
    global _FIRST_RUN
    token = (os.getenv("DASHBOARD_TOKEN", "") or "").strip()
    if not token:
        log.warning("DASHBOARD_TOKEN unset; static dashboard skipped")
        return
    if not _LOCK.acquire(blocking=False):
        log.debug("dashboard regenerate already running; skipping this tick")
        return
    try:
        base = Path(config.DATA_DIR) / "dashboard"
        target = base / token
        target.mkdir(parents=True, exist_ok=True)
        (base / "index.html").write_text(_PUBLIC_INDEX, encoding="utf-8")
        # Populate the source-URL cache ONCE per regenerate (single SQL
        # scan) so _source_li resolves links by dict lookup instead of a
        # per-source LIKE query. This is the fix for the worker thread
        # that sat at ~100% CPU 24/7 (py-spy: regenerate → _source_li →
        # meta.search_title on every card's every source, every 60 s).
        global _SOURCE_URL_CACHE
        try:
            _SOURCE_URL_CACHE = meta_store.title_url_map()
        except Exception:
            log.exception("title_url_map failed; source links degraded")
            _SOURCE_URL_CACHE = {}
        rows = qna.recent(limit=2000)
        today = cost.today_krw()
        mtd = cost.month_to_date_krw()
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        generated_at = _dt.now(_tz(_td(hours=9))).strftime("%Y-%m-%d %H:%M")
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
            "generated_at": generated_at,
        }
        # Atomic index write so the http.server never serves a
        # half-written page. pid-suffixed tmp: the bot and the dashboard
        # server both call regenerate() — with a shared tmp name one
        # process's os.replace could consume the other's half-written tmp.
        pid = os.getpid()
        idx = target / "index.html"
        tmp = target / f"index.html.{pid}.tmp"
        tmp.write_text(_render_index(rows, stats, token=token), encoding="utf-8")
        os.replace(tmp, idx)
        # Detail pages: full rebuild only on first run after (re)start;
        # afterwards just the ids whose file is missing (new Q&As).
        full = _FIRST_RUN
        written = 0
        for it in rows:
            fname = target / f"q-{int(it['id'])}.html"
            if full or not fname.exists():
                dtmp = target / f"{fname.name}.{pid}.tmp"
                dtmp.write_text(_render_detail(it, token), encoding="utf-8")
                os.replace(dtmp, fname)
                written += 1
        _FIRST_RUN = False
        if written:
            log.info("dashboard: wrote %d detail page(s)%s", written,
                     " (full rebuild)" if full else "")
        try:
            from .wiki_render import render_wiki
            wiki_pages = render_wiki(token)
            if wiki_pages:
                log.info("dashboard: wrote %d wiki page(s)", wiki_pages)
        except Exception:
            log.exception("dashboard wiki render failed (non-fatal)")
        try:
            from .notes_render import render_notes
            note_pages = render_notes(token)
            if note_pages:
                log.info("dashboard: wrote %d note page(s)", note_pages)
        except Exception:
            log.exception("dashboard notes render failed (non-fatal)")
        try:
            from .kg_render import render_kg
            if render_kg(token):
                log.info("dashboard: wrote KG view")
        except Exception:
            log.exception("dashboard kg render failed (non-fatal)")
        try:
            import sys
            bot_mod = sys.modules.get("src.bot")
            if bot_mod is None:
                try:
                    from .. import bot as bot_mod
                except Exception:
                    bot_mod = None
            if bot_mod and hasattr(bot_mod, "_LOOKUP_GUIDE_TEXT"):
                guide = getattr(bot_mod, "_LOOKUP_GUIDE_TEXT", "")
                paid = getattr(bot_mod, "_DASH_PAID_COMMANDS", frozenset())
                mutation = getattr(bot_mod, "_DASH_MUTATION_COMMANDS", frozenset())
                guide_texts = {}
                for attr, key in (
                    ("_PATENTS_GUIDE_TEXT", "patents_guide"),
                    ("_PAPERS_GUIDE_TEXT", "papers_guide"),
                    ("_WIKI_GUIDE_TEXT", "wiki_guide"),
                ):
                    txt = getattr(bot_mod, attr, "")
                    if txt:
                        guide_texts[key] = txt
                cmd_dir = target / "commands"
                cmd_dir.mkdir(parents=True, exist_ok=True)
                cmd_html = _render_commands_page(
                    token, guide, paid, mutation, guides=guide_texts)
                cmd_tmp = cmd_dir / f"index.html.{pid}.tmp"
                cmd_idx = cmd_dir / "index.html"
                cmd_tmp.write_text(cmd_html, encoding="utf-8")
                os.replace(cmd_tmp, cmd_idx)
            else:
                log.warning("commands page: bot module not available yet")
        except Exception:
            log.exception("dashboard commands page render failed (non-fatal)")
    except Exception:
        log.exception("dashboard regenerate failed")
    finally:
        _LOCK.release()
