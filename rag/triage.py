"""
PyMuPDF Page Triage — cheap signal extraction before OCR / LLM spend.

Strategy
--------
For each page we collect a small set of cheap signals, then assign it
to one of four triage buckets:

 SKIP        — blank / near-blank, not worth processing at all
 TEXT_ONLY   — native text is extractable, no OCR needed
 OCR_NEEDED  — image-heavy with little/no native text, send to OCR
 LLM_NEEDED  — requires semantic reasoning (forms, mixed layouts, tables, etc.)

Costs (approximate, relative)
 PyMuPDF signal extraction                 ~0.001x
 OCR (e.g. Tesseract/cloud)                ~1x
 LLM (e.g. PyMuPDF4LLM, GPT-4o, Claude)    ~2-50x

In this project it acts as a pre-flight for the RAG-Anything ingest pipeline
(rag/ingest.py): triage first, then only pay for the pages that need it.

Usage
-----
    python rag/triage.py input.pdf
    python rag/triage.py input.pdf --details
"""

import os
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Optional

# find_tables() otherwise prints a "Consider using pymupdf_layout" hint to
# stdout, which would corrupt the machine-readable --details JSON. Silence it.
os.environ.setdefault("PYMUPDF_SUGGEST_LAYOUT_ANALYZER", "0")

import pymupdf

try:
    pymupdf.no_recommend_layout()
except AttributeError:  # older PyMuPDF without the helper — env var covers it
    pass


# -- Triage buckets ----------------------------------------------------------

class Bucket(Enum):
    SKIP       = auto()   # blank / negligible content
    TEXT_ONLY  = auto()   # native text, no further processing needed
    OCR_NEEDED = auto()   # image page or scanned, needs OCR
    LLM_NEEDED = auto()   # requires semantic reasoning


# -- Per-page signals --------------------------------------------------------

@dataclass
class PageSignals:
    page_number:        int
    width:              float
    height:             float

    # Text
    char_count:         int   = 0
    word_count:         int   = 0
    text_coverage:      float = 0.0   # fraction of page bbox covered by text blocks
    has_native_text:    bool  = False

    # Images
    image_count:        int   = 0
    image_coverage:     float = 0.0   # fraction of page bbox covered by images

    # Structure hints
    has_tables:         bool  = False
    has_forms:          bool  = False   # detected via widget annotations
    block_count:        int   = 0
    vector_drawing:     bool  = False   # any non-image, non-text drawing commands

    # Derived
    bucket:             Optional[Bucket] = field(default=None, init=False)
    reason:             str              = field(default="",   init=False)


# -- Signal extraction -------------------------------------------------------

def extract_signals(page: pymupdf.Page) -> PageSignals:
    """
    Extract cheap signals from a single PyMuPDF page object.
    All operations stay in Python/C; nothing is sent to an external service.
    """
    rect = page.rect
    page_area = rect.width * rect.height or 1.0  # guard /0

    sig = PageSignals(
        page_number=page.number,  # zero-indexed page number
        width=rect.width,
        height=rect.height,
    )

    # Text
    # get_text("words") is faster than "blocks" for character/word counts
    words = page.get_text("words")          # list of (x0,y0,x1,y1,word,...)
    sig.word_count = len(words)
    sig.char_count = sum(len(w[4]) for w in words)
    sig.has_native_text = sig.char_count > 20   # ignore stray watermarks/footers

    # Text spatial coverage via blocks
    blocks = page.get_text("blocks")        # (x0,y0,x1,y1,text,block_no,block_type)
    sig.block_count = len(blocks)
    text_area = sum(
        (b[2] - b[0]) * (b[3] - b[1])
        for b in blocks if b[6] == 0        # block_type 0 = text
    )
    sig.text_coverage = min(text_area / page_area, 1.0)

    # Images
    # get_image_info() returns bbox data without extracting pixel data — very cheap
    images = page.get_image_info(hashes=False, xrefs=False)
    sig.image_count = len(images)
    img_area = sum(
        (img["bbox"][2] - img["bbox"][0]) * (img["bbox"][3] - img["bbox"][1])
        for img in images if img.get("bbox")
    )
    sig.image_coverage = min(img_area / page_area, 1.0)

    # Tables
    # PyMuPDF has find_tables(); use it
    tabs = page.find_tables()
    sig.has_tables = len(tabs.tables) > 0

    # Forms: widget annotations (checkboxes, text fields, dropdowns, etc.)
    for annot in page.annots():
        if annot.type[0] == pymupdf.PDF_ANNOT_WIDGET:
            sig.has_forms = True
            break

    # Vector drawings: any path/curve drawing that is not an image.
    # get_drawings() is cheap and returns strokes/fills.
    drawings = page.get_drawings()
    sig.vector_drawing = len(drawings) > 0

    return sig


