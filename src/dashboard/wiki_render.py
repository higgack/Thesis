"""Wikipedia / Namuwiki-style static HTML renderer for Noah LLM Wiki pages.

Reads wiki markdown pages from the Obsidian vault and wiki_index.json,
renders them into clean static HTML under data/dashboard/<token>/wiki/.
Called from regenerate.regenerate() so it stays in sync with the rest of
the dashboard.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

# Normalisation used to fuzzy-match footnote titles against doc titles.
# Module-level so render_wiki can pre-build the (expensive, 32k-entry)
# normalised indexes ONCE per run instead of once per page.
_NORM_KEY_RE = re.compile(r"[\W_]+")


def _norm_key(s: str) -> str:
    return _NORM_KEY_RE.sub("", s).lower()


def _build_norm_idx(m: dict) -> dict[str, str]:
    return {_norm_key(k): k for k in m}

# ---------------------------------------------------------------------------
# Lightweight markdown → HTML (handles what wiki merge pages actually use)
# ---------------------------------------------------------------------------

# Compiled topic-autolink regex, cached across _inline calls (one build
# per topic-set change, not one scan per topic per line — see the
# incident note inside _inline). Longest-first alternation = longest
# topic wins at the same position, matching the old loop's priority.
_AUTOLINK_CACHE: dict = {"key": None, "rx": None}

# Stale pages rendered per render_wiki() call. 2,000 pages in one call was
# the problem; 200 keeps a tick to a few seconds, so a full rebuild spreads
# over roughly a dozen 15s ticks while the other three dashboard renders —
# which run after this one under the same lock — stay responsive.
# Env-overridable so this can be tuned on the VM without a deploy.
_MAX_PAGES_PER_CALL = max(1, int(os.getenv("WIKI_RENDER_PAGES_PER_TICK", "200")))


def _topic_autolink_re(topics: set[str]):
    if not topics:
        return None
    key = (len(topics), hash(frozenset(topics)))
    if _AUTOLINK_CACHE["key"] == key:
        return _AUTOLINK_CACHE["rx"]
    alts = [re.escape(html.escape(t))
            for t in sorted(topics, key=len, reverse=True) if len(t) >= 2]
    rx = (re.compile(r"(?<![가-힣A-Za-z0-9])(?:" + "|".join(alts)
                     + r")(?![A-Za-z0-9])") if alts else None)
    _AUTOLINK_CACHE["key"] = key
    _AUTOLINK_CACHE["rx"] = rx
    return rx


def _md_to_html(md: str, topic_set: set[str] | None = None,
                source_url_map: dict[str, str] | None = None,
                source_date_map: dict[str, dict] | None = None,
                norm_url_idx: dict[str, str] | None = None,
                norm_date_idx: dict[str, str] | None = None,
                ) -> tuple[str, list[dict], list[dict]]:
    """Convert wiki markdown to HTML. Returns (html_str, toc_entries, footnotes).
    toc_entries: [{level, id, text}, ...] for TOC sidebar.
    footnotes: [{id, title, url, date, date_kind}, ...] for source footnotes.

    norm_url_idx / norm_date_idx: pre-built normalised-title indexes
    (via _build_norm_idx) — at 32k docs building them costs more than the
    rest of the render, so render_wiki builds them once for all pages."""
    lines = (md or "").split("\n")
    out: list[str] = []
    toc: list[dict] = []
    _topics = topic_set or set()
    _urls = source_url_map or {}
    _dates = source_date_map or {}
    _nk = _norm_key

    _norm_url_idx = (norm_url_idx if norm_url_idx is not None
                     else _build_norm_idx(_urls))
    _norm_date_idx = (norm_date_idx if norm_date_idx is not None
                      else _build_norm_idx(_dates))

    def _prefix_search(nk: str, norm_idx: dict[str, str]) -> str | None:
        if len(nk) < 8:
            return None
        best: tuple[int, str] | None = None
        for nk_title, orig_title in norm_idx.items():
            if len(nk_title) < 8:
                continue
            short, long = (nk, nk_title) if len(nk) <= len(nk_title) else (nk_title, nk)
            # Prefix match with relaxed ratio
            if long.startswith(short) and len(short) / len(long) >= 0.3:
                score = len(short)
                if best is None or score > best[0]:
                    best = (score, orig_title)
            # Contains match: footnote title is a substring of a doc title
            elif len(nk) >= 10 and nk in nk_title:
                score = len(nk)
                if best is None or score > best[0]:
                    best = (score, orig_title)
        return best[1] if best else None

    def _fuzzy_get_url(key: str) -> str:
        if key in _urls:
            return _urls[key]
        nk = _nk(key)
        orig = _norm_url_idx.get(nk)
        if orig:
            return _urls[orig]
        orig = _prefix_search(nk, _norm_url_idx)
        return _urls[orig] if orig else ""

    def _fuzzy_get_date(key: str) -> dict:
        if key in _dates:
            return _dates[key]
        nk = _nk(key)
        orig = _norm_date_idx.get(nk)
        if orig:
            return _dates[orig]
        orig = _prefix_search(nk, _norm_date_idx)
        return _dates[orig] if orig else {}

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
        key = re.sub(r"^자료\s*\d+\]\s*", "", key)
        key = key.strip("[]")
        is_first = key not in _fn_seen
        if is_first:
            num = len(_footnotes) + 1
            _fn_seen[key] = num
            url = _fuzzy_get_url(key)
            di = _fuzzy_get_date(key)
            date = di.get("date", "") if isinstance(di, dict) else str(di)
            date_kind = di.get("kind", "학습") if isinstance(di, dict) else "학습"
            _footnotes.append({"id": num, "title": key, "url": url,
                               "date": date, "date_kind": date_kind})
        else:
            num = _fn_seen[key]
        ref_id = f' id="ref-{num}"' if is_first else ""
        return (f'<sup class="wiki-fn"{ref_id}><a href="#fn-{num}" '
                f'title="{html.escape(key)}">[{num}]</a></sup>')

    def _topic_link(name: str) -> str:
        """If name matches a wiki topic, return a link; else plain text."""
        clean = html.unescape(name)
        if clean in _topics:
            return (f'<a href="{_topic_filename(clean)}" '
                    f'class="wiki-internal">{html.escape(clean)}</a>')
        return html.escape(clean)

    def _split_sources(raw: str) -> list[str]:
        """Split '[[A]], [[B]]' or '[[A]] (1회차), [[B]] (2회차)' into
        individual source titles. Strips [[ ]] and trailing annotations."""
        raw = raw.strip()
        if not raw:
            return []
        refs = re.findall(r"\[\[(.+?)\]\]", raw)
        if refs:
            return [r.strip().strip("[]") for r in refs if r.strip()]
        parts = [raw.strip("[] ")]
        return [p.strip() for p in parts if p.strip()]

    def _inline(text: str) -> str:
        t = html.escape(text)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"\*(.+?)\*", r"<em>\1</em>", t)
        t = re.sub(r"`(.+?)`", r'<code class="wiki-code">\1</code>', t)
        t = re.sub(
            r"\(출처:\s*(.+?)\)",
            lambda m: "".join(
                _make_footnote(s) for s in _split_sources(m.group(1))
            ) or m.group(0),
            t,
        )
        t = re.sub(
            r"—\s*출처:\s*(.+)",
            lambda m: "".join(
                _make_footnote(s) for s in _split_sources(m.group(1))
            ) or m.group(0),
            t,
        )
        t = re.sub(
            r"\[\[자료\s*\d+\]\s*(.+?)\]\]",
            lambda m: _make_footnote(m.group(1)),
            t,
        )
        def _link_or_fn(m):
            name = m.group(1)
            clean = html.unescape(name)
            if clean in _topics:
                return _topic_link(clean)
            nk = re.sub(r"[\s_\-]+", "", clean).lower()
            if nk in _norm_date_idx or nk in _norm_url_idx:
                return _make_footnote(clean)
            return _topic_link(clean)
        t = re.sub(r"\[\[(.+?)\]\]", _link_or_fn, t)
        # Topic auto-linking — SINGLE combined-regex scan. 2026-07-08:
        # the old per-topic loop (sort + substring scan + re.split for
        # EVERY topic, on EVERY line) went quadratic as the wiki grew —
        # hundreds of topics × 30K-char pages × every stale page after
        # each hourly batch pegged a GIL-holding worker thread for
        # minutes at a time, starving the event loop bot-wide (frozen
        # ingest, stalled heartbeats, the 15-min ingest wait_for firing
        # hours late). Caught red-handed by py-spy: _inline() active+gil.
        # Same output semantics: longest topic wins at a position
        # (alternation order), each topic linked at most once per call,
        # boundary rules preserved (no 한글/alnum before, no alnum
        # after), text inside tags/anchors untouched.
        rx = _topic_autolink_re(_topics)
        if rx is not None and rx.search(t):
            parts = re.split(r"(<[^>]+>)", t)
            in_anchor = False
            linked: set[str] = set()

            def _link_topic(m):
                name = m.group(0)
                topic = html.unescape(name)
                if topic in linked:
                    return name
                linked.add(topic)
                return (f'<a href="{_topic_filename(topic)}" '
                        f'class="wiki-internal">{name}</a>')

            for i, part in enumerate(parts):
                if part.startswith("<"):
                    if part.startswith("<a ") or part == "<a>":
                        in_anchor = True
                    elif part == "</a>":
                        in_anchor = False
                    continue
                if in_anchor or not part:
                    continue
                parts[i] = rx.sub(_link_topic, part)
            t = "".join(parts)
        return t

    in_table = False
    table_rows: list[list[str]] = []
    table_has_header = False

    def _flush_table():
        nonlocal in_table, table_has_header
        if not table_rows:
            in_table = False
            return
        out.append('<table class="wiki-table">')
        for i, cells in enumerate(table_rows):
            tag = "th" if i == 0 and table_has_header else "td"
            out.append("<tr>" + "".join(
                f"<{tag}>{_inline(c)}</{tag}>" for c in cells
            ) + "</tr>")
        out.append("</table>")
        table_rows.clear()
        table_has_header = False
        in_table = False

    for raw in lines:
        line = raw.rstrip()

        if line.strip().startswith("|") and "|" in line.strip()[1:]:
            _flush_bq()
            _flush_list()
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.match(r"^[-:]+$", c) for c in cells):
                table_has_header = True
            else:
                table_rows.append(cells)
            in_table = True
            continue
        elif in_table:
            _flush_table()

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

        if len(stripped) > 200:
            sentences = re.split(r"(?<=[다됨음임])\.(?:\s+|(?=[A-Z가-힣]))", stripped)
            if len(sentences) <= 1:
                sentences = re.split(r"(?<=\.)\s+", stripped)
            if len(sentences) > 1:
                for s in sentences:
                    s = s.strip()
                    if s:
                        if not re.search(r"[.다됨음임]$", s):
                            s += "."
                        out.append(f"<p>{_inline(s)}</p>")
                continue

        out.append(f"<p>{_inline(stripped)}</p>")

    _flush_bq()
    _flush_list()
    _flush_table()
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
  --important: #f59e0b; --memo: #10b981;
  --shadow: 0 1px 3px rgba(0,0,0,0.08);
  --header-border: #a2a9b1;
}
[data-theme="dark"] {
  --bg: #0d1117; --panel: #161b22; --panel-alt: #1c2128;
  --border: #3d444d; --border-light: #2d333b;
  --text: #f0f3f6; --muted: #9da5b0; --accent: #6cb6ff;
  --link: #6cb6ff; --link-visited: #b8d4f0;
  --toc-bg: #131920; --toc-border: #2d333b;
  --bq-bg: #131920; --bq-border: #6cb6ff;
  --code-bg: #131920;
  --highlight: #263040;
  --important: #fbbf24; --memo: #34d399;
  --shadow: 0 1px 4px rgba(0,0,0,0.5);
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
  font-size: 14px; color: var(--text); font-weight: 500;
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
  padding: 5px 12px; border: 1px solid var(--border);
  border-radius: 16px; text-decoration: none; white-space: nowrap;
}
.wiki-topbar .nav-link:hover {
  background: var(--accent); color: #fff; border-color: var(--accent);
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
.fn-orig {
  display: inline-block; font-size: 11px; font-weight: 600;
  padding: 1px 8px; margin-left: 6px; border-radius: 10px;
  background: var(--accent); color: #fff !important;
  text-decoration: none !important; white-space: nowrap;
}
.fn-orig:hover { opacity: 0.85; }
.fn-back {
  font-size: 11px; color: var(--accent); text-decoration: none;
  font-weight: 600;
}
.fn-back:hover { text-decoration: underline; }
.wiki-spacer { height: 8px; }
.wiki-table {
  width: 100%; border-collapse: collapse; margin: 12px 0;
  font-size: 14px;
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
}
.wiki-table th, .wiki-table td {
  padding: 6px 12px; border: 1px solid var(--border-light);
  text-align: left;
}
.wiki-table th {
  background: var(--panel-alt); font-weight: 600;
}
.wiki-table tr:hover td { background: var(--highlight); }
.wiki-article p { margin: 8px 0; line-height: 1.8; }

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

/* ── Title row + filter bar ───────────────── */
.wiki-title-row {
  display: flex; align-items: center; gap: 16px;
  margin: 0 0 20px; flex-wrap: wrap;
}
.wiki-title-row h1 {
  font: 700 28px/1.3 -apple-system, sans-serif; margin: 0;
}
.wiki-filters { display: flex; gap: 6px; }
.wiki-filter {
  padding: 4px 14px; border-radius: 14px; border: 1px solid var(--border-light);
  background: var(--panel); color: var(--muted); cursor: pointer;
  font: 600 13px/1.4 -apple-system, BlinkMacSystemFont, sans-serif;
  transition: background 0.15s, border-color 0.15s, color 0.15s;
}
.wiki-filter:hover { border-color: var(--accent); color: var(--text); }
.wiki-filter.active {
  background: var(--accent); color: #fff; border-color: var(--accent);
}
/* ── Badges ──────────────────────────────── */
.wiki-badge {
  display: inline-block; font-size: 10px; font-weight: 700;
  padding: 1px 6px; border-radius: 8px; vertical-align: middle;
  margin-left: 6px; letter-spacing: 0.3px;
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
}
.wiki-badge.new { background: #2da44e; color: #fff; }
.wiki-badge.recent { background: #bf8700; color: #fff; }
[data-theme="dark"] .wiki-badge.new { background: #238636; }
[data-theme="dark"] .wiki-badge.recent { background: #9e6a00; }

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
  /* skip offscreen layout/paint as the topic list grows (weak-PC fix) */
  content-visibility: auto; contain-intrinsic-size: auto 150px;
}
.wiki-card:hover {
  border-color: var(--accent);
  box-shadow: 0 2px 8px rgba(0,0,0,0.1);
}
[data-theme="dark"] .wiki-card:hover {
  box-shadow: 0 2px 8px rgba(0,0,0,0.4);
}
.wiki-card h3 { margin: 0 0 6px 0; font-size: 17px; }
.wiki-card h3 a { color: var(--text); }
.wiki-card h3 a:hover { color: var(--accent); }
.wiki-card .wstar { cursor: pointer; background: transparent; border: 0;
  color: var(--muted); font-size: 16px; line-height: 1; padding: 0 4px;
  margin-left: 6px; transition: 0.12s; }
.wiki-card .wstar:hover { color: var(--important); transform: scale(1.15); }
.wiki-card .wstar.on { color: var(--important); }
.wiki-card .wdel { cursor: pointer; background: transparent; border: 0;
  color: var(--muted); font-size: 15px; line-height: 1; padding: 0 4px;
  margin-left: 2px; opacity: 0.6; transition: 0.12s; }
.wiki-card .wdel:hover { color: #ef4444; opacity: 1; transform: scale(1.15); }
.wiki-card.removing { opacity: 0; transform: scale(0.97); transition: 0.2s; }
.wiki-card[data-important="1"] { border-color: rgba(245,158,11,0.55);
  background: rgba(245,158,11,0.06); }
.wiki-filter.wstarfilter.active { background: var(--important); border-color: var(--important); color: #fff; }
.wiki-filter.wmemofilter.active { background: var(--memo); border-color: var(--memo); color: #fff; }
.topic-memo { max-width: 760px; margin: 18px auto 0; padding: 12px 14px;
  background: var(--panel); border: 1px solid var(--border); border-radius: 10px; }
.topic-memo .memo-h { font-size: 12px; color: var(--muted); font-weight: 600; margin-bottom: 6px; }
.topic-memo textarea { width: 100%; min-height: 64px; background: var(--bg);
  border: 1px solid var(--border); color: var(--text); border-radius: 8px; padding: 9px;
  font-size: 13.5px; outline: none; resize: vertical; box-sizing: border-box; }
.topic-memo textarea:focus { border-color: var(--accent); }
.topic-memo .memo-row { display: flex; align-items: center; gap: 8px; margin-top: 7px; }
.topic-memo .memo-save { background: var(--accent); border: 0; color: #fff; padding: 6px 16px;
  border-radius: 7px; cursor: pointer; font-size: 12.5px; font-weight: 600; }
.topic-memo .memo-del { background: rgba(148,163,184,.25); border: 0; color: var(--muted);
  padding: 6px 14px; border-radius: 7px; cursor: pointer; font-size: 12.5px; font-weight: 600; }
.topic-memo .memo-status { font-size: 11px; color: var(--muted); }
.topic-memo .alarm-row { display: flex; align-items: center; gap: 6px; margin-top: 7px; flex-wrap: wrap; }
.topic-memo .alarm-time { background: var(--bg); border: 1px solid var(--border); color: var(--text);
  border-radius: 6px; padding: 4px 6px; font-size: 12px; }
.topic-memo .alarm-set, .topic-memo .alarm-clear { border: 0; border-radius: 6px; cursor: pointer;
  font-size: 11.5px; padding: 5px 10px; font-weight: 600; }
.topic-memo .alarm-set { background: #6366f1; color: #fff; }
.topic-memo .alarm-clear { background: rgba(148,163,184,.25); color: var(--muted); }
.topic-memo .alarm-status { font-size: 11px; color: #818cf8; }
.wiki-card .card-meta {
  font: 12px/1.4 -apple-system, BlinkMacSystemFont, sans-serif;
  color: var(--muted); margin-bottom: 8px;
}
.wiki-card .card-excerpt {
  font-size: 13px; color: var(--muted); line-height: 1.5;
  overflow: hidden; display: -webkit-box;
  -webkit-line-clamp: 3; -webkit-box-orient: vertical;
}
.wiki-card .memo-preview {
  margin-top: 8px; padding: 8px 10px; font-size: 13px; line-height: 1.55;
  color: var(--text); white-space: pre-wrap; word-break: break-word;
  background: rgba(16,185,129,0.10); border: 1px solid rgba(16,185,129,0.4);
  border-radius: 8px;
}
mark.kw {
  background: #fef08a; color: #202122; padding: 0 1px;
  border-radius: 2px;
}
[data-theme="dark"] mark.kw { background: #b59f3b; color: #0d1117; }

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

_SEARCH_JS = r"""
(function(){
  var input = document.getElementById('wiki-search');
  if (!input) return;
  var cards = document.querySelectorAll('.wiki-card');
  var KEY = 'wiki-search-q';
  function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
  function rx(s){return s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');}
  function snippet(text, query){
    if(!text||!query) return '';
    var i=text.toLowerCase().indexOf(query.toLowerCase());
    if(i<0) return '';
    var s=Math.max(0,i-80), e=Math.min(text.length,i+query.length+80);
    var slice=(s>0?'…':'')+text.slice(s,e)+(e<text.length?'…':'');
    return esc(slice).replace(new RegExp(rx(query),'gi'),
      function(m){return '<mark class="kw">'+m+'</mark>';});
  }
  cards.forEach(function(c){ var ex=c.querySelector('.card-excerpt');
    if(ex && ex.dataset.orig===undefined) ex.dataset.orig=ex.textContent; });
  // Full-text filter: matches topic title + full body (data-text), shows a
  // highlighted snippet in the card excerpt when the hit is in the body.
  function apply(){
    var q = input.value.trim(), ql=q.toLowerCase();
    cards.forEach(function(c){
      var hay = c.getAttribute('data-text') || '';
      var h3 = c.querySelector('h3'); var title = h3 ? h3.textContent : '';
      var inBody = !ql || hay.toLowerCase().indexOf(ql)>=0;
      var inTitle = !ql || title.toLowerCase().indexOf(ql)>=0;
      var ok = !ql || inBody || inTitle;
      c.style.display = ok ? '' : 'none';
      var ex = c.querySelector('.card-excerpt');
      if(ex){
        if(ok && ql && inBody){ ex.innerHTML = snippet(hay,q) || esc(ex.dataset.orig||''); }
        else { ex.textContent = ex.dataset.orig || ''; }
      }
    });
  }
  input.addEventListener('input', function(){
    try { sessionStorage.setItem(KEY, input.value); } catch(e){}
    apply();
  });
  // Back-navigation restore: each click is a full page load (static
  // site), which used to wipe the query. Re-fill and re-filter so the
  // list looks exactly like the user left it.
  try {
    var saved = sessionStorage.getItem(KEY);
    if (saved) { input.value = saved; if (cards.length) apply(); }
  } catch(e){}
})();
"""

_FILTER_JS = """
(function(){
  var btns = document.querySelectorAll('.wiki-filter');
  var grid = document.querySelector('.wiki-grid');
  var cards = Array.prototype.slice.call(document.querySelectorAll('.wiki-card'));
  if (!btns.length || !grid) return;
  var KEY = 'wiki-filter';
  var original = cards.slice();
  var cutoff = new Date(Date.now() - 7*24*60*60*1000).toISOString();
  function applyFilter(f){
    if (f === 'all') f = null;
    cards.forEach(function(c){
      var omp = c.querySelector('.memo-preview'); if (omp) omp.remove();
      if (!f){ c.style.display = ''; return; }
      var cr = c.getAttribute('data-created');
      var up = c.getAttribute('data-updated');
      var show;
      if (f === 'important') {
        show = c.getAttribute('data-important') === '1';
      } else if (f === 'memo') {
        show = c.getAttribute('data-hasmemo') === '1';
      } else if (f === 'new') {
        show = cr && cr >= cutoff;
      } else {
        // New wins for its whole 7-day window: a still-New topic is
        // never Recent, even if it was updated yesterday.
        show = up && up >= cutoff && !(cr && cr >= cutoff);
      }
      c.style.display = show ? '' : 'none';
      // 메모만 필터 → 카드 밑에 메모 내용 미리보기.
      if (show && f === 'memo' && c.getAttribute('data-memo')){
        var mp = document.createElement('div');
        mp.className = 'memo-preview';
        mp.textContent = '📝 ' + c.getAttribute('data-memo');
        c.appendChild(mp);
      }
    });
    // Time order inside a filter: New sorts by creation date, Recent by
    // update date — newest on top, older pushed down. All restores the
    // as-rendered order (plain time order, no New/Recent grouping).
    var order = original;
    if (f) {
      var attr = (f === 'new') ? 'data-created' : 'data-updated';
      order = cards.slice().sort(function(a, b){
        var ka = a.getAttribute(attr) || '';
        var kb = b.getAttribute(attr) || '';
        return ka > kb ? -1 : (ka < kb ? 1 : 0);
      });
    }
    order.forEach(function(c){ grid.appendChild(c); });
    btns.forEach(function(b){
      b.classList.toggle('active',
        b.getAttribute('data-filter') === (f || 'all'));
    });
  }
  btns.forEach(function(btn){
    btn.addEventListener('click', function(){
      var f = btn.getAttribute('data-filter');
      // All, or re-clicking the active filter, returns to All.
      if (f === 'all' || btn.classList.contains('active')) {
        try { sessionStorage.removeItem(KEY); } catch(e){}
        applyFilter(null);
        return;
      }
      try { sessionStorage.setItem(KEY, f); } catch(e){}
      applyFilter(f);
    });
  });
  // Back-navigation restore (runs after the search restore — same
  // precedence as a user clicking a filter after typing a query).
  // No saved filter -> leave the server-rendered state alone (All is
  // pre-marked active, all cards visible) so a restored search query's
  // hiding isn't wiped by a redundant applyFilter(null).
  var savedF = null;
  try { savedF = sessionStorage.getItem(KEY); } catch(e){}
  if (savedF) applyFilter(savedF);
})();
"""

# ★ 중요 표시 토글 — POST /<token>/mark {kind:'wiki', id:topic}. Optimistic,
# revert on failure (mirrors the notes/Q&A star).
_WIKI_STAR_JS = """
(function(){
  var token = location.pathname.split('/').filter(Boolean)[0] || '';
  document.querySelectorAll('.wiki-card .wstar').forEach(function(btn){
    btn.addEventListener('click', function(e){
      e.preventDefault(); e.stopPropagation();
      var card = btn.closest('.wiki-card'); if(!card) return;
      var topic = card.getAttribute('data-topic'); if(!topic) return;
      var now = card.getAttribute('data-important') !== '1';
      card.setAttribute('data-important', now ? '1' : '0');
      btn.textContent = now ? '★' : '☆';
      btn.classList.toggle('on', now);
      fetch('/'+token+'/mark', {method:'POST',
        headers:{'content-type':'application/json'},
        body: JSON.stringify({kind:'wiki', id:topic, important:now})})
        .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
        .catch(function(err){
          card.setAttribute('data-important', now ? '0' : '1');
          btn.textContent = now ? '☆' : '★';
          btn.classList.toggle('on', !now);
          alert('중요 표시 변경 실패: '+err.message);
        });
    });
  });
  document.querySelectorAll('.wiki-card .wdel').forEach(function(btn){
    btn.addEventListener('click', function(e){
      e.preventDefault(); e.stopPropagation();
      var card = btn.closest('.wiki-card'); if(!card) return;
      var topic = card.getAttribute('data-topic'); if(!topic) return;
      if(!confirm('이 위키 페이지를 영구 삭제할까요?\\n\\n'+topic
        +'\\n\\n재학습돼도 다시 생기지 않습니다.')) return;
      fetch('/'+token+'/wiki/'+encodeURIComponent(topic)+'/delete',
        {method:'POST'})
        .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
        .then(function(){
          card.classList.add('removing');
          setTimeout(function(){ card.remove(); }, 220);
        })
        .catch(function(err){ alert('삭제 실패: '+err.message); });
    });
  });
})();
"""

# 📝 위키 토픽 페이지 메모 저장 — POST /<token>/memo {kind:'wiki', id:topic}
_TOPIC_MEMO_JS = """
(function(){
  var box=document.querySelector('.topic-memo'); if(!box)return;
  var ta=box.querySelector('textarea'), btn=box.querySelector('.memo-save');
  var del=box.querySelector('.memo-del');
  var st=box.querySelector('.memo-status'), id=box.dataset.id;
  var token=location.pathname.split('/').filter(Boolean)[0]||'';
  if(!ta||!btn||!id)return;
  function save(text,msg){
    btn.disabled=true; if(del)del.disabled=true; if(st)st.textContent='저장 중…';
    fetch('/'+token+'/memo',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({kind:'wiki',id:id,text:text})})
      .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
      .then(function(){if(st)st.textContent=msg;})
      .catch(function(err){if(st)st.textContent='실패: '+err.message;})
      .finally(function(){btn.disabled=false; if(del)del.disabled=false;});
  }
  btn.addEventListener('click',function(){save(ta.value,'저장됨 ✓');});
  if(del)del.addEventListener('click',function(){ta.value='';save('','삭제됨');});
})();
"""

# Restore the index page's window scroll on back-navigation. Must run
# AFTER the search/filter restores above (visible-card set changes the
# page height). scrollRestoration='manual' stops the browser's own
# (pre-filter, wrong-offset) restore from fighting ours.
_INDEX_SCROLL_JS = """
(function(){
  var KEY = 'wiki-index-scroll';
  try {
    if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
  } catch(e){}
  window.addEventListener('scroll', function(){
    try {
      sessionStorage.setItem(KEY, String(window.scrollY || window.pageYOffset || 0));
    } catch(e){}
  }, {passive:true});
  try {
    var saved = sessionStorage.getItem(KEY);
    if (saved !== null) window.scrollTo(0, parseInt(saved, 10) || 0);
  } catch(e){}
})();
"""

# Preserve the left topic-list scroll position across full-page navigations.
# Each topic is a plain <a href> → clicking reloads the page and the sidebar
# resets to top. We stash scrollTop in sessionStorage on every scroll and
# restore it on load, so clicking a topic keeps the list exactly where it was.
# On the very first visit (no saved value) we center the active item instead.
_SIDEBAR_SCROLL_JS = """
(function(){
  var sb = document.querySelector('.wiki-sidebar');
  if (!sb) return;
  var KEY = 'wiki-sidebar-scroll';
  try {
    var saved = sessionStorage.getItem(KEY);
    if (saved !== null) {
      sb.scrollTop = parseInt(saved, 10) || 0;
    } else {
      var active = sb.querySelector('.wiki-nav a.active');
      if (active && active.scrollIntoView) active.scrollIntoView({block:'center'});
    }
    sb.addEventListener('scroll', function(){
      sessionStorage.setItem(KEY, String(sb.scrollTop));
    }, {passive:true});
  } catch(e){}
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
        f"<title>{html.escape(title)} — Noah LLM Wiki</title>"
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
        f'<a href="/{token}/kg/" class="nav-link">🕸 KG</a>'
        f'<a href="/{token}/notes/" class="nav-link">Note</a>'
        f'<a href="/{token}/universe/" class="nav-link">🌌 Universe</a>'
        f'<a href="/{token}/commands/" class="nav-link">Commands</a>'
        '<a href="https://echodiary-eng.vercel.app/" class="nav-link" target="_blank" rel="noopener">Daily</a>'
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


def _format_ts(ts: str) -> str:
    if not ts:
        return ""
    from datetime import datetime, timedelta, timezone
    _KST = timezone(timedelta(hours=9))
    if "T" in ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(_KST).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    return ts[:16]


def _build_infobox(topic: str, meta: dict) -> str:
    doc_count = len(meta.get("doc_ids") or [])
    updated = _format_ts(meta.get("updated") or "")
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
        num = fn["id"]
        title = html.escape(fn["title"].strip("[]"))
        url = fn.get("url", "")
        date = fn.get("date", "")
        if url:
            link = (f'{title} '
                    f'<a href="{html.escape(url)}" target="_blank" '
                    f'class="fn-orig">원본 →</a>')
        else:
            link = title
        if date:
            date_kind = fn.get("date_kind", "학습")
            link += f' <span class="fn-date">({html.escape(date)} {date_kind})</span>'
        link += (f' <sup class="wiki-fn"><a href="#ref-{num}" '
                 f'class="fn-back" title="본문으로 돌아가기">[{num}]</a></sup>')
        items.append(f'<li id="fn-{num}">{link}</li>')
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
                       norm_url_idx: dict[str, str] | None = None,
                       norm_date_idx: dict[str, str] | None = None,
                       ) -> str:
    topic_set = set(all_topics) - {topic}
    body_html, toc, footnotes = _md_to_html(
        page_md, topic_set=topic_set, source_url_map=source_url_map,
        source_date_map=source_date_map,
        norm_url_idx=norm_url_idx, norm_date_idx=norm_date_idx,
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

    from . import widgets as _widgets
    try:
        from ..store import marks as _marks
        _wmemo = _marks.memo("wiki", topic)
        _walarm = _marks.alarm("wiki", topic)
    except Exception:
        _wmemo, _walarm = "", {}
    memo_box = (
        f'<div class="topic-memo" data-id="{html.escape(topic)}">'
        '<div class="memo-h">📝 내 메모</div>'
        f'<textarea placeholder="이 페이지에 대한 내 생각…">{html.escape(_wmemo)}</textarea>'
        '<div class="memo-row"><button type="button" class="memo-save">저장</button>'
        '<button type="button" class="memo-del">삭제</button>'
        '<span class="memo-status"></span></div>'
        + _widgets.alarm_row("wiki", topic, _walarm)
        + '</div>'
    )

    return (
        f"{_head(topic)}"
        f"<style>{_widgets.ALARM_CSS}</style><body>"
        f"{_topbar(token, topic)}"
        '<div class="wiki-layout">'
        f'<aside class="wiki-sidebar">{sidebar_nav}</aside>'
        '<div class="wiki-main">'
        f'<article class="wiki-article">'
        f"<h1>{html.escape(topic)}</h1>"
        f"{infobox}{toc_html}{body_html}{fn_html}"
        "</article>"
        f"{memo_box}"
        "</div></div>"
        f"<script>{_SEARCH_JS}</script>"
        f"<script>{_SIDEBAR_SCROLL_JS}</script>"
        f"<script>{_TOPIC_MEMO_JS}</script>"
        f"<script>{_widgets.ALARM_JS}</script>"
        "</body></html>"
    )


def _wiki_search_text(md: str) -> str:
    """Topic markdown → plain search haystack for the index full-text
    search (drops code/mermaid fences + md symbols, capped)."""
    s = re.sub(r"```.*?```", " ", md or "", flags=re.S)
    s = re.sub(r"[#>*`|]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:4000]


def _render_index_page(topics_data: list[dict], token: str,
                       wiki_stats: dict) -> str:
    from . import widgets as _wgt
    total_pages = wiki_stats.get("pages", 0)
    total_docs = wiki_stats.get("docs", 0)
    queue = wiki_stats.get("queue", 0)
    today_cost = wiki_stats.get("today_cost", 0)
    budget = wiki_stats.get("budget", config.WIKI_DAILY_BUDGET_KRW)
    month_cost = wiki_stats.get("month_cost", 0)
    total_cost = wiki_stats.get("total_cost", 0)

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
        '<div class="wiki-stat-item">'
        f'<div class="stat-value">₩{month_cost:,.0f}</div>'
        '<div class="stat-label">This Month</div></div>'
        '<div class="wiki-stat-item">'
        f'<div class="stat-value">₩{total_cost:,.0f}</div>'
        '<div class="stat-label">Total</div></div>'
        "</div>"
    )

    from datetime import datetime, timedelta, timezone
    _kst = timezone(timedelta(hours=9))
    _7d_ago = (datetime.now(_kst) - timedelta(days=7)).isoformat()

    try:
        from ..store import marks as _marks
        _important = _marks.marked("wiki")
        _wmemos = _marks.memos("wiki")
    except Exception:
        _important = set()
        _wmemos = {}

    cards = []
    for td in topics_data:
        topic = td["topic"]
        imp = 1 if topic in _important else 0
        _wmemo_txt = (_wmemos.get(topic) or "").strip()
        hasmemo = 1 if _wmemo_txt else 0
        display = td.get("title") or topic
        excerpt = html.escape(td.get("excerpt", "")[:200])
        search_text = html.escape(td.get("search_text", "")[:4000])
        doc_count = td.get("docs", 0)
        updated = (td.get("updated") or "")[:10]
        created_iso = td.get("created") or ""
        updated_iso = td.get("updated") or ""
        badge = ""
        # New = created within 7d (wins for its whole window, even when
        # updated meanwhile). Recent = updated within 7d but past the New
        # window. Mirrors the index filter buttons' logic in _FILTER_JS.
        if created_iso and created_iso >= _7d_ago:
            badge = ' <span class="wiki-badge new">New</span>'
        elif updated_iso and updated_iso >= _7d_ago:
            badge = ' <span class="wiki-badge recent">Recent</span>'
        cards.append(
            f'<div class="wiki-card" data-created="{html.escape(created_iso)}"'
            f' data-updated="{html.escape(updated_iso)}"'
            f' data-topic="{html.escape(topic)}" data-important="{imp}"'
            f' data-hasmemo="{hasmemo}" data-memo="{html.escape(_wmemo_txt)}"'
            f' data-text="{search_text}">'
            f'<h3><a href="{_topic_filename(topic)}">'
            f"{html.escape(display)}</a>{badge}"
            f"<button type='button' class='wstar{' on' if imp else ''}' "
            f"title='중요 표시 토글'>{'★' if imp else '☆'}</button>"
            f"<button type='button' class='wdel' "
            f"title='이 위키 페이지 영구 삭제 (재학습돼도 안 생김)'>🗑</button></h3>"
            f'<div class="card-meta">'
            f"{doc_count} sources · {updated}</div>"
            f'<div class="card-excerpt">{excerpt}</div>'
            "</div>"
        )

    return (
        f'{_head("Noah LLM Wiki")}<body>'
        f"{_topbar(token)}"
        '<div style="max-width:1200px;margin:0 auto;padding:24px 16px 80px">'
        '<div class="wiki-title-row">'
        "<h1>Noah LLM Wiki</h1>"
        '<div class="wiki-filters">'
        '<button class="wiki-filter active" data-filter="all">All</button>'
        '<button class="wiki-filter" data-filter="new">New</button>'
        '<button class="wiki-filter" data-filter="recent">Recent</button>'
        '<button class="wiki-filter wstarfilter" data-filter="important">★ 중요만</button>'
        '<button class="wiki-filter wmemofilter" data-filter="memo">📝 메모만</button>'
        '</div></div>'
        f"{stats_html}"
        f"{_render_lint_panel(token)}"
        f'<div class="wiki-grid">{"".join(cards)}</div>'
        "</div>"
        f"<script>{_SEARCH_JS}</script>"
        f"<script>{_FILTER_JS}</script>"
        f"<script>{_WIKI_STAR_JS}</script>"
        f"<script>{_INDEX_SCROLL_JS}</script>"
        + _wgt.live_reload_js("wiki") +
        "</body></html>"
    )


def _render_lint_panel(token: str) -> str:
    """Compact health panel from wiki_lint.json (written ₩0/hour by the
    wiki batch). Topics link to their pages. Returns '' when the scan
    hasn't run yet so the index stays clean on a fresh deploy."""
    try:
        path = config.DATA_DIR / "wiki_lint.json"
        if not path.exists():
            return ""
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return ""
    except Exception:
        return ""
    contra = data.get("contradictions") or []
    stale = data.get("stale_singletons") or []
    missing = data.get("missing_pages") or []
    gen = html.escape(str(data.get("generated_at", "")))

    def _chip(t: str) -> str:
        return (f'<a href="{_topic_filename(t)}" '
                f'style="color:inherit">{html.escape(t)}</a>')

    if not contra and not stale and not missing:
        return (
            '<div style="margin:0 0 18px;font-size:13px;color:var(--muted)">'
            f'🩺 점검: 이상 없음 '
            f'<span style="opacity:.7">({gen})</span></div>'
        )

    body = []
    if contra:
        items = ", ".join(_chip(t) for t in contra[:15])
        more = f" … 외 {len(contra) - 15}개" if len(contra) > 15 else ""
        body.append(
            '<div style="margin:6px 0"><b>⚠️ 미해결 모순</b> '
            '<span style="color:var(--muted);font-size:12px">'
            '(페이지 열어 검토)</span><br>'
            f'{items}{more}</div>')
    if stale:
        items = ", ".join(
            f'{_chip(r.get("topic", ""))} '
            f'<span style="color:var(--muted);font-size:11px">'
            f'({html.escape(str(r.get("updated", "")))})</span>'
            for r in stale[:15])
        more = f" … 외 {len(stale) - 15}개" if len(stale) > 15 else ""
        body.append(
            '<div style="margin:6px 0"><b>🧹 정체 단일소스</b> '
            '<span style="color:var(--muted);font-size:12px">'
            f'({data.get("stale_days", 30)}일+ 미갱신 → 병합/삭제 후보)</span><br>'
            f'{items}{more}</div>')
    if missing:
        items = ", ".join(_chip(t) for t in missing[:15])
        more = f" … 외 {len(missing) - 15}개" if len(missing) > 15 else ""
        body.append(
            '<div style="margin:6px 0"><b>🗂 누락 페이지</b> '
            '<span style="color:var(--muted);font-size:12px">'
            '(인덱스엔 있으나 .md 없음)</span><br>'
            f'{items}{more}</div>')

    open_attr = " open" if (contra or missing) else ""
    acked = int(data.get("acked_singletons") or 0)
    summary = (f"🩺 위키 점검 — ⚠️ 모순 {len(contra)} · "
               f"🧹 정체 {len(stale)}"
               + (f" (📌 보존확정 {acked} 제외)" if acked else "")
               + f" · 🗂 누락 {len(missing)}")
    return (
        f'<details{open_attr} style="margin:0 0 18px;padding:10px 14px;'
        'border:1px solid var(--border-light);border-radius:10px;'
        'background:var(--panel)">'
        f'<summary style="cursor:pointer;font-weight:600">{summary} '
        '<span style="color:var(--muted);font-weight:400;font-size:12px">'
        f'({gen})</span></summary>'
        '<div style="margin-top:8px;font-size:13px;line-height:1.7">'
        f'{"".join(body)}</div></details>'
    )


def _topic_filename(topic: str) -> str:
    slug = re.sub(r"[^\w가-힣\-]+", "_", topic).strip("_")
    return f"{slug}.html"


# ---------------------------------------------------------------------------
# Public entry point (called from regenerate.py)
# ---------------------------------------------------------------------------

def render_wiki(token: str) -> int:
    """Generate wiki HTML pages incrementally. Returns pages written.

    Runs every 60s via regenerate(); at 1000+ topics a full re-render per
    tick pegged a worker thread continuously and rewrote ~100MB/min, so a
    page is re-rendered only when its .md mtime changed. Any topic-list
    change (add/delete/rename) forces a full rebuild because every page
    embeds the sidebar + autolink topic set. Render state persists in
    DATA_DIR/wiki_render_cache.json so restarts don't trigger a rebuild."""
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

    # Build slug → original topic name reverse map for index lookup.
    # Two strategies so we never miss:
    #   1) Recompute slug from topic name (same logic as wiki.py _slug)
    #   2) Use the "file" field stored in each index record
    _SAFE_RE = re.compile(r"[^\w가-힣\- ]+")
    def _slug_of(s: str) -> str:
        s = _SAFE_RE.sub(" ", s or "").strip()
        s = re.sub(r"\s+", " ", s)
        return s[:80] or "기타"

    _slug_to_topic: dict[str, str] = {}
    for topic_name, rec in idx.items():
        _slug_to_topic[_slug_of(topic_name)] = topic_name
        _slug_to_topic[topic_name] = topic_name
        if isinstance(rec, dict) and rec.get("file"):
            stem = rec["file"].rsplit(".", 1)[0]
            _slug_to_topic[stem] = topic_name

    target = Path(config.DATA_DIR) / "dashboard" / token / "wiki"
    target.mkdir(parents=True, exist_ok=True)

    md_files = sorted(wiki_dir.glob("*.md"))
    if not md_files:
        return 0

    _SKIP_TOPICS = {"기타"}
    md_files = [f for f in md_files if f.stem not in _SKIP_TOPICS]
    all_topics = [f.stem for f in md_files]

    # Render cache: fingerprint of the topic list + per-topic md mtime and
    # cached excerpt. Fingerprint change → full rebuild (sidebars/autolinks
    # on every page reference the topic set).
    # _TPL_VERSION: bump on ANY template/CSS/JS change in this file —
    # the incremental skip means already-rendered topic pages would
    # otherwise keep old markup forever (their .md never changes).
    _TPL_VERSION = "16"  # bumped: ★/메모 강조색 토큰화(var(--important)/--memo)
    cache_path = config.DATA_DIR / "wiki_render_cache.json"
    fp = hashlib.sha1(
        ("|".join(all_topics) + "\x00" + token + "\x00" + _TPL_VERSION
         ).encode("utf-8")
    ).hexdigest()
    cache: dict = {}
    try:
        if cache_path.exists():
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            cache = {}
    except Exception:
        cache = {}
    entries: dict = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    full = cache.get("fp") != fp

    if full:
        # Drop orphan pages of deleted/renamed topics so stale HTML
        # doesn't linger behind a sidebar that no longer links it.
        expected = {_topic_filename(t) for t in all_topics} | {"index.html"}
        for old in target.glob("*.html"):
            if old.name not in expected:
                try:
                    old.unlink()
                except OSError:
                    pass

    # ★/memo/alarm are baked into each topic page by _render_topic_page,
    # but a mark edit never touches the topic's .md — so keying staleness
    # on mtime alone meant a memo left on a wiki page was rendered into
    # HTML only if the page happened to be re-merged later, and otherwise
    # never showed up (the dashboard's mark/memo POST handlers write to
    # marks.db and do not trigger a re-render). Fold a per-topic mark
    # fingerprint into the staleness test: three small whole-table reads
    # here, and only the topics whose marks actually moved re-render —
    # unlike putting marks in `fp`, which would force all ~2,000 pages.
    try:
        from ..store import marks as _marks_store
        _imp_set = _marks_store.marked("wiki")
        _memo_map = _marks_store.memos("wiki")
        _alarm_map = _marks_store.alarm_map("wiki")
    except Exception:
        log.debug("wiki marks unavailable; mark-driven refresh disabled")
        _imp_set, _memo_map, _alarm_map = set(), {}, {}

    def _mark_fp(topic: str) -> str:
        # sha1, not hash(): this value is persisted in
        # wiki_render_cache.json, and str hashing is salted per process —
        # every restart would otherwise look like a mark change and
        # re-render every memo-bearing page.
        raw = "\x00".join([
            "1" if topic in _imp_set else "0",
            (_memo_map.get(topic) or "").strip(),
            repr(sorted((_alarm_map.get(topic) or {}).items())),
        ])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    # Decide which pages actually need re-rendering before touching the
    # heavy 32k-row title maps.
    stale: list[Path] = []
    mtimes: dict[str, float] = {}
    mark_fps: dict[str, str] = {}
    for md_file in md_files:
        topic = md_file.stem
        try:
            mt = md_file.stat().st_mtime
        except OSError:
            continue
        mtimes[topic] = mt
        mfp = _mark_fp(topic)
        mark_fps[topic] = mfp
        ent = entries.get(topic)
        if (full or not isinstance(ent, dict) or ent.get("mtime") != mt
                or ent.get("mark_fp") != mfp
                or not (target / _topic_filename(topic)).exists()):
            stale.append(md_file)

    source_url_map: dict[str, str] = {}
    source_date_map: dict[str, str] = {}
    norm_url_idx: dict[str, str] = {}
    norm_date_idx: dict[str, str] = {}
    if stale:
        try:
            from ..store import meta as meta_store
            source_url_map = meta_store.title_url_map()
            source_date_map = meta_store.title_date_map()
            norm_url_idx = _build_norm_idx(source_url_map)
            norm_date_idx = _build_norm_idx(source_date_map)
        except Exception:
            log.debug("title_url_map/title_date_map unavailable; "
                      "source links/dates degraded")

    stale_names = {f.stem for f in stale}
    new_entries: dict[str, dict] = {}
    topics_data: list[dict] = []
    pages_written = 0
    # Cap the pages rendered per call. A full rebuild is ~2,000 pages and
    # regenerate() holds one lock for its whole run, so an uncapped rebuild
    # starved the notes, KG and universe renders that come after it — on
    # 2026-08-26 all four dashboards sat frozen for the better part of an
    # hour and nothing said so, because the skipped ticks log at DEBUG.
    # Worse, the render cache is only written at the END of a run, so every
    # deploy killed the rebuild mid-way and the next boot started over from
    # zero; with deploys minutes apart it could never finish. Stopping at a
    # budget writes the cache for what WAS rendered, so the next tick picks
    # up where this one stopped instead of restarting.
    budget_left = _MAX_PAGES_PER_CALL
    deferred = 0
    total_docs = 0
    # DISTINCT source docs. This used to be a running sum of each topic's
    # doc_ids length, which counts one document once per topic it appears
    # in — and wiki topics deliberately share sources (that overlap is
    # exactly what universe_render draws its dashed "shared document"
    # links from), so the "Sources Integrated" headline was inflated by
    # every multi-topic document.
    all_doc_ids: set[str] = set()
    pid = os.getpid()

    for md_file in md_files:
        topic = md_file.stem
        if topic not in mtimes:
            continue

        idx_key = _slug_to_topic.get(topic)
        if idx_key is None:
            # Fallback: try direct lookup (topic == index key)
            idx_key = topic if topic in idx else None
        if idx_key is None:
            log.debug("wiki card: no index match for stem %r", topic)
        meta = idx.get(idx_key or topic, {})
        if not isinstance(meta, dict):
            meta = {}

        _doc_ids = meta.get("doc_ids") or []
        doc_count = len(_doc_ids)   # per-topic card count — correct as-is
        total_docs += doc_count
        all_doc_ids.update(_doc_ids)

        search_text = ""
        rendered = False
        if topic in stale_names and budget_left <= 0:
            deferred += 1
        elif topic in stale_names:
            try:
                page_md = md_file.read_text(encoding="utf-8")
            except Exception:
                continue

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
            search_text = _wiki_search_text(page_md)

            page_html = _render_topic_page(
                topic, page_md, meta, token, all_topics,
                source_url_map=source_url_map,
                source_date_map=source_date_map,
                norm_url_idx=norm_url_idx,
                norm_date_idx=norm_date_idx,
            )
            fname = target / _topic_filename(topic)
            # Atomic + pid-suffixed tmp: bot and dashboard processes can
            # regenerate concurrently; a shared tmp name made one side's
            # os.replace fail and the http.server could serve a torn page.
            tmp = fname.parent / f"{fname.name}.{pid}.tmp"
            tmp.write_text(page_html, encoding="utf-8")
            os.replace(tmp, fname)
            pages_written += 1
            budget_left -= 1
            rendered = True
        else:
            excerpt = (entries.get(topic) or {}).get("excerpt", "")
            search_text = (entries.get(topic) or {}).get("search_text", "")

        # A topic deferred by the budget must NOT get a cache entry: an
        # entry records "rendered at this mtime", so writing one would mark
        # an unrendered page fresh and it would never be built.
        if rendered or topic not in stale_names:
            new_entries[topic] = {"mtime": mtimes[topic], "excerpt": excerpt,
                                  "search_text": search_text,
                                  "mark_fp": mark_fps.get(topic, "")}
        topics_data.append({
            "topic": topic,
            "title": meta.get("title") or topic,
            "docs": doc_count,
            "updated": meta.get("updated", ""),
            "created": meta.get("created", ""),
            "excerpt": excerpt,
            "search_text": search_text,
            "mtime": mtimes[topic],
        })

    # Unfiltered index order = plain time order, no New/Recent grouping:
    # newest activity first by the index `updated` stamp (set on creation
    # AND on every merge, so it equals max(created, updated)). The stamp,
    # not file mtime, is what the card displays — and mtime drifts on
    # vault git ops / restores / manual Obsidian edits. mtime stays as
    # tiebreak + fallback for orphan pages the index doesn't know.
    topics_data.sort(
        key=lambda x: (x.get("updated") or "", x.get("mtime", 0)),
        reverse=True,
    )

    queue_size = 0
    queue_path = config.DATA_DIR / "wiki_queue.json"
    if queue_path.exists():
        try:
            q = json.loads(queue_path.read_text(encoding="utf-8"))
            queue_size = len(q) if isinstance(q, list) else 0
        except Exception:
            pass

    today_cost = 0.0
    budget = float(config.WIKI_DAILY_BUDGET_KRW)
    month_cost = 0.0
    total_cost = 0.0
    try:
        from ..store import wiki as wiki_store
        today_cost = wiki_store.today_cost_krw()
        budget = wiki_store.budget_krw()
        month_cost = wiki_store.month_cost_krw()
        total_cost = wiki_store.total_cost_krw()
    except Exception:
        pass

    wiki_stats = {
        "pages": len(topics_data),
        "docs": len(all_doc_ids),
        "queue": queue_size,
        "today_cost": today_cost,
        "budget": budget,
        "month_cost": month_cost,
        "total_cost": total_cost,
    }

    index_html = _render_index_page(topics_data, token, wiki_stats)
    idx_file = target / "index.html"
    tmp_file = target / f"index.html.{pid}.tmp"
    tmp_file.write_text(index_html, encoding="utf-8")
    os.replace(tmp_file, idx_file)

    try:
        cache_tmp = cache_path.with_name(f"{cache_path.name}.{pid}.tmp")
        cache_tmp.write_text(
            json.dumps({"fp": fp, "entries": new_entries},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(cache_tmp, cache_path)
    except Exception:
        log.exception("wiki render cache save failed (render still ok)")

    if deferred:
        log.info("wiki render: %d page(s) written, %d deferred to the next "
                 "tick (per-call cap %d) — rebuild in progress",
                 pages_written, deferred, _MAX_PAGES_PER_CALL)
    return pages_written
