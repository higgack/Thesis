"""Wikipedia / Namuwiki-style static HTML renderer for LLM Wiki pages.

Reads wiki markdown pages from the Obsidian vault and wiki_index.json,
renders them into clean static HTML under data/dashboard/<token>/wiki/.
Called from regenerate.regenerate() so it stays in sync with the rest of
the dashboard.
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

# ---------------------------------------------------------------------------
# Lightweight markdown → HTML (handles what wiki merge pages actually use)
# ---------------------------------------------------------------------------

def _md_to_html(md: str, topic_set: set[str] | None = None,
                source_url_map: dict[str, str] | None = None,
                source_date_map: dict[str, str] | None = None,
                ) -> tuple[str, list[dict], list[dict]]:
    """Convert wiki markdown to HTML. Returns (html_str, toc_entries, footnotes).
    toc_entries: [{level, id, text}, ...] for TOC sidebar.
    footnotes: [{id, title, url, date}, ...] for source footnotes."""
    lines = (md or "").split("\n")
    out: list[str] = []
    toc: list[dict] = []
    _topics = topic_set or set()
    _urls = source_url_map or {}
    _dates = source_date_map or {}
    _footnotes: list[dict] = []
    _fn_seen: dict[str, int] = {}
    in_list = False
    in_code = False
    in_blockquote = False
    bq_buf: list[str] = []

    def _flush_bq():
        nonlocal in_blockquote
        if bq_buf:
            out.append('<blockquote class="wiki-bq">'
                       + "<br>".join(bq_buf) + "</blockquote>")
            bq_buf.clear()
        in_blockquote = False

    def _flush_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    def _make_footnote(title: str) -> str:
        """Register a source and return a superscript footnote link."""
        key = html.unescape(title.strip())
        if key in _fn_seen:
            num = _fn_seen[key]
        else:
            num = len(_footnotes) + 1
            _fn_seen[key] = num
            url = _urls.get(key, "")
            date = _dates.get(key, "")
            _footnotes.append({"id": num, "title": key, "url": url, "date": date})
        return (f'<sup class="wiki-fn"><a href="#fn-{num}" '
                f'title="{html.escape(key)}">[{num}]</a></sup>')

    def _topic_link(name: str) -> str:
        """If name matches a wiki topic, return a link; else plain text."""
        clean = html.unescape(name)
        if clean in _topics:
            return (f'<a href="{_topic_filename(clean)}" '
                    f'class="wiki-internal">{html.escape(clean)}</a>')
        return html.escape(clean)

    def _inline(text: str) -> str:
        t = html.escape(text)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"\*(.+?)\*", r"<em>\1</em>", t)
        t = re.sub(r"`(.+?)`", r'<code class="wiki-code">\1</code>', t)
        t = re.sub(
            r"\(출처:\s*(.+?)\)",
            lambda m: "".join(
                _make_footnote(s)
                for s in re.findall(r"\[\[(.+?)\]\]", m.group(1))
            ) or m.group(0),
            t,
        )
        t = re.sub(
            r"—\s*출처:\s*(.+)",
            lambda m: "".join(
                _make_footnote(s)
                for s in re.findall(r"\[\[(.+?)\]\]", m.group(1))
            ) or m.group(0),
            t,
        )
        t = re.sub(
            r"\[\[(.+?)\]\]",
            lambda m: _topic_link(m.group(1)),
            t,
        )
        for topic in sorted(_topics, key=len, reverse=True):
            if len(topic) < 2:
                continue
            escaped = html.escape(topic)
            if escaped in t:
                link = (f'<a href="{_topic_filename(topic)}" '
                        f'class="wiki-internal">{escaped}</a>')
                t = t.replace(escaped, link, 1)
        return t

    for raw in lines:
        line = raw.rstrip()

        if line.startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                _flush_bq()
                _flush_list()
                lang = line[3:].strip()
                out.append(f'<pre class="wiki-pre"><code data-lang="{html.escape(lang)}">')
                in_code = True
            continue
        if in_code:
            out.append(html.escape(line))
            continue

        if line.startswith("> "):
            _flush_list()
            bq_buf.append(_inline(line[2:]))
            in_blockquote = True
            continue
        elif in_blockquote:
            _flush_bq()

        hm = re.match(r"^(#{1,4})\s+(.+)$", line)
        if hm:
            _flush_list()
            level = len(hm.group(1))
            text = hm.group(2).strip()
            slug = re.sub(r"[^\w가-힣]+", "-", text).strip("-").lower()
            if not slug:
                slug = f"s{len(toc)}"
            toc.append({"level": level, "id": slug, "text": text})
            out.append(
                f'<h{level} id="{slug}" class="wiki-h">'
                f'{_inline(text)}'
                f'<a href="#{slug}" class="wiki-anchor">#</a>'
                f'</h{level}>'
            )
            continue

        if re.match(r"^\s*[-*]\s+", line):
            content = re.sub(r"^\s*[-*]\s+", "", line)
            if not in_list:
                out.append('<ul class="wiki-ul">')
                in_list = True
            out.append(f"<li>{_inline(content)}</li>")
            continue
        elif in_list:
            _flush_list()

        if line.startswith("---") or line.startswith("***"):
            out.append("<hr>")
            continue

        stripped = line.strip()
        if not stripped:
            out.append('<div class="wiki-spacer"></div>')
            continue

        out.append(f"<p>{_inline(stripped)}</p>")

    _flush_bq()
    _flush_list()
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out), toc, _footnotes


# ---------------------------------------------------------------------------
# CSS — Wikipedia / Namuwiki inspired
# ---------------------------------------------------------------------------

_WIKI_CSS = """
:root {
  --bg: #f8f9fa; --panel: #ffffff; --panel-alt: #f6f6f6;
  --border: #a2a9b1; --border-light: #eaecf0;
  --text: #202122; --muted: #54595d; --accent: #3366cc;
  --link: #0645ad; --link-visited: #0b0080;
  --toc-bg: #f8f9fa; --toc-border: #a2a9b1;
  --bq-bg: #f0f4f8; --bq-border: #3366cc;
  --code-bg: #f5f5f5;
  --highlight: #fff8dc;
  --shadow: 0 1px 3px rgba(0,0,0,0.08);
  --header-border: #a2a9b1;
}
[data-theme="dark"] {
  --bg: #101418; --panel: #1a1e24; --panel-alt: #22272e;
  --border: #444c56; --border-light: #30363d;
  --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff;
  --link: #58a6ff; --link-visited: #a5d6ff;
  --toc-bg: #161b22; --toc-border: #30363d;
  --bq-bg: #161b22; --bq-border: #58a6ff;
  --code-bg: #161b22;
  --highlight: #2d333b;
  --shadow: 0 1px 3px rgba(0,0,0,0.4);
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0;
  font: 15px/1.7 "Noto Serif KR", Georgia, "Times New Roman",
        "Apple SD Gothic Neo", serif;
  background: var(--bg); color: var(--text);
  -webkit-font-smoothing: antialiased;
}
a { color: var(--link); text-decoration: none; }
a:visited { color: var(--link-visited); }
a:hover { text-decoration: underline; }

