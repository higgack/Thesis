import asyncio
import logging
import re
from pathlib import Path
import httpx
import trafilatura
from pypdf import PdfReader
from youtube_transcript_api import YouTubeTranscriptApi

log = logging.getLogger(__name__)

_YOUTUBE_RE = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([\w-]{11})"
)
_ARXIV_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([\w.\-]+)")


def is_youtube(url: str) -> str | None:
    m = _YOUTUBE_RE.search(url)
    return m.group(1) if m else None


_JS_PLACEHOLDER_PATTERNS = (
    "javascript is not available",
    "please enable javascript",
    "you need to enable javascript",
    "javascript이 비활성화",
)
_MIN_BODY_CHARS = 200


# Hosts that block bot/datacenter access — skip the fetch entirely so
# the user gets the recovery guidance (📋 본문 복사 / 스크린샷) instantly
# instead of waiting 30+ seconds for a TCP timeout. Includes:
#   • social/auth-walled (LinkedIn, Facebook, etc.)
#   • paywalled financial press (Reuters, Bloomberg, WSJ, FT, etc.)
_BLOCKED_HOSTS = (
    # Auth walls — bot can never see the body
    "linkedin.com", "facebook.com", "instagram.com", "story.kakao.com",
    # X (twitter) — public oEmbed strips threads + we don't pay for the
    # API. Forwarded channels routinely cite X posts; without this block
    # every cite spawns a useless ingest that yields an empty body and
    # spends a retry slot.
    "x.com", "twitter.com",
    # English-language paywalls — server returns a stub or login redirect
    # (한국 매체는 보통 본문 추출되니 제외; 막으면 좋은 자료까지 잃음)
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com",
    "economist.com", "nytimes.com", "washingtonpost.com", "barrons.com",
    # Korean URL shorteners — opaque redirect, trafilatura can't extract
    # anything from the landing page. Every digest cite of these was
    # piling up in the failed log without ever succeeding.
    "buly.kr", "vo.la", "zrr.kr", "bit.ly", "tinyurl.com",
    # Forum / community boards with low signal + heavy noise (long
    # comment threads that overwhelm any analyst content).
    "dvdprime.com",
    # Stub / placeholder hosts that extract no actual content:
    #   • finance.naver.com / n.stock.naver.com — body is just the
    #     "증권사 로그인 안내" boilerplate; the real price/financials
    #     are loaded by JS and don't surface to trafilatura.
    #   • dart.fss.or.kr — viewer URL extracts only the report table
    #     of contents; substantive text lives in the attached PDF
    #     which needs to be downloaded separately. Daju channel
    #     already strips these via _PLAIN_URL_STRIP_PATTERNS so
    #     blocking here has no downside.
    "finance.naver.com", "n.stock.naver.com", "stock.naver.com",
    "dart.fss.or.kr",
)


