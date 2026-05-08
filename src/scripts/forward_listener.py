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


def _forward_target() -> str:
    """Where to mirror new messages.

    FORWARD_TARGET takes precedence so users can pipe everything into
    their own knowledge channel (where the bot is admin, so channel_post
    triggers ingest naturally and the user keeps a visible archive).
    Falls back to BOT_USERNAME (DM mode) for backward compatibility."""
    target = os.getenv("FORWARD_TARGET", "").strip().lstrip("@")
    if target:
        return target
    name = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
    if not name:
        sys.exit(
            "Neither FORWARD_TARGET nor BOT_USERNAME is set in .env.\n"
            "  Set FORWARD_TARGET=<your_channel_username> to forward into\n"
            "  a channel (recommended), or BOT_USERNAME=<bot> for DM mode."
        )
    return name


async def _resolve_channel(client: TelegramClient, channel: str):
    if "+" in channel and ("t.me" in channel or channel.startswith("+")):
        from telethon.tl.functions.messages import (
            CheckChatInviteRequest, ImportChatInviteRequest,
        )
        invite_hash = channel.rsplit("+", 1)[-1].rstrip("/")
        try:
            invite = await client(CheckChatInviteRequest(invite_hash))
            chat = getattr(invite, "chat", None)
            if chat is not None:
                return chat
        except Exception:
            pass
        result = await client(ImportChatInviteRequest(invite_hash))
        return result.chats[0]
    return await client.get_entity(channel)


RECONNECT_DELAY_SEC = 5
# How many low-level (re)connect attempts Telethon does before giving up
# inside one client lifecycle. Big number = effectively never.
TELETHON_CONN_RETRIES = 1000


# Always-skipped substrings, hardcoded so they ship with the code and
# don't depend on the user remembering to update INGEST_SKIP_PATTERNS
# on each new bot meta-message we want to ignore.
_BUILTIN_SKIP_PATTERNS = [
    # Our own help text — leading title that only ever appears in /help
    "second brain 봇 사용법",
    # /usage outputs from this bot and the user's other dashboards
    "second brain 사용 현황",
    "noah 요약봇 사용 현황",
    "noah 요약봇 사용현황",
    "gemini api 비용 (추정)",
    "봇 사용 현황",
    # Deploy notifications from any of the user's auto-deploy scripts
    "배포 완료",
    "봇 배포",
    "자동 배포 시작",
]


def _skip_patterns() -> list[str]:
    """Substring blocklist — any match in a message body skips the
    forward. Combines hardcoded baseline (system messages, deploy
    notifications, our own help text) with INGEST_SKIP_PATTERNS env
    for the user's per-deployment additions (semicolon-separated)."""
    raw = os.getenv("INGEST_SKIP_PATTERNS", "")
    extras = [p.strip().lower() for p in raw.split(";") if p.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for p in _BUILTIN_SKIP_PATTERNS + extras:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


async def _client_lifecycle(channel: str, target_name: str,
                            api_id: int, api_hash: str,
                            session_path) -> None:
    """One full client connection — start, subscribe, run until the
    socket dies, then return so the outer loop reconnects."""
    client = TelegramClient(
        str(session_path), api_id, api_hash,
        connection_retries=TELETHON_CONN_RETRIES,
        retry_delay=2,
        auto_reconnect=True,
        request_retries=10,
    )
    try:
        await client.start()
        try:
            src = await _resolve_channel(client, channel)
            target = await _resolve_channel(client, target_name)
        except Exception as e:
            print(
                f"resolve failed: {type(e).__name__}: {e} — "
                f"sleeping {RECONNECT_DELAY_SEC}s before retry"
            )
            return

        print(f"listening: {getattr(src, 'title', channel)}")
        print(f"forwarding to: {getattr(target, 'title', None) or target_name}")
        print("running... Ctrl+C to stop\n")

        skip_patterns = _skip_patterns()
        if skip_patterns:
            print(f"skip patterns: {skip_patterns}")

        @client.on(events.NewMessage(chats=src))
        async def handler(event):
            msg = event.message
            text = (msg.message or "").strip()

            # Skip user-typed slash commands so they never get learned.
            if text.startswith("/"):
                print(f"  skip slash cmd {msg.id}")
                return

            # Skip a bot's direct reply to a slash command (e.g. Noah
            # 요약봇 답하는 /usage 출력). Regular bot-posted summaries
            # aren't reply_to anything, so they pass through.
            reply_to_id = getattr(msg, "reply_to_msg_id", None)
            if reply_to_id:
                try:
                    parent = await client.get_messages(src, ids=reply_to_id)
                    parent_text = (getattr(parent, "message", "") or "").strip()
                    if parent_text.startswith("/"):
                        print(f"  skip reply-to-cmd {msg.id} → parent {reply_to_id}")
                        return
                except Exception as e:
                    # If the parent lookup fails (deleted/inaccessible),
                    # err on the side of forwarding rather than blocking.
                    print(f"  reply lookup failed {reply_to_id}: {e}")

            # Pattern-based skip — fires when reply_to chain doesn't
            # exist (channel context, forwarded posts, edited messages).
            if skip_patterns and text:
                lc = text.lower()
                for pat in skip_patterns:
                    if pat in lc:
                        print(f"  skip pattern '{pat}' on {msg.id}")
                        return

            try:
                await event.message.forward_to(target)
                print(f"  forwarded msg {msg.id}")
            except FloodWaitError as e:
                print(f"  flood wait {e.seconds}s")
                await asyncio.sleep(e.seconds + 1)
            except Exception as e:
                print(f"  err {msg.id}: {type(e).__name__}: {e}")

        await client.run_until_disconnected()
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def run(channel: str) -> None:
    """Outer loop: reconnect forever so a flaky network or a
    Telethon-side socket close can never silently drop forwarded
    messages. Each iteration spins up a fresh TelegramClient with
    auto_reconnect on, runs until disconnected, then sleeps a short
    backoff before reconnecting."""
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    target_name = _forward_target()
    session_path = config.DATA_DIR / "import_session"

    while True:
        try:
            await _client_lifecycle(
                channel, target_name, api_id, api_hash, session_path,
            )
            print(
                f"client disconnected — reconnecting in "
                f"{RECONNECT_DELAY_SEC}s..."
            )
        except KeyboardInterrupt:
            print("interrupted")
            raise
        except Exception as e:
            print(
                f"client loop error: {type(e).__name__}: {e} — "
                f"sleeping {RECONNECT_DELAY_SEC}s before retry"
            )
        await asyncio.sleep(RECONNECT_DELAY_SEC)


def main() -> None:
    if len(sys.argv) >= 2:
        channel = sys.argv[1]
    else:
        channel = os.getenv("LISTEN_CHANNEL", "").strip()
    if not channel:
        sys.exit(
            "Channel not provided. Pass as CLI arg or set LISTEN_CHANNEL env var.\n"
            "  e.g. LISTEN_CHANNEL='https://t.me/+ABCxyz'"
        )
    if (channel.startswith("https://t.me/") or channel.startswith("t.me/")) \
            and "+" not in channel:
        channel = channel.split("t.me/", 1)[1].rstrip("/")
    asyncio.run(run(channel))


if __name__ == "__main__":
    main()
