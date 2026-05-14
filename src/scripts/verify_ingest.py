"""Verify recent forward-target messages were actually ingested.

Compares the bot's "storage channel" (FORWARD_TARGET, where the
forward-listener pushes everything for the bot to pick up) against
meta.documents to surface any messages that landed in the channel
but never made it into the searchable corpus. Read-only — never
modifies anything.

For each of the last N messages in the target channel:
  - text body  → expected source `tg-msg:<msg_id>:<sha1[:8]>`
  - document   → expected source `tg-doc:<file_unique_id>:<filename>`
  - photo      → expected source `tg-photo:<file_unique_id>`
  - inline URL → expected source = the URL itself

Prints a summary (learned / duplicated-by-content / missing) and
the first ~20 missing items so the operator can /failed_retry or
manually re-send them.

Run:
  docker exec thesis-forward-listener-1 python3 -m src.scripts.verify_ingest
  docker exec thesis-forward-listener-1 python3 -m src.scripts.verify_ingest --limit 200
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import sqlite3
import sys
import tempfile

from telethon import TelegramClient

from .. import config
from .forward_listener import _all_message_urls, _resolve_channel, _forward_target


_HTTP_URL_RE = re.compile(r"https?://[^\s)\]>]+")


def _doc_exists_by_source(con: sqlite3.Connection, source: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM documents WHERE source=? LIMIT 1", (source,)
    ).fetchone()
    return row is not None


def _doc_exists_by_text_hash(con: sqlite3.Connection, hash8: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM documents WHERE type='text' AND source LIKE ? LIMIT 1",
        (f"%:{hash8}",),
    ).fetchone()
    return row is not None


def _doc_exists_by_url(con: sqlite3.Connection, url: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM documents WHERE source=? LIMIT 1", (url,)
    ).fetchone()
    return row is not None


def _classify(con: sqlite3.Connection, msg) -> tuple[str, str]:
    """Return (status, label).
    status ∈ {'learned', 'missing', 'skipped_blocked'}.
    label is a short description of what the message is."""
    # Document attachment
    if getattr(msg, "document", None) and getattr(msg.document, "attributes", None):
        # Find filename attribute
        from telethon.tl.types import DocumentAttributeFilename
        fname = ""
        for attr in msg.document.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                fname = attr.file_name
                break
        if fname:
            uniq = getattr(msg.document, "id", "")
            # source uses file_unique_id; we don't have a way to compute
            # that without the bot api. Filename match is the next best
            # signal — find_by_filename catches both tg-doc:%:<name> and
            # local:<name>.
            row = con.execute(
                "SELECT 1 FROM documents "
                "WHERE source LIKE ? OR source = ? LIMIT 1",
                (f"tg-doc:%:{fname}", f"local:{fname}"),
            ).fetchone()
            return ("learned" if row else "missing", f"doc {fname}")

    # Photo
    if getattr(msg, "photo", None):
        # Photo source label uses Telegram's file_unique_id. Telethon
        # doesn't expose the same id format as Bot API, so we can't
        # match exactly; sniff by text-prefix instead.
        return ("unknown", "photo (id format mismatch)")

    text = (msg.message or "").strip()
    if not text:
        return ("skipped_blocked", "empty body")

    # Text body — primary signal
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    if _doc_exists_by_text_hash(con, h):
        return ("learned", f"text \"{text[:40]}…\"")

    # Inline URL — listener may have ingested via URL path instead of text
    urls = _all_message_urls(msg)
    for url in urls:
        if _doc_exists_by_url(con, url):
            return ("learned", f"url {url[:60]}")

    # Nothing matched
    short = text[:60].replace("\n", " ")
    return ("missing", f"text \"{short}…\"")


async def run(limit: int) -> None:
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    target_name = _forward_target()

    src_session = config.DATA_DIR / "import_session.session"
    if not src_session.exists():
        sys.exit(f"session not found at {src_session}")
    db_path = config.DATA_DIR / "meta.db"
    if not db_path.exists():
        sys.exit(f"meta.db not found at {db_path}")

    with tempfile.TemporaryDirectory(prefix="verify_ingest_") as tmp:
        copy_path = os.path.join(tmp, "verify.session")
        src_conn = sqlite3.connect(
            f"file:{src_session}?mode=ro", uri=True, timeout=10,
        )
        dst_conn = sqlite3.connect(copy_path)
        try:
            src_conn.backup(dst_conn)
        finally:
            src_conn.close()
            dst_conn.close()

        client = TelegramClient(copy_path[:-len(".session")], api_id, api_hash)
        await client.start()
        try:
            target = await _resolve_channel(client, target_name)
            title = getattr(target, "title", target_name)
            print(f"verifying last {limit} msgs in '{title}' against meta.documents")

            con = sqlite3.connect(str(db_path))
            con.row_factory = sqlite3.Row

            counts = {"learned": 0, "missing": 0, "skipped_blocked": 0,
                      "unknown": 0}
            missing_samples: list[tuple[int, str]] = []
            try:
                async for m in client.iter_messages(target, limit=limit):
                    status, label = _classify(con, m)
                    counts[status] = counts.get(status, 0) + 1
                    if status == "missing" and len(missing_samples) < 25:
                        missing_samples.append((m.id, label))
            finally:
                con.close()

            total = sum(counts.values())
            print(f"\n  total scanned : {total}")
            print(f"  ✅ learned    : {counts.get('learned', 0)}")
            print(f"  ⚪ skipped    : {counts.get('skipped_blocked', 0)}  (empty body)")
            print(f"  ❓ unknown    : {counts.get('unknown', 0)}  (id-format mismatch)")
            print(f"  ❌ missing    : {counts.get('missing', 0)}")

            if missing_samples:
                print(f"\n  missing samples (first {len(missing_samples)}):")
                for mid, label in missing_samples:
                    print(f"    [#{mid}] {label}")
            elif counts.get("missing", 0) == 0:
                print("\n  ✨ no missing items in this window.")
        finally:
            await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=100,
        help="how many recent forward-target messages to scan (default: 100)",
    )
    args = parser.parse_args()
    asyncio.run(run(args.limit))


if __name__ == "__main__":
    main()