def _is_blocked_host(url: str) -> bool:
    """True if url's host matches the _BLOCKED_HOSTS list. Used by
    pipeline.ingest_url to short-circuit BEFORE the failure log gets
    touched, so blocked URLs no longer accumulate in /failed."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        return any(h in host for h in _BLOCKED_HOSTS)
    except Exception:
        return False


_NAVER_BLOG_RE = re.compile(
    r"^https?://(?:m\.)?blog\.naver\.com/([^/?#]+)/(\d+)(?:[/?#].*)?$"
)


def _rewrite_for_better_fetch(url: str) -> str:
    """Rewrite known JS-iframe / SPA URLs to alternative forms that
    yield real HTML body to trafilatura. Currently:
      • blog.naver.com/<user>/<id>  →  m.blog.naver.com/<user>/<id>
        (mobile view ships the post body inline, desktop view loads
         it via an iframe that trafilatura can't follow)
    Returns the original URL when no rewrite rule matches."""
    m = _NAVER_BLOG_RE.match(url)
    if m:
        user, post_id = m.group(1), m.group(2)
        return f"https://m.blog.naver.com/{user}/{post_id}"
    return url


async def load_url(url: str) -> tuple[str, str, str | None]:
    """Returns (title, text, hint_summary).

    hint_summary is a free, source-provided abstract / og:description that
    can replace LLM summarization when good enough."""
    yt = is_youtube(url)
    if yt:
        return await load_youtube(yt, url)
    if m := _ARXIV_RE.search(url):
        return await load_arxiv(m.group(1))
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        if any(h in host for h in _BLOCKED_HOSTS):
            return "", "", None  # short-circuit; downstream will say empty
    except Exception:
        pass
    # Rewrite SPA-heavy URLs to scraper-friendly alternates BEFORE the
    # fetch so trafilatura sees real markup. Naver blog is the big
    # one: desktop ships an iframe, mobile ships inline content.
    fetch_url = _rewrite_for_better_fetch(url)
    title, body, hint = "", "", None
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 SecondBrain"}) as c:
            r = await c.get(fetch_url)
            r.raise_for_status()
            ct = r.headers.get("content-type", "").lower()
            if "application/pdf" in ct or r.content[:5] == b"%PDF-":
                return await _load_pdf_from_bytes(r.content, str(r.url))
            html = r.text
        title, body, hint = await asyncio.to_thread(_parse_html, fetch_url, html)
    except Exception:
        pass

    if _is_js_placeholder(body):
        try:
            j_title, j_body, j_hint = await _load_via_jina(url)
            if j_body and len(j_body) >= _MIN_BODY_CHARS:
                title = j_title or title
                body = j_body
                hint = j_hint or hint
        except Exception:
            pass

    return title or url[:200], body, hint


def _is_js_placeholder(body: str) -> bool:
    if len(body) < _MIN_BODY_CHARS:
        return True
    low = body.lower()
    return any(p in low for p in _JS_PLACEHOLDER_PATTERNS)


async def _load_via_jina(url: str) -> tuple[str, str, str | None]:
    """r.jina.ai renders JS and returns clean markdown. Free, no auth."""
    api_url = f"https://r.jina.ai/{url}"
    async with httpx.AsyncClient(timeout=60, follow_redirects=True,
                                 headers={"Accept": "text/plain"}) as c:
        r = await c.get(api_url)
        r.raise_for_status()
        text = r.text
    title = ""
    body = text
    if "Markdown Content:" in text:
        head, body = text.split("Markdown Content:", 1)
        body = body.strip()
        if "Title:" in head:
            title = head.split("Title:", 1)[1].split("\n", 1)[0].strip()
    return title, body, None


def _parse_html(url: str, html: str) -> tuple[str, str, str | None]:
    extracted = trafilatura.extract(html, include_comments=False) or ""
    meta = trafilatura.extract_metadata(html)
    title = (meta.title if meta and meta.title else url)[:200]
    hint = (meta.description if meta and meta.description else None)
    return title, extracted.strip(), hint


async def _fetch_youtube_subs_yt_dlp(video_id: str) -> str:
    """Fallback transcript fetcher using yt-dlp. yt-dlp talks to
    YouTube via the same internal endpoints the web client uses, so
    it usually works from GCP/AWS server IPs where the
    youtube-transcript-api library gets blocked. Tries manual
    Korean → English subtitles first, then auto-generated captions
    in the same order. Returns the plain transcript text (one line
    per caption block) or '' if nothing is available."""
    try:
        from yt_dlp import YoutubeDL
    except Exception:
        log.warning("yt-dlp not installed — skipping fallback")
        return ""

    import json
    url = f"https://www.youtube.com/watch?v={video_id}"
    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "subtitleslangs": ["ko", "en"],
    }

    def _extract() -> dict | None:
        try:
            with YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            log.info("yt-dlp extract_info failed: %s", e)
            return None

    # Health tracking — every yt-dlp call records success/failure
    # into a 24h sliding window. The bot's hourly health-check job
    # alerts the user (via the ack-button pattern) when failures
    # cross threshold so they can rebuild with a fresher yt-dlp.
    from ..store import yt_dlp_health

    info = await asyncio.to_thread(_extract)
    if not info:
        yt_dlp_health.record_attempt(success=False)
        return ""

    # Prefer manual subs (more accurate); fall back to auto.
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    for source in (manual, auto):
        for lang in ("ko", "en"):
            tracks = source.get(lang) or []
            # Prefer json3 (clean text segments); vtt is the fallback.
            sub_url = None
            for t in tracks:
                if t.get("ext") == "json3":
                    sub_url = t.get("url"); break
            if not sub_url:
                for t in tracks:
                    if t.get("ext") == "vtt":
                        sub_url = t.get("url"); break
            if not sub_url:
                continue
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                             headers={"User-Agent": "Mozilla/5.0"}) as c:
                    r = await c.get(sub_url)
                    r.raise_for_status()
                    content = r.text
            except Exception as e:
                log.info("yt-dlp subtitle fetch %s/%s failed: %s",
                         "manual" if source is manual else "auto", lang, e)
                continue
            text = _parse_subtitle_payload(content)
            if text.strip():
                log.info("yt-dlp: %s/%s captions OK (%d chars)",
                         "manual" if source is manual else "auto", lang, len(text))
                yt_dlp_health.record_attempt(success=True)
                return text.strip()

    # extract_info succeeded but no usable captions found
    yt_dlp_health.record_attempt(success=False)
    return ""


def _parse_subtitle_payload(content: str) -> str:
    """Extract plain text from json3 (preferred) or vtt subtitle
    payloads. json3 events look like {events: [{segs: [{utf8: '...'}]}]}.
    vtt lines have HH:MM:SS --> HH:MM:SS timestamps separated by
    blank lines from cue text."""
    import json
    # json3: real YouTube payloads start with {"wireMagic":"pb3","pens":...
    # and "events" lives ~200+ chars in, past any naive prefix check.
    # Detect by leading '{' and try a full parse — falls through to vtt
    # if parsing fails or there's no events array.
    stripped = content.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except Exception:
            data = None
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            # json3 splits captions into per-word segments where the
            # leading space lives inside the next seg's utf8 ("월간",
            # " 안테나", " 딱"). Stripping each seg destroys those
            # spaces and yields compressed garbage ("월간안테나딱"),
            # so we only strip the joined line.
            lines: list[str] = []
            for event in data["events"]:
                buf = "".join((seg.get("utf8") or "")
                              for seg in (event.get("segs") or []))
                buf = buf.strip()
                if buf:
                    lines.append(buf)
            return "\n".join(lines)
    # vtt fallback — strip timestamps and metadata
    out: list[str] = []
    for raw in content.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("WEBVTT") or line.startswith("Kind:") or line.startswith("Language:"):
            continue
        if "-->" in line:
            continue
        if line.isdigit():  # cue number
            continue
        out.append(line)
    return "\n".join(out)


async def load_youtube(video_id: str, url: str) -> tuple[str, str, str | None]:
    title = f"YouTube {video_id}"
    description = None
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.get(f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json")
            if r.status_code == 200:
                title = r.json().get("title", title)
    except Exception:
        pass
    # Primary: youtube-transcript-api (fast pure-Python). Often
    # blocked on cloud IPs but free + zero deps. v1.0 of the library
    # made `list_transcripts` an instance method (was classmethod on
    # 0.x); we support both by detecting the legacy attribute first.
    try:
        if hasattr(YouTubeTranscriptApi, "list_transcripts"):
            # 0.x API
            transcripts = YouTubeTranscriptApi.list_transcripts(video_id)
            try:
                t = transcripts.find_transcript(["ko", "en"])
            except Exception:
                t = next(iter(transcripts))
            entries = t.fetch()
            text = "\n".join(e["text"] for e in entries)
        else:
            # 1.x API — instance method, returns FetchedTranscript
            # iterable of snippets with .text attribute.
            api = YouTubeTranscriptApi()
            fetched = api.fetch(video_id, languages=["ko", "en"])
            text = "\n".join(s.text for s in fetched)
        if text.strip():
            return title, text.strip(), description
    except Exception as e:
        log_msg = f"transcript_api: {e}"
    else:
        log_msg = "transcript_api: empty"

    # Fallback: yt-dlp (different API path, usually works on GCP
    # server IPs where youtube-transcript-api gets blocked). Catches
    # ~90% of remaining videos.
    yt_dlp_text = await _fetch_youtube_subs_yt_dlp(video_id)
    if yt_dlp_text:
        return title, yt_dlp_text, description

    # Both transcript fetchers failed. DO NOT fall back to jina.ai
    # for youtube — jina returns page HTML (nav, related videos,
    # comments) without the JS-rendered transcript, polluting RAG
    # retrieval. Return a minimal stub so /find surfaces the URL with
    # a clean, actionable manual-paste instruction. The raw library
    # error (huge wall of text including library URLs and ban-risk
    # warnings) gets trimmed to a single line — the full error stays
    # in the log for diagnostics.
    log_short = (log_msg or "").splitlines()[0][:120] if log_msg else "unknown"
    stub = (
        f"📺 자막 자동 fetch 실패 (YouTube cloud-IP 차단)\n\n"
        f"제목: {title}\n"
        f"링크: {url}\n\n"
        f"📋 수동 학습 (5초):\n"
        f"1. 영상 페이지에서 ⋯ 메뉴 → '스크립트 표시' 클릭\n"
        f"2. 전체 스크립트 텍스트 복사\n"
        f"3. 이 봇에 메시지로 붙여넣기 (제목 자동 인식)\n\n"
        f"[기술 사유: {log_short}]"
    )
    return title, stub, description


async def load_arxiv(arxiv_id: str) -> tuple[str, str, str | None]:
    """Use the arXiv Atom API: free, structured, abstract included."""
    api = f"https://export.arxiv.org/api/query?id_list={arxiv_id}"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": "SecondBrain"}) as c:
        r = await c.get(api)
        r.raise_for_status()
        xml = r.text
    title = _xml_field(xml, "title", skip_first=True) or f"arXiv:{arxiv_id}"
    abstract = _xml_field(xml, "summary", skip_first=True) or ""
    return title.strip()[:200], abstract.strip(), abstract.strip() or None


def _xml_field(xml: str, tag: str, skip_first: bool = False) -> str | None:
    pattern = re.compile(fr"<{tag}>(.*?)</{tag}>", re.DOTALL)
    matches = pattern.findall(xml)
    if skip_first and len(matches) > 1:
        return matches[1]
    return matches[0] if matches else None


# Auto OCR cap for sparse-text (chart-heavy) PDFs. PDFs larger than
# this trigger an inline-button prompt asking whether to OCR the rest;
# the user opts in / out per document so we never silently bill ₩200+
# on a 100-page deep-research report. Lowered 20→10 after the user
# decided most reports' essential content sits in the first ~10 pages.
import os as _os

# Vision OCR quality / cost knobs — all env-overridable for fast
# rollback if quality regression shows up on real broker reports.
SPARSE_OCR_AUTO_CAP = int(_os.getenv("OCR_AUTO_CAP", "0"))  # pages
# Image-only PDFs (PyMuPDF returns zero text) need at least a small
# Vision pass or the doc lands in /failed as empty body. 3 pages gives
# us a title + first-page summary so the doc is searchable, and the
# OCR-extend prompt picks up the rest under user control.
OCR_IMAGE_ONLY_CAP = int(_os.getenv("OCR_IMAGE_ONLY_CAP", "3"))
# Progressive OCR ([A] cost cut, 2026-05): on the AUTO sparse/image-PDF
# trigger only (not /ocr_extend), OCR the first PROBE pages, and only
# spend Vision on the remaining cap if the probe yielded enough text
# to be worth continuing. A chart-only deck where the first 3 pages
# render <300 chars of OCR text almost never has readable content on
# pages 4-7 either — saves 4 Vision calls (~₩6/doc) with low recall
# loss. Threshold 300 (was 200) leaves a margin so mixed PDFs whose
# real text only starts on page 4-5 still get continued; the rare
# false-skip is recoverable via /pending_ocr + /ocr_extend.
_OCR_PROGRESSIVE_PROBE_PAGES = int(_os.getenv("OCR_PROBE_PAGES", "3"))
_OCR_PROGRESSIVE_MIN_TEXT = int(_os.getenv("OCR_PROBE_MIN_TEXT", "300"))
OCR_DPI = int(_os.getenv("OCR_DPI", "100"))  # render DPI
OCR_SPARSE_THRESHOLD = int(_os.getenv("OCR_SPARSE_THRESHOLD", "800"))  # chars/page


def _looks_like_title(s: str) -> bool:
    """Reject PDF metadata.title placeholders so the filename is used
    instead. Catches:
      - empty / pure-digit / pure-symbol strings
      - date-only stubs: '2013년 0월 0일'
      - internal report codes: '신한투자증권20230823f', 'KB증권20240115a',
        'samsung20230101' — company name + 6+ digits + optional letter
      - bare ID tokens: '20230823rpt' (no separators, mostly digits)
    """
    s = (s or "").strip()
    if not re.search(r"[A-Za-z가-힣]{2,}", s):
        return False
    # Internal report code pattern: word + 6+ digits + optional letter,
    # no spaces. Almost always less informative than the filename.
    if re.match(r"^[가-힣A-Za-z]+\d{6,}[A-Za-z]?$", s):
        return False
    # Bare ID-ish token: no spaces, mostly digits — generic placeholder.
    if " " not in s and len(s) < 25:
        digit_ratio = sum(c.isdigit() for c in s) / max(len(s), 1)
        if digit_ratio >= 0.4:
            return False
    return True


def load_pdf(path: Path, on_stage=None) -> tuple[str, str, str | None, dict | None]:
    """Extract text from a PDF.

    Returns (title, body, hint, ocr_meta). `ocr_meta` is non-None when
    Vision OCR augmentation ran and was page-capped; bot uses it to
    offer a "OCR the remaining N pages (~₩X)" inline button.

    Tries PyMuPDF first (handles complex layouts, embedded fonts, and
    image-text overlays better) and falls back to pypdf. When the
    text-only extract looks sparse for the number of pages (chart/
    table-heavy brokerage reports etc.), augments with a Vision OCR
    pass over the first SPARSE_OCR_AUTO_CAP pages so the numbers in
    figures don't get lost."""
    title = path.stem
    body = ""
    page_count = 0
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(str(path))
        page_count = doc.page_count
        meta_title = (doc.metadata or {}).get("title")
        if meta_title and meta_title.strip() and _looks_like_title(meta_title):
            title = meta_title.strip()
        # Per-page text + structured table extraction. find_tables()
        # surfaces broker-report tables that page.get_text("text") often
        # serialises as ragged whitespace; appending the structured rows
        # gives the embedder a cleaner signal AND lets sparse-text pages
        # cross the auto-OCR threshold without spending Vision tokens.
        page_parts: list[str] = []
        for page in doc:
            t = page.get_text("text") or ""
            tables_text = _extract_pdf_tables(page)
            if tables_text:
                t = (t + "\n\n[Tables]\n" + tables_text).strip()
            page_parts.append(t)
        body = "\n\n".join(page_parts).strip()
        doc.close()
    except Exception:
        body = ""

    if not body:
        try:
            reader = PdfReader(str(path))
            page_count = page_count or len(reader.pages)
            pages = [p.extract_text() or "" for p in reader.pages]
            body = "\n\n".join(pages).strip()
            if reader.metadata and reader.metadata.title and _looks_like_title(reader.metadata.title):
                title = reader.metadata.title
        except Exception:
            pass

    ocr_meta: dict | None = None
    if not body:
        # Image-only PDF: PyMuPDF returned zero text, so without at
        # least a few OCR'd pages the doc lands in /failed as empty
        # body. OCR_IMAGE_ONLY_CAP (default 3) gives us a title +
        # first-page summary so the doc is searchable; the rest goes
        # through the user-controlled OCR-extend prompt.
        r = _ocr_pdf_pages(path, max_pages=OCR_IMAGE_ONLY_CAP,
                           on_stage=on_stage, progressive=True)
        if r["text"]:
            body = r["text"]
        if page_count > 0:
            ocr_meta = {
                "kind": "image_only",
                "applied_pages": min(page_count, OCR_IMAGE_ONLY_CAP),
                "ocrd_pages": r["ocrd"],
                "skipped_pages": r["skipped"],
                "total_pages": page_count,
                "capped": page_count > OCR_IMAGE_ONLY_CAP,
            }
    elif page_count > 0 and len(body) / max(page_count, 1) < OCR_SPARSE_THRESHOLD:
        # Sparse text/page → likely chart/table-heavy. SPARSE_OCR_AUTO_CAP
        # default 0 (2026-05): no auto OCR — every sparse PDF emits an
        # OCR-extend prompt and the user explicitly chooses OCR /
        # 텍스트만 / Cancel. Set OCR_AUTO_CAP=3 (or higher) via env to
        # restore partial auto-OCR.
        applied = min(page_count, SPARSE_OCR_AUTO_CAP)
        if SPARSE_OCR_AUTO_CAP > 0:
            r = _ocr_pdf_pages(path, max_pages=SPARSE_OCR_AUTO_CAP,
                               on_stage=on_stage, progressive=True)
            if r["text"]:
                body = body + "\n\n--- Vision OCR augmentation ---\n\n" + r["text"]
            ocrd_pages = r["ocrd"]
            skipped_pages = r["skipped"]
        else:
            ocrd_pages = 0
            skipped_pages = 0
        ocr_meta = {
            "kind": "sparse",
            "applied_pages": applied,
            "ocrd_pages": ocrd_pages,
            "skipped_pages": skipped_pages,
            "total_pages": page_count,
            "capped": page_count > SPARSE_OCR_AUTO_CAP,
        }

    return (title or path.stem)[:200], body, None, ocr_meta


def _extract_pdf_tables(page) -> str:
    """Use PyMuPDF's table finder to pull structured rows out of a
    page. Returns a TSV-ish string ready to append to the page's text,
    or '' when no usable tables are found.

    Conservative thresholds: at least 2 rows × 2 cols and ≥6 non-empty
    cells. This filters out chart-axis lines and decorative grids that
    find_tables() sometimes flags as tables, which would otherwise
    pollute the body with noise."""
    try:
        finder = page.find_tables()
    except Exception:
        return ""
    rows_all: list[str] = []
    try:
        tables = list(finder.tables) if hasattr(finder, "tables") else list(finder)
    except Exception:
        return ""
    for t in tables:
        try:
            rows = t.extract() or []
        except Exception:
            continue
        if len(rows) < 2:
            continue
        max_cols = max((len(r) for r in rows), default=0)
        if max_cols < 2:
            continue
        filled = sum(1 for r in rows for c in r if (c or "").strip())
        if filled < 6:
            continue
        rows_all.append(
            "\n".join("\t".join((c or "").strip() for c in r) for r in rows)
        )
    return "\n\n".join(rows_all)


def _page_is_blank(img_bytes: bytes, existing_text: str) -> bool:
    """Detect cover / disclaimer / blank pages so we don't waste a
    Vision call on them. Heuristic: PyMuPDF found <30 chars of text
    AND the rendered image is ≥95% near-white pixels."""
    if len(existing_text.strip()) >= 30:
        return False
    try:
        from PIL import Image
        import io as _io
        img = Image.open(_io.BytesIO(img_bytes)).convert("L")
    except Exception:
        return False
    pixels = list(img.getdata())
    if not pixels:
        return False
    # Sample to keep the scan cheap on 1275×1650-ish images.
    sample = pixels[::100]
    white = sum(1 for p in sample if p >= 240)
    return white / len(sample) >= 0.95


def _ocr_pdf_pages(path: Path, max_pages: int = 80, dpi: int = OCR_DPI,
                   start_page: int = 1, on_stage=None,
                   skip_if_text_chars: int = 1500,
                   progressive: bool = False) -> dict:
    # DPI 150 → 100: image input tokens scale roughly with pixel
    # area, so ~55% fewer input tokens per page. Vision OCR on
    # broker-report text (12-14pt body) reads identically at 100 dpi;
    # only fine print (footnotes, dense table cells) would degrade,
    # and those are usually already in the PyMuPDF text pass.
    """Render each page to PNG and ask Gemini Vision to extract text.

    Returns {text, ocrd, skipped} so callers can report actual cost
    instead of the pessimistic range size. Pages whose PyMuPDF text
    extract already exceeds `skip_if_text_chars` are skipped — the
    body already has that text via the initial PyMuPDF pass, so
    OCR-ing them again would just create duplicate chunks and bill
    Vision unnecessarily (this matters a lot on long deep-research
    papers where back-half pages are text-heavy).

    `start_page` (1-indexed inclusive) lets the resumable-OCR flow
    extend coverage beyond the auto cap by OCR-ing pages
    [start_page, start_page + max_pages - 1]."""
    try:
        import fitz  # PyMuPDF
    except Exception:
        return {"text": "", "ocrd": 0, "skipped": 0}

    try:
        doc = fitz.open(str(path))
    except Exception:
        return {"text": "", "ocrd": 0, "skipped": 0}

    # Vision OCR backend (gemini / local / hybrid) routed through
    # ocr_client. Default OCR_BACKEND=gemini keeps the original
    # inline Gemini path bit-identical; local/hybrid route to the
    # ocr-worker container via the file queue (dormant unless
    # docker-compose --profile ocr-local is up).
    from . import ocr_client as _ocr_client

    import hashlib as _hashlib
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ..store import meta as _meta
    pages_out: list[tuple[int, str]] = []  # (page_idx, text) — sorted at end
    ocrd = 0
    skipped = 0
    cached_hits = 0
    blank_skips = 0
    end_page = start_page + max_pages - 1

    # First pass (sequential, fast): decide per-page action — skip,
    # cache hit, blank, or queue for Vision. Renders happen here too
    # because PyMuPDF page objects aren't thread-safe.
    work_items: list[tuple[int, bytes, str]] = []  # (page_idx, img_bytes, img_hash)
    for i, page in enumerate(doc, 1):
        if i < start_page:
            continue
        if i > end_page:
            pages_out.append((i, f"-- Page {i}+ truncated --"))
            break
        existing_text = (page.get_text("text") or "").strip()
        if len(existing_text) >= skip_if_text_chars:
            skipped += 1
            continue
        try:
            pix = page.get_pixmap(dpi=dpi)
            img_bytes = pix.tobytes("png")
        except Exception as e:
            pages_out.append((i, f"-- Page {i} --\n[render error: {type(e).__name__}]"))
            continue
        # L: blank / boilerplate skip
        if _page_is_blank(img_bytes, existing_text):
            blank_skips += 1
            continue
        # K: cross-doc OCR cache hit — no Vision call needed
        img_hash = _hashlib.sha1(img_bytes).hexdigest()
        cached_text = _meta.ocr_cache_get(img_hash)
        if cached_text is not None:
            if cached_text.strip():
                pages_out.append((i, f"-- Page {i} --\n{cached_text}"))
            cached_hits += 1
            continue
        work_items.append((i, img_bytes, img_hash))

    # Second pass (parallel): OCR queued pages concurrently via the
    # configured backend (Gemini direct / local worker / hybrid).
    # max_workers=7 caps in-flight calls so Gemini per-min quota isn't
    # bumped even with multiple PDFs ingesting; for local mode the
    # worker itself is the bottleneck (single-process) but the queue
    # serialises naturally, so multiple threads just block on the
    # queue read — harmless.

    def _ocr_one(item):
        i, img_bytes, img_hash = item
        try:
            text = _ocr_client.ocr_one_page(img_bytes, page_idx=i)
            if text and not text.startswith("[OCR error"):
                _meta.ocr_cache_put(img_hash, text)
            return (i, text)
        except Exception as e:
            return (i, f"[OCR error: {type(e).__name__}: {e}]")

    def _run_batch(items, completed_offset, total):
        """OCR a list of work items in parallel and collect results."""
        results: list[tuple[int, str]] = []
        if not items:
            return results
        max_workers = min(len(items), 7)
        completed = completed_offset
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_ocr_one, w): w for w in items}
            for fut in as_completed(futures):
                completed += 1
                if on_stage:
                    on_stage(f"Vision OCR {completed}/{total} 페이지")
                results.append(fut.result())
        return results

    if work_items:
        total = len(work_items)
        if on_stage:
            on_stage(f"Vision OCR 0/{total} 페이지")

        if progressive and len(work_items) > _OCR_PROGRESSIVE_PROBE_PAGES:
            # Probe: run the first N pages. Decide whether the
            # remainder is worth the Vision spend based on real text
            # yield (excludes [OCR error] markers).
            probe = work_items[:_OCR_PROGRESSIVE_PROBE_PAGES]
            rest = work_items[_OCR_PROGRESSIVE_PROBE_PAGES:]
            probe_results = _run_batch(probe, 0, total)
            probe_text_len = sum(
                len(t) for _, t in probe_results
                if t and not t.startswith("[OCR error")
            )
            if probe_text_len < _OCR_PROGRESSIVE_MIN_TEXT:
                log.info(
                    "progressive OCR: probe %d pages → %d chars "
                    "(threshold %d) — skipping remaining %d pages",
                    len(probe), probe_text_len,
                    _OCR_PROGRESSIVE_MIN_TEXT, len(rest),
                )
                batch_results = probe_results
            else:
                rest_results = _run_batch(
                    rest, len(probe_results), total,
                )
                batch_results = probe_results + rest_results
        else:
            batch_results = _run_batch(work_items, 0, total)

        for page_idx, text in batch_results:
            if text:
                pages_out.append((page_idx, f"-- Page {page_idx} --\n{text}"))
            ocrd += 1

    # Sort by page index so output order matches reading order.
    pages_out.sort(key=lambda t: t[0])
    pages_out_str = [t[1] for t in pages_out]
    doc.close()
    if cached_hits or blank_skips:
        log.info("ocr cache hits=%d, blank skips=%d", cached_hits, blank_skips)
    return {
        "text": "\n\n".join(pages_out_str).strip(),
        "ocrd": ocrd,
        "skipped": skipped,
    }


