import asyncio
import re
from pathlib import Path
import httpx
import trafilatura
from pypdf import PdfReader
from youtube_transcript_api import YouTubeTranscriptApi

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


async def load_url(url: str) -> tuple[str, str, str | None]:
    """Returns (title, text, hint_summary).

    hint_summary is a free, source-provided abstract / og:description that
    can replace LLM summarization when good enough."""
    yt = is_youtube(url)
    if yt:
        return await load_youtube(yt, url)
    if m := _ARXIV_RE.search(url):
        return await load_arxiv(m.group(1))
    title, body, hint = "", "", None
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 SecondBrain"}) as c:
            r = await c.get(url)
            r.raise_for_status()
            ct = r.headers.get("content-type", "").lower()
            if "application/pdf" in ct or r.content[:5] == b"%PDF-":
                return await _load_pdf_from_bytes(r.content, str(r.url))
            html = r.text
        title, body, hint = await asyncio.to_thread(_parse_html, url, html)
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
    try:
        transcripts = YouTubeTranscriptApi.list_transcripts(video_id)
        try:
            t = transcripts.find_transcript(["ko", "en"])
        except Exception:
            t = next(iter(transcripts))
        entries = t.fetch()
        text = "\n".join(e["text"] for e in entries)
        return title, text.strip(), description
    except Exception as e:
        return title, f"[transcript unavailable: {e}]", description


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


def load_pdf(path: Path) -> tuple[str, str, str | None]:
    """Extract text from a PDF. Tries PyMuPDF first (handles complex layouts,
    embedded fonts, and image-text overlays better) and falls back to pypdf."""
    title = path.stem
    body = ""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(str(path))
        meta_title = (doc.metadata or {}).get("title")
        if meta_title and meta_title.strip():
            title = meta_title.strip()
        body = "\n\n".join(page.get_text("text") or "" for page in doc).strip()
        doc.close()
    except Exception:
        body = ""

    if not body:
        try:
            reader = PdfReader(str(path))
            pages = [p.extract_text() or "" for p in reader.pages]
            body = "\n\n".join(pages).strip()
            if reader.metadata and reader.metadata.title:
                title = reader.metadata.title
        except Exception:
            pass

    if len(body) < 200:
        ocr_text = _ocr_pdf_pages(path)
        if ocr_text:
            body = ocr_text

    return (title or path.stem)[:200], body, None


def _ocr_pdf_pages(path: Path, max_pages: int = 80, dpi: int = 150) -> str:
    """Render each page to PNG and ask Gemini Vision to extract text.

    Used as a last resort when PyMuPDF and pypdf both return empty text
    (image-only / scanned PDFs). Cost per page is roughly $0.001 on
    gemini-2.5-flash-lite, so a 50-page PDF is about $0.05."""
    try:
        import fitz  # PyMuPDF
        from google import genai
        from google.genai import types
        from .. import config
    except Exception:
        return ""

    try:
        doc = fitz.open(str(path))
    except Exception:
        return ""

    client = genai.Client(api_key=config.GOOGLE_API_KEY)
    prompt = (
        "이 페이지의 모든 텍스트를 그대로 추출하세요. "
        "단락/제목/표 구조를 유지하고, 설명이나 코멘트 없이 텍스트만 출력하세요."
    )

    pages_out: list[str] = []
    for i, page in enumerate(doc, 1):
        if i > max_pages:
            pages_out.append(f"-- Page {i}+ truncated --")
            break
        try:
            pix = page.get_pixmap(dpi=dpi)
            img_bytes = pix.tobytes("png")
            resp = client.models.generate_content(
                model=config.SUMMARY_MODEL,
                contents=[
                    types.Part.from_bytes(data=img_bytes, mime_type="image/png"),
                    types.Part.from_text(text=prompt),
                ],
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=2048,
                ),
            )
            text = (resp.text or "").strip()
            if text:
                pages_out.append(f"-- Page {i} --\n{text}")
        except Exception as e:
            pages_out.append(f"-- Page {i} --\n[OCR error: {type(e).__name__}: {e}]")
    doc.close()
    return "\n\n".join(pages_out).strip()


async def load_pdf_async(path: Path) -> tuple[str, str, str | None]:
    return await asyncio.to_thread(load_pdf, path)


def _ocr_image(img_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """Extract text from a single image via Gemini Vision. Used for
    standalone photos (screenshots, table captures, news clippings).
    Cost is ~$0.0003 per image on gemini-2.5-flash-lite."""
    try:
        from google import genai
        from google.genai import types
        from .. import config
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
        return (resp.text or "").strip()
    except Exception:
        return ""


async def transcribe_audio_async(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    return await asyncio.to_thread(_transcribe_audio, audio_bytes, mime_type)


async def _load_pdf_from_bytes(data: bytes, source_url: str) -> tuple[str, str, str | None]:
    """When a URL fetch returns PDF bytes (brokerage shortlinks like
    bbn.kiwoom.com → PDF redirect), save to a temp file and reuse the
    standard PDF extractor instead of trying to parse PDF as HTML."""
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
        return await load_pdf_async(tmp_path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
            tmpdir.rmdir()
        except Exception:
            pass


def load_pptx(path: Path) -> tuple[str, str, str | None]:
    from pptx import Presentation
    prs = Presentation(str(path))
    parts: list[str] = []
    title_guess = path.stem
    for i, slide in enumerate(prs.slides, 1):
        slide_lines: list[str] = []
        for shape in slide.shapes:
            text = getattr(shape, "text", "") or ""
            if text.strip():
                slide_lines.append(text.strip())
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
                    if cells:
                        slide_lines.append(" | ".join(cells))
        if getattr(slide, "has_notes_slide", False):
            notes = (slide.notes_slide.notes_text_frame.text or "").strip()
            if notes:
                slide_lines.append(f"[Notes] {notes}")
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
