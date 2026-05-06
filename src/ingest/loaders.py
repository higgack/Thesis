import re
from pathlib import Path
import httpx
import trafilatura
from pypdf import PdfReader
from youtube_transcript_api import YouTubeTranscriptApi

_YOUTUBE_RE = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([\w-]{11})"
)


def is_youtube(url: str) -> str | None:
    m = _YOUTUBE_RE.search(url)
    return m.group(1) if m else None


async def load_url(url: str) -> tuple[str, str]:
    """Returns (title, text)."""
    yt = is_youtube(url)
    if yt:
        return await load_youtube(yt, url)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 SecondBrain"}) as c:
        r = await c.get(url)
        r.raise_for_status()
        html = r.text
    extracted = trafilatura.extract(html, include_comments=False, include_tables=True,
                                    favor_recall=True) or ""
    meta = trafilatura.extract_metadata(html)
    title = (meta.title if meta and meta.title else url)[:200]
    return title, extracted.strip()


async def load_youtube(video_id: str, url: str) -> tuple[str, str]:
    try:
        transcripts = YouTubeTranscriptApi.list_transcripts(video_id)
        try:
            t = transcripts.find_transcript(["ko", "en"])
        except Exception:
            t = next(iter(transcripts))
        entries = t.fetch()
        text = "\n".join(e["text"] for e in entries)
        return f"YouTube {video_id}", text.strip()
    except Exception as e:
        return f"YouTube {video_id}", f"[transcript unavailable: {e}]"


def load_pdf(path: Path) -> tuple[str, str]:
    reader = PdfReader(str(path))
    pages = [p.extract_text() or "" for p in reader.pages]
    title = reader.metadata.title if reader.metadata and reader.metadata.title else path.stem
    return (title or path.stem)[:200], "\n\n".join(pages).strip()
