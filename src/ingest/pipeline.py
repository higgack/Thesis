import asyncio
import hashlib
import logging
import re
from pathlib import Path
from typing import Callable, Optional

from .. import config
from ..store import meta, vector, obsidian
from .chunker import split
from .loaders import load_url, load_pdf_async, load_arxiv, load_pptx_async, load_docx_async, load_xlsx_async, ocr_image_async, transcribe_audio_async
from .summarize import summarize, summarize_and_extract

log = logging.getLogger(__name__)


# Optional progress callback type. Bot passes a closure that writes the
# stage name into _ACTIVE_INGESTS[job_id]['stage'] so the live status
# bubble can show "Vision OCR" / "임베딩" instead of an opaque "처리 중".
StageCb = Optional[Callable[[str], None]]


def _emit(cb: StageCb, stage: str) -> None:
    if cb is None:
        return
    try:
        cb(stage)
    except Exception:
        pass

_ARXIV_IN_PDF = re.compile(r"arXiv:\s*(\d{4}\.\d{4,5})", re.IGNORECASE)


def _doc_id(source: str) -> str:
    return hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]


async def ingest_url(url: str) -> dict:
    if existing := meta.find_by_source(url):
        return {"status": "duplicate", "doc_id": existing["id"],
                "title": existing["title"], "source": url}
    title, body, hint = await load_url(url)
    if not body:
        return {"status": "empty", "title": title, "source": url}
    return await _ingest("url", url, title, body, hint)


async def ingest_pdf(path: Path, source_label: str,
                     on_stage: StageCb = None) -> dict:
    _emit(on_stage, "dedup")
    if existing := meta.find_by_source(source_label):
        return {"status": "duplicate", "doc_id": existing["id"], "title": existing["title"]}
    file_hash = _hash_file(path)
    if file_hash and (existing := meta.find_by_file_hash(file_hash)):
        return {"status": "duplicate", "doc_id": existing["id"],
                "title": existing["title"]}
    _emit(on_stage, "PDF 로드 / OCR")
    title, body, hint, ocr_meta = await load_pdf_async(path)
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
    result = await _ingest(doc_type, source_label, title, body, hint,
                           file_hash=file_hash, on_stage=on_stage)
    if ocr_meta and ocr_meta.get("capped") and result.get("status") == "ok":
        result["ocr_meta"] = {**ocr_meta, "pdf_path": str(path)}
    return result


def _hash_file(path: Path) -> str:
    """SHA-1 prefix of the file's bytes. 16 chars is plenty of entropy
    for collision avoidance across ~10k docs. Returns '' on read
    failure so the caller can fall back to source-label dedup only."""
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except Exception:
        return ""


async def extend_pdf_ocr(pdf_path: Path, doc_id: str,
                         start_page: int, end_page: int) -> dict:
    """Continue Vision OCR on pages [start_page, end_page] of an
    already-ingested PDF and append the extracted text as new chunks
    on the same doc. Pages whose PyMuPDF text is already dense get
    skipped — surfaces as `pages_skipped` so the user sees actual
    spend vs requested range."""
    from .loaders import ocr_pdf_pages_async
    if end_page < start_page:
        return {"status": "noop", "pages_attempted": 0,
                "pages_ocrd": 0, "pages_skipped": 0, "chunks_added": 0}
    max_pages = end_page - start_page + 1
    r = await ocr_pdf_pages_async(
        pdf_path, start_page=start_page, max_pages=max_pages,
    )
    ocr_text = r.get("text", "")
    if not ocr_text:
        return {
            "status": "empty", "pages_attempted": max_pages,
            "pages_ocrd": r.get("ocrd", 0),
            "pages_skipped": r.get("skipped", 0),
            "chunks_added": 0,
        }
    new_chunks = split(ocr_text)
    chunk_items = [{
        "id": f"{doc_id}:ocr-{start_page}:{i}",
        "text": c,
        "kind": "chunk",
        "idx": 10_000 + start_page + i,  # well above the dense-text idx range
    } for i, c in enumerate(new_chunks)]
    await vector.add_chunks(doc_id, chunk_items)
    log.info("extend_pdf_ocr doc=%s range=%d-%d ocrd=%d skipped=%d chunks=%d",
             doc_id, start_page, end_page, r["ocrd"], r["skipped"],
             len(chunk_items))
    return {
        "status": "ok",
        "doc_id": doc_id,
        "pages_attempted": max_pages,
        "pages_ocrd": r["ocrd"],
        "pages_skipped": r["skipped"],
        "chunks_added": len(chunk_items),
    }


async def ingest_pptx(path: Path, source_label: str,
                      on_stage: StageCb = None) -> dict:
    _emit(on_stage, "dedup")
    if existing := meta.find_by_source(source_label):
        return {"status": "duplicate", "doc_id": existing["id"], "title": existing["title"]}
    file_hash = _hash_file(path)
    if file_hash and (existing := meta.find_by_file_hash(file_hash)):
        return {"status": "duplicate", "doc_id": existing["id"], "title": existing["title"]}
    _emit(on_stage, "PPT 로드")
    title, body, hint = await load_pptx_async(path)
    if not body:
        return {"status": "empty", "title": title}
    return await _ingest("pptx", source_label, title, body, hint,
                         file_hash=file_hash, on_stage=on_stage)


