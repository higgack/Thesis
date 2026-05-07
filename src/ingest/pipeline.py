import asyncio
import hashlib
import logging
import re
from pathlib import Path

from .. import config
from ..store import meta, vector, obsidian
from .chunker import split
from .loaders import load_url, load_pdf_async, load_arxiv, load_pptx_async, load_docx_async, load_xlsx_async, ocr_image_async
from .summarize import summarize

log = logging.getLogger(__name__)

_ARXIV_IN_PDF = re.compile(r"arXiv:\s*(\d{4}\.\d{4,5})", re.IGNORECASE)


def _doc_id(source: str) -> str:
    return hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]


async def ingest_url(url: str) -> dict:
    if existing := meta.find_by_source(url):
        return {"status": "duplicate", "doc_id": existing["id"], "title": existing["title"]}
    title, body, hint = await load_url(url)
    if not body:
        return {"status": "empty", "title": title}
    return await _ingest("url", url, title, body, hint)


async def ingest_pdf(path: Path, source_label: str) -> dict:
    if existing := meta.find_by_source(source_label):
        return {"status": "duplicate", "doc_id": existing["id"], "title": existing["title"]}
    title, body, hint = await load_pdf_async(path)
    if not body:
        return {"status": "empty", "title": title}
    if hint is None and (m := _ARXIV_IN_PDF.search(body[:5000])):
        try:
            ax_title, ax_body, ax_hint = await load_arxiv(m.group(1))
            if ax_hint:
                hint = ax_hint
            if ax_title and (not title or title == path.stem):
                title = ax_title
            log.info("PDF detected as arXiv:%s, using free abstract", m.group(1))
        except Exception as e:
            log.warning("arxiv enrich failed: %s", e)
    doc_type = "paper" if hint else "pdf"
    return await _ingest(doc_type, source_label, title, body, hint)


async def ingest_pptx(path: Path, source_label: str) -> dict:
    if existing := meta.find_by_source(source_label):
        return {"status": "duplicate", "doc_id": existing["id"], "title": existing["title"]}
    title, body, hint = await load_pptx_async(path)
    if not body:
        return {"status": "empty", "title": title}
    return await _ingest("pptx", source_label, title, body, hint)


async def ingest_docx(path: Path, source_label: str) -> dict:
    if existing := meta.find_by_source(source_label):
        return {"status": "duplicate", "doc_id": existing["id"], "title": existing["title"]}
    title, body, hint = await load_docx_async(path)
    if not body:
        return {"status": "empty", "title": title}
    return await _ingest("docx", source_label, title, body, hint)


async def ingest_xlsx(path: Path, source_label: str) -> dict:
    if existing := meta.find_by_source(source_label):
        return {"status": "duplicate", "doc_id": existing["id"], "title": existing["title"]}
    title, body, hint = await load_xlsx_async(path)
    if not body:
        return {"status": "empty", "title": title}
    return await _ingest("xlsx", source_label, title, body, hint)


async def ingest_image(img_bytes: bytes, source_label: str, caption: str = "",
                       mime_type: str = "image/jpeg") -> dict:
    """Standalone photo: caption-first, OCR fallback. If caption is long
    enough we skip the LLM call entirely (free path)."""
    if existing := meta.find_by_source(source_label):
        return {"status": "duplicate", "doc_id": existing["id"], "title": existing["title"]}
    caption = (caption or "").strip()
    if len(caption) >= 80:
        body = caption
    else:
        ocr_text = await ocr_image_async(img_bytes, mime_type=mime_type)
        if not ocr_text and not caption:
            return {"status": "empty", "title": "image"}
        body = (caption + "\n\n" + ocr_text).strip() if caption else ocr_text
    first = body.strip().splitlines()[0] if body.strip() else "image"
    title = first[:80]
    return await _ingest("image", source_label, title, body, None)


async def ingest_text(text: str, label: str = "text") -> dict:
    src = f"{label}:{hashlib.sha1(text.encode()).hexdigest()[:8]}"
    if existing := meta.find_by_source(src):
        return {"status": "duplicate", "doc_id": existing["id"], "title": existing["title"]}
    title = text.strip().splitlines()[0][:80] if text.strip() else label
    return await _ingest("text", src, title, text, None)


async def _ingest(doc_type: str, source: str, title: str, body: str,
                  hint: str | None) -> dict:
    doc_id = _doc_id(source)
    log.info("ingest %s %s (%d chars, hint=%s)",
             doc_type, source, len(body), bool(hint))

    chunks = split(body)
    chunk_items = [{
        "id": f"{doc_id}:{i}",
        "text": c,
        "kind": "chunk",
        "idx": i,
    } for i, c in enumerate(chunks)]

    # Summary (Gemini text gen) and chunk embedding (Gemini embed) hit
    # different endpoints — run them in parallel.
    summary, _ = await asyncio.gather(
        summarize(title, body, hint=hint),
        vector.add_chunks(doc_id, chunk_items),
    )

    await vector.add_chunks(doc_id, [{
        "id": f"{doc_id}:s",
        "text": f"[요약] {title}\n{summary}",
        "kind": "summary",
        "idx": -1,
    }])

    obsidian_path = None
    if obsidian.enabled():
        try:
            obsidian_path = await obsidian.write_note(
                doc_type=doc_type, title=title, source=source,
                summary=summary, body=body, doc_id=doc_id,
            )
        except Exception as e:
            log.exception("obsidian sync failed: %s", e)

    meta.upsert_doc(doc_id, source, doc_type, title, summary, obsidian_path)
    return {
        "status": "ok",
        "doc_id": doc_id,
        "title": title,
        "type": doc_type,
        "chunks": len(chunks),
        "summary_chars": len(summary),
        "obsidian_path": obsidian_path,
        "summary_source": "hint" if hint and config.HINT_SUMMARY_MIN_CHARS <= len(hint) <= config.HINT_SUMMARY_MAX_CHARS else "llm",
    }
