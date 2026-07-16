import asyncio
import hashlib
import logging
import re
from pathlib import Path
from typing import Callable, Optional

from .. import config
from ..store import meta, vector, obsidian, wiki
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


# Korean finance/investing blogs almost universally lead with a
# boilerplate disclaimer ("주의사항 (Disclaimer) 본인은 본 글에...")
# that ends ~500-1500 chars later, just before a section header like
# "세 줄 요약". Without pruning, chunk #0 is half disclaimer — useless
# noise that dilutes embeddings and hurts retrieval. Only fires when
# BOTH the start marker (within first 500 chars) AND a known end
# marker (within 3500 chars after) are found, so it's a no-op on
# posts without this structure.
_DISCLAIMER_START_RE = re.compile(
    r"주의사항\s*[\(（]\s*Disclaimer\s*[\)）]", re.IGNORECASE
)
_DISCLAIMER_END_MARKERS = (
    "세 줄 요약", "세줄 요약", "세줄요약",
    "한 줄 요약", "한줄 요약", "한줄요약",
    "Summary",
)


def _strip_disclaimer(text: str) -> str:
    if not text:
        return text
    m = _DISCLAIMER_START_RE.search(text[:500])
    if not m:
        return text
    start = m.start()
    after_start = start + len(m.group(0))
    search_window = text[after_start:after_start + 3500]
    end_offset = -1
    for marker in _DISCLAIMER_END_MARKERS:
        idx = search_window.find(marker)
        if idx > 0 and (end_offset < 0 or idx < end_offset):
            end_offset = idx
    if end_offset < 0:
        return text  # no end marker found — don't risk stripping body
    pruned = (text[:start] + text[after_start + end_offset:]).strip()
    log.info("stripped disclaimer: %d chars removed", len(text) - len(pruned))
    return pruned


def _doc_id(source: str) -> str:
    return hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]


_URL_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "referrer",
    "__source", "source", "share", "sharedfrom",
}


_YOUTUBE_ID_RE = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/|"
    r"youtube\.com/embed/|m\.youtube\.com/watch\?v=)([\w-]{11})"
)
_ARXIV_ID_RE = re.compile(r"arxiv\.org/(?:abs|pdf|html)/([\w.\-]+?)(?:v\d+)?(?:\.pdf)?$")


def _canonical_url(url: str) -> str:
    """Aggressively canonicalise URLs so trivial differences (tracking
    params, scheme, www prefix, mobile subdomain, trailing slash,
    YouTube short-link, arXiv version) all dedup to ONE entry.

    Examples:
      http://www.example.com/post?utm_source=tw → https://example.com/post
      youtu.be/aBcDeFgHiJk                       → https://www.youtube.com/watch?v=aBcDeFgHiJk
      arxiv.org/pdf/2401.12345v2.pdf             → https://arxiv.org/abs/2401.12345
      m.example.com/article                      → https://example.com/article
    """
    if not url:
        return url
    try:
        # YouTube → canonical watch URL (id-based).
        m = _YOUTUBE_ID_RE.search(url)
        if m:
            return f"https://www.youtube.com/watch?v={m.group(1)}"
        # arXiv → canonical abs URL with version stripped.
        m = _ARXIV_ID_RE.search(url)
        if m:
            return f"https://arxiv.org/abs/{m.group(1)}"

        from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse
        p = urlparse(url)
        scheme = "https"  # force https — most sites redirect anyway
        netloc = p.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        if netloc.startswith("m."):
            netloc = netloc[2:]
        path = p.path.rstrip("/") or "/"
        kept = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
                if k.lower() not in _URL_TRACKING_PARAMS]
        return urlunparse((scheme, netloc, path, p.params,
                           urlencode(kept), ""))
    except Exception:
        return url


# Access-denied / paywall / bot-check pages return a short body that
# isn't empty, so they slip past the `if not body` guard and get learned
# as junk (e.g. a 13-char "Access Denied" doc). Treat them like an empty
# body: not learned, host failure recorded (→ auto-block after 2), routed
# to /failed (manual retry possible) instead of churning the retry queue.
# Only UNAMBIGUOUS denial markers — phrases that essentially never
# appear in a real article body. Ambiguous Korean paywall/login words
# (로그인/구독/유료/결제) are deliberately excluded: they show up in
# legitimate short articles too, so keying on them would route real
# content to /failed. Better to occasionally learn a tiny paywall stub
# than to drop a genuine article.
_DENIAL_MARKERS = (
    "access denied", "403 forbidden", "forbidden", "unauthorized",
    "are you a robot", "are you human", "captcha", "cloudflare",
    "enable javascript", "please enable js", "bot detection",
    "verify you are human", "접근이 거부", "권한이 없", "차단되었",
    "로봇이 아닙니",
)