/* ── Layout ───────────────────────────────── */
.wiki-layout {
  display: flex; max-width: 1200px; margin: 0 auto;
  padding: 0 16px;
}
.wiki-sidebar {
  width: 240px; flex-shrink: 0;
  position: sticky; top: 16px; align-self: flex-start;
  max-height: calc(100vh - 32px); overflow-y: auto;
  padding: 16px 0 16px 0; margin-right: 24px;
}
.wiki-main { flex: 1; min-width: 0; padding: 20px 0 80px; }

/* ── Top bar ──────────────────────────────── */
.wiki-topbar {
  background: var(--panel); border-bottom: 1px solid var(--border-light);
  padding: 10px 24px; position: sticky; top: 0; z-index: 100;
  display: flex; align-items: center; gap: 16px;
  box-shadow: var(--shadow);
}
.wiki-topbar .logo {
  font-size: 20px; font-weight: 700; color: var(--text);
  text-decoration: none; letter-spacing: -0.5px;
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
}
.wiki-topbar .logo:hover { text-decoration: none; }
.wiki-search {
  flex: 1; max-width: 400px;
  padding: 6px 14px; border: 1px solid var(--border);
  border-radius: 20px; font-size: 14px;
  background: var(--bg); color: var(--text);
  outline: none; transition: border-color 0.2s;
}
.wiki-search:focus { border-color: var(--accent); }
.wiki-topbar .nav-link {
  font-size: 13px; color: var(--muted);
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
}

