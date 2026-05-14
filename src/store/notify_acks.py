"""
Actionable notification ack tracking.

When the bot needs the user to take a deliberate action (e.g.
"new paddle release out — try the OCR worker again?"), a one-shot
Telegram message is easy to miss. This module backs a daily-resend
pattern: each alert carries a [✅ 확인 / 알람 정지] inline button;
until the user taps it, the bot re-sends the same alert once per
24 hours at the same approximate time.

State file: data/notify_acks.json
Schema:
    {
      "<notify_id>": {
        "message": str,           # exact Telegram body to resend
        "parse_mode": str | null, # 'HTML' | 'Markdown' | null
        "first_sent_at": str,     # ISO 8601 UTC, never changes
        "last_sent_at": str,      # ISO 8601 UTC, updated on resend
        "acked": bool,
        "ack_at": str | null,
      },
      ...
    }

Use via bot.py:
    notify_acks.record_pending(notify_id, message, parse_mode='HTML')
    notify_acks.list_due()      # for the hourly resend job
    notify_acks.update_last_sent(notify_id)
    notify_acks.mark_acked(notify_id)   # called from ack callback

Persistence is atomic (tmp → rename) so a crash mid-write can't
torpedo the schedule.
"""
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

_PATH = config.DATA_DIR / "notify_acks.json"
_RESEND_HOURS = int(os.getenv("NOTIFY_ACK_RESEND_HOURS", "24"))
_HARD_CAP = 30  # safety: never track more than 30 unacked alerts


def _atomic_write(data: dict) -> None:
    tmp = _PATH.with_suffix(_PATH.suffix + ".tmp")
    bak = _PATH.with_suffix(_PATH.suffix + ".bak")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    if _PATH.exists():
        try:
            os.replace(_PATH, bak)
        except OSError:
            pass
    os.replace(tmp, _PATH)


def _load() -> dict:
    if not _PATH.exists():
        return {}
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        log.exception("notify_acks load failed (using empty)")
    # try .bak
    bak = _PATH.with_suffix(_PATH.suffix + ".bak")
    if bak.exists():
        try:
            data = json.loads(bak.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            log.exception("notify_acks bak load failed")
    return {}


def record_pending(notify_id: str, message: str,
                   parse_mode: str | None = None) -> bool:
    """Register a new actionable alert. Returns True on insert, False
    when an unacked entry with the same id already exists (caller
    typically wants to skip — the alert is already in the resend loop).
    """
    if not notify_id or not message:
        return False
    data = _load()
    existing = data.get(notify_id)
    if existing and not existing.get("acked"):
        return False  # already pending, don't reset the clock
    if len(data) >= _HARD_CAP:
        # Drop oldest acked entries first to make room
        acked = sorted(
            (k for k, v in data.items() if v.get("acked")),
            key=lambda k: data[k].get("ack_at", ""),
        )
        for k in acked[:max(1, len(data) - _HARD_CAP + 1)]:
            data.pop(k, None)
    now = datetime.utcnow().isoformat(timespec="seconds")
    data[notify_id] = {
        "message": message,
        "parse_mode": parse_mode,
        "first_sent_at": now,
        "last_sent_at": now,
        "acked": False,
        "ack_at": None,
    }
    try:
        _atomic_write(data)
    except Exception:
        log.exception("notify_acks persist failed")
        return False
    return True


def update_last_sent(notify_id: str) -> None:
    """Bump the resend clock — called after a successful resend so the
    next eligible time slides forward 24h."""
    data = _load()
    rec = data.get(notify_id)
    if not rec or rec.get("acked"):
        return
    rec["last_sent_at"] = datetime.utcnow().isoformat(timespec="seconds")
    try:
        _atomic_write(data)
    except Exception:
        log.exception("notify_acks update_last_sent failed")


def mark_acked(notify_id: str) -> bool:
    """User tapped the ✅ button. Stop resending. Returns True if a
    pending entry was actually flipped (False if unknown id or
    already acked)."""
    data = _load()
    rec = data.get(notify_id)
    if not rec or rec.get("acked"):
        return False
    rec["acked"] = True
    rec["ack_at"] = datetime.utcnow().isoformat(timespec="seconds")
    try:
        _atomic_write(data)
    except Exception:
        log.exception("notify_acks mark_acked failed")
        return False
    return True


def list_due() -> list[tuple[str, dict]]:
    """Return (notify_id, record) pairs whose last_sent_at is older
    than _RESEND_HOURS and which haven't been acked. Used by the
    bot's hourly resend job."""
    data = _load()
    cutoff = datetime.utcnow() - timedelta(hours=_RESEND_HOURS)
    out = []
    for nid, rec in data.items():
        if rec.get("acked"):
            continue
        last = rec.get("last_sent_at", "")
        try:
            last_dt = datetime.fromisoformat(last)
        except Exception:
            continue
        if last_dt <= cutoff:
            out.append((nid, rec))
    return out


def list_all() -> list[dict]:
    """Snapshot of every tracked entry, ordered most-recent-first.
    Used by a future /alerts command and for debugging."""
    data = _load()
    items = []
    for nid, rec in data.items():
        items.append({
            "id": nid,
            "acked": bool(rec.get("acked")),
            "first_sent_at": rec.get("first_sent_at", ""),
            "last_sent_at": rec.get("last_sent_at", ""),
            "ack_at": rec.get("ack_at"),
            "message_preview": (rec.get("message") or "")[:80],
        })
    items.sort(key=lambda r: r["first_sent_at"], reverse=True)
    return items