def _looks_like_denial(title: str, body: str) -> bool:
    """True when a SHORT page (<500 chars) is dominated by an
    access-denied / bot-check marker. Length gate keeps real articles
    (which are longer and rarely contain these phrases) safe; a false
    positive is recoverable anyway — it lands in /failed, not lost."""
    b = (body or "").strip()
    if len(b) >= 500:
        return False
    hay = f"{title or ''} {b}".lower()
    return any(m in hay for m in _DENIAL_MARKERS)


async def ingest_url(url: str, on_stage: StageCb = None) -> dict:
    # Resolve URL shorteners FIRST so dedup, blocked-host check, and
    # canonicalisation operate on the real destination. Without this,
    # every buly.kr/bit.ly-laced digest gets all its URLs silent-blocked
    # at the shortener-domain level even when the real target is a
    # perfectly fetchable article.
    from .loaders import unshorten_url
    url = await unshorten_url(url)
    canonical = _canonical_url(url)
    if existing := meta.find_by_source(canonical):
        return {"status": "duplicate", "doc_id": existing["id"],
                "title": existing["title"], "source": canonical}
    # Also catch the raw form (older entries may have been stored
    # without canonicalisation).
    if canonical != url and (existing := meta.find_by_source(url)):
        return {"status": "duplicate", "doc_id": existing["id"],
                "title": existing["title"], "source": url}
    # Blocked host short-circuit: paywalls / X / URL shorteners / forum
    # boards return nothing useful and previously piled up in the
    # failed log on every digest cite. Return a 'blocked' status that
    # the caller treats as a silent skip (no retry, no failed entry).
    from .loaders import _is_blocked_host
    if _is_blocked_host(canonical):
        return {"status": "blocked", "title": canonical, "source": canonical}
    # Auto-learned blocklist: hosts that have failed body extraction
    # N times in a row. Saves ~₩0 (no LLM call here either) but skips
    # the wasted trafilatura + Jina fallback + empty-body failed log
    # entry that piles up otherwise. Reset on first success.
    from ..store import url_blocklist
    if url_blocklist.is_blocked(canonical):
        log.info("ingest_url skip — auto-blocked host: %s", canonical[:120])
        return {"status": "blocked", "title": canonical, "source": canonical,
                "detail": "auto-blocked (반복 추출 실패)"}
    _emit(on_stage, "URL 로드")
    title, body, hint, outlinks = await load_url(canonical)
    if not body or _looks_like_denial(title, body):
        url_blocklist.record_failure(canonical)
        detail = "접근 거부/페이월 추정" if body else "본문 추출 실패"
        return {"status": "empty", "title": title, "source": canonical,
                "detail": detail}
    # Body extracted OK — reset any prior failure count for this host.
    url_blocklist.record_success(canonical)
    body = _strip_disclaimer(body)
    result = await _ingest("url", canonical, title, body, hint, on_stage=on_stage)
    # Surface in-post links the author embedded so the bot can offer
    # them as optional follow-up ingests (the user picks which). Only on
    # a fresh successful learn — duplicates/empties have nothing new.
    if isinstance(result, dict) and result.get("status") == "ok" and outlinks:
        fresh = _filter_outlinks(outlinks)
        if fresh:
            result["found_links"] = fresh
    return result


def _filter_outlinks(links: list[dict]) -> list[dict]:
    """Trim discovered in-post links to ones worth offering: drop
    already-learned URLs and statically/auto-blocked hosts, so the link
    prompt never shows a target that would just be skipped. Each item is
    {"url","anchor"}; the shape is preserved."""
    from .loaders import _is_blocked_host
    from ..store import url_blocklist
    out: list[dict] = []
    for item in links:
        u = item.get("url") if isinstance(item, dict) else item
        if not u:
            continue
        try:
            c = _canonical_url(u)
        except Exception:
            c = u
        if meta.find_by_source(c) or meta.find_by_source(u):
            continue
        if _is_blocked_host(c) or url_blocklist.is_blocked(c):
            continue
        out.append(item if isinstance(item, dict) else {"url": u, "anchor": ""})
    return out


async def ingest_pdf(path: Path, source_label: str,
                     on_stage: StageCb = None) -> dict:
    _emit(on_stage, "dedup")
    if existing := meta.find_by_source(source_label):
        return {"status": "duplicate", "doc_id": existing["id"], "title": existing["title"]}
    file_hash = _hash_file(path)
    if file_hash and (existing := meta.find_by_file_hash(file_hash)):
        return {"status": "duplicate", "doc_id": existing["id"],
                "title": existing["title"]}
    _emit(on_stage, "PDF 로드")
    title, body, hint, ocr_meta = await load_pdf_async(path, on_stage=on_stage)
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