async def load_pdf_async(path: Path, on_stage=None) -> tuple[str, str, str | None, dict | None]:
    return await asyncio.to_thread(load_pdf, path, on_stage)


async def ocr_pdf_pages_async(path: Path, start_page: int, max_pages: int,
                              dpi: int = 150) -> str:
    """Async wrapper around _ocr_pdf_pages used by extend_pdf_ocr."""
    return await asyncio.to_thread(
        _ocr_pdf_pages, path, max_pages=max_pages, dpi=dpi,
        start_page=start_page,
    )


def _ocr_image(img_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """Extract text from a single image via Gemini Vision. Used for
    standalone photos (screenshots, table captures, news clippings).
    Cost is ~$0.0003 per image on gemini-2.5-flash-lite."""
    try:
        from google import genai
        from google.genai import types
        from .. import config
        from ..store import cost as _cost
    except Exception:
        return ""
    client = genai.Client(api_key=config.GOOGLE_API_KEY)
    prompt = (
        "이 이미지에 포함된 모든 텍스트를 그대로 추출하세요. "
        "표는 마크다운 표 형식으로 변환하고, 단락/제목 구조를 유지하세요. "
        "설명이나 코멘트 없이 텍스트만 출력하세요."
    )
    try:
        resp = client.models.generate_content(
            model=config.SUMMARY_MODEL,
            contents=[
                types.Part.from_bytes(data=img_bytes, mime_type=mime_type),
                types.Part.from_text(text=prompt),
            ],
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=2048,
            ),
        )
        _cost.record_resp(config.SUMMARY_MODEL, resp, purpose="ingest")
        return (resp.text or "").strip()
    except Exception:
        return ""


