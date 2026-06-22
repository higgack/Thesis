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
        for i in routes["ocr"]:                          # OCR only these
            try:
                png = await asyncio.to_thread(
                    lambda idx=i: doc[idx].get_pixmap(dpi=dpi).tobytes("png"))
                page_text[i] = await loaders.ocr_image_async(png, "image/png") or ""
            except Exception:
                page_text[i] = ""
    finally:
        doc.close()
    log.info("notes pdf triage %s: %d pages → text=%d ocr=%d llm=%d skip=%d",
             p.name, report.page_count, len(routes["text"]),
             len(routes["ocr"]), len(routes["llm"]), len(routes["skip"]))
    body = "\n\n".join(page_text[i] for i in sorted(page_text)
                       if page_text[i].strip())
    return body.strip(), title


async def ingest_text(source_type: str, source_ref: str, raw_text: str,
                      title: str | None = None) -> str | None:
    """Core path: synthesise a note from already-extracted text, persist
    it. Returns the note id or None."""
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


async def ingest_url(url: str) -> str | None:
    vid = loaders.is_youtube(url)
    if vid:
        return await ingest_youtube(url, vid)
    body, title, _meta, _links = await loaders.load_url(url)
    if not (body or "").strip():
        log.info("study ingest_url: empty body for %s", url)
        return None
    return await ingest_text("web", url, body, title)


async def ingest_youtube(url: str, video_id: str | None = None) -> str | None:
    vid = video_id or loaders.is_youtube(url)
    if not vid:
        return None
    body, title, _ = await loaders.load_youtube(vid, url)
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
                body, title, _src, _ocr = await loaders.load_pdf_async(p)
        elif ext == "pptx":
            body, title, _ = await loaders.load_pptx_async(p)
        elif ext == "docx":
            body, title, _ = await loaders.load_docx_async(p)
        elif ext in ("xlsx", "xls"):
            body, title, _ = await loaders.load_xlsx_async(p)
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
