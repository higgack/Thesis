"""24/7 listener that mirrors a source channel into the bot in real
time.

Telethon logs in with the user account (same session as
import_channel.py) and subscribes to NewMessage events on the source
channel. Each new post is forwarded to the bot DM, which then runs
through the standard ingest pipeline (with the new dedup + URL-skip
rules so automation digests don't blow up costs).

Usage (run inside tmux/systemd so it survives logout):

    python -m src.scripts.forward_listener <channel_username>

Reads BOT_USERNAME, TELEGRAM_API_ID, TELEGRAM_API_HASH from env
(same as import_channel.py). Reuses
$DATA_DIR/import_session.session — phone/SMS only on first run.
"""
import asyncio
import os
import sys

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

from .. import config


def _bot_username() -> str:
    name = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
    if not name:
        sys.exit(
            "BOT_USERNAME not set. Add it to .env (just the username, no @)."
        )
    return name


async def run(channel: str) -> None:
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

    print(f"listening: {getattr(src, 'title', channel)}")
    print(f"forwarding to: @{bot_username}")
    print("running... Ctrl+C to stop\n")

    @client.on(events.NewMessage(chats=src))
    async def handler(event):
        try:
            await event.message.forward_to(bot)
            print(f"  forwarded msg {event.message.id}")
        except FloodWaitError as e:
            print(f"  flood wait {e.seconds}s")
            await asyncio.sleep(e.seconds + 1)
        except Exception as e:
            print(f"  err {event.message.id}: {type(e).__name__}: {e}")

    await client.run_until_disconnected()


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: python -m src.scripts.forward_listener <channel_username>")
    channel = sys.argv[1]
    if channel.startswith("https://t.me/") or channel.startswith("t.me/"):
        channel = channel.split("t.me/", 1)[1].rstrip("/")
    asyncio.run(run(channel))


if __name__ == "__main__":
    main()