async def ocr_image_async(img_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    return await asyncio.to_thread(_ocr_image, img_bytes, mime_type)


def _transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Gemini Audio STT. Used for Telegram voice notes and uploaded audio
    files. Cost ~₩50 per audio hour on gemini-2.5-flash-lite. Inline byte
    limit is ~20MB; longer recordings should be split client-side."""
    try:
        from google import genai
        from google.genai import types
        from .. import config
        from ..store import cost as _cost
    except Exception:
        return ""
    client = genai.Client(api_key=config.GOOGLE_API_KEY)
    prompt = (
        "이 오디오를 그대로 받아쓰기 하세요. 화자가 여럿이면 단락으로 "
        "구분하고, 들리는 언어 그대로(한국어/영어 등) 출력하세요. "
        "설명/코멘트 없이 받아쓰기 텍스트만 출력하세요."
    )
    try:
        resp = client.models.generate_content(
            model=config.SUMMARY_MODEL,
            contents=[
                types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                types.Part.from_text(text=prompt),
            ],
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=8192,
            ),
        )
        _cost.record_resp(config.SUMMARY_MODEL, resp, purpose="ingest")
        return (resp.text or "").strip()
    except Exception:
        return ""


async def transcribe_audio_async(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    return await asyncio.to_thread(_transcribe_audio, audio_bytes, mime_type)


async def _load_pdf_from_bytes(data: bytes, source_url: str) -> tuple[str, str, str | None]:
    """When a URL fetch returns PDF bytes (brokerage shortlinks like
    bbn.kiwoom.com → PDF redirect), save to a temp file and reuse the
    standard PDF extractor instead of trying to parse PDF as HTML.

    Drops the ocr_meta because the temp file is cleaned up before any
    user-confirmed extension OCR could run."""
    import tempfile
    from urllib.parse import urlparse, unquote
    parsed = urlparse(source_url)
    fname = unquote(Path(parsed.path).name) or "document"
    if not fname.lower().endswith(".pdf"):
        fname = (fname or "document") + ".pdf"
    tmpdir = Path(tempfile.mkdtemp(prefix="urlpdf_"))
    tmp_path = tmpdir / fname
    try:
        tmp_path.write_bytes(data)
        title, body, hint, _ocr_meta = await load_pdf_async(tmp_path)
        return title, body, hint
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
            tmpdir.rmdir()
        except Exception:
            pass


def load_pptx(path: Path) -> tuple[str, str, str | None]:
    """Robust PPTX text extraction. python-pptx returns None instead
    of '' / safe defaults in several edge cases (notes_text_frame on
    slides whose notes XML is malformed, cells missing a text_frame,
    SmartArt shapes that lack a .text accessor). The user hit an
    AttributeError 'NoneType has no text' that re-queued the file on
    every restart — wrap every accessor with a None guard so a single
    odd slide can't blow up the whole load."""
    from pptx import Presentation
    prs = Presentation(str(path))
    parts: list[str] = []
    title_guess = path.stem
    for i, slide in enumerate(prs.slides, 1):
        slide_lines: list[str] = []
        for shape in slide.shapes:
            try:
                text = getattr(shape, "text", "") or ""
                if text.strip():
                    slide_lines.append(text.strip())
            except Exception:
                pass
            if getattr(shape, "has_table", False):
                try:
                    for row in shape.table.rows:
                        cells_raw = []
                        for c in row.cells:
                            t = getattr(c, "text", None)
                            if t and t.strip():
                                cells_raw.append(t.strip())
                        if cells_raw:
                            slide_lines.append(" | ".join(cells_raw))
                except Exception:
                    pass
        if getattr(slide, "has_notes_slide", False):
            try:
                ns = slide.notes_slide
                ntf = getattr(ns, "notes_text_frame", None) if ns else None
                notes = (getattr(ntf, "text", None) or "").strip() if ntf else ""
                if notes:
                    slide_lines.append(f"[Notes] {notes}")
            except Exception:
                pass
        if i == 1 and slide_lines:
            first = slide_lines[0].splitlines()[0].strip()
            if 3 <= len(first) <= 120:
                title_guess = first
        if slide_lines:
            parts.append(f"Slide {i}:\n" + "\n".join(slide_lines))
    body = "\n\n".join(parts).strip()
    return title_guess[:200], body, None


async def load_pptx_async(path: Path) -> tuple[str, str, str | None]:
    return await asyncio.to_thread(load_pptx, path)


def load_docx(path: Path) -> tuple[str, str, str | None]:
    from docx import Document
    doc = Document(str(path))
    paragraphs: list[str] = []
    for p in doc.paragraphs:
        t = p.text.strip()
        if t:
            paragraphs.append(t)
    for tbl in doc.tables:
        for row in tbl.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                paragraphs.append(" | ".join(cells))
    title_guess = path.stem
    if paragraphs and 3 <= len(paragraphs[0]) <= 120:
        title_guess = paragraphs[0]
    body = "\n\n".join(paragraphs).strip()
    return title_guess[:200], body, None


async def load_docx_async(path: Path) -> tuple[str, str, str | None]:
    return await asyncio.to_thread(load_docx, path)


def load_xlsx(path: Path) -> tuple[str, str, str | None]:
    """Flatten each sheet to '| col1 | col2 | ...' rows. Skips empty
    cells and rows so a wide model with sparse data doesn't waste tokens.
    Caps rows per sheet at 1500 to keep huge ledgers under control."""
    from openpyxl import load_workbook
    wb = load_workbook(filename=str(path), read_only=True, data_only=True)
    parts: list[str] = []
    MAX_ROWS_PER_SHEET = 1500
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        sheet_lines: list[str] = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= MAX_ROWS_PER_SHEET:
                sheet_lines.append(f"... ({MAX_ROWS_PER_SHEET}+ rows truncated)")
                break
            cells = [
                str(c).strip() for c in row
                if c is not None and str(c).strip()
            ]
            if cells:
                sheet_lines.append(" | ".join(cells))
        if sheet_lines:
            parts.append(f"## Sheet: {sheet_name}\n" + "\n".join(sheet_lines))
    wb.close()
    body = "\n\n".join(parts).strip()
    title_guess = path.stem
    return title_guess[:200], body, None


async def load_xlsx_async(path: Path) -> tuple[str, str, str | None]:
    return await asyncio.to_thread(load_xlsx, path)