/* ── Article ──────────────────────────────── */
.wiki-article {
  background: var(--panel); border: 1px solid var(--border-light);
  border-radius: 4px; padding: 32px 40px;
  box-shadow: var(--shadow);
}
.wiki-article h1 {
  font-size: 28px; font-weight: 700;
  border-bottom: 1px solid var(--header-border);
  padding-bottom: 8px; margin: 0 0 16px 0;
}
.wiki-h {
  border-bottom: 1px solid var(--border-light);
  padding-bottom: 4px; margin: 28px 0 12px 0;
  position: relative;
}
h2.wiki-h { font-size: 22px; }
h3.wiki-h { font-size: 18px; border-bottom: none; }
h4.wiki-h { font-size: 15px; border-bottom: none; }
.wiki-anchor {
  font-size: 14px; color: var(--muted); opacity: 0;
  margin-left: 6px; transition: opacity 0.15s;
  font-weight: 400;
}
.wiki-h:hover .wiki-anchor { opacity: 1; }
.wiki-bq {
  margin: 12px 0; padding: 10px 16px;
  border-left: 4px solid var(--bq-border);
  background: var(--bq-bg); border-radius: 0 4px 4px 0;
  font-style: italic; color: var(--muted);
}
.wiki-ul { margin: 8px 0 8px 20px; padding: 0; }
.wiki-ul li { margin: 3px 0; }
.wiki-pre {
  background: var(--code-bg); border: 1px solid var(--border-light);
  border-radius: 4px; padding: 12px 16px;
  overflow-x: auto; font-size: 13px; line-height: 1.5;
}
.wiki-code {
  background: var(--code-bg); padding: 1px 5px;
  border-radius: 3px; font-size: 13px;
}
.wiki-ref {
  background: var(--highlight); padding: 0 3px;
  border-radius: 2px; font-size: 13px;
}
.wiki-internal {
  color: var(--link); border-bottom: 1px dotted var(--link);
}
.wiki-internal:hover { border-bottom-style: solid; }
.wiki-fn a {
  color: var(--accent); font-size: 11px; font-weight: 600;
  text-decoration: none; vertical-align: super;
}
.wiki-fn a:hover { text-decoration: underline; }
.wiki-footnotes {
  margin-top: 32px; padding-top: 16px;
  border-top: 1px solid var(--border);
}
.wiki-footnotes ol {
  margin: 8px 0 0 20px; padding: 0;
  font-size: 13px; line-height: 1.7;
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
}
.wiki-footnotes li { margin: 4px 0; }
.wiki-footnotes li:target { background: var(--highlight); }
.fn-date { color: var(--muted); font-size: 12px; white-space: nowrap; }
.wiki-spacer { height: 8px; }
.wiki-article p { margin: 6px 0; }

/* ── Table of Contents ────────────────────── */
.wiki-toc {
  background: var(--toc-bg); border: 1px solid var(--toc-border);
  border-radius: 4px; padding: 14px 18px; margin-bottom: 20px;
}
.wiki-toc-title {
  font: 700 14px/1.4 -apple-system, BlinkMacSystemFont, sans-serif;
  margin: 0 0 8px 0; color: var(--text);
}
.wiki-toc ul {
  list-style: none; margin: 0; padding: 0;
}
.wiki-toc li { margin: 2px 0; }
.wiki-toc a {
  font: 13px/1.5 -apple-system, BlinkMacSystemFont, sans-serif;
  color: var(--link); display: block;
  padding: 1px 0; border-radius: 3px;
}
.wiki-toc a:hover { background: var(--highlight); }
.wiki-toc .toc-2 { padding-left: 0; }
.wiki-toc .toc-3 { padding-left: 16px; font-size: 12px; }
.wiki-toc .toc-4 { padding-left: 32px; font-size: 12px; color: var(--muted); }

/* ── Sidebar nav (index page) ─────────────── */
.wiki-nav { list-style: none; margin: 0; padding: 0; }
.wiki-nav li { margin: 1px 0; }
.wiki-nav a {
  display: block; padding: 5px 10px; border-radius: 4px;
  font: 14px/1.4 -apple-system, BlinkMacSystemFont, sans-serif;
  color: var(--text); white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; transition: background 0.1s;
}
.wiki-nav a:hover { background: var(--highlight); text-decoration: none; }
.wiki-nav a.active { background: var(--accent); color: #fff; }
.wiki-nav .nav-count {
  font-size: 11px; color: var(--muted); margin-left: 4px;
}

/* ── Index page grid ──────────────────────── */
.wiki-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 16px;
}
.wiki-card {
  background: var(--panel); border: 1px solid var(--border-light);
  border-radius: 6px; padding: 20px 24px;
  box-shadow: var(--shadow);
  transition: box-shadow 0.15s, border-color 0.15s;
}
.wiki-card:hover {
  border-color: var(--accent);
  box-shadow: 0 2px 8px rgba(0,0,0,0.1);
}
.wiki-card h3 { margin: 0 0 6px 0; font-size: 17px; }
.wiki-card h3 a { color: var(--text); }
.wiki-card h3 a:hover { color: var(--accent); }
.wiki-card .card-meta {
  font: 12px/1.4 -apple-system, BlinkMacSystemFont, sans-serif;
  color: var(--muted); margin-bottom: 8px;
}
.wiki-card .card-excerpt {
  font-size: 13px; color: var(--muted); line-height: 1.5;
  overflow: hidden; display: -webkit-box;
  -webkit-line-clamp: 3; -webkit-box-orient: vertical;
}

