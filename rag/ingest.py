#!/usr/bin/env python3
"""Ingest documents into the RAG-Anything index.

Usage:
    python rag/ingest.py rag/sources/paper.pdf      # single file
    python rag/ingest.py rag/sources/               # every file in a folder

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
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    files = collect(Path(sys.argv[1]))
    if not files:
        raise SystemExit("Nothing to ingest (no supported files found).")
    asyncio.run(main(files))
