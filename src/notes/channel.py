"""Orchestration: study-channel item → parsed text → note → vault.

Reuses the existing thesis loaders (`src/ingest/loaders.py`) for every
source type the user studies (PDF, web, YouTube, PPTX, DOCX, XLSX) so we
add no new parsing dependency in Phase 0. Each entry point returns the
saved note id (or None when the source yields no usable text).

Wiring into the live bot (a handler that routes the dedicated study
channel's messages here) is the integration step — these functions are
the stable surface that handler calls.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..ingest import loaders
from . import store, synth, triage

log = logging.getLogger(__name__)


async def _triaged_pdf_extract(p: Path) -> tuple[str, str | None]:
    """Phase 1 cost gate: per-page triage so we spend on OCR only where
    needed. TEXT_ONLY/LLM_NEEDED pages → free get_text; OCR_NEEDED pages →
    Gemini Vision; SKIP (blank) pages ignored. Raises on hard failure so
    the caller can fall back to the full loader (never makes ingest worse).
    """
    import fitz  # PyMuPDF
    report = await asyncio.to_thread(triage.triage_document, str(p))
    routes = report.routes()
    dpi = getattr(loaders, "OCR_DPI", 150)
    doc = await asyncio.to_thread(fitz.open, str(p))
    try:
        title = ((doc.metadata or {}).get("title") or "").strip() or p.stem
        page_text: dict[int, str] = {}
        for i in routes["text"] + routes["llm"]:        # free native text
            try:
                page_text[i] = doc[i].get_text("text") or ""
            except Exception:
                page_text[i] = ""
        # OCR is paid + slow (one Gemini Vision call/page). Cap it so a
        # fully-scanned PDF can't trigger N Vision calls — mirrors the
        # loader's image-only cap. Pages past the cap are skipped.
        ocr_pages = sorted(routes["ocr"])
        cap = getattr(loaders, "OCR_AUTO_CAP", 7)
        if len(ocr_pages) > cap:
            log.info("notes pdf triage: capping OCR %d→%d pages (%s)",
                     len(ocr_pages), cap, p.name)
            ocr_pages = ocr_pages[:cap]
        for i in ocr_pages:
            try:
                png = await asyncio.to_thread(
                    lambda idx=i: doc[idx].get_pixmap(dpi=dpi).tobytes("png"))
                page_text[i] = await loaders.ocr_image_async(png, "image/png") or ""
            except Exception:
                page_text[i] = ""
        # Table/chart-dense pages: native get_text garbles tables, so render
        # the page and let Gemini Vision rebuild it (markdown tables + the
        # surrounding prose) — the PixelRAG "see the page" insight, done
        # cheaply with our existing flash-lite Vision. Targets only real
        # table pages (PyMuPDF find_tables), capped to bound cost. Replaces
        # that page's garbled get_text only when Vision returns something.
        table_pages = sorted(
            r.page_number for r in report.results
            if r.bucket is triage.Bucket.LLM_NEEDED and r.has_tables)
        vcap = getattr(loaders, "VISION_TABLE_CAP", 8)
        n_table_vis = 0
        if len(table_pages) > vcap:
            log.info("notes pdf triage: capping table-vision %d→%d pages (%s)",
                     len(table_pages), vcap, p.name)
            table_pages = table_pages[:vcap]
        for i in table_pages:
            try:
                png = await asyncio.to_thread(
                    lambda idx=i: doc[idx].get_pixmap(dpi=dpi).tobytes("png"))
                vis = await loaders.ocr_image_async(png, "image/png")
                if vis and vis.strip():
                    page_text[i] = vis.strip()
                    n_table_vis += 1
            except Exception:
                pass  # keep the get_text already captured for this page
    finally:
        doc.close()
    log.info("notes pdf triage %s: %d pages → text=%d ocr=%d llm=%d skip=%d "
             "(table-vision=%d)",
             p.name, report.page_count, len(routes["text"]),
             len(routes["ocr"]), len(routes["llm"]), len(routes["skip"]),
             n_table_vis)
    body = "\n\n".join(page_text[i] for i in sorted(page_text)
                       if page_text[i].strip())
    return body.strip(), title


async def ingest_text(source_type: str, source_ref: str, raw_text: str,
                      title: str | None = None) -> str | None:
    """Core path: synthesise a note from already-extracted text, persist
    it. Returns the note id or None."""
    # RAG-style dedup: if this source (URL/file) already has a note, skip
    # — no duplicate note, no wasted LLM synth. Plain-text shares the
    # constant ref "study-text", so it's excluded (would collide).
    if source_type != "text" and source_ref:
        dup = await asyncio.to_thread(store.note_id_by_source, source_ref)
        if dup:
            log.info("notes ingest: duplicate source '%s' → skip (note %s)",
                     source_ref, dup)
            return dup
    log.info("notes ingest: %s '%s' (%d chars)",
             source_type, source_ref, len((raw_text or "")))
    note = await synth.synthesize(source_type, source_ref, raw_text, title)
    if not note:
        log.info("notes ingest: no note (synth skipped/failed) for %s",
                 source_ref)
        return None
    nid = store.save_note(note)
    log.info("notes ingest: saved note %s from %s", nid, source_ref)
    return nid


async def resync_one(note: dict) -> str:
    """Re-fetch + re-synthesise an existing URL/YouTube note IN PLACE.
    Used to repair notes synthesised from the title (the title/body bug).
    Returns: 'ok' | 'skip' (non-url) | 'empty' (fetch failed) |
    'synthfail'. Non-destructive: the old note is kept on any failure."""
    ref = (note.get("source_ref") or "").strip()
    stype = note.get("source_type") or "web"
    if not ref.startswith("http"):
        return "skip"
    vid = loaders.is_youtube(ref)
    if vid:
        title, body, _ = await loaders.load_youtube(vid, ref)
    else:
        title, body, _h, _l = await loaders.load_url(ref)
    if not (body or "").strip() or len((body or "").strip()) < 40:
        return "empty"
    new = await synth.synthesize(stype, ref, body, title)
    if not new:
        return "synthfail"
    ok = store.resync_note(
        note["id"], title=new.get("title") or title or note.get("title") or "",
        md=new.get("md") or "", questions=new.get("questions") or [],
        cost_krw=new.get("cost_krw"), gen_seconds=new.get("gen_seconds"))
    return "ok" if ok else "synthfail"


async def ingest_url(url: str) -> str | None:
    vid = loaders.is_youtube(url)
    if vid:
        return await ingest_youtube(url, vid)
    # load_url 자체가 강한 폴백 사다리(httpx→DOM→curl_cffi→Jina→Jina-browser)를
    # 가져서 JS/Notion 페이지도 한 번에 처리한다. 예전의 3회 외부 재시도는 그
    # 사다리를 통째로 3번 돌려 실패 URL 1건이 수 분씩 인입을 점유했음(학습 지연
    # 주범) → 1회로. 정상 페이지는 어차피 1차 성공이라 영향 없음.
    title, body, _hint, _links = await loaders.load_url(url)
    if not (body or "").strip():
        log.info("study ingest_url: empty body for %s", url)
        return None
    stype = "blog" if loaders.is_blog(url) else "web"
    return await ingest_text(stype, url, body, title)


async def ingest_youtube(url: str, video_id: str | None = None) -> str | None:
    vid = video_id or loaders.is_youtube(url)
    if not vid:
        return None
    title, body, _ = await loaders.load_youtube(vid, url)
    if not (body or "").strip():
        log.info("study ingest_youtube: no transcript for %s", url)
        return None
    return await ingest_text("youtube", url, body, title)


async def ingest_file(path: str | Path, source_type: str | None = None) -> str | None:
    """Dispatch a local file to the right loader by extension."""
    p = Path(path)
    ext = p.suffix.lower().lstrip(".")
    stype = source_type or ext
    try:
        if ext == "pdf":
            # Phase 1: triage-gated extraction; fall back to the full
            # loader on any failure or empty result so triage can never
            # make ingest worse than before.
            try:
                body, title = await _triaged_pdf_extract(p)
            except Exception as e:
                log.warning("notes pdf triage failed (%s) — fallback: %s",
                            p.name, str(e)[:120])
                body = ""
                title = None
            if not (body or "").strip():
                title, body, _src, _ocr = await loaders.load_pdf_async(p)
        elif ext == "pptx":
            title, body, _ = await loaders.load_pptx_async(p)
        elif ext == "docx":
            title, body, _ = await loaders.load_docx_async(p)
        elif ext in ("xlsx", "xls"):
            title, body, _ = await loaders.load_xlsx_async(p)
        else:
            log.info("study ingest_file: unsupported ext '%s'", ext)
            return None
    except Exception as e:
        log.warning("study ingest_file load failed (%s): %s", p.name, str(e)[:160])
        return None
    if not (body or "").strip():
        log.info("study ingest_file: empty body for %s", p.name)
        return None
    return await ingest_text(stype, p.name, body, title)