STT_MAX_BYTES = 20 * 1024 * 1024


async def ingest_audio(audio_bytes: bytes, source_label: str, caption: str = "",
                       mime_type: str = "audio/ogg") -> dict:
    """Voice note / audio file → Gemini STT → text ingest. Caption is
    prepended to the transcript so any user-provided context survives.

    HARD size cap (STT_MAX_BYTES=20MB): Gemini's inline-bytes limit is
    ~20MB, so a bigger file can never transcribe — but building the
    request base64-inflates it +50% in RAM first. A forwarded 128MB
    lecture mp3 spiked the bot to 94.7% of its 5.5GB cgroup and stalled
    the event loop (2026-07-04 incident). Refuse early with a clear
    message instead."""
    if len(audio_bytes) > STT_MAX_BYTES:
        mb = len(audio_bytes) // (1024 * 1024)
        return {"status": "empty",
                "title": f"오디오 {mb}MB — STT 한도 20MB 초과 (20분 안팎으로 분할해서 다시)"}
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
#
# Conservative policy (2026-05): only 알파 스캐너 remains in this list.
# Earlier additions for 신고가 리스트 / ✅ +X% / 📌 시가총액 were
# reverted after the user decided those carry SOME signal value (price
# context, market-cap snapshot, daily sector roundup) that's worth
# preserving even though the body is noisy.
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
    ' "report_date": "본문 내 발행일 YYYY.MM 또는 YYYY.MM.DD, 없으면 빈 문자열",\n'
    ' "brokerage": "발행 증권사/연구소/언론사 (NH투자증권/신한투자증권/SK증권/'
    '현대차증권/Goldman Sachs/Bloomberg 등), 없으면 빈 문자열",\n'
    ' "analyst": "리포트 작성 애널리스트 1명 (본문 첫 페이지/footer/email '
    '라인에서 추출), 없으면 빈 문자열"}\n\n'
    "Rules:\n"
    "- company는 정확한 회사명만. 삼성전자/삼성전기/삼성SDI 구분.\n"
    "- 산업 동향/매크로/일반 분석이면 company는 빈 문자열.\n"
    "- 다국적/해외 기업도 OK (예: NVIDIA, TSMC, ARM).\n"
    "- tags는 일반 카테고리 + 핵심 주제.\n"
    "- brokerage / analyst 는 본문에 명시된 경우만 채움. 추측 금지."
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
                "brokerage": (data.get("brokerage") or "").strip() or None,
                "analyst": (data.get("analyst") or "").strip() or None,
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

    # Cross-format body dedup: catches the case where the SAME source
    # material was already ingested in a different file format (PDF
    # exported as PPTX, paste of an article that was also fetched via
    # URL, etc.). Computed on the extracted body so it ignores
    # container differences. Returned 'duplicate' bypasses chunking +
    # summary + embedding entirely.
    body_hash = meta.compute_body_hash(body)
    if body_hash:
        existing = meta.find_by_body_hash(body_hash)
        if existing and existing.get("id") != doc_id:
            log.info("ingest body-hash duplicate of %s for %s",
                     existing["id"], source)
            return {"status": "duplicate", "doc_id": existing["id"],
                    "title": existing["title"]}

    # body_signature: short prefix hash used by find_by_normalized_title
    # to distinguish docs with identical titles but different opening
    # paragraphs (e.g. one channel posts both pre- and post-market
    # writeups of the same earnings event with the same headline).
    body_signature = meta.compute_body_signature(body)

    # Title-similarity fallback. Less precise than body_hash (a
    # 4-word title 'Q1 2026 실적 발표' can clash across companies) so
    # only trips on titles that are sufficiently distinctive — the
    # ≥6 normalised-chars guard inside find_by_normalized_title
    # filters generic stubs. Catches the case where the same article
    # arrives with slightly different body wording (re-posted, paraphrased)
    # but exactly the same headline. body_signature cross-check rescues
    # the legit same-title-different-content case.
    title_existing = meta.find_by_normalized_title(
        title, body_signature=body_signature
    )
    if title_existing and title_existing.get("id") != doc_id:
        log.info("ingest title-norm duplicate of %s for %s",
                 title_existing["id"], source)
        return {"status": "duplicate", "doc_id": title_existing["id"],
                "title": title_existing["title"]}

    _emit(on_stage, "청크 분할")
    # tiktoken-encoding the whole body is CPU-bound; keep it off the
    # event loop so a big doc's chunking doesn't freeze the typing
    # indicator + concurrent Q&A.
    chunks = await asyncio.to_thread(split, body)
    chunk_items = [{
        "id": f"{doc_id}:{i}",
        "text": c,
        "kind": "chunk",
        "idx": i,
    } for i, c in enumerate(chunks)]

    # Metadata gating ([C] cost cut, 2026-05): skip the company/tags/
    # report_date extraction for inputs that almost never carry that
    # structure — short forwards, image OCR captions, plain photos,
    # voice/audio transcripts, and forwarded captions. The summary
    # itself still runs; only the structured-meta JSON pull is
    # skipped, saving ~₩0.2 per skipped doc. Long PDF/PPTX/DOCX/URL
    # paths are unchanged so analyst reports keep their company tag.
    skip_meta = (
        (source.startswith("tg-msg:") and len(body) < 500)
        or source.startswith("tg-photo:")
        or source.startswith("tg-voice:")
        or source.startswith("tg-audio:")
        or source.startswith("tg-doc-caption:")
        or doc_type == "image"
        or len(body) < 800
    )

    _emit(on_stage, f"요약 + 임베딩 ({len(chunks)} 청크)")
    # return_exceptions so a summarize failure doesn't leave add_chunks
    # running detached (plain gather propagates the first error while the
    # sibling keeps writing; the retry then re-adds the same chunk ids
    # concurrently). Both finish, THEN we re-raise the first failure into
    # the existing retry-classification path.
    _res = await asyncio.gather(
        summarize_and_extract(title, body, doc_type, hint=hint, skip_meta=skip_meta),
        vector.add_chunks(doc_id, chunk_items),
        return_exceptions=True,
    )
    for _r in _res:
        if isinstance(_r, BaseException):
            raise _r
    (summary, metadata), _ = _res

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

    # Final dedup re-check (close the race window). The body_hash /
    # title checks above ran BEFORE the multi-second embed+summarize
    # work; a second ingest of the same content (e.g. the same article
    # forwarded twice, or URL + paste) can pass those early checks while
    # we were busy, then both reach here and write two rows with
    # different doc_ids. Re-checking immediately before the write shrinks
    # the window from "entire embedding duration" to microseconds. If we
    # lost the race, roll back the chunks we just added under our doc_id
    # so Chroma doesn't accumulate orphan vectors, and report duplicate.
    if body_hash:
        existing = meta.find_by_body_hash(body_hash)
        if existing and existing.get("id") != doc_id:
            log.info("ingest body-hash duplicate (late) of %s for %s",
                     existing["id"], source)
            await asyncio.to_thread(vector.delete_doc, doc_id)
            return {"status": "duplicate", "doc_id": existing["id"],
                    "title": existing["title"]}
    title_existing = meta.find_by_normalized_title(
        title, body_signature=body_signature
    )
    if title_existing and title_existing.get("id") != doc_id:
        log.info("ingest title-norm duplicate (late) of %s for %s",
                 title_existing["id"], source)
        await asyncio.to_thread(vector.delete_doc, doc_id)
        return {"status": "duplicate", "doc_id": title_existing["id"],
                "title": title_existing["title"]}

    meta.upsert_doc(doc_id, source, doc_type, title, summary, obsidian_path,
                    metadata=metadata or None, file_hash=file_hash,
                    body_hash=body_hash or None,
                    body_signature=body_signature or None)

    # LLM Wiki (additive, dormant unless WIKI_ENABLED=1): queue this doc
    # for the nightly synthesis batch. No LLM / network here — a cheap
    # local append that self-guards on enabled() + swallows all errors,
    # so it can NEVER slow or break ingest. See src/store/wiki.py.
    try:
        wiki.enqueue(doc_id=doc_id, title=title, summary=summary,
                     doc_type=doc_type, source=source,
                     metadata=metadata or None)
    except Exception:
        log.exception("wiki enqueue hook failed (ignored)")

    # Knowledge-graph backend (additive, env-gated KG_AUTO): extract fact
    # triples from this doc's summary in the background — never blocks or
    # breaks ingest. One cheap Flash-Lite call/doc, capped by a daily
    # budget so a bulk import can't run up a surprise bill.
    if config.KG_AUTO:
        try:
            async def _kg_hook(did=doc_id, t=title, s=summary):
                try:
                    from ..store import cost as _cost, kg as _kg
                    from ..agent import kg_extract as _kgx
                    spent = (await asyncio.to_thread(
                        _cost.purpose_today_month, "kg_extract"
                    )).get("today_krw", 0.0)
                    if spent >= config.KG_DAILY_BUDGET_KRW:
                        return  # daily cap reached → skip until KST midnight
                    triples = await _kgx.extract(t, s)
                    if triples:
                        await asyncio.to_thread(_kg.add_edges, did, triples)
                except Exception:
                    log.debug("kg auto-extract failed (ignored)")
            asyncio.create_task(_kg_hook())
        except Exception:
            log.debug("kg hook schedule failed (ignored)")

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
