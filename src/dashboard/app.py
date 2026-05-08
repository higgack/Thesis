"""Read-only browser for the personal Q&A archive.

Lightweight FastAPI app rendered with Jinja. Lives in its own compose
service (`dashboard`) so a redeploy of the bot doesn't churn the
dashboard, and so the dashboard's deps stay isolated.

Authentication is a single shared owner token via ?t=… query string
or Authorization: Bearer header. Set DASHBOARD_TOKEN in .env. With no
token configured the app refuses to start so an open instance can't
ship by accident.
"""
from __future__ import annotations

import os
from collections import OrderedDict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..store import qna, cost, meta as meta_store, vector as vector_store


_TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()
if not _TOKEN:
    raise SystemExit(
        "DASHBOARD_TOKEN is required. Add a non-empty value to .env "
        "before launching the dashboard service."
    )

app = FastAPI(title="Second Brain Archive")


def _check(token: str | None, request: Request) -> None:
    candidate = (token or "").strip()
    if not candidate:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            candidate = auth[7:].strip()
    if candidate != _TOKEN:
        raise HTTPException(status_code=403, detail="forbidden")


def _group_by_date(rows: list[dict]) -> "OrderedDict[str, list[dict]]":
    out: OrderedDict[str, list[dict]] = OrderedDict()
    for r in rows:
        day = (r.get("ts") or "")[:10]
        out.setdefault(day, []).append(r)
    return out


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, t: str | None = None,
                q: str | None = Query(default=None, description="search"),
                limit: int = 100):
    _check(t, request)
    rows = qna.recent(limit=limit, search=q)
    grouped = _group_by_date(rows)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "token": _TOKEN,
        "grouped": grouped,
        "total_shown": len(rows),
        "total_archive": qna.count(),
        "buckets": qna.date_buckets(60),
        "search": q or "",
        "limit": limit,
        "stats": {
            "docs": meta_store.count(),
            "chunks": vector_store.chunk_count(),
            "today_krw": cost.today_krw()["total_krw"],
            "month_krw": cost.period_krw(30)["total_krw"],
        },
    })


@app.get("/q/{qna_id}", response_class=HTMLResponse)
async def detail(request: Request, qna_id: int, t: str | None = None):
    _check(t, request)
    item = qna.get(qna_id)
    if not item:
        raise HTTPException(status_code=404, detail="not found")
    return templates.TemplateResponse("detail.html", {
        "request": request,
        "token": _TOKEN,
        "item": item,
    })


@app.get("/healthz")
async def healthz():
    return {"ok": True, "qna_count": qna.count()}


def main() -> None:
    import uvicorn
    port = int(os.getenv("DASHBOARD_PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