/* ── Metadata infobox ─────────────────────── */
.wiki-infobox {
  float: right; width: 260px; margin: 0 0 16px 24px;
  background: var(--toc-bg); border: 1px solid var(--toc-border);
  border-radius: 4px; font-size: 13px;
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
}
.wiki-infobox caption, .wiki-infobox .ib-title {
  background: var(--accent); color: #fff; padding: 8px 14px;
  font-weight: 700; font-size: 14px; border-radius: 4px 4px 0 0;
  display: block;
}
.wiki-infobox table { width: 100%; border-collapse: collapse; }
.wiki-infobox td {
  padding: 5px 14px; border-bottom: 1px solid var(--border-light);
  vertical-align: top;
}
.wiki-infobox td:first-child { font-weight: 600; width: 80px; color: var(--muted); }

/* ── Stats bar ────────────────────────────── */
.wiki-stats {
  display: flex; gap: 24px; flex-wrap: wrap;
  margin-bottom: 24px; padding: 16px 20px;
  background: var(--panel); border: 1px solid var(--border-light);
  border-radius: 6px; box-shadow: var(--shadow);
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
}
.wiki-stat-item .stat-value {
  font-size: 24px; font-weight: 700; color: var(--accent);
}
.wiki-stat-item .stat-label {
  font-size: 12px; color: var(--muted);
}

