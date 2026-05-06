import hashlib
import logging
import re
from pathlib import Path

from .. import config
from ..store import meta, vector, obsidian
from .chunker import split
from .loaders import load_url, load_pdf, load_arxiv
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
    title, body, hint = load_pdf(path)
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

    summary = await summarize(title, body, hint=hint)
    chunks = split(body)

    items = [{
        "id": f"{doc_id}:s",
        "text": f"[요약] {title}\n{summary}",
        "kind": "summary",
        "idx": -1,
    }]
    for i, c in enumerate(chunks):
        items.append({
            "id": f"{doc_id}:{i}",
            "text": c,
            "kind": "chunk",
            "idx": i,
        })
    await vector.add_chunks(doc_id, items)

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
