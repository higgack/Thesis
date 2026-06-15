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
   Continuation messages (Noah splits long digests across multiple
   Telegram messages because of the 4096-char limit; only the first
   carries the header) are detected via a short time window and run
   through the same expander as their parent. Everything else (X
   timeline digests, daily ranking lists, chat, screenshots, ad-hoc
   forwards) is dropped — only the two formats above produce useful
   ingest.

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
from datetime import datetime

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


# Hard denylist — channels deprecated by user decision. Filtered out
# regardless of .env contents so a stale LISTEN_CHANNELS entry can't
# resurrect a removed source. Push-only, no shell needed for the user
# to drop a channel: add the lowercased name here and the next listener
# restart ignores it.
_CHANNEL_DENYLIST: set[str] = {
    "finter_gpt",     # 머니터링 공시 — 사용자가 daju_dart 로 교체
    "jubung",         # 주붕이 리포트 — 제거
    "awake_globalwatch",   # 글로벌 Watch — 제거
    "awake_realtimecheck", # 52주 신고가 — 제거
    "darthacking",    # 옛 다트해킹 — finter 로 잠시 교체 후 daju_dart 로 통일
    "daju_017_bot",   # 옛 봇 DM — daju_dart 채널로 교체
    "daju_dart",      # DAJU 기업공시 — 사용자 결정으로 제거 (더 이상 수신 안 함)
}


def _parse_channel_list(env_key: str) -> list[str]:
    """Comma-separated env var → cleaned channel name list. Strips
    surrounding whitespace, `@`, and any `https://t.me/` prefix so
    users can paste channel URLs directly. Entries in
    _CHANNEL_DENYLIST get silently dropped so deprecated channels can't
    leak back in via a stale .env."""
    raw = os.getenv(env_key, "")
    out: list[str] = []
    for part in raw.split(","):
        ch = part.strip().lstrip("@")
        if not ch:
            continue
        if (ch.startswith("https://t.me/") or ch.startswith("t.me/")) and "+" not in ch:
            ch = ch.split("t.me/", 1)[1].rstrip("/")
        if ch.lower() in _CHANNEL_DENYLIST:
            continue
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
    # Bot-API numeric form for channels/supergroups: -100<raw_id>.
    # Telethon's get_entity(str) can't parse this style — wrap it as
    # PeerChannel so a private channel referenced by its chat_id
    # (recommended for privacy-flip / username-rename immunity) still
    # resolves. Without this, a FORWARD_TARGET like "-1003515899076"
    # errors with "Cannot find any entity corresponding to ..." and the
    # forward loop spins forever.
    if channel.startswith("-100") and channel[4:].isdigit():
        from telethon.tl.types import PeerChannel
        return await client.get_entity(PeerChannel(int(channel[4:])))
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

# How many times _relay_plain retries on FloodWaitError before giving
# up on a single message. Each retry waits the full Telegram-mandated
# duration first; 3 attempts is enough to clear typical 60–200s waits
# without leaving the listener stuck on one message for >10 minutes.
_RELAY_MAX_ATTEMPTS = 3

# Persistent "already forwarded" set so the listener doesn't relay
# the same (channel, msg_id) twice. Two real sources of duplicates:
# (a) a single Noah digest sometimes cites the same source message
#     in multiple sections — we'd otherwise forward it once per cite,
# (b) BACKFILL replay after a restart re-walks the same 12h of
#     messages, re-firing every relay.
# Bot-side source-label dedup catches the ingest cost, but the user
# still sees the duplicate Telegram message in the RAG channel
# because the listener already pushed it. Tracking here skips the
# send entirely. Capped at _SEEN_MAX entries; older entries dropped
# first when the cap is hit.
import json as _json
_SEEN_PATH = config.DATA_DIR / "forward_listener_seen.json"
_SEEN_MAX = 10000
_seen_forwards: dict[str, str] = {}