/* ── Mobile ───────────────────────────────── */
@media (max-width: 768px) {
  .wiki-sidebar { display: none; }
  .wiki-article { padding: 20px 16px; }
  .wiki-infobox { float: none; width: 100%; margin: 0 0 16px 0; }
  .wiki-grid { grid-template-columns: 1fr; }
  .wiki-topbar { padding: 8px 12px; }
}
"""

_THEME_JS = """
(function(){
  function t(){
    var h=parseInt(new Date().toLocaleString('en-US',
      {timeZone:'Asia/Seoul',hour:'numeric',hour12:false}),10);
    document.documentElement.dataset.theme=(h>=19||h<7)?'dark':'light';
  }
  t(); setInterval(t,60000);
})();
"""

_SEARCH_JS = """
(function(){
  var input = document.getElementById('wiki-search');
  if (!input) return;
  var cards = document.querySelectorAll('.wiki-card');
  input.addEventListener('input', function(){
    var q = this.value.toLowerCase().trim();
    cards.forEach(function(c){
      var text = c.textContent.toLowerCase();
      c.style.display = (!q || text.indexOf(q) >= 0) ? '' : 'none';
    });
  });
})();
"""


# ---------------------------------------------------------------------------
# Page builders
# ---------------------------------------------------------------------------

def _head(title: str, extra_css: str = "") -> str:
    return (
        '<!DOCTYPE html><html lang="ko"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)} — LLM Wiki</title>"
        f"<style>{_WIKI_CSS}{extra_css}</style>"
        f"<script>{_THEME_JS}</script>"
        "</head>"
    )


def _topbar(token: str, current: str = "") -> str:
    return (
        '<div class="wiki-topbar">'
        f'<a href="/{token}/wiki/" class="logo">LLM Wiki</a>'
        '<input type="text" id="wiki-search" class="wiki-search" '
        'placeholder="Search topics...">'
        f'<a href="/{token}/" class="nav-link">Q&A Archive</a>'
        "</div>"
    )


def _build_toc_html(toc: list[dict]) -> str:
    if len(toc) < 3:
        return ""
    items = []
    for entry in toc:
        lvl = entry["level"]
        if lvl > 4:
            continue
        items.append(
            f'<li><a href="#{entry["id"]}" class="toc-{lvl}">'
            f'{html.escape(entry["text"])}</a></li>'
        )
    return (
        '<nav class="wiki-toc">'
        '<div class="wiki-toc-title">Contents</div>'
        f'<ul>{"".join(items)}</ul>'
        "</nav>"
    )


def _build_infobox(topic: str, meta: dict) -> str:
    doc_count = len(meta.get("doc_ids") or [])
    updated = (meta.get("updated") or "")[:16].replace("T", " ")
    claims = meta.get("claims", 0)
    return (
        '<div class="wiki-infobox">'
        f'<div class="ib-title">{html.escape(topic)}</div>'
        "<table>"
        f"<tr><td>Updated</td><td>{html.escape(updated) or '—'}</td></tr>"
        f"<tr><td>Sources</td><td>{doc_count}</td></tr>"
        f"<tr><td>Claims</td><td>{claims}</td></tr>"
        "</table></div>"
    )


def _build_footnotes_html(footnotes: list[dict]) -> str:
    if not footnotes:
        return ""
    items = []
    for fn in footnotes:
        title = html.escape(fn["title"])
        url = fn.get("url", "")
        date = fn.get("date", "")
        if url:
            link = f'<a href="{html.escape(url)}" target="_blank">{title}</a>'
        else:
            link = title
        if date:
            link += f' <span class="fn-date">({html.escape(date)})</span>'
        items.append(f'<li id="fn-{fn["id"]}">{link}</li>')
    return (
        '<div class="wiki-footnotes">'
        '<h3 class="wiki-h">Sources</h3>'
        f'<ol>{"".join(items)}</ol>'
        "</div>"
    )


def _render_topic_page(topic: str, page_md: str, meta: dict,
                       token: str, all_topics: list[str],
                       source_url_map: dict[str, str] | None = None,
                       source_date_map: dict[str, str] | None = None,
                       ) -> str:
    topic_set = set(all_topics) - {topic}
    body_html, toc, footnotes = _md_to_html(
        page_md, topic_set=topic_set, source_url_map=source_url_map,
        source_date_map=source_date_map,
    )
    toc_html = _build_toc_html(toc)
    infobox = _build_infobox(topic, meta)
    fn_html = _build_footnotes_html(footnotes)

    nav_items = []
    for t in sorted(all_topics):
        active = ' class="active"' if t == topic else ""
        nav_items.append(
            f'<li><a href="{_topic_filename(t)}"{active}>'
            f"{html.escape(t)}</a></li>"
        )
    sidebar_nav = f'<ul class="wiki-nav">{"".join(nav_items)}</ul>'

    return (
        f"{_head(topic)}<body>"
        f"{_topbar(token, topic)}"
        '<div class="wiki-layout">'
        f'<aside class="wiki-sidebar">{sidebar_nav}</aside>'
        '<div class="wiki-main">'
        f'<article class="wiki-article">'
        f"<h1>{html.escape(topic)}</h1>"
        f"{infobox}{toc_html}{body_html}{fn_html}"
        "</article></div></div>"
        f"<script>{_SEARCH_JS}</script>"
        "</body></html>"
    )


def _render_index_page(topics_data: list[dict], token: str,
                       wiki_stats: dict) -> str:
    total_pages = wiki_stats.get("pages", 0)
    total_docs = wiki_stats.get("docs", 0)
    queue = wiki_stats.get("queue", 0)
    today_cost = wiki_stats.get("today_cost", 0)
    budget = wiki_stats.get("budget", 2000)

    stats_html = (
        '<div class="wiki-stats">'
        '<div class="wiki-stat-item">'
        f'<div class="stat-value">{total_pages}</div>'
        '<div class="stat-label">Wiki Pages</div></div>'
        '<div class="wiki-stat-item">'
        f'<div class="stat-value">{total_docs:,}</div>'
        '<div class="stat-label">Sources Integrated</div></div>'
        '<div class="wiki-stat-item">'
        f'<div class="stat-value">{queue:,}</div>'
        '<div class="stat-label">Queue Pending</div></div>'
        '<div class="wiki-stat-item">'
        f'<div class="stat-value">{today_cost:,.0f}/{budget:,.0f}</div>'
        '<div class="stat-label">Today (KRW)</div></div>'
        "</div>"
    )

    cards = []
    for td in topics_data:
        topic = td["topic"]
        excerpt = html.escape(td.get("excerpt", "")[:200])
        doc_count = td.get("docs", 0)
        updated = (td.get("updated") or "")[:10]
        cards.append(
            '<div class="wiki-card">'
            f'<h3><a href="{_topic_filename(topic)}">'
            f"{html.escape(topic)}</a></h3>"
            f'<div class="card-meta">'
            f"{doc_count} sources · {updated}</div>"
            f'<div class="card-excerpt">{excerpt}</div>'
            "</div>"
        )

    return (
        f'{_head("LLM Wiki")}<body>'
        f"{_topbar(token)}"
        '<div style="max-width:1200px;margin:0 auto;padding:24px 16px 80px">'
        "<h1 style=\"font:700 28px/1.3 -apple-system,sans-serif;"
        "margin:0 0 20px\">LLM Wiki</h1>"
        f"{stats_html}"
        f'<div class="wiki-grid">{"".join(cards)}</div>'
        "</div>"
        f"<script>{_SEARCH_JS}</script>"
        "</body></html>"
    )


def _topic_filename(topic: str) -> str:
    slug = re.sub(r"[^\w가-힣\-]+", "_", topic).strip("_")
    return f"{slug}.html"


# ---------------------------------------------------------------------------
# Public entry point (called from regenerate.py)
# ---------------------------------------------------------------------------

def render_wiki(token: str) -> int:
    """Generate all wiki HTML pages. Returns number of pages written."""
    from ..store import wiki

    if not wiki.enabled():
        return 0

    wiki_dir = None
    if config.OBSIDIAN_VAULT_PATH:
        wiki_dir = Path(config.OBSIDIAN_VAULT_PATH).resolve() / "SecondBrain" / "Wiki"

    if not wiki_dir or not wiki_dir.exists():
        return 0

    idx_path = config.DATA_DIR / "wiki_index.json"
    idx: dict = {}
    if idx_path.exists():
        try:
            idx = json.loads(idx_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    source_url_map: dict[str, str] = {}
    source_date_map: dict[str, str] = {}
    try:
        from ..store import meta as meta_store
        source_url_map = meta_store.title_url_map()
        source_date_map = meta_store.title_date_map()
    except Exception:
        log.debug("title_url_map/title_date_map unavailable; source links/dates degraded")

    target = Path(config.DATA_DIR) / "dashboard" / token / "wiki"
    target.mkdir(parents=True, exist_ok=True)

    md_files = sorted(wiki_dir.glob("*.md"))
    if not md_files:
        return 0

    all_topics: list[str] = []
    topics_data: list[dict] = []
    pages_written = 0

    for md_file in md_files:
        topic = md_file.stem
        all_topics.append(topic)

    total_docs = 0
    for md_file in md_files:
        topic = md_file.stem
        try:
            page_md = md_file.read_text(encoding="utf-8")
        except Exception:
            continue

        meta = idx.get(topic, {})
        if not isinstance(meta, dict):
            meta = {}

        doc_count = len(meta.get("doc_ids") or [])
        total_docs += doc_count

        lines = page_md.strip().split("\n")
        excerpt = ""
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(">"):
                excerpt = stripped.lstrip("> ").strip()
                break
        if not excerpt and len(lines) > 2:
            for line in lines[1:6]:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    excerpt = stripped
                    break

        topics_data.append({
            "topic": topic,
            "docs": doc_count,
            "updated": meta.get("updated", ""),
            "excerpt": excerpt,
        })

        page_html = _render_topic_page(
            topic, page_md, meta, token, all_topics,
            source_url_map=source_url_map,
            source_date_map=source_date_map,
        )
        fname = target / _topic_filename(topic)
        fname.write_text(page_html, encoding="utf-8")
        pages_written += 1

    topics_data.sort(key=lambda x: x.get("updated", ""), reverse=True)

    queue_size = 0
    queue_path = config.DATA_DIR / "wiki_queue.json"
    if queue_path.exists():
        try:
            q = json.loads(queue_path.read_text(encoding="utf-8"))
            queue_size = len(q) if isinstance(q, list) else 0
        except Exception:
            pass

    today_cost = 0.0
    budget = 2000.0
    try:
        from ..store import wiki as wiki_store
        today_cost = wiki_store.today_cost_krw()
        budget = wiki_store.budget_krw()
    except Exception:
        pass

    wiki_stats = {
        "pages": pages_written,
        "docs": total_docs,
        "queue": queue_size,
        "today_cost": today_cost,
        "budget": budget,
    }

    index_html = _render_index_page(topics_data, token, wiki_stats)
    idx_file = target / "index.html"
    tmp_file = target / "index.html.tmp"
    tmp_file.write_text(index_html, encoding="utf-8")
    os.replace(tmp_file, idx_file)

    return pages_written
