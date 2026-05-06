"""Bulk-ingest every PDF / PPTX / DOCX / TXT under a folder.

Usage (inside the bot container):
    docker compose exec bot python -m src.scripts.bulk_ingest /app/data/incoming

Files already in the brain (matching source label) are skipped, so re-running
is safe.
"""
import asyncio
import sys
from pathlib import Path

from ..ingest import pipeline
from ..store import meta

_HANDLERS = {
    ".pdf": pipeline.ingest_pdf,
    ".pptx": pipeline.ingest_pptx,
    ".docx": pipeline.ingest_docx,
}


async def _ingest_one(path: Path, label: str) -> dict:
    ext = path.suffix.lower()
    handler = _HANDLERS.get(ext)
    if handler is not None:
        return await handler(path, label)
    if ext in {".txt", ".md"}:
        text = path.read_text(encoding="utf-8", errors="ignore")
        return await pipeline.ingest_text(text, label)
    return {"status": "skipped", "title": path.name, "reason": f"unsupported ext {ext}"}


async def main(root: Path) -> None:
    if not root.exists():
        print(f"path not found: {root}")
        sys.exit(1)
    meta.init()
    files = sorted(p for p in root.rglob("*") if p.is_file())
    accepted = [".pdf", ".pptx", ".docx", ".txt", ".md"]
    files = [p for p in files if p.suffix.lower() in accepted]

    print(f"Found {len(files)} ingestable files under {root}")
    counts: dict[str, int] = {}
    for i, p in enumerate(files, 1):
        rel = p.relative_to(root)
        label = f"bulk:{rel}"
        print(f"[{i:>4}/{len(files)}] {rel}", flush=True)
        try:
            r = await _ingest_one(p, label)
            status = r.get("status", "?")
            title = (r.get("title") or "")[:70]
            print(f"      -> {status:<10} {title}")
        except Exception as e:
            r = {"status": "error"}
            print(f"      -> error      {type(e).__name__}: {e}")
        counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1

    print()
    print("=== Summary ===")
    for k, v in sorted(counts.items()):
        print(f"  {k:<10} {v}")
    print(f"Total docs in brain: {meta.count()}")


if __name__ == "__main__":
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/app/data/incoming").resolve()
    asyncio.run(main(root))