async def ingest_docx(path: Path, source_label: str,
                      on_stage: StageCb = None) -> dict:
    _emit(on_stage, "dedup")
    if existing := meta.find_by_source(source_label):
        return {"status": "duplicate", "doc_id": existing["id"], "title": existing["title"]}
    file_hash = _hash_file(path)
    if file_hash and (existing := meta.find_by_file_hash(file_hash)):
        return {"status": "duplicate", "doc_id": existing["id"], "title": existing["title"]}
    _emit(on_stage, "DOCX 로드")
    title, body, hint = await load_docx_async(path)
    if not body:
        return {"status": "empty", "title": title}
    return await _ingest("docx", source_label, title, body, hint,
                         file_hash=file_hash, on_stage=on_stage)


async def ingest_xlsx(path: Path, source_label: str,
                      on_stage: StageCb = None) -> dict:
    _emit(on_stage, "dedup")
    if existing := meta.find_by_source(source_label):
        return {"status": "duplicate", "doc_id": existing["id"], "title": existing["title"]}
    file_hash = _hash_file(path)
    if file_hash and (existing := meta.find_by_file_hash(file_hash)):
        return {"status": "duplicate", "doc_id": existing["id"], "title": existing["title"]}
    _emit(on_stage, "XLSX 로드")
    title, body, hint = await load_xlsx_async(path)
    if not body:
        return {"status": "empty", "title": title}
    return await _ingest("xlsx", source_label, title, body, hint,
                         file_hash=file_hash, on_stage=on_stage)


async def ingest_audio(audio_bytes: bytes, source_label: str, caption: str = "",
                       mime_type: str = "audio/ogg") -> dict:
    """Voice note / audio file → Gemini STT → text ingest. Caption is
    prepended to the transcript so any user-provided context survives."""
    if existing := meta.find_by_source(source_label):
        return {"status": "duplicate", "doc_id": existing["id"], "title": existing["title"]}
    transcript = await transcribe_audio_async(audio_bytes, mime_type=mime_type)
    if not transcript:
        return {"status": "empty", "title": "audio"}
    caption = (caption or "").strip()
    body = (caption + "\n\n" + transcript).strip() if caption else transcript
    first_line = body.strip().splitlines()[0] if body.strip() else ""
    title = (caption.splitlines()[0][:80] if caption
             else first_line[:80] if first_line else "음성 메모")
    return await _ingest("audio", source_label, title, body, None)


async def ingest_image(img_bytes: bytes, source_label: str, caption: str = "",
                       mime_type: str = "image/jpeg") -> dict:
    """Standalone photo: caption-first, OCR fallback.

    Default: caption ≥ 80 chars uses caption only (free path).
    Override: include "[OCR]" anywhere in the caption to force Vision
    OCR alongside the caption — useful when the caption is a long
    analyst note but the image itself is a chart/table whose numbers
    matter (caption + OCR text are concatenated)."""
    if existing := meta.find_by_source(source_label):
        return {"status": "duplicate", "doc_id": existing["id"], "title": existing["title"]}
    caption = (caption or "").strip()
    force_ocr = "[OCR]" in caption.upper()
    if caption:
        # strip the tag from the stored body
        caption_clean = caption.replace("[OCR]", "").replace("[ocr]", "").strip()
    else:
        caption_clean = caption
    if len(caption_clean) >= 80 and not force_ocr:
        body = caption_clean
    else:
        ocr_text = await ocr_image_async(img_bytes, mime_type=mime_type)
        if not ocr_text and not caption_clean:
            return {"status": "empty", "title": "image"}
        body = (caption_clean + "\n\n" + ocr_text).strip() if caption_clean else ocr_text
    first = body.strip().splitlines()[0] if body.strip() else "image"
    title = first[:80]
    return await _ingest("image", source_label, title, body, None)


# Drop patterns: text bodies matching these are silently skipped at
# ingest time so retry-queue items + any bypassed forwards are filtered
# consistently with forward-listener's _PLAIN_DROP_PATTERNS. Each entry
# is a compiled regex. Add narrative-free auto-generated formats here.
_TEXT_DROP_PATTERNS = (
    # 🚨 알파 스캐너 시리즈 (FundEasy_Earnings) — ticker tables only,
    # no narrative. Same pattern as forward-listener side, applied
    # here too in case the message slipped through (legacy retry-queue
    # entries, manual paste, etc.).
    re.compile(r"알파\s*스캐너", re.UNICODE),
)


