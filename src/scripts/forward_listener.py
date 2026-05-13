"""24/7 listener that mirrors one or more source channels into the bot
in real time.

Telethon logs in with the user account (same session as
import_channel.py) and subscribes to NewMessage events on every source
channel listed in `LISTEN_CHANNELS` (comma-separated; falls back to the
legacy `LISTEN_CHANNEL` env or a CLI arg for a single channel).

Each source can be handled in one of two modes:

1. **Digest mode (default)** — for Noah Second Brain's periodic digests
   the listener does NOT forward the digest body itself. It extracts
   every original-source link inside and forwards each:
     - 📋 텔레그램 채널 요약 → t.me/<ch>/<id> links fetched via
       Telethon and forwarded so the bot sees the ORIGINAL author/text.
     - 📰 Substack 요약 → every http URL relayed as plain text so the
       bot's URL pipeline crawls the full article.
     - 🐦 X 타임라인 요약 → dropped entirely (paid API, low value).
   Everything else (chat, screenshots, ad-hoc forwards) is dropped.

2. **Plain mode** — for channels listed in `LISTEN_PLAIN_CHANNELS` we
   forward each message body as-is, but strip channel-specific URL
   lines (DART, Naver finance, awakeplus.co.kr, transcript links, …)
   so the bot's URL pipeline doesn't chase auth-walled or duplicate
   pages. Charts/images attached to the original message are NOT
   forwarded (we use `send_message(text)` not `forward_to()`), which
   matches the user's "텍스트만 학습" preference.

Usage (run inside Docker — bot stack auto-restarts it on disconnect):

    LISTEN_CHANNELS=noah_channel,finter_gpt,jubung,...
    LISTEN_PLAIN_CHANNELS=finter_gpt,jubung,awake_globalwatch,...
    python -m src.scripts.forward_listener

Reads BOT_USERNAME, TELEGRAM_API_ID, TELEGRAM_API_HASH from env (same
as import_channel.py). Reuses `$DATA_DIR/import_session.session` —
phone/SMS only on first run.
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


def _parse_channel_list(env_key: str) -> list[str]:
    """Comma-separated env var → cleaned channel name list. Strips
    surrounding whitespace, `@`, and any `https://t.me/` prefix so
    users can paste channel URLs directly."""
    raw = os.getenv(env_key, "")
    out: list[str] = []
    for part in raw.split(","):
        ch = part.strip().lstrip("@")
        if not ch:
            continue
        if (ch.startswith("https://t.me/") or ch.startswith("t.me/")) and "+" not in ch:
            ch = ch.split("t.me/", 1)[1].rstrip("/")
        out.append(ch)
    return out


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


# ----------------- Digest expansion (Noah Second Brain) -----------------

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


def _all_message_urls(msg) -> list[str]:
    """Collect every URL appearing in a Telegram message — text body
    AND link entities. Noah's bot delivers each '원문 보기' as a
    Markdown/HTML hyperlink, so the URL lives in `msg.entities` rather
    than `msg.message`. Scanning text alone misses the entire link
    list (the symptom we hit on the first live Substack digest:
    `0 URLs to relay` despite 11 visible articles)."""
    from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl
    urls: list[str] = []
    text = msg.message or ""
    # Plain URLs typed inline in the body.
    for m in _HTTP_URL_RE.finditer(text):
        urls.append(m.group(0).rstrip(".,);"))
    # Hidden URLs from text-link entities ([label](url) markdown form).
    for ent in (msg.entities or []):
        if isinstance(ent, MessageEntityTextUrl):
            urls.append(ent.url.strip().rstrip(".,);"))
        elif isinstance(ent, MessageEntityUrl):
            # Auto-detected URL inside the body — we already caught
            # this via _HTTP_URL_RE, but slice from offset to be safe
            # for cases where the regex stopped early.
            urls.append(text[ent.offset:ent.offset + ent.length])
    return urls


def _extract_telegram_links(msg) -> list[tuple[str, int]]:
    seen: set[tuple[str, int]] = set()
    out: list[tuple[str, int]] = []
    for url in _all_message_urls(msg):
        m = _TG_URL_RE.search(url)
        if not m:
            continue
        ch, mid = m.group(1), int(m.group(2))
        key = (ch, mid)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _extract_substack_links(msg) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for url in _all_message_urls(msg):
        url = url.strip().rstrip(".,);")
        if not url:
            continue
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


async def _expand_telegram_digest(client: TelegramClient, msg,
                                   target) -> None:
    links = _extract_telegram_links(msg)
    parent_id = msg.id
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


async def _expand_substack_digest(client: TelegramClient, msg,
                                   target) -> None:
    urls = _extract_substack_links(msg)
    parent_id = msg.id
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


# ----------------- Plain-text channel relay -----------------

# Per-channel URL-line strip patterns. Each entry is a list of compiled
# regexes applied as `pattern.sub("", text)` so any matching line is
# removed before relaying. We keep these line-anchored (^...$ with
# MULTILINE) so a URL appearing inside a paragraph isn't stripped —
# only standalone metadata footers like "공시링크: https://...".
_PLAIN_URL_STRIP_PATTERNS: dict[str, list[re.Pattern]] = {
    "finter_gpt": [
        re.compile(
            r"^(공시링크|회사정보|최근계약)\s*:\s*https?://\S+\s*$",
            re.MULTILINE,
        ),
    ],
    "jubung": [
        re.compile(
            r"^주붕이가 읽은 리포트.*\(https?://[^\s)]+\)\s*$",
            re.MULTILINE,
        ),
    ],
    "awake_globalwatch": [
        re.compile(r"^회사정보\s*:\s*https?://\S+\s*$", re.MULTILINE),
        # Trailing hashtag pair like `#대만 #TW_3711` — clutter, not info.
        re.compile(r"^#\S+\s+#\S+_\S+\s*$", re.MULTILINE),
    ],
    "awake_realtimecheck": [
        re.compile(r"^회사정보\s*:\s*https?://\S+\s*$", re.MULTILINE),
        re.compile(r"^https?://www\.awakeplus\.co\.kr/\S+\s*$", re.MULTILINE),
    ],
    "fundeasyearnings": [
        re.compile(
            r"^📎\s*Transcript Link\s*\(https?://[^\s)]+\)\s*$",
            re.MULTILINE,
        ),
    ],
}

# After URL stripping, a body shorter than this is treated as "nothing
# left to learn" and dropped — covers headers like
# "🇺🇸 오늘 종가 기준 미국기업 52주 신고가 리스트입니다." that only
# referenced the now-stripped link.
_PLAIN_MIN_CHARS = 30


def _strip_plain_urls(text: str, channel_key: str) -> str:
    patterns = _PLAIN_URL_STRIP_PATTERNS.get(channel_key, [])
    out = text
    for pat in patterns:
        out = pat.sub("", out)
    # Collapse runs of >= 3 newlines (created by stripped lines) into
    # a single blank line so the relayed body stays readable.
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


async def _relay_plain(client: TelegramClient, msg, target,
                       channel_name: str) -> None:
    text = (msg.message or "").strip()
    cleaned = _strip_plain_urls(text, channel_name.lower())
    if len(cleaned) < _PLAIN_MIN_CHARS:
        print(
            f"  drop plain msg {msg.id} from {channel_name}: "
            f"{len(cleaned)} chars after URL strip"
        )
        return
    try:
        await client.send_message(target, cleaned, link_preview=False)
        print(
            f"  relayed plain msg {msg.id} from {channel_name} "
            f"({len(cleaned)} chars)"
        )
    except FloodWaitError as e:
        print(f"  flood wait {e.seconds}s on {channel_name}/{msg.id}")
        await asyncio.sleep(e.seconds + 1)
    except Exception as e:
        print(
            f"  err relay {channel_name}/{msg.id}: "
            f"{type(e).__name__}: {e}"
        )


# ----------------- Lifecycle -----------------

async def _client_lifecycle(channels: list[str], plain_channels: set[str],
                            target_name: str, api_id: int, api_hash: str,
                            session_path) -> None:
    """One full client connection — start, subscribe to every source,
    run until the socket dies, then return so the outer loop reconnects."""
    client = TelegramClient(
        str(session_path), api_id, api_hash,
        connection_retries=TELETHON_CONN_RETRIES,
        retry_delay=2,
        auto_reconnect=True,
        request_retries=10,
    )
    try:
        await client.start()

        # Resolve every source channel + the target. A single failure
        # (e.g. user not a member of one private channel) drops that
        # source but keeps the rest running.
        resolved: dict[int, str] = {}  # chat_id → channel name
        source_entities = []
        for ch in channels:
            try:
                src = await _resolve_channel(client, ch)
            except Exception as e:
                print(
                    f"resolve failed for {ch}: {type(e).__name__}: {e} — "
                    "skipping this source"
                )
                continue
            source_entities.append(src)
            # Telethon entity `id` is the raw positive integer, but
            # `event.chat_id` returns the marked form (-100<id> for
            # channels). Store both so handler lookup matches either.
            from telethon.utils import get_peer_id
            resolved[src.id] = ch
            resolved[get_peer_id(src)] = ch
            mode = "plain" if ch.lower() in plain_channels else "digest"
            print(f"listening [{mode}]: {getattr(src, 'title', ch)} ({ch})")
        if not source_entities:
            print(
                f"no source channels resolved — sleeping {RECONNECT_DELAY_SEC}s"
            )
            return

        try:
            target = await _resolve_channel(client, target_name)
        except Exception as e:
            print(
                f"resolve target failed: {type(e).__name__}: {e} — "
                f"sleeping {RECONNECT_DELAY_SEC}s before retry"
            )
            return
        print(
            f"forwarding to: {getattr(target, 'title', None) or target_name}"
        )
        print(
            "digest mode: 📋 Telegram → expand · 📰 Substack → expand · "
            "🐦 X → drop · other → drop"
        )
        if plain_channels:
            print(
                f"plain-relay mode: {sorted(plain_channels)} "
                "(text only, URL lines stripped, attachments dropped)"
            )
        print("running... Ctrl+C to stop\n")

        @client.on(events.NewMessage(chats=source_entities))
        async def handler(event):
            msg = event.message
            text = (msg.message or "").strip()

            # Identify which configured source this came from. Falls
            # back to the raw chat id if the channel isn't in our map
            # (shouldn't happen, but defensive).
            channel_name = resolved.get(event.chat_id, str(event.chat_id))

            # Plain-relay path: ignore digest detection, just strip
            # known URL footers and re-send as text.
            if channel_name.lower() in plain_channels:
                await _relay_plain(client, msg, target, channel_name)
                return

            # X digest: drop. X API access is paid/restricted and the
            # LLM summary in the digest body alone isn't worth the cost
            # of ingesting at scale.
            if _DIGEST_X_RE.search(text):
                print(f"  skip X digest {msg.id} from {channel_name}")
                return

            # Telegram digest: expand each 원문 link into its original
            # message and forward those instead of the digest body.
            if _DIGEST_TG_RE.search(text):
                await _expand_telegram_digest(client, msg, target)
                return

            # Substack digest: relay each article URL as a plain text
            # message so the bot's URL pipeline crawls the full article.
            if _DIGEST_SUBSTACK_RE.search(text):
                await _expand_substack_digest(client, msg, target)
                return

            # Anything else (chat, screenshots, ad-hoc forwards) is
            # dropped. Earlier behaviour was to mirror everything, but
            # that summarised ~₩2-5k/day of noise. Only digest content
            # makes it through now — see module docstring.
            print(f"  drop non-digest msg {msg.id} from {channel_name}")

        await client.run_until_disconnected()
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def run(channels: list[str], plain_channels: set[str]) -> None:
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
                channels, plain_channels, target_name,
                api_id, api_hash, session_path,
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
    # Multi-channel takes precedence; fall back to legacy single-channel
    # env / CLI arg so old deployments keep working without env changes.
    channels = _parse_channel_list("LISTEN_CHANNELS")
    if not channels:
        legacy = os.getenv("LISTEN_CHANNEL", "").strip()
        if legacy:
            channels = _parse_channel_list("LISTEN_CHANNEL") or [legacy]
    if not channels and len(sys.argv) >= 2:
        cli = sys.argv[1].strip().lstrip("@")
        if (cli.startswith("https://t.me/") or cli.startswith("t.me/")) \
                and "+" not in cli:
            cli = cli.split("t.me/", 1)[1].rstrip("/")
        channels = [cli]
    if not channels:
        sys.exit(
            "No channels provided. Set LISTEN_CHANNELS=ch1,ch2,... in "
            ".env, or pass a single channel as CLI arg.\n"
            "  Optional: LISTEN_PLAIN_CHANNELS=finter_gpt,jubung,...\n"
            "  for channels whose body should be relayed verbatim (URL "
            "footers stripped, no digest expansion)."
        )

    plain_channels = {c.lower() for c in _parse_channel_list("LISTEN_PLAIN_CHANNELS")}
    asyncio.run(run(channels, plain_channels))


if __name__ == "__main__":
    main()
