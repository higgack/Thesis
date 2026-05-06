import hashlib
import logging
from pathlib import Path

from .. import config
from ..store import meta, vector, notion
from .chunker import split
from .loaders import load_url, load_pdf
from .summarize import summarize

log = logging.getLogger(__name__)


def _doc_id(source: str) -> str:
    return hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]


async def ingest_url(url: str) -> dict:
    if existing := meta.find_by_source(url):
        return {"status": "duplicate", "doc_id": existing["id"], "title": existing["title"]}
    title, body = await load_url(url)
    if not body:
        return {"status": "empty", "title": title}
    return await _ingest("url", url, title, body)


async def ingest_pdf(path: Path, source_label: str) -> dict:
    if existing := meta.find_by_source(source_label):
        return {"status": "duplicate", "doc_id": existing["id"], "title": existing["title"]}
    title, body = load_pdf(path)
    if not body:
        return {"status": "empty", "title": title}
    return await _ingest("pdf", source_label, title, body)


async def ingest_text(text: str, label: str = "text") -> dict:
    src = f"{label}:{hashlib.sha1(text.encode()).hexdigest()[:8]}"
    if existing := meta.find_by_source(src):
        return {"status": "duplicate", "doc_id": existing["id"], "title": existing["title"]}
    title = text.strip().splitlines()[0][:80] if text.strip() else label
    return await _ingest("text", src, title, text)


async def _ingest(doc_type: str, source: str, title: str, body: str) -> dict:
    doc_id = _doc_id(source)
    log.info("ingest %s %s (%d chars)", doc_type, source, len(body))

    summary = await summarize(title, body)
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

    notion_page_id = None
    if notion.enabled():
        try:
            notion_page_id = await notion.create_page(
                title=title, source=source, doc_type=doc_type,
                summary=summary, doc_id=doc_id, body=body,
            )
        except Exception as e:
            log.exception("notion sync failed: %s", e)

    meta.upsert_doc(doc_id, source, doc_type, title, summary, notion_page_id)
    return {
        "status": "ok",
        "doc_id": doc_id,
        "title": title,
        "type": doc_type,
        "chunks": len(chunks),
        "summary_chars": len(summary),
        "notion_page_id": notion_page_id,
    }
