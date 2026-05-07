"""One-shot importer that backfills the brain with a Telegram channel's
full history.

Bot API can only deliver channel_post updates from the moment the bot
became admin, so older messages are invisible to live ingest. This
script logs in with the *user* account (MTProto via Telethon), iterates
the channel from oldest → newest, and forwards each message to the bot
DM. The bot processes them through the normal _ingest_message_locked
path, which means file_unique_id-based dedup automatically skips
anything already in the brain — only the missing ones get embedded.

Usage (from VM, inside repo root):
    python -m src.scripts.import_channel <channel_username> [--throttle 2]

Env vars required (already in .env for this project):
    TELEGRAM_API_ID
    TELEGRAM_API_HASH
    TELEGRAM_BOT_TOKEN  (used only to derive bot username via /me)
    BOT_USERNAME        (e.g. mybot — without @)

First run prompts for phone + SMS code; session is saved to
$DATA_DIR/import_session.session for subsequent runs.
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from .. import config
from ..store import meta


def _bot_username() -> str:
    name = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
    if not name:
        sys.exit(
            "BOT_USERNAME not set. Add it to .env (just the username, no @).\n"
            "You can find it in BotFather: /mybots → your bot → username."
        )
    return name


async def run(channel: str, throttle: float, limit: int | None,
              since_id: int | None, resume: bool, resume_margin_hours: float) -> None:
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    bot_username = _bot_username()

    session_path = config.DATA_DIR / "import_session"
    client = TelegramClient(str(session_path), api_id, api_hash)
    await client.start()

    try:
        src = await client.get_entity(channel)
        bot = await client.get_entity(bot_username)
    except Exception as e:
        sys.exit(
            f"Could not resolve channel '{channel}' or bot '@{bot_username}': {e}"
        )

    iter_kwargs: dict = {"reverse": True, "limit": limit}
    resume_label = ""
    if since_id:
        iter_kwargs["min_id"] = since_id
        resume_label = f"since msg id {since_id}"
    elif resume:
        last = meta.last_ingested_at()
        if last:
            try:
                ts = datetime.fromisoformat(last)
            except ValueError:
                ts = None
            if ts:
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                cutoff = ts - timedelta(hours=resume_margin_hours)
                iter_kwargs["offset_date"] = cutoff
                resume_label = f"resume from {cutoff.isoformat()} (last ingest {ts.isoformat()}, margin {resume_margin_hours}h)"
        if not resume_label:
            resume_label = "resume requested but DB empty — full history"

    print(f"channel: {getattr(src, 'title', channel)}")
    print(f"forwarding to: @{bot_username}")
    print(f"throttle: {throttle}s between messages")
    if limit:
        print(f"limit: first {limit} messages")
    if resume_label:
        print(resume_label)
    print()

    forwarded = skipped = errors = 0
    async for msg in client.iter_messages(src, **iter_kwargs):
        if not (msg.text or msg.media):
            skipped += 1
            continue
        try:
            await msg.forward_to(bot)
            forwarded += 1
            if forwarded % 10 == 0:
                print(f"  forwarded {forwarded}  skipped {skipped}  errors {errors}")
            await asyncio.sleep(throttle)
        except FloodWaitError as e:
            print(f"  flood wait {e.seconds}s — sleeping")
            await asyncio.sleep(e.seconds + 1)
        except Exception as e:
            errors += 1
            print(f"  err msg {msg.id}: {type(e).__name__}: {e}")
            await asyncio.sleep(throttle * 2)

    print()
    print(f"done. forwarded={forwarded}  skipped={skipped}  errors={errors}")
    print("the bot is now processing them in the background — watch /queue and /failed.")
    await client.disconnect()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("channel", help="Channel username or t.me/<name> link")
    ap.add_argument("--throttle", type=float, default=2.0,
                    help="Seconds between forwards (default 2 ≈ 30/min, well under telegram caps)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Optional cap for testing (oldest N messages)")
    ap.add_argument("--since-id", type=int, default=None,
                    help="Only forward messages with id > SINCE_ID. Lets you pick up after a partial run.")
    ap.add_argument("--resume", action="store_true",
                    help="Auto-resume from the most recently ingested doc time minus a safety margin.")
    ap.add_argument("--resume-margin-hours", type=float, default=1.0,
                    help="Safety overlap when --resume is used (default 1.0h).")
    args = ap.parse_args()
    channel = args.channel
    if channel.startswith("https://t.me/") or channel.startswith("t.me/"):
        channel = channel.split("t.me/", 1)[1].rstrip("/")
    asyncio.run(run(channel, args.throttle, args.limit,
                    args.since_id, args.resume, args.resume_margin_hours))


if __name__ == "__main__":
    main()