# -- Triage rules ------------------------------------------------------------

def triage(sig: PageSignals,
           *,
           blank_char_threshold:    int   = 10,
           blank_image_threshold:   float = 0.02,
           ocr_image_threshold:     float = 0.25,
           ocr_text_threshold:      int   = 30,
           llm_complexity_score:    int   = 2) -> PageSignals:
    """
    Apply triage rules and attach bucket + reason to the signals object.

    Thresholds are keyword-only so callers can tune per document type.
    """

    chars  = sig.char_count
    imgcov = sig.image_coverage

    # Rule 1: SKIP — blank page
    if chars < blank_char_threshold and imgcov < blank_image_threshold:
        sig.bucket = Bucket.SKIP
        sig.reason = f"blank (chars={chars}, img_cov={imgcov:.2f})"
        return sig

    # Rule 2: OCR_NEEDED — image-dominant, little/no native text
    if imgcov >= ocr_image_threshold and chars < ocr_text_threshold:
        sig.bucket = Bucket.OCR_NEEDED
        sig.reason = (
            f"image-dominant (img_cov={imgcov:.2f}, chars={chars}) — "
            "likely scanned or image-only page"
        )
        return sig

    # Rule 3: LLM_NEEDED — structured/complex content
    complexity = sum([
        sig.has_tables,
        sig.has_forms,
        sig.image_count > 0 and sig.has_native_text,   # mixed image+text
        sig.block_count > 30,                          # dense layout
        sig.text_coverage < 0.10 and chars > 50,       # sparse text (forms-like)
    ])
    if complexity >= llm_complexity_score:
        sig.bucket = Bucket.LLM_NEEDED
        sig.reason = (
            f"complex layout (complexity_score={complexity}/5): "
            + ", ".join(filter(None, [
                "tables"        if sig.has_tables else "",
                "forms"         if sig.has_forms  else "",
                "mixed content" if sig.image_count > 0 and sig.has_native_text else "",
                f"{sig.block_count} blocks" if sig.block_count > 30 else "",
                "sparse text"   if sig.text_coverage < 0.10 and chars > 50 else "",
            ]))
        )
        return sig

    # Rule 4: TEXT_ONLY — clean native text
    sig.bucket = Bucket.TEXT_ONLY
    sig.reason = (
        f"native text (chars={chars}, words={sig.word_count}, "
        f"text_cov={sig.text_coverage:.2f})"
    )
    return sig


# -- Document-level triage ---------------------------------------------------

@dataclass
class TriageReport:
    path:       str
    page_count: int
    results:    list[PageSignals]

    @property
    def by_bucket(self) -> dict[Bucket, list[PageSignals]]:
        out: dict[Bucket, list[PageSignals]] = {b: [] for b in Bucket}
        for r in self.results:
            out[r.bucket].append(r)
        return out

    def summary(self) -> str:
        bb = self.by_bucket
        lines = [
            f"Document : {self.path}",
            f"Pages    : {self.page_count}",
            "-" * 50,
        ]
        for bucket in Bucket:
            pages = bb[bucket]
            if not pages:
                continue
            nums = ", ".join(str(p.page_number) for p in pages[:10])
            if len(pages) > 10:
                nums += f" ... (+{len(pages)-10} more)"
            lines.append(f"  {bucket.name:<12} {len(pages):>4} pages   [{nums}]")
        lines.append("-" * 50)

        total = self.page_count or 1
        skip  = len(bb[Bucket.SKIP])
        lines.append(
            f"  Skippable: {skip}/{total} ({skip/total*100:.0f}%)  "
            f"— estimated cost avoided relative to sending all to LLM"
        )
        return "\n".join(lines)


