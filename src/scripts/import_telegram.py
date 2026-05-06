"""Bulk-import a Telegram Desktop HTML export into the brain.

Usage (inside the bot container):
    python -m src.scripts.import_telegram /app/data/imports/tg_export

The export folder must contain at least one messages*.html and a files/
sub-folder. Re-running is safe: each item is keyed by a stable source
label and the pipeline skips duplicates.
"""
import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

from ..ingest import pipeline
from ..store import meta

URL_RE = re.compile(r"https?://\S+")


def _set_log_level(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _ingest_file(path: Path, label: str) -> dict:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return await pipeline.ingest_pdf(path, label)
    if suffix == ".pptx":
        return await pipeline.ingest_pptx(path, label)
    if suffix == ".docx":
        return await pipeline.ingest_docx(path, label)
    if suffix in {".txt", ".md"}:
        text = path.read_text(encoding="utf-8", errors="ignore")
        return await pipeline.ingest_text(text, label)
    return {"status": "skipped", "title": path.name, "reason": f"unsupported {suffix}"}


async def import_files(files_dir: Path) -> dict:
    counts = {"ok": 0, "duplicate": 0, "empty": 0, "skipped": 0, "error": 0}
    for path in sorted(files_dir.rglob("*")):
        if not path.is_file():
            continue
        label = f"tg-export-file:{path.name}"
        try:
            r = await _ingest_file(path, label)
        except Exception as e:
            logging.exception("file failed: %s", path)
            r = {"status": "error", "error": str(e), "title": path.name}
        status = r.get("status", "error")
        counts[status if status in counts else "error"] += 1
        title = r.get("title", path.name)
        print(f"  [{status:9}] {title}")
    return counts


async def import_messages(html_paths: list[Path]) -> dict:
    counts = {"ok": 0, "duplicate": 0, "empty": 0, "error": 0}
    for html_path in html_paths:
        soup = BeautifulSoup(
            html_path.read_text(encoding="utf-8", errors="ignore"), "html.parser"
        )
        msgs = soup.select("div.message.default") or soup.select("div.message")
        for msg in msgs:
            if "service" in (msg.get("class") or []):
                continue
            text_div = msg.find("div", class_="text")
            if not text_div:
                continue
            text = text_div.get_text("\n", strip=True)
            if not text or len(text) < 5:
                continue
            msg_id = msg.get("id", f"unknown-{counts['ok']}")
            for url in URL_RE.findall(text):
                try:
                    r = await pipeline.ingest_url(url)
                except Exception as e:
                    logging.warning("url failed %s: %s", url, e)
                    r = {"status": "error", "error": str(e)}
                status = r.get("status", "error")
                counts[status if status in counts else "error"] += 1
                print(f"  [{status:9}] URL {r.get('title') or url}")
            text_no_urls = URL_RE.sub("", text).strip()
            if len(text_no_urls) >= 50:
                label = f"tg-export-msg:{html_path.stem}:{msg_id}"
                try:
                    r = await pipeline.ingest_text(text_no_urls, label)
                except Exception as e:
                    logging.exception("msg failed")
                    r = {"status": "error", "error": str(e)}
                status = r.get("status", "error")
                counts[status if status in counts else "error"] += 1
                print(f"  [{status:9}] MSG {r.get('title') or text_no_urls[:40]}")
    return counts


async def main_async(export_root: Path) -> int:
    if not export_root.is_dir():
        print(f"❌ Not a directory: {export_root}")
        return 2

    meta.init()

    files_dir = export_root / "files"
    html_paths = sorted(export_root.glob("messages*.html"))

    if not html_paths and not files_dir.is_dir():
        print(f"❌ No messages*.html or files/ found in {export_root}")
        return 2

    file_counts = {"ok": 0, "duplicate": 0, "empty": 0, "skipped": 0, "error": 0}
    msg_counts = {"ok": 0, "duplicate": 0, "empty": 0, "error": 0}

    if files_dir.is_dir():
        print(f"\n=== Files in {files_dir} ===")
        file_counts = await import_files(files_dir)

    if html_paths:
        print(f"\n=== Messages from {len(html_paths)} HTML page(s) ===")
        msg_counts = await import_messages(html_paths)

    print("\n=== Summary ===")
    print(f"Files     : {file_counts}")
    print(f"Messages  : {msg_counts}")
    print(f"Total docs in brain: {meta.count()}")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("export_dir", type=Path,
                        help="Telegram Desktop export folder root")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    _set_log_level(args.verbose)
    sys.exit(asyncio.run(main_async(args.export_dir.resolve())))


if __name__ == "__main__":
    main()
