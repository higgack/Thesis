"""24/7 listener that mirrors a source channel into the bot in real
time.

Telethon logs in with the user account (same session as
import_channel.py) and subscribes to NewMessage events on the source
channel.

Two operating modes (auto-detected from each message body):

1. Digest expansion — when a message is one of Noah Second Brain's
   periodic digests (📋 텔레그램, 📰 Substack), the listener does NOT
   forward the digest body itself. Instead it extracts every original-
   source link inside the digest and forwards each to the target
   channel (Telegram links via native message-forward so the bot sees
   the ORIGINAL author/text, Substack/web links as plain-text URLs so
   the bot's URL pipeline crawls them).

   The 🐦 X (Twitter) digest is dropped entirely — X API access is
   gated/paid so chasing tweet URLs isn't viable; the LLM-summarised
   body itself was the only signal and it's not worth ingesting.

2. Everything else (chat, non-digest forwards, edited messages) is
   dropped silently. This is intentional: the bot was paying ~₩2-5k/day
   summarising mirrored noise before digest mode landed. Only digest
   content makes it through now.

Usage (run inside tmux/systemd so it survives logout):

    python -m src.scripts.forward_listener <channel_username>

Reads BOT_USERNAME, TELEGRAM_API_ID, TELEGRAM_API_HASH from env
(same as import_channel.py). Reuses
$DATA_DIR/import_session.session — phone/SMS only on first run.
"""
import asyncio
import os
import re
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


# ----------------- Digest expansion -----------------

# Digest header signatures — these are stable Noah Second Brain
# templates posted 4-5 times/day each. Detection is anchored on emoji +
# "요약" so a regular forward that just happens to include the word
# 채널 doesn't get expanded by mistake.
_DIGEST_TG_RE = re.compile(r"📋[^\n]*채널\s*요약")
_DIGEST_SUBSTACK_RE = re.compile(r"📰[^\n]*Substack\s*요약")
_DIGEST_X_RE = re.compile(r"🐦[^\n]*(?:X\s*타임라인|타임라인)\s*요약")

# Pull every t.me/<channel>/<msg_id> or t.me/c/<chat_id>/<msg_id> hit
# from the digest body. Private-channel links use the numeric c/... form
# and only resolve when the listener's Telethon account is already a
# member of that chat — failures are logged and skipped.
_TG_URL_RE = re.compile(
    r"https?://t\.me/(c/\d+|[A-Za-z][\w]{1,63})/(\d+)"
)

# Match any http(s) URL but exclude t.me (telegram has its own path).
# Substack digests link to substack.com, custom domains
# (newsletter.semianalysis.com), and occasional non-substack analyst
# sites — capturing every link in the body is the most robust.
_HTTP_URL_RE = re.compile(r"https?://[^\s)\]>]+")

# Per-link throttle so a 100-URL digest doesn't trip Telegram's
# floodwait. 0.5s = ~50s for a typical 100-link digest, safe.
_THROTTLE_SEC = 0.5


def _extract_telegram_links(text: str) -> list[tuple[str, int]]:
    seen: set[tuple[str, int]] = set()
    out: list[tuple[str, int]] = []
    for m in _TG_URL_RE.finditer(text):
        ch, mid = m.group(1), int(m.group(2))
        key = (ch, mid)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _extract_substack_links(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in _HTTP_URL_RE.finditer(text):
        url = m.group(0).rstrip(".,);")
        if "t.me/" in url:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


async def _resolve_tg_channel(client: TelegramClient, ch: str):
    """Resolve 'c/1234567890' (numeric private id) or username to a
    Telethon peer. Falls back to None on any error so we don't crash
    the digest loop over one bad link."""
    try:
        if ch.startswith("c/"):
            from telethon.tl.types import PeerChannel
            # 100-prefix is the Telegram-standard MTProto channel id
            # encoding for clients; user-side links use the bare digits.
            chat_id = int("100" + ch[2:])
            return await client.get_entity(PeerChannel(chat_id))
        return await client.get_entity(ch)
    except Exception as e:
        print(f"    cannot resolve channel '{ch}': {type(e).__name__}: {e}")
        return None


async def _expand_telegram_digest(client: TelegramClient, text: str,
                                   target, parent_id: int) -> None:
    links = _extract_telegram_links(text)
    print(f"  TG digest msg {parent_id}: {len(links)} links to expand")
    forwarded = 0
    for ch, mid in links:
        peer = await _resolve_tg_channel(client, ch)
        if peer is None:
            continue
        try:
            orig = await client.get_messages(peer, ids=mid)
            if not orig:
                print(f"    skip {ch}/{mid}: message not found")
                continue
            await orig.forward_to(target)
            forwarded += 1
            print(f"    fwd {ch}/{mid} ({forwarded}/{len(links)})")
        except FloodWaitError as e:
            print(f"    flood wait {e.seconds}s on {ch}/{mid}")
            await asyncio.sleep(e.seconds + 1)
        except Exception as e:
            print(f"    err {ch}/{mid}: {type(e).__name__}: {e}")
        await asyncio.sleep(_THROTTLE_SEC)
    print(f"  TG digest msg {parent_id}: done, forwarded {forwarded}/{len(links)}")


async def _expand_substack_digest(client: TelegramClient, text: str,
                                   target, parent_id: int) -> None:
    urls = _extract_substack_links(text)
    print(f"  Substack digest msg {parent_id}: {len(urls)} URLs to relay")
    sent = 0
    for url in urls:
        try:
            await client.send_message(target, url, link_preview=False)
            sent += 1
            print(f"    sent {url} ({sent}/{len(urls)})")
        except FloodWaitError as e:
            print(f"    flood wait {e.seconds}s on {url}")
            await asyncio.sleep(e.seconds + 1)
        except Exception as e:
            print(f"    err {url}: {type(e).__name__}: {e}")
        await asyncio.sleep(_THROTTLE_SEC)
    print(f"  Substack digest msg {parent_id}: done, relayed {sent}/{len(urls)}")


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
        print("digest mode: 📋 Telegram → expand · 📰 Substack → expand · "
              "🐦 X → drop · other → drop")
        print("running... Ctrl+C to stop\n")

        @client.on(events.NewMessage(chats=src))
        async def handler(event):
            msg = event.message
            text = (msg.message or "").strip()

            # X digest: drop. X API access is paid/restricted and the
            # LLM summary in the digest body alone isn't worth the cost
            # of ingesting at scale.
            if _DIGEST_X_RE.search(text):
                print(f"  skip X digest {msg.id}")
                return

            # Telegram digest: expand each 원문 link into its original
            # message and forward those instead of the digest body.
            if _DIGEST_TG_RE.search(text):
                await _expand_telegram_digest(client, text, target, msg.id)
                return

            # Substack digest: relay each article URL as a plain text
            # message so the bot's URL pipeline crawls the full article.
            if _DIGEST_SUBSTACK_RE.search(text):
                await _expand_substack_digest(client, text, target, msg.id)
                return

            # Anything else (chat, screenshots, ad-hoc forwards) is
            # dropped. Earlier behaviour was to mirror everything, but
            # that summarised ~₩2-5k/day of noise. Only digest content
            # makes it through now — see module docstring.
            print(f"  drop non-digest msg {msg.id}")

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
