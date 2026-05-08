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
  --bg: #0f1419; --panel: #1a2028; --border: #2a3441;
  --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff;
  --tool-brain: #f78166; --tool-paper: #a371f7;
  --tool-web: #3fb950; --tool-ingest: #d29922;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font: 14px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI",
        "Apple SD Gothic Neo", "Noto Sans KR", sans-serif;
  background: var(--bg); color: var(--text);
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
"""

_INDEX_CSS = _BASE_CSS + """
.layout { max-width: 980px; margin: 0 auto; padding: 24px 20px 80px; }
header h1 {
  font-size: 18px; margin: 0 0 4px 0; color: var(--accent);
}
header .sub { color: var(--muted); font-size: 12px; margin-bottom: 16px; }
.stats {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px,1fr));
  gap: 8px; margin-bottom: 24px;
}
.stats div {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 6px; padding: 10px 12px; font-size: 12px;
  color: var(--muted);
}
.stats div b {
  display: block; color: var(--text); font-size: 16px; margin-top: 2px;
}
.day-header {
  font-size: 13px; color: var(--muted); margin: 24px 0 8px;
  padding-bottom: 6px; border-bottom: 1px solid var(--border);
  font-weight: 600; letter-spacing: 0.3px;
}
details {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px 16px; margin-bottom: 8px;
}
details[open] { padding-bottom: 18px; }
summary {
  cursor: pointer; list-style: none; outline: none;
}
summary::-webkit-details-marker { display: none; }
summary .row1 {
  display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
  font-size: 11px; color: var(--muted); margin-bottom: 6px;
}
summary .question {
  font-size: 14px; font-weight: 600; color: var(--text);
  word-break: break-word;
}
summary .question a { color: inherit; }
.tool {
  display: inline-block; padding: 1px 7px; border-radius: 3px;
  font-size: 10px; font-weight: 600;
}
.tool-brain { background: rgba(247,129,102,0.15); color: var(--tool-brain); }
.tool-paper { background: rgba(163,113,247,0.15); color: var(--tool-paper); }
.tool-web { background: rgba(63,185,80,0.15); color: var(--tool-web); }
.tool-ingest { background: rgba(210,153,34,0.15); color: var(--tool-ingest); }
.tool-other { background: rgba(139,148,158,0.15); color: var(--muted); }
.warning {
  background: rgba(210,153,34,0.1); border-left: 3px solid var(--tool-ingest);
  padding: 6px 10px; font-size: 12px; margin: 10px 0;
  border-radius: 0 4px 4px 0;
}
.answer {
  white-space: pre-wrap; word-break: break-word; line-height: 1.65;
  margin-top: 12px; color: #c9d1d9;
}
.sources {
  margin-top: 12px; padding-top: 10px;
  border-top: 1px dashed var(--border); font-size: 12px;
  color: var(--muted);
}
.sources li { padding: 2px 0; }
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
  border-radius: 8px; padding: 20px 24px;
  white-space: pre-wrap; word-break: break-word;
  line-height: 1.7; color: #c9d1d9;
}
.warning {
  background: rgba(210,153,34,0.1); border-left: 3px solid #d29922;
  padding: 8px 12px; font-size: 13px; margin-bottom: 16px;
  border-radius: 0 4px 4px 0;
}
.sources {
  margin-top: 24px; padding: 16px 20px;
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 8px; font-size: 13px;
}
.sources h3 { margin: 0 0 10px 0; font-size: 13px; color: var(--muted); }
.sources ul { margin: 0; padding-left: 18px; color: #c9d1d9; }
.sources li { padding: 3px 0; }
.tool {
  display: inline-block; padding: 2px 8px; border-radius: 3px;
  font-size: 11px; font-weight: 600; background: rgba(139,148,158,0.15);
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


def _render_index(rows: list[dict], stats: dict) -> str:
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        day = (r.get("ts") or "")[:10]
        grouped.setdefault(day, []).append(r)
    days_sorted = sorted(grouped.keys(), reverse=True)

    parts = [
        "<!DOCTYPE html><html lang='ko'><head>",
        "<meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>🧠 Second Brain Archive</title>",
        f"<style>{_INDEX_CSS}</style>",
        "</head><body><div class='layout'>",
        "<header><h1>🧠 Second Brain Archive</h1>",
        "<div class='sub'>저장된 모든 Q&amp;A — 영구 보관</div></header>",
        "<div class='stats'>",
        f"<div>총 Q&amp;A<b>{stats['total_qna']:,}</b></div>",
        f"<div>학습 문서<b>{stats['docs']:,}</b></div>",
        f"<div>청크<b>{stats['chunks']:,}</b></div>",
        f"<div>오늘 비용<b>₩{stats['today_krw']:,.0f}</b></div>",
        f"<div>30일 비용<b>₩{stats['month_krw']:,.0f}</b></div>",
        "</div>",
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
            f"<div class='day-header'>📅 {_esc(day)} "
            f"<span style='color:var(--muted);font-weight:400'>· {len(items)}건</span></div>"
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
            parts.append(
                "<details><summary>"
                "<div class='row1'>"
                f"<span>{_esc(it['ts'][11:16])}</span>"
                f"{tool_chips}{model_chip}"
                "</div>"
                "<div class='question'>"
                f"<a href='q-{int(it['id'])}.html'>Q. {_esc(it['question'])}</a>"
                "</div></summary>"
                f"{warn}"
                f"<div class='answer'>{_esc(it['answer'])}</div>"
                f"{sources_html}"
                "</details>"
            )

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
        "</head><body><main>",
        "<a class='back' href='index.html'>← 목록으로</a>",
        "<div class='meta'>",
        f"<span>{_esc(item['ts'][:10])} {_esc(item['ts'][11:16])}</span>",
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
            stats = {
                "total_qna": qna.count(),
                "docs": meta_store.count(),
                "chunks": vector_store.chunk_count(),
                "today_krw": cost.today_krw()["total_krw"],
                "month_krw": cost.period_krw(30)["total_krw"],
            }
            (target / "index.html").write_text(
                _render_index(rows, stats), encoding="utf-8"
            )
            for it in rows:
                fname = target / f"q-{int(it['id'])}.html"
                fname.write_text(_render_detail(it, token), encoding="utf-8")
    except Exception:
        log.exception("dashboard regenerate failed")