async def ingest_text(text: str, label: str = "text") -> dict:
    for pat in _TEXT_DROP_PATTERNS:
        if pat.search(text):
            log.info("ingest text dropped (pattern %r): %s",
                     pat.pattern, text[:60].replace("\n", " "))
            return {"status": "skipped",
                    "title": text.strip().splitlines()[0][:80] if text.strip() else label,
                    "detail": "drop pattern matched"}
    hash8 = hashlib.sha1(text.encode()).hexdigest()[:8]
    src = f"{label}:{hash8}"
    # Exact source match handles the common case (same upload pasted
    # twice). Content-hash fallback handles re-forwarded TG messages
    # where the wrapping msg_id changes but the body is byte-identical —
    # without this, every digest expansion re-summarises and re-embeds
    # already-known text at ~₩0.5 / doc plus minutes of pipeline time.
    if existing := meta.find_by_source(src):
        return {"status": "duplicate", "doc_id": existing["id"], "title": existing["title"]}
    if existing := meta.find_text_by_hash(hash8):
        return {"status": "duplicate", "doc_id": existing["id"], "title": existing["title"]}
    title = text.strip().splitlines()[0][:80] if text.strip() else label
    return await _ingest("text", src, title, text, None)


_META_EXTRACT_PROMPT = (
    "Extract structured metadata from this document. Output JSON only:\n"
    '{"company": "주 분석/언급 대상 회사명 한 개 (모호하면 빈 문자열)",\n'
    ' "tags": ["반도체"|"AI"|"바이오"|"방산"|"로봇"|"보고서"|"뉴스"|"실적"|'
    '"분석"|"차트"|"공시"|"인터뷰"|"리포트" 등 적절한 1~5개],\n'
    ' "report_date": "본문 내 발행일 YYYY.MM 형태, 없으면 빈 문자열"}\n\n'
    "Rules:\n"
    "- company는 정확한 회사명만. 삼성전자/삼성전기/삼성SDI 구분.\n"
    "- 산업 동향/매크로/일반 분석이면 company는 빈 문자열.\n"
    "- 다국적/해외 기업도 OK (예: NVIDIA, TSMC, ARM).\n"
    "- tags는 일반 카테고리 + 핵심 주제."
)


async def _extract_metadata(title: str, body: str, doc_type: str) -> dict:
    """One Flash-Lite call to tag each ingested doc with company name,
    topic tags, and report date. Cost ≈ ₩0.5/doc. Failures return {}
    so they never block ingest."""
    if len(body) < 100:
        return {}
    sample = (body[:1500] + "\n...\n" + body[-300:]) if len(body) > 2000 else body
    user_msg = (
        f"Title: {title}\n"
        f"Type: {doc_type}\n"
        f"Body:\n{sample}"
    )
    try:
        from ..llm.gemini import complete
        import json as _json
        import re as _re
        resp = await complete(
            model=config.SUMMARY_MODEL,
            system="You extract structured metadata. Output JSON only.",
            user=f"{_META_EXTRACT_PROMPT}\n\n{user_msg}",
            max_tokens=300,
            temperature=0.0,
            purpose="ingest",
        )
        match = _re.search(r"\{.*\}", resp, _re.DOTALL)
        if match:
            data = _json.loads(match.group(0))
            company = (data.get("company") or "").strip()
            tags = data.get("tags") or []
            if not isinstance(tags, list):
                tags = []
            return {
                "company": company or None,
                "tags": [t.strip() for t in tags if isinstance(t, str) and t.strip()][:8],
                "report_date": (data.get("report_date") or "").strip() or None,
            }
    except Exception as e:
        log.warning("metadata extract failed: %s", e)
    return {}


async def _ingest(doc_type: str, source: str, title: str, body: str,
                  hint: str | None, file_hash: str | None = None,
                  on_stage: StageCb = None) -> dict:
    doc_id = _doc_id(source)
    log.info("ingest %s %s (%d chars, hint=%s)",
             doc_type, source, len(body), bool(hint))

    _emit(on_stage, "청크 분할")
    chunks = split(body)
    chunk_items = [{
        "id": f"{doc_id}:{i}",
        "text": c,
        "kind": "chunk",
        "idx": i,
    } for i, c in enumerate(chunks)]

    skip_meta = source.startswith("tg-msg:") and len(body) < 500

    _emit(on_stage, f"요약 + 임베딩 ({len(chunks)} 청크)")
    (summary, metadata), _ = await asyncio.gather(
        summarize_and_extract(title, body, doc_type, hint=hint, skip_meta=skip_meta),
        vector.add_chunks(doc_id, chunk_items),
    )

    _emit(on_stage, "요약 임베딩")
    await vector.add_chunks(doc_id, [{
        "id": f"{doc_id}:s",
        "text": f"[요약] {title}\n{summary}",
        "kind": "summary",
        "idx": -1,
    }])

    _emit(on_stage, "저장")
    obsidian_path = None
    if obsidian.enabled():
        try:
            obsidian_path = await obsidian.write_note(
                doc_type=doc_type, title=title, source=source,
                summary=summary, body=body, doc_id=doc_id,
            )
        except Exception as e:
            log.exception("obsidian sync failed: %s", e)

    meta.upsert_doc(doc_id, source, doc_type, title, summary, obsidian_path,
                    metadata=metadata or None, file_hash=file_hash)
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