def triage_document(
    path: str | Path,
    **triage_kwargs,
) -> TriageReport:
    """
    Open a PDF and triage every page. Returns a TriageReport.
    The PDF is opened read-only; no modifications are made.
    """
    path = str(path)
    doc  = pymupdf.open(path)
    results = []

    for page in doc:
        sig = extract_signals(page)
        triage(sig, **triage_kwargs)
        results.append(sig)

    doc.close()
    return TriageReport(path=path, page_count=len(results), results=results)


# -- Routing helpers ---------------------------------------------------------

def route_pages(report: TriageReport) -> dict[str, list[int]]:
    """
    Return zero-indexed page numbers grouped by processing route.
    Plug these directly into your OCR / LLM pipeline.

    Usage
    -----
        routes = route_pages(report)
        ocr_pages  = routes["ocr"]    # send to Tesseract / cloud OCR
        llm_pages  = routes["llm"]    # send to PyMuPDF4LLM / GPT-4o / Claude etc.
        text_pages = routes["text"]   # extract with page.get_text() — free
        skip_pages = routes["skip"]   # ignore entirely
    """
    bb = report.by_bucket
    return {
        "skip":  [p.page_number for p in bb[Bucket.SKIP]],
        "text":  [p.page_number for p in bb[Bucket.TEXT_ONLY]],
        "ocr":   [p.page_number for p in bb[Bucket.OCR_NEEDED]],
        "llm":   [p.page_number for p in bb[Bucket.LLM_NEEDED]],
    }


def extract_text_pages(pdf_path: str | Path, page_indices: list[int]) -> dict[int, str]:
    """
    Cheaply extract native text from TEXT_ONLY pages.
    Returns {page_index: text}.
    """
    doc = pymupdf.open(str(pdf_path))
    out = {}
    for i in page_indices:
        out[i] = doc[i].get_text("text")
    doc.close()
    return out


def render_pages_for_ocr(
    pdf_path: str | Path,
    page_indices: list[int],
    dpi: int = 200,
) -> dict[int, bytes]:
    """
    Render OCR_NEEDED pages to PNG bytes at the given DPI.
    Returns {page_index: png_bytes} — pass directly to your OCR engine.

    Tip: 150 dpi is usually enough for Tesseract; 300 dpi for cloud APIs.
    """
    doc = pymupdf.open(str(pdf_path))
    out = {}
    mat = pymupdf.Matrix(dpi / 72, dpi / 72)
    for i in page_indices:
        pix = doc[i].get_pixmap(matrix=mat, colorspace=pymupdf.csGRAY)
        out[i] = pix.tobytes("png")
    doc.close()
    return out


# -- CLI entry point ---------------------------------------------------------

if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 2:
        print("Usage: python triage.py <file.pdf> [--details]")
        sys.exit(1)

    pdf_path     = sys.argv[1]
    show_details = "--details" in sys.argv

    report = triage_document(pdf_path)

    if show_details:
        routes = route_pages(report)
        output = json.dumps({
            "path":       report.path,
            "page_count": report.page_count,
            "routes":     routes,
            "details": [
                {
                    "page":       s.page_number,
                    "bucket":     s.bucket.name,
                    "reason":     s.reason,
                    "chars":      s.char_count,
                    "words":      s.word_count,
                    "images":     s.image_count,
                    "image_cov":  round(s.image_coverage, 3),
                    "text_cov":   round(s.text_coverage, 3),
                    "has_tables": s.has_tables,
                    "has_forms":  s.has_forms,
                    "has_vector": s.vector_drawing,
                }
                for s in report.results
            ]
        }, indent=2)

        print(output)

    else:
        print(report.summary())
        print()
        for s in report.results:
            print(f"  p{s.page_number:>4}  [{s.bucket.name:<12}]  {s.reason}")
