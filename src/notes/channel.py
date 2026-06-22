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

import logging
from pathlib import Path

from ..ingest import loaders
from . import store, synth

log = logging.getLogger(__name__)


async def ingest_text(source_type: str, source_ref: str, raw_text: str,
                      title: str | None = None) -> str | None:
    """Core path: synthesise a note from already-extracted text, persist
    it. Returns the note id or None."""
    note = await synth.synthesize(source_type, source_ref, raw_text, title)
    if not note:
        return None
    return store.save_note(note)


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
