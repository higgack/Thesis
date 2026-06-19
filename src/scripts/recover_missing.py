"""Re-forward forward-target messages that were never ingested.

Companion to verify_ingest: takes the same audit and, for any message
that landed in FORWARD_TARGET (Noah's RAG) but isn't in
meta.documents, forwards it again to the same target. The bot picks
up the new message via on_channel_post and runs the full ingest
pipeline — file_hash / text_hash dedup short-circuits anything that
WAS already learned, so the re-forward is safe even on close calls.

Per-forward sleep keeps Telegram's outbound rate limit happy
(~1 message/sec sustained per chat).

Run:
  docker exec thesis-forward-listener-1 python3 -m src.scripts.recover_missing
  docker exec thesis-forward-listener-1 python3 -m src.scripts.recover_missing --limit 500 --dry-run
  docker exec thesis-forward-listener-1 python3 -m src.scripts.recover_missing --confirm
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import tempfile

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from .. import config
from .forward_listener import _resolve_channel, _forward_target
from .verify_ingest import _classify


_RATE_LIMIT_SLEEP = 1.2  # seconds between forwards


async def run(limit: int, confirm: bool) -> None:
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    target_name = _forward_target()

    src_session = config.DATA_DIR / "import_session.session"
    if not src_session.exists():
        sys.exit(f"session not found at {src_session}")
    db_path = config.DATA_DIR / "meta.db"
    if not db_path.exists():
        sys.exit(f"meta.db not found at {db_path}")

    with tempfile.TemporaryDirectory(prefix="recover_missing_") as tmp:
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
            print(f"scanning last {limit} msgs in '{title}' for missing items")

            con = sqlite3.connect(str(db_path), timeout=30)
            con.row_factory = sqlite3.Row
            missing: list = []
            try:
                async for m in client.iter_messages(target, limit=limit):
                    status, label = _classify(con, m)
                    if status == "missing":
                        missing.append((m, label))
            finally:
                con.close()

            print(f"\nfound {len(missing)} missing items.")
            if not missing:
                return

            # Show preview regardless of confirm so the operator sees
            # what's about to be re-sent.
            for m, label in missing[:25]:
                print(f"  [#{m.id}] {label}")
            if len(missing) > 25:
                print(f"  ... and {len(missing) - 25} more")

            if not confirm:
                print(
                    f"\n(dry run) re-run with --confirm to forward "
                    f"{len(missing)} messages back to '{title}'."
                )
                return

            print(f"\nforwarding {len(missing)} items "
                  f"(~{int(len(missing) * _RATE_LIMIT_SLEEP)}s with rate limit)...")
            sent = 0
            failed = 0
            for m, label in missing:
                try:
                    await client.forward_messages(target, m)
                    sent += 1
                    if sent % 10 == 0:
                        print(f"  ... forwarded {sent}/{len(missing)}")
                except FloodWaitError as e:
                    print(f"  flood wait {e.seconds}s, sleeping...")
                    await asyncio.sleep(e.seconds + 1)
                    try:
                        await client.forward_messages(target, m)
                        sent += 1
                    except Exception as e2:
                        failed += 1
                        print(f"  err [#{m.id}]: {type(e2).__name__}: {e2}")
                except Exception as e:
                    failed += 1
                    print(f"  err [#{m.id}]: {type(e).__name__}: {e}")
                await asyncio.sleep(_RATE_LIMIT_SLEEP)

            print(f"\n✓ re-forwarded: {sent}")
            if failed:
                print(f"✗ failed:        {failed}")
            print(
                "\nbot will ingest each one via on_channel_post; "
                "watch /queue and /status for progress."
            )
        finally:
            await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=500,
        help="how many recent forward-target messages to scan (default: 500)",
    )
    parser.add_argument(
        "--confirm", action="store_true",
        help="actually forward. Without this flag, only previews.",
    )
    args = parser.parse_args()
    asyncio.run(run(args.limit, args.confirm))


if __name__ == "__main__":
    main()