def _seen_load() -> None:
    global _seen_forwards
    if not _SEEN_PATH.exists():
        return
    try:
        data = _json.loads(_SEEN_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _seen_forwards = data
            print(f"loaded {len(_seen_forwards)} seen-forward entries")
    except Exception as e:
        print(f"seen-set load failed: {type(e).__name__}: {e}")
        bak = _SEEN_PATH.with_suffix(_SEEN_PATH.suffix + ".bak")
        if bak.exists():
            try:
                _seen_forwards = _json.loads(bak.read_text(encoding="utf-8"))
            except Exception:
                pass


def _seen_persist() -> None:
    try:
        tmp = _SEEN_PATH.with_suffix(_SEEN_PATH.suffix + ".tmp")
        bak = _SEEN_PATH.with_suffix(_SEEN_PATH.suffix + ".bak")
        tmp.write_text(
            _json.dumps(_seen_forwards, ensure_ascii=False),
            encoding="utf-8",
        )
        if _SEEN_PATH.exists():
            try:
                os.replace(_SEEN_PATH, bak)
            except OSError:
                pass
        os.replace(tmp, _SEEN_PATH)
    except Exception as e:
        print(f"seen-set persist failed: {type(e).__name__}: {e}")


def _seen_check(key: str) -> bool:
    return key in _seen_forwards


def _seen_mark(key: str) -> None:
    _seen_forwards[key] = datetime.utcnow().isoformat(timespec="seconds")
    if len(_seen_forwards) > _SEEN_MAX:
        # Drop the oldest 10% of entries to amortize the trim cost.
        ordered = sorted(_seen_forwards.items(), key=lambda kv: kv[1])
        drop_n = max(1, _SEEN_MAX // 10)
        for k, _ in ordered[:drop_n]:
            _seen_forwards.pop(k, None)
    _seen_persist()


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
    # Hidden URLs from text-link entities ([label](url) markdown form),
    # plus auto-detected URLs whose visible text is the URL itself.
    # Use Telethon's get_entities_text() so the visible-text slice
    # respects UTF-16 offsets (a single 📰 in the body shifts every
    # later entity by one Python char if we slice msg.message directly).
    try:
        ent_pairs = msg.get_entities_text()
    except Exception:
        ent_pairs = []
    for ent, label in ent_pairs:
        if isinstance(ent, MessageEntityTextUrl):
            urls.append((getattr(ent, "url", "") or "").strip().rstrip(".,);"))
        elif isinstance(ent, MessageEntityUrl):
            # Auto-detected URL — label IS the URL.
            urls.append((label or "").strip().rstrip(".,);"))
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


def _extract_substack_links(msg) -> list[tuple[str, str | None]]:
    """Return (url, label) pairs from the digest body — label is the
    visible text of a [label](url) text_link entity (Noah's Substack
    digests usually mark each article up that way so the label IS the
    article title), or None for plain URLs. _expand_substack_digest
    uses the label to prepend a 📌 {title} hint line to each relayed
    URL so the target chat shows what each link is about at a glance.

    Label extraction uses Telethon's get_entities_text() helper, which
    resolves entity offsets against the message text in UTF-16 code
    units (Telegram's native convention). Slicing the Python string
    directly with ent.offset/length is wrong: Python indices are code
    points but Telegram offsets are UTF-16 units, so a single 📰
    emoji (surrogate pair = 2 units, 1 code point) shifts every later
    label by one character. That mismatch was what produced garbage
    prefixes like '3.', '4. In', 'MTB M' on relays."""
    from telethon.tl.types import MessageEntityTextUrl
    seen: set[str] = set()
    out: list[tuple[str, str | None]] = []
    # 1) text_link entities first — label is the visible markup text.
    try:
        ent_pairs = msg.get_entities_text(MessageEntityTextUrl)
    except Exception:
        ent_pairs = []
    for ent, label in ent_pairs:
        u = (getattr(ent, "url", "") or "").strip().rstrip(".,);")
        if not u or "t.me/" in u or u in seen:
            continue
        label = (label or "").strip()
        # Skip a label that's literally the URL again ([url](url)) —
        # no information gain over a bare relay.
        if label.startswith(("http://", "https://")):
            label = ""
        seen.add(u)
        out.append((u, label or None))
    # 2) Plain URLs typed into the body — no label.
    for raw in _all_message_urls(msg):
        u = (raw or "").strip().rstrip(".,);")
        if not u or "t.me/" in u or u in seen:
            continue
        seen.add(u)
        out.append((u, None))
    return out


async def _fetch_url_title(url: str) -> str | None:
    """Best-effort og:title (or <title>) fetch — used as a fallback
    prefix when a Substack digest URL has no markdown label, so plain
    URLs in the relay chat still carry context. Capped 5s, single
    request, no retries; any failure (timeout, 403, DNS, etc.) returns
    None and the caller falls back to a bare URL relay. Only the first
    64 KB of the response is scanned — page titles always sit in
    <head>, so this avoids dragging multi-MB pages through the listener
    for nothing. The 70-char cap matches the label cap so the prefix
    line never trips the bot's 80-char text-ingest threshold."""
    import httpx
    import re as _re
    try:
        async with httpx.AsyncClient(
            timeout=5.0, follow_redirects=True,
            headers={"User-Agent":
                     "Mozilla/5.0 (compatible; SecondBrain-listener/1.0)"},
        ) as c:
            r = await c.get(url)
            r.raise_for_status()
    except Exception as e:
        print(f"    title-fetch err {url[:60]}: {type(e).__name__}: {e}")
        return None
    body = r.text[:65536]
    # og:title — content attr can appear before or after the property
    # attr, so try both orderings before falling back to <title>.
    m = _re.search(
        r'<meta[^>]+(?:property|name)=["\']og:title["\'][^>]+'
        r'content=["\']([^"\']{1,200})["\']',
        body, _re.IGNORECASE)
    if not m:
        m = _re.search(
            r'<meta[^>]+content=["\']([^"\']{1,200})["\'][^>]+'
            r'(?:property|name)=["\']og:title["\']',
            body, _re.IGNORECASE)
    if not m:
        m = _re.search(r"<title[^>]*>([^<]{1,200})</title>",
                       body, _re.IGNORECASE)
    if not m:
        return None
    title = (m.group(1).strip()
             .replace("&amp;", "&").replace("&#39;", "'")
             .replace("&quot;", '"').replace("&lt;", "<")
             .replace("&gt;", ">").replace("&nbsp;", " "))
    return title[:70] if title else None


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
        # Seen-set guard: same (ch, mid) already forwarded → skip.
        # Catches both same-digest duplicate cites and digest replays
        # after a listener restart with BACKFILL.
        seen_key = f"{ch.lower()}/{mid}"
        if _seen_check(seen_key):
            print(f"    skip {ch}/{mid}: already forwarded (seen)")
            continue
        peer = await _resolve_tg_channel(client, ch)
        if peer is None:
            continue
        try:
            orig = await client.get_messages(peer, ids=mid)
        except Exception as e:
            print(f"    err get_messages {ch}/{mid}: {type(e).__name__}: {e}")
            continue
        if not orig:
            print(f"    skip {ch}/{mid}: message not found")
            continue
        # Apply drop patterns BEFORE forwarding so noise formats
        # (e.g. fundeasyearnings 알파 스캐너) don't reach the bot
        # via digest citation. Without this the plain-relay drop
        # only catches the live posting; Noah's digest cite of
        # the same message slipped through.
        orig_text = (orig.text or getattr(orig, "message", "") or "")
        drop_patterns = _PLAIN_DROP_PATTERNS.get(ch.lower(), [])
        if any(p.search(orig_text) for p in drop_patterns):
            print(f"    drop {ch}/{mid}: matches drop pattern")
            continue
        # Retry forward_to on FloodWait so digest-cited messages
        # don't silently disappear during high-volume bursts.
        delivered = False
        last_err: str | None = None
        for attempt in range(_RELAY_MAX_ATTEMPTS):
            try:
                await orig.forward_to(target)
                forwarded += 1
                print(
                    f"    fwd {ch}/{mid} ({forwarded}/{len(links)})"
                    + (f" [retry {attempt}]" if attempt else "")
                )
                delivered = True
                _seen_mark(seen_key)
                break
            except FloodWaitError as e:
                wait = max(e.seconds + 1, 1)
                print(
                    f"    flood wait {wait}s on {ch}/{mid} "
                    f"(attempt {attempt + 1}/{_RELAY_MAX_ATTEMPTS})"
                )
                await asyncio.sleep(wait)
                last_err = f"FloodWaitError({e.seconds}s)"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                print(f"    err {ch}/{mid}: {last_err}")
                break
        if not delivered and last_err:
            print(
                f"    ✗ DROP {ch}/{mid} after "
                f"{_RELAY_MAX_ATTEMPTS} attempts (last: {last_err})"
            )
        await asyncio.sleep(_THROTTLE_SEC)
    print(f"  TG digest msg {parent_id}: done, forwarded {forwarded}/{len(links)}")


async def _expand_substack_digest(client: TelegramClient, msg,
                                   target) -> None:
    pairs = _extract_substack_links(msg)
    parent_id = msg.id
    print(f"  Substack digest msg {parent_id}: {len(pairs)} URLs to relay")
    sent = 0
    for url, label in pairs:
        seen_key = f"substack:{url}"
        if _seen_check(seen_key):
            print(f"    skip {url}: already sent (seen)")
            continue
        # Markdown label preferred; if Noah's digest emitted only plain
        # URLs (no [Article](url) markup), fall back to fetching og:title
        # from the page so the relay row still carries context. Best-
        # effort: returns None on any failure → bare URL.
        if not label:
            label = await _fetch_url_title(url)
        # 70-char cap on the label keeps the relayed text body below
        # the bot's 80-char text-ingest threshold, so the prefix never
        # gets saved as a separate text doc — same constraint as
        # _relay_url_only_post.
        payload = f"📌 {label[:70]}\n{url}" if label else url
        delivered = False
        last_err: str | None = None
        for attempt in range(_RELAY_MAX_ATTEMPTS):
            try:
                await client.send_message(target, payload, link_preview=False)
                sent += 1
                print(
                    f"    sent {url} ({sent}/{len(pairs)})"
                    + (f" [retry {attempt}]" if attempt else "")
                )
                delivered = True
                _seen_mark(seen_key)
                break
            except FloodWaitError as e:
                wait = max(e.seconds + 1, 1)
                print(
                    f"    flood wait {wait}s on {url} "
                    f"(attempt {attempt + 1}/{_RELAY_MAX_ATTEMPTS})"
                )
                await asyncio.sleep(wait)
                last_err = f"FloodWaitError({e.seconds}s)"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                print(f"    err {url}: {last_err}")
                break  # non-flood: skip the rest of this URL's retries
        if not delivered:
            print(
                f"    ✗ DROP {url} after {_RELAY_MAX_ATTEMPTS} attempts"
                + (f" (last: {last_err})" if last_err else "")
            )
        await asyncio.sleep(_THROTTLE_SEC)
    print(f"  Substack digest msg {parent_id}: done, relayed {sent}/{len(pairs)}")


# Title-filter channels: forward each message ONLY when its body
# matches the registered pattern; everything else is dropped silently.
# Used for high-volume channels where a small fraction of posts carries
# the format the user actually wants to learn (e.g. t.me/insidertracking
# posts other content alongside the "미국 레딧 게시물 분석" report we
# care about). Match is re.search over msg.message, so the pattern can
# anchor on the post header line. Forward is Telethon forward_to so the
# original message arrives in the target chat with text + attachments
# intact, and the bot's on_channel_post ingests it through the normal
# pipeline (text body / URLs / photo OCR / etc.).
_TITLE_FILTER_CHANNELS: dict[str, "re.Pattern[str]"] = {
    "insidertracking": re.compile(r"미국\s*레딧\s*게시물\s*분석"),
}


async def _forward_full_message(client: TelegramClient, msg, target,
                                channel_name: str) -> None:
    """Forward one matched message via Telethon, preserving attachments
    and original-message context. Mirrors the single-link branch of
    _expand_telegram_digest minus the per-message seen-set bookkeeping
    (those used a cross-channel seen key — here we use a per-channel
    one so the same source-channel id can't double-fire across listener
    restarts/backfills)."""
    seen_key = f"titlefilter:{channel_name.lower()}/{msg.id}"
    if _seen_check(seen_key):
        print(f"  skip {channel_name}/{msg.id}: already forwarded (seen)")
        return
    last_err: str | None = None
    for attempt in range(_RELAY_MAX_ATTEMPTS):
        try:
            await msg.forward_to(target)
            _seen_mark(seen_key)
            print(f"  title-filter fwd {channel_name}/{msg.id}"
                  + (f" [retry {attempt}]" if attempt else ""))
            return
        except FloodWaitError as e:
            wait = max(e.seconds + 1, 1)
            print(f"    flood wait {wait}s on {channel_name}/{msg.id} "
                  f"(attempt {attempt + 1}/{_RELAY_MAX_ATTEMPTS})")
            await asyncio.sleep(wait)
            last_err = f"FloodWaitError({e.seconds}s)"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"    err fwd {channel_name}/{msg.id}: {last_err}")
            return
    print(f"    ✗ DROP {channel_name}/{msg.id} after "
          f"{_RELAY_MAX_ATTEMPTS} attempts"
          + (f" (last: {last_err})" if last_err else ""))


# ----------------- Plain-text channel relay -----------------

# Per-channel drop patterns. If a message body matches any of the
# regexes in this list for the source channel, it gets dropped
# entirely instead of relayed. Used for noisy auto-generated content
# (ticker dumps, alert spam) that has no analytical value worth
# embedding.
_PLAIN_DROP_PATTERNS: dict[str, list[re.Pattern]] = {
    "fundeasyearnings": [
        # 🚨 알파 스캐너 시리즈 — Alpha consensus / Alpha options /
        # Alpha scanner. Pure ticker tables with probability scores,
        # no narrative. Each post is ~30 tickers × repeating fields.
        # Drop on the '알파 스캐너' header (covers every subtype).
        re.compile(r"알파\s*스캐너", re.UNICODE),
    ],
}


# Per-channel URL-line strip patterns. Each entry is a list of compiled
# regexes applied as `pattern.sub("", text)` so any matching line is
# removed before relaying. We keep these line-anchored (^...$ with
# MULTILINE) so a URL appearing inside a paragraph isn't stripped —
# only standalone metadata footers like "공시링크: https://...".
_PLAIN_URL_STRIP_PATTERNS: dict[str, list[re.Pattern]] = {
    "finter_gpt": [
        # 머니터링 공시 알림 포맷은 라벨 한 줄 + URL 한 줄 구조라
        # `label: url` 한 줄 패턴으로는 안 잡힘. URL 라인만 직접 제거.
        re.compile(r"^https?://www\.moneytoring\.ai/\S+\s*$", re.MULTILINE),
        re.compile(r"^https?://dart\.fss\.or\.kr/\S+\s*$", re.MULTILINE),
        # 라벨만 남은 외톨이 줄도 제거 (URL 없으면 의미 없음)
        re.compile(r"^머니터링 공시 알림\s*$", re.MULTILINE),
        re.compile(r"^공시링크\s*$", re.MULTILINE),
    ],
    "daju_dart": [
        # 다주 DART 공시 채널 — 보통 본문 끝에 DART 원문 URL 라인 포함.
        # Pattern 보면서 더 추가 필요 시 sed 한 줄로 가능.
        re.compile(r"^https?://dart\.fss\.or\.kr/\S+\s*$", re.MULTILINE),
        re.compile(r"^https?://m\.dart\.fss\.or\.kr/\S+\s*$", re.MULTILINE),
        re.compile(r"^원문\s*보기\s*$", re.MULTILINE),
        re.compile(r"^공시링크\s*:?\s*$", re.MULTILINE),
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


# PLAIN channels that want FULL ingestion (text + URLs intact +
# attachments forwarded as-is). Defaulting PLAIN channels strip URLs
# and drop attachments because their feeds are high-volume noise
# (DART filings, alpha-scanner ticker tables). These two are user-
# curated research feeds where every PDF / Word / PPT / image / audio
# carries signal the bot should learn. Add a channel handle (lower
# case) here to switch it from "text-only" to "full ingestion".
# PLAIN channels whose posts are forwarded message-for-message (text +
# URLs + attachments) instead of stripped to text-only. Used for
# high-signal research feeds where the body itself is worth embedding.
_PLAIN_FULL_INGEST = {"aicorporateanalysisdeepdive"}

# URL-only channels: extract EVERY URL from each post and route each by
# domain — the post body and attachments are ignored. Used for AI-
# curation / link-blog channels where only the linked source is worth
# ingesting (the surrounding 📝 본문 요약 / ✨(In)sight commentary or
# the blogger's narration would just duplicate the source in search).
#   getfeed   = '석학들의 마켓 (인)사이트' (AI-summary curation)
#   benineb9  = '비나인의 블로그 스크랩' (link-blog narration)
# Per-URL routing inside _relay_url_only_post: blog.naver/youtube →
# plain URL relay, t.me → Telethon forward of the original message,
# anything else → batched owner notice.
_URL_ONLY_CHANNELS = {"getfeed", "benineb9"}


# Domain whitelist for the URL-only channels above. Anything outside
# this list gets a batched owner notice so the user can decide manually
# whether to paste it into the bot.
#   "tg"   → forward the original Telegram message via Telethon (t.me
#            isn't an HTTP-fetchable page so plain URL relay fails)
#   "blog" / "youtube" → relay as plain URL; bot's existing pipeline
#            handles them well (Naver PostView rewrite for blog,
#            transcript_api + nightly yt-dlp + Deno for YouTube).
# Users can request additions when an unsupported-domain notice fires.
def _classify_original_url(url: str) -> str | None:
    from urllib.parse import urlparse
    h = (urlparse(url).hostname or "").lower()
    if h in ("t.me",):
        return "tg"
    if h in ("blog.naver.com", "m.blog.naver.com"):
        return "blog"
    if h in ("youtube.com", "www.youtube.com",
             "m.youtube.com", "youtu.be"):
        return "youtube"
    return None


def _post_title_hint(msg) -> str:
    """First substantive body line of a URL-only-channel post, used as
    a context prefix on the relayed URL so the reader of the target
    chat can tell what each link is about at a glance (the bot's ✅
    ingest reply only arrives a moment later, by which point a bare
    URL has already scrolled past without context). Skips structural
    markers (📝 본문 요약, ✨(In)sight, leading hashtags, 📜/📀 marker
    icons) so the prefix lands on the actual title line, not on a
    section header. Capped at 70 chars so the prefix never trips the
    bot's 80-char `len(plain) >= 80` text-ingest threshold."""
    text = (msg.message or "").strip()
    if not text:
        return ""
    skip_prefixes = ("📝", "✨", "#", "📜", "📀")
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line.startswith(skip_prefixes):
            continue
        return line[:70]
    return ""


async def _forward_tg_message(client: TelegramClient, target,
                              url: str) -> None:
    """Forward one t.me/<ch>/<msg_id> via Telethon (mirrors the inner
    loop of _expand_telegram_digest for a single-link case). Skips
    silently on seen/resolve/fetch failure; logs each outcome."""
    m = _TG_URL_RE.search(url)
    if not m:
        print(f"    drop: malformed t.me URL: {url}")
        return
    ch, mid = m.group(1), int(m.group(2))
    seen_key = f"{ch.lower()}/{mid}"
    if _seen_check(seen_key):
        print(f"    skip t.me/{ch}/{mid}: already forwarded (seen)")
        return
    peer = await _resolve_tg_channel(client, ch)
    if peer is None:
        return
    try:
        orig = await client.get_messages(peer, ids=mid)
    except Exception as e:
        print(f"    err get_messages {ch}/{mid}: {type(e).__name__}: {e}")
        return
    if not orig:
        print(f"    skip {ch}/{mid}: message not found")
        return
    last_err: str | None = None
    for attempt in range(_RELAY_MAX_ATTEMPTS):
        try:
            await orig.forward_to(target)
            _seen_mark(seen_key)
            print(f"    원문 fwd t.me/{ch}/{mid}"
                  + (f" [retry {attempt}]" if attempt else ""))
            return
        except FloodWaitError as e:
            wait = max(e.seconds + 1, 1)
            print(f"    flood wait {wait}s on {ch}/{mid} "
                  f"(attempt {attempt + 1}/{_RELAY_MAX_ATTEMPTS})")
            await asyncio.sleep(wait)
            last_err = f"FloodWaitError({e.seconds}s)"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"    err fwd {ch}/{mid}: {last_err}")
            return
    print(f"    ✗ DROP {ch}/{mid} after {_RELAY_MAX_ATTEMPTS} attempts"
          + (f" (last: {last_err})" if last_err else ""))


async def _notify_unsupported_urls(client: TelegramClient, target,
                                   channel_name: str,
                                   urls: list[str]) -> None:
    """Send ONE owner notice for every unsupported-domain URL in a
    single post (vs N pings for N urls — a benineb9 link-blog post can
    carry several). seen-set dedupes across listener restarts so the
    same URL isn't re-notified on backfill or repeated forwards."""
    new_urls: list[str] = []
    for u in urls:
        if not _seen_check(f"origlink-notify:{u}"):
            new_urls.append(u)
            _seen_mark(f"origlink-notify:{u}")
    if not new_urls:
        return
    from urllib.parse import urlparse
    if len(new_urls) == 1:
        url = new_urls[0]
        host = (urlparse(url).hostname or "?").lower()
        notice = (
            f"⚠️ {channel_name}: 자동 ingest 미지원 도메인 ({host})\n"
            f"원문: {url}\n"
            f"학습하려면 위 URL을 봇에 그대로 보내세요. "
            f"(도메인을 화이트리스트에 추가하려면 알려주세요.)"
        )
    else:
        hosts = sorted({(urlparse(u).hostname or "?").lower()
                        for u in new_urls})
        body = "\n".join(f"  • {u}" for u in new_urls)
        notice = (
            f"⚠️ {channel_name}: 자동 ingest 미지원 도메인 "
            f"{len(new_urls)}건 ({', '.join(hosts)})\n"
            f"{body}\n\n"
            f"학습하려면 위 URL들을 봇에 보내세요. "
            f"(도메인을 화이트리스트에 추가하려면 알려주세요.)"
        )
    try:
        await client.send_message(target, notice, link_preview=False)
        print(f"  notify (unsupported {len(new_urls)} urls): {channel_name}")
    except Exception as e:
        print(f"    notify err: {type(e).__name__}: {e}")


async def _relay_url_only_post(client: TelegramClient, msg, target,
                               channel_name: str) -> None:
    """Extract every URL from the post and route each by domain:
      • t.me        → Telethon forward of the original message
      • blog/youtube → plain URL relay (bot's URL pipeline crawls)
      • other       → batched owner notice (manual decision)
    The post body / attachments are NEVER forwarded — so AI-summary
    commentary (getfeed) and link-blog narration (benineb9) stay out
    of the corpus, and only the real source content is ingested."""
    raw_urls = _all_message_urls(msg)
    # Dedup preserving order; trim trailing punctuation glued onto URLs.
    seen: set[str] = set()
    urls: list[str] = []
    for u in raw_urls:
        u = (u or "").strip().rstrip(".,);")
        if not u or u in seen:
            continue
        seen.add(u)
        urls.append(u)
    if not urls:
        print(f"  drop {channel_name} msg {msg.id}: no urls")
        return

    # Per-post title hint, prepended to every blog/youtube relay so the
    # reader of the target chat sees what each link is BEFORE the bot's
    # ✅ ingest reply lands. t.me uses Telethon forward (full original
    # already comes through) and unsupported uses a batched notice, so
    # neither needs the prefix.
    hint = _post_title_hint(msg)
    # Reject hints that are themselves URLs — happens on link-scrap
    # channels (benineb9) whose posts are sometimes a bare URL with
    # no surrounding text. Echoing the URL as the prefix adds zero
    # information; clear it so the og:title fallback kicks in below.
    if hint and (hint.startswith("http://") or hint.startswith("https://")):
        hint = ""

    unsupported: list[str] = []
    routed = 0
    for url in urls:
        kind = _classify_original_url(url)
        if kind == "tg":
            await _forward_tg_message(client, target, url)
            routed += 1
        elif kind in ("blog", "youtube"):
            seen_key = f"origlink:{url}"
            if _seen_check(seen_key):
                print(f"  skip {url}: already sent (seen)")
                continue
            # Per-URL og:title fallback when the post body gave us
            # nothing usable. Same best-effort path Substack expand
            # uses (5s timeout, single GET, 64 KB scan, None on
            # failure → bare URL).
            title = hint or await _fetch_url_title(url)
            payload = f"📌 {title[:70]}\n{url}" if title else url
            delivered = False
            last_err: str | None = None
            for attempt in range(_RELAY_MAX_ATTEMPTS):
                try:
                    await client.send_message(target, payload, link_preview=False)
                    print(f"  원문 relay sent {url} [{kind}]"
                          + (f" [retry {attempt}]" if attempt else ""))
                    _seen_mark(seen_key)
                    delivered = True
                    routed += 1
                    break
                except FloodWaitError as e:
                    wait = max(e.seconds + 1, 1)
                    print(f"    flood wait {wait}s on {url} "
                          f"(attempt {attempt + 1}/{_RELAY_MAX_ATTEMPTS})")
                    await asyncio.sleep(wait)
                    last_err = f"FloodWaitError({e.seconds}s)"
                except Exception as e:
                    last_err = f"{type(e).__name__}: {e}"
                    print(f"    err {url}: {last_err}")
                    break
            if not delivered:
                print(f"    ✗ DROP {url} after {_RELAY_MAX_ATTEMPTS} attempts"
                      + (f" (last: {last_err})" if last_err else ""))
        else:
            unsupported.append(url)
        await asyncio.sleep(_THROTTLE_SEC)

    if unsupported:
        await _notify_unsupported_urls(client, target, channel_name, unsupported)

    print(f"  {channel_name} msg {msg.id}: routed {routed}, "
          f"unsupported {len(unsupported)}")


async def _relay_plain(client: TelegramClient, msg, target,
                       channel_name: str) -> None:
    # Seen-set guard — skip BACKFILL replays of messages we've
    # already relayed (and dedup live duplicates if Telethon ever
    # surfaces the same NewMessage event twice across reconnects).
    seen_key = f"{channel_name.lower()}/{msg.id}"
    if _seen_check(seen_key):
        print(
            f"  skip plain msg {msg.id} from {channel_name}: "
            f"already relayed (seen)"
        )
        return
    text = (msg.message or "").strip()
    # Per-channel drop filter — noisy auto-generated content (ticker
    # tables, alpha scanner dumps) that has no narrative value.
    # Matches against the raw body BEFORE URL stripping / forwarding
    # so the header pattern stays visible on both paths.
    drop_patterns = _PLAIN_DROP_PATTERNS.get(channel_name.lower(), [])
    for pat in drop_patterns:
        if pat.search(text):
            print(
                f"  drop plain msg {msg.id} from {channel_name}: "
                f"matched drop pattern {pat.pattern!r}"
            )
            return

    full_ingest = channel_name.lower() in _PLAIN_FULL_INGEST

    # FULL-ingest path (per user policy for research feeds):
    # forward the message as-is — bot's on_channel_post →
    # _ingest_message catches msg.document / photo / audio / voice /
    # video and runs them through the normal ingest pipeline, and
    # URLs in the text body get extracted + ingested by
    # _ingest_message_locked's URL loop.
    if full_ingest:
        has_media = bool(
            getattr(msg, "document", None)
            or getattr(msg, "photo", None)
            or getattr(msg, "audio", None)
            or getattr(msg, "voice", None)
            or getattr(msg, "video", None)
        )
        if has_media:
            last_err: str | None = None
            for attempt in range(_RELAY_MAX_ATTEMPTS):
                try:
                    await client.forward_messages(target, msg)
                    print(
                        f"  forwarded full plain msg {msg.id} from "
                        f"{channel_name} (media attached)"
                        + (f" [retry {attempt}]" if attempt else "")
                    )
                    _seen_mark(seen_key)
                    return
                except FloodWaitError as e:
                    wait = max(e.seconds + 1, 1)
                    print(
                        f"  flood wait {wait}s on {channel_name}/{msg.id} "
                        f"(media, attempt {attempt + 1}/{_RELAY_MAX_ATTEMPTS})"
                    )
                    await asyncio.sleep(wait)
                    last_err = f"FloodWaitError({e.seconds}s)"
                except Exception as e:
                    last_err = f"{type(e).__name__}: {e}"
                    print(
                        f"  err forward media {channel_name}/{msg.id}: "
                        f"{last_err}"
                    )
                    return
            print(
                f"  ✗ DROP media {channel_name}/{msg.id} after "
                f"{_RELAY_MAX_ATTEMPTS} attempts (last: {last_err})"
            )
            return
        # Text-only message on a full-ingest channel — relay with URLs
        # intact (no strip). Bot's URL canonical dedup makes repeated
        # links cheap (₩0 LLM cost on dedup hit).
        outgoing = text
        if len(outgoing) < _PLAIN_MIN_CHARS:
            print(
                f"  drop plain msg {msg.id} from {channel_name}: "
                f"{len(outgoing)} chars below {_PLAIN_MIN_CHARS}"
            )
            return
    else:
        # TEXT-ONLY path (fundeasyearnings): strip the
        # channel-specific URL noise patterns, drop attachments by
        # not forwarding them. Per user policy these feeds are noisy
        # and attachments would inflate cost.
        outgoing = _strip_plain_urls(text, channel_name.lower())
        if len(outgoing) < _PLAIN_MIN_CHARS:
            print(
                f"  drop plain msg {msg.id} from {channel_name}: "
                f"{len(outgoing)} chars after URL strip"
            )
            return

    # Common send loop with FloodWait retry. Before this loop the
    # except branch only slept and returned — the message was
    # permanently lost whenever Telegram throttled (quarter-end DART
    # firehose) and silently caused other channels' messages to
    # disappear too because the listener kept going after one drop.
    last_err: str | None = None
    for attempt in range(_RELAY_MAX_ATTEMPTS):
        try:
            await client.send_message(target, outgoing, link_preview=False)
            mode = "full" if full_ingest else "plain"
            print(
                f"  relayed {mode} msg {msg.id} from {channel_name} "
                f"({len(outgoing)} chars)"
                + (f" [retry {attempt}]" if attempt else "")
            )
            _seen_mark(seen_key)
            return
        except FloodWaitError as e:
            wait = max(e.seconds + 1, 1)
            print(
                f"  flood wait {wait}s on {channel_name}/{msg.id} "
                f"(attempt {attempt + 1}/{_RELAY_MAX_ATTEMPTS})"
            )
            await asyncio.sleep(wait)
            last_err = f"FloodWaitError({e.seconds}s)"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            print(
                f"  err relay {channel_name}/{msg.id}: {last_err}"
            )
            return  # non-flood errors: don't retry (likely persistent)
    print(
        f"  ✗ DROP {channel_name}/{msg.id} after "
        f"{_RELAY_MAX_ATTEMPTS} attempts (last: {last_err})"
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
            "other → drop"
        )
        if _URL_ONLY_CHANNELS:
            print(
                f"url-only mode: {sorted(_URL_ONLY_CHANNELS)} — extract "
                f"every URL from each post and route by domain "
                f"(blog.naver/youtube=URL relay, t.me=Telethon forward, "
                f"other=batched owner notice; body & attachments ignored)"
            )
        if _TITLE_FILTER_CHANNELS:
            print(
                f"title-filter mode: {sorted(_TITLE_FILTER_CHANNELS)} — "
                f"forward the original message (text + attachments intact) "
                f"only when the body matches the registered pattern; drop "
                f"everything else"
            )
        if plain_channels:
            # URL-only channels short-circuit before plain in _dispatch,
            # so subtract them here so the operator log reflects what
            # actually hits the plain branch.
            plain_effective = plain_channels - _URL_ONLY_CHANNELS
            print(
                f"plain-relay mode: {sorted(plain_effective)} — "
                f"text-only (URL strip, attachments dropped) for "
                f"{sorted(plain_effective - _PLAIN_FULL_INGEST)}, "
                f"full ingest (text + URLs + attachments) for "
                f"{sorted(plain_effective & _PLAIN_FULL_INGEST)}"
            )
        print("running... Ctrl+C to stop\n")

        # Track the most recent 📋/📰 digest header per source channel so
        # we can recognise continuation messages. Noah's bot splits
        # digests > 4096 chars into N consecutive Telegram messages; only
        # the first carries the "📋 ... 채널 요약" / "📰 ... Substack
        # 요약" header. Without this window, parts 2..N look like plain
        # non-digest messages and lose every t.me URL inside them.
        # Value: (kind, monotonic_seconds). kind ∈ {"tg", "ss"}.
        last_digest: dict[int, tuple[str, float]] = {}
        DIGEST_CONTINUATION_WINDOW_SEC = 90.0

        async def _dispatch(msg, channel_name: str, chat_id: int) -> None:
            """Single message → digest expand / plain relay / drop. Shared
            between the live NewMessage handler and the startup backfill
            so we never miss messages that landed during a container
            restart (Telethon doesn't replay history on reconnect)."""
            text = (msg.message or "").strip()

            # URL-only path (BEFORE plain so it wins when a channel is
            # listed in both LISTEN_PLAIN_CHANNELS and _URL_ONLY_CHANNELS,
            # e.g. benineb9 during the migration). Extracts every URL
            # and routes each by domain; the body / attachments are
            # ignored so AI-summary / link-blog narration never reach
            # the bot.
            if channel_name.lower() in _URL_ONLY_CHANNELS:
                await _relay_url_only_post(client, msg, target, channel_name)
                return

            # Title-filter path: only forward when the post body matches
            # the channel's registered pattern (e.g. insidertracking →
            # "미국 레딧 게시물 분석"). Everything else from a filtered
            # channel is dropped silently — keeps high-volume channels
            # from flooding the corpus with non-target content.
            tf_pat = _TITLE_FILTER_CHANNELS.get(channel_name.lower())
            if tf_pat is not None:
                if tf_pat.search(text):
                    await _forward_full_message(
                        client, msg, target, channel_name)
                else:
                    print(f"  drop {channel_name} msg {msg.id}: "
                          f"title filter not matched")
                return

            # Plain-relay path: ignore digest detection, just strip
            # known URL footers and re-send as text.
            if channel_name.lower() in plain_channels:
                await _relay_plain(client, msg, target, channel_name)
                return

            now = asyncio.get_event_loop().time()

            # Telegram digest: expand each 원문 link into its original
            # message and forward those instead of the digest body.
            if _DIGEST_TG_RE.search(text):
                last_digest[chat_id] = ("tg", now)
                await _expand_telegram_digest(client, msg, target)
                return

            # Substack digest: relay each article URL as a plain text
            # message so the bot's URL pipeline crawls the full article.
            if _DIGEST_SUBSTACK_RE.search(text):
                last_digest[chat_id] = ("ss", now)
                await _expand_substack_digest(client, msg, target)
                return

            # Split-digest continuation: a header-less message arriving
            # within DIGEST_CONTINUATION_WINDOW_SEC of a digest from the
            # same channel is treated as part 2..N of that digest. We
            # reuse the parent kind's expander so a 📋 split keeps doing
            # t.me forwards and a 📰 split keeps doing URL relays.
            prev = last_digest.get(chat_id)
            if prev and (now - prev[1]) <= DIGEST_CONTINUATION_WINDOW_SEC:
                kind = prev[0]
                if kind == "tg" and _extract_telegram_links(msg):
                    print(f"  digest continuation (TG) {msg.id} from {channel_name}")
                    last_digest[chat_id] = (kind, now)  # extend window
                    await _expand_telegram_digest(client, msg, target)
                    return
                if kind == "ss" and _extract_substack_links(msg):
                    print(f"  digest continuation (SS) {msg.id} from {channel_name}")
                    last_digest[chat_id] = (kind, now)
                    await _expand_substack_digest(client, msg, target)
                    return

            # Anything else (chat, screenshots, ad-hoc forwards, X
            # timeline digests, daily ranking lists) is dropped. Earlier
            # behaviour was to mirror everything, but that summarised
            # ~₩2-5k/day of noise. Only digest content + continuations
            # make it through now — see module docstring.
            print(f"  drop non-digest msg {msg.id} from {channel_name}")

        # Backfill is disabled by default: replay floods the bot with
        # hundreds of dedup hits per restart (mostly already-known
        # forwarded content), and the ✅/♻️ message stream saturates
        # the local telegram-bot-api proxy. Set BACKFILL_MINUTES=N to
        # opt in for a one-time recovery (e.g. after a long outage),
        # then remove the env so the next restart stays clean.
        from datetime import datetime, timedelta, timezone
        backfill_min = int(os.getenv("BACKFILL_MINUTES", "0"))
        if backfill_min <= 0:
            print("backfill disabled (BACKFILL_MINUTES=0) — skipping replay")
        BACKFILL_WINDOW_SEC = backfill_min * 60
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=BACKFILL_WINDOW_SEC)
        if backfill_min > 0:
            print(f"backfilling last {backfill_min} min "
                  f"(since {cutoff.isoformat(timespec='seconds')}) ...")
        from telethon.utils import get_peer_id
        for src in source_entities if backfill_min > 0 else []:
            chat_id = get_peer_id(src)
            channel_name = resolved.get(chat_id, str(chat_id))
            recent: list = []
            try:
                async for m in client.iter_messages(src, limit=100):
                    if m.date < cutoff:
                        break
                    recent.append(m)
            except Exception as e:
                print(f"  backfill iter failed for {channel_name}: "
                      f"{type(e).__name__}: {e}")
                continue
            if not recent:
                continue
            # Process oldest → newest so digest headers come before
            # their continuations and last_digest window logic stays
            # consistent.
            recent.reverse()
            print(f"  backfill {channel_name}: {len(recent)} msgs to replay")
            for msg in recent:
                try:
                    await _dispatch(msg, channel_name, chat_id)
                except Exception as e:
                    print(f"    backfill dispatch err {msg.id} "
                          f"{channel_name}: {type(e).__name__}: {e}")
        print("backfill done — switching to live mode\n")

        @client.on(events.NewMessage(chats=source_entities))
        async def handler(event):
            await _dispatch(event.message,
                            resolved.get(event.chat_id, str(event.chat_id)),
                            event.chat_id)

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

    # Exponential backoff on consecutive failures. A permanent failure
    # mode (invalidated session → interactive-login EOFError in a no-tty
    # container, zero sources resolved, …) used to degrade into a silent
    # 5s tight loop: a fresh TelegramClient + connect every 5s forever
    # (~17k connects/day — account flood-limit risk). A lifecycle that
    # survives >60s counts as healthy and resets the backoff.
    delay = RECONNECT_DELAY_SEC
    max_delay = 300
    while True:
        started = asyncio.get_event_loop().time()
        try:
            await _client_lifecycle(
                channels, plain_channels, target_name,
                api_id, api_hash, session_path,
            )
            print(f"client disconnected — reconnecting in {delay}s...")
        except KeyboardInterrupt:
            print("interrupted")
            raise
        except Exception as e:
            print(
                f"client loop error: {type(e).__name__}: {e} — "
                f"sleeping {delay}s before retry"
            )
        alive_sec = asyncio.get_event_loop().time() - started
        if alive_sec > 60:
            delay = RECONNECT_DELAY_SEC
        else:
            delay = min(delay * 2, max_delay)
        await asyncio.sleep(delay)


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
    _seen_load()
    asyncio.run(run(channels, plain_channels))


if __name__ == "__main__":
    main()
