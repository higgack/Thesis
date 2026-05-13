"""One-shot verification for the forward-listener pipeline.

Pulls the last N messages from every channel listed in
LISTEN_CHANNELS, runs each through the same classifier + strip logic
the live listener uses, and prints what would happen (relay / drop /
expand) plus a per-channel summary. Use this any time a channel
format might have changed, or to confirm patterns are still catching
what they should.

Read-only: never sends anything to FORWARD_TARGET. Safe to run
alongside the live listener.

Usage:
  docker exec thesis-forward-listener-1 python3 -m src.scripts.verify_forward
  docker exec thesis-forward-listener-1 python3 -m src.scripts.verify_forward --limit 50
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile

from telethon import TelegramClient

from .. import config
from .forward_listener import (
    _DIGEST_SUBSTACK_RE,
    _DIGEST_TG_RE,
    _DIGEST_X_RE,
    _PLAIN_MIN_CHARS,
    _extract_substack_links,
    _extract_telegram_links,
    _parse_channel_list,
    _resolve_channel,
    _strip_plain_urls,
)


def _classify_plain(text: str, channel_key: str) -> tuple[str, str, int]:
    """Return (verdict, reason, kept_chars) for a plain-relay candidate."""
    cleaned = _strip_plain_urls(text, channel_key)
    if len(cleaned) < _PLAIN_MIN_CHARS:
        if not text.strip():
            reason = "empty body (likely photo/album)"
        elif len(cleaned) == 0:
            reason = "URL strip removed everything"
        else:
            reason = f"only {len(cleaned)} chars after strip (< {_PLAIN_MIN_CHARS})"
        return "DROP", reason, len(cleaned)
    return "RELAY", "", len(cleaned)


def _classify_digest(text: str, msg) -> tuple[str, str, int]:
    """Return (verdict, reason, count) for a digest-mode candidate.
    `count` is # of expansions that would happen (TG forwards or
    Substack URLs)."""
    if _DIGEST_X_RE.search(text):
        return "SKIP", "X digest (intentionally dropped)", 0
    if _DIGEST_TG_RE.search(text):
        n = len(_extract_telegram_links(msg))
        return "EXPAND-TG", f"{n} t.me links to forward", n
    if _DIGEST_SUBSTACK_RE.search(text):
        n = len(_extract_substack_links(msg))
        return "EXPAND-SS", f"{n} URLs to relay", n
    return "DROP", "not a digest (non-Noah message)", 0


async def _verify_channel(
    client: TelegramClient, channel: str, plain_channels: set[str],
    limit: int,
) -> None:
    try:
        src = await _resolve_channel(client, channel)
    except Exception as e:
        print(f"\n=== {channel} ===")
        print(f"  resolve failed: {type(e).__name__}: {e}")
        return

    title = getattr(src, "title", channel)
    is_plain = channel.lower() in plain_channels
    mode = "plain" if is_plain else "digest"
    print(f"\n=== [{mode}] {title} ({channel}) ===")

    msgs = []
    async for m in client.iter_messages(src, limit=limit):
        msgs.append(m)
    if not msgs:
        print("  no messages found")
        return

    counts: dict[str, int] = {}
    for m in msgs:
        text = (m.message or "").strip()
        if is_plain:
            verdict, reason, n = _classify_plain(text, channel.lower())
        else:
            verdict, reason, n = _classify_digest(text, m)
        counts[verdict] = counts.get(verdict, 0) + 1
        preview = text.replace("\n", " ")[:60]
        extra = f" [{reason}]" if verdict in ("DROP", "SKIP") else ""
        suffix = f" → {n} items" if verdict.startswith("EXPAND") else f" ({n} chars)"
        print(f"  [#{m.id}] {verdict:9}{suffix}{extra}")
        if preview:
            print(f"           {preview!r}")

    total = len(msgs)
    parts = [f"{k}={v}" for k, v in sorted(counts.items())]
    summary = ", ".join(parts)
    relay_or_expand = counts.get("RELAY", 0) + counts.get("EXPAND-TG", 0) + counts.get("EXPAND-SS", 0)
    pct = (relay_or_expand / total * 100) if total else 0
    print(f"  summary: {summary} (effective relay {relay_or_expand}/{total} = {pct:.0f}%)")


async def run(limit: int) -> None:
    channels = _parse_channel_list("LISTEN_CHANNELS")
    if not channels:
        sys.exit("LISTEN_CHANNELS env not set")
    plain_channels = {c.lower() for c in _parse_channel_list("LISTEN_PLAIN_CHANNELS")}

    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    # The live forward-listener holds an exclusive SQLite lock on
    # import_session.session, so we copy it into a tmpdir and let
    # Telethon open the duplicate. Auth key inside is identical, so
    # no phone-code prompt. tmpdir is cleaned up on exit.
    src_session = config.DATA_DIR / "import_session.session"
    if not src_session.exists():
        sys.exit(f"session not found at {src_session} — run import_channel first")
    with tempfile.TemporaryDirectory(prefix="verify_forward_") as tmp:
        copy_path = os.path.join(tmp, "verify.session")
        shutil.copyfile(src_session, copy_path)

        client = TelegramClient(copy_path[:-len(".session")], api_id, api_hash)
        await client.start()
        try:
            print(f"verifying {len(channels)} channels (last {limit} msgs each)")
            for ch in channels:
                await _verify_channel(client, ch, plain_channels, limit)
        finally:
            await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit", type=int, default=20,
        help="messages per channel to inspect (default: 20)",
    )
    args = parser.parse_args()
    asyncio.run(run(args.limit))


if __name__ == "__main__":
    main()
