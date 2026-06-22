"""Page triage — cheap PyMuPDF signals route each PDF page to the
cheapest adequate processor BEFORE spending on OCR / LLM.

Adapted from the ePapyrus page-triage approach for the study-notes
pipeline. The cost gate for note ingest (docs/notes_mvp_design.md
Phase 1): TEXT_ONLY pages cost ~0 (get_text), only OCR_NEEDED pages hit
Gemini Vision, only LLM_NEEDED pages hit the expensive parse/synthesis.

Sequencing: this module is a ready building block. Wiring it into
channel.py is the Phase 1 activation, gated on Phase 0 validation — the
pure rule logic (`triage`) is import-free so it is unit-testable without
PyMuPDF installed; `extract_signals`/`triage_document` import fitz lazily.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class Bucket(Enum):
    SKIP = auto()         # blank / negligible — don't process
    TEXT_ONLY = auto()    # native text extractable — free get_text
    OCR_NEEDED = auto()   # image-dominant / scanned — send to OCR
    LLM_NEEDED = auto()   # tables/forms/mixed — needs semantic parse


@dataclass
class PageSignals:
    page_number: int
    width: float
    height: float
    char_count: int = 0
    word_count: int = 0
    text_coverage: float = 0.0
    has_native_text: bool = False
    image_count: int = 0
    image_coverage: float = 0.0
    has_tables: bool = False
    has_forms: bool = False
    block_count: int = 0
    vector_drawing: bool = False
    bucket: Optional[Bucket] = field(default=None, init=False)
    reason: str = field(default="", init=False)


# ---- pure triage rules (no PyMuPDF import → unit-testable) -------------

def triage(sig: PageSignals, *,
           blank_char_threshold: int = 10,
           blank_image_threshold: float = 0.02,
           ocr_image_threshold: float = 0.25,
           ocr_text_threshold: int = 30,
           llm_complexity_score: int = 2) -> PageSignals:
    """Attach bucket + reason to a PageSignals. Thresholds keyword-only
    so callers tune per document type."""
    chars = sig.char_count
    imgcov = sig.image_coverage

    # Rule 1: blank → SKIP
    if chars < blank_char_threshold and imgcov < blank_image_threshold:
        sig.bucket = Bucket.SKIP
        sig.reason = f"blank (chars={chars}, img_cov={imgcov:.2f})"
        return sig

    # Rule 2: image-dominant + little text → OCR
    if imgcov >= ocr_image_threshold and chars < ocr_text_threshold:
        sig.bucket = Bucket.OCR_NEEDED
        sig.reason = (f"image-dominant (img_cov={imgcov:.2f}, chars={chars})"
                      " — likely scanned/image-only")
        return sig

    # Rule 3: structured/complex → LLM
    complexity = sum([
        sig.has_tables,
        sig.has_forms,
        sig.image_count > 0 and sig.has_native_text,   # mixed image+text
        sig.block_count > 30,                          # dense layout
        sig.text_coverage < 0.10 and chars > 50,       # sparse/forms-like
    ])
    if complexity >= llm_complexity_score:
        bits = [
            "tables" if sig.has_tables else "",
            "forms" if sig.has_forms else "",
            "mixed" if (sig.image_count > 0 and sig.has_native_text) else "",
            f"{sig.block_count} blocks" if sig.block_count > 30 else "",
            "sparse text" if (sig.text_coverage < 0.10 and chars > 50) else "",
        ]
        sig.bucket = Bucket.LLM_NEEDED
        sig.reason = (f"complex (score={complexity}/5): "
                      + ", ".join(b for b in bits if b))
        return sig

    # Rule 4: clean native text → TEXT_ONLY
    sig.bucket = Bucket.TEXT_ONLY
    sig.reason = (f"native text (chars={chars}, words={sig.word_count}, "
                  f"text_cov={sig.text_coverage:.2f})")
    return sig


# ---- signal extraction (lazy fitz import) -----------------------------

def extract_signals(page) -> PageSignals:
    """Cheap per-page signals from a PyMuPDF page. All local C/Python —
    nothing sent to a network service."""
    import fitz  # PyMuPDF (thesis already depends on it)
    rect = page.rect
    page_area = (rect.width * rect.height) or 1.0
    sig = PageSignals(page_number=page.number, width=rect.width,
                      height=rect.height)

    words = page.get_text("words")
    sig.word_count = len(words)
    sig.char_count = sum(len(w[4]) for w in words)
    sig.has_native_text = sig.char_count > 20

    blocks = page.get_text("blocks")
    sig.block_count = len(blocks)
    text_area = sum((b[2] - b[0]) * (b[3] - b[1])
                    for b in blocks if len(b) > 6 and b[6] == 0)
    sig.text_coverage = min(text_area / page_area, 1.0)

    try:
        images = page.get_image_info(hashes=False, xrefs=False)
    except Exception:
        images = []
    sig.image_count = len(images)
    img_area = sum((im["bbox"][2] - im["bbox"][0]) * (im["bbox"][3] - im["bbox"][1])
                   for im in images if im.get("bbox"))
    sig.image_coverage = min(img_area / page_area, 1.0)

    try:
        tabs = page.find_tables()
        sig.has_tables = len(getattr(tabs, "tables", tabs) or []) > 0
    except Exception:
        sig.has_tables = False

    try:
        for annot in (page.annots() or []):
            if annot.type[0] == fitz.PDF_ANNOT_WIDGET:
                sig.has_forms = True
                break
    except Exception:
        pass

    try:
        sig.vector_drawing = len(page.get_drawings()) > 0
    except Exception:
        sig.vector_drawing = False
    return sig


@dataclass
class TriageReport:
    path: str
    page_count: int
    results: list

    def routes(self) -> dict[str, list[int]]:
        """Zero-indexed page numbers grouped by processing route."""
        out = {"skip": [], "text": [], "ocr": [], "llm": []}
        key = {Bucket.SKIP: "skip", Bucket.TEXT_ONLY: "text",
               Bucket.OCR_NEEDED: "ocr", Bucket.LLM_NEEDED: "llm"}
        for r in self.results:
            out[key[r.bucket]].append(r.page_number)
        return out


def triage_document(path: str | Path, **kw) -> TriageReport:
    """Open a PDF read-only and triage every page."""
    import fitz
    doc = fitz.open(str(path))
    results = []
    try:
        for page in doc:
            results.append(triage(extract_signals(page), **kw))
    finally:
        doc.close()
    return TriageReport(path=str(path), page_count=len(results), results=results)
