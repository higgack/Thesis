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


async def load_url(url: str) -> tuple[str, str, str | None]:
    """Returns (title, text, hint_summary).

    hint_summary is a free, source-provided abstract / og:description that
    can replace LLM summarization when good enough."""
    yt = is_youtube(url)
    if yt:
        return await load_youtube(yt, url)
    if m := _ARXIV_RE.search(url):
        return await load_arxiv(m.group(1))
    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 SecondBrain"}) as c:
        r = await c.get(url)
        r.raise_for_status()
        html = r.text
    return await asyncio.to_thread(_parse_html, url, html)


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

    return (title or path.stem)[:200], body, None


async def load_pdf_async(path: Path) -> tuple[str, str, str | None]:
    return await asyncio.to_thread(load_pdf, path)


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
