#!/usr/bin/env python3
"""Ingest documents into the RAG-Anything index.

Usage:
    python rag/ingest.py rag/sources/paper.pdf            # single file
    python rag/ingest.py rag/sources/                     # every file in a folder
    python rag/ingest.py rag/sources/ --triage           # skip blank PDFs first

With --triage, each PDF is run through rag/triage.py first (cheap PyMuPDF
signals); documents whose every page is blank are skipped before paying for
RAG-Anything parsing/OCR/LLM. Requires pymupdf (in rag/requirements.txt).

Parses + indexes into rag/rag_storage/ (configured via rag/.env).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from _common import build_rag

# Extensions RAG-Anything can parse (best-effort; unsupported files are skipped).
SUPPORTED = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".txt", ".md", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff",
}


def collect(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    if target.is_dir():
        return sorted(
            p for p in target.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED
        )
    raise SystemExit(f"No such file or directory: {target}")


def filter_with_triage(paths: list[Path]) -> list[Path]:
    """Drop PDFs that triage finds entirely blank. Non-PDFs pass through."""
    from triage import Bucket, triage_document

    keep: list[Path] = []
    for path in paths:
        if path.suffix.lower() != ".pdf":
            keep.append(path)
            continue
        report = triage_document(path)
        skipped = report.by_bucket[Bucket.SKIP]
        if report.page_count and len(skipped) == report.page_count:
            print(f"⊘ skipping (all {report.page_count} pages blank): {path}")
            continue
        print(
            f"✓ keep {path}  "
            f"[blank={len(skipped)}/{report.page_count}]"
        )
        keep.append(path)
    return keep


async def main(paths: list[Path]) -> None:
    rag = build_rag()
    output_dir = os.getenv("OUTPUT_DIR", "rag/output")
    for path in paths:
        print(f"→ ingesting {path}")
        await rag.process_document_complete(
            file_path=str(path),
            output_dir=output_dir,
        )
    print(f"Done. Indexed {len(paths)} document(s).")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--triage"]
    use_triage = "--triage" in sys.argv
    if len(args) != 1:
        print(__doc__)
        raise SystemExit(2)
    files = collect(Path(args[0]))
    if use_triage:
        files = filter_with_triage(files)
    if not files:
        raise SystemExit("Nothing to ingest (no supported files found).")
    asyncio.run(main(files))
