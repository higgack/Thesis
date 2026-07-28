"""User-decision queue for URL ingest failures.

When a forwarded URL's body extraction returns empty (paywall,
JS-render, blocked, etc.), we don't immediately bury it in /failed.
Instead we park it here and prompt the user — once the retry queue
is quiet — with [🔁 재시도] / [🚫 차단] buttons.

If the user doesn't tap within `OVERDUE_MIN` minutes the entry
surfaces inside /pending so it never gets lost. Same buttons there.
Entries vanish when:
  • retry succeeds (learned),
  • user taps 🚫 차단 (URL added to _IGNORED_URLS),
  • retry FAILS — auto-blocked per user policy (one manual retry
    is enough; if it fails again we stop bothering the user).

State file: data/pending_url_decisions.json
Schema:
    [
        {
          "url": "...",
          "title": "...",
          "error": "...",
          "ts": "ISO 8601 UTC (added)",
          "prompted_at": "ISO 8601 UTC (last bubble sent) or null",
          "retry_count": 0
        },
        ...
    ]
"""
import json
import logging
import os
from datetime import datetime, timedelta

from .. import config

log = logging.getLogger(__name__)

_PATH = config.DATA_DIR / "pending_url_decisions.json"
# How long after a bubble is sent before the entry shows up in
# /pending. Kept short by user request — "5분내로 컨펌안해주면
# 자동으로 Pending으로". Pure data-side threshold; the /pending
# command applies it at read time, so changing this is hot-reload
# safe.
OVERDUE_MIN = 5


def _atomic_write(data: list) -> None:
    tmp = _PATH.with_suffix(_PATH.suffix + ".tmp")
    bak = _PATH.with_suffix(_PATH.suffix + ".bak")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if _PATH.exists():
        try:
            os.replace(_PATH, bak)
        except OSError:
            pass
    os.replace(tmp, _PATH)


def _load() -> list:
    if not _PATH.exists():
        return []
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        log.exception("pending_url_decisions load failed (using .bak)")
    bak = _PATH.with_suffix(_PATH.suffix + ".bak")
    if bak.exists():
        try:
            d = json.loads(bak.read_text(encoding="utf-8"))
            return d if isinstance(d, list) else []
        except Exception:
            log.exception("pending_url_decisions .bak load failed")
    return []


def add(url: str, title: str, error: str) -> None:
    """Add a new pending decision or refresh an existing one.
    Re-adding the same URL coalesces into the existing row and
    resets prompted_at=None so the next idle drain re-asks."""
    if not url:
        return
    data = _load()
    for entry in data:
        if entry.get("url") == url:
            entry["title"] = title or entry.get("title") or ""
            entry["error"] = error or entry.get("error") or ""
            entry["ts"] = datetime.utcnow().isoformat(timespec="seconds")
            entry["prompted_at"] = None
            try:
                _atomic_write(data)
            except Exception:
                log.exception("pending_url_decisions persist failed")
            return
    data.append({
        "url": url,
        "title": (title or "")[:200],
        "error": (error or "")[:200],
        "ts": datetime.utcnow().isoformat(timespec="seconds"),
        "prompted_at": None,
        "retry_count": 0,
    })
    try:
        _atomic_write(data)
    except Exception:
        log.exception("pending_url_decisions persist failed")


def list_unprompted() -> list:
    """Entries that haven't been sent a bubble yet — drain target."""
    return [e for e in _load() if not e.get("prompted_at")]


def list_overdue(minutes: int = OVERDUE_MIN) -> list:
    """Entries whose bubble was sent more than `minutes` ago and
    the user still hasn't decided. Used by /pending so the user
    can resolve them after the original bubble scrolled off."""
    cutoff = datetime.utcnow() - timedelta(minutes=minutes)
    out = []
    for e in _load():
        pa = e.get("prompted_at")
        if not pa:
            continue
        try:
            pa_dt = datetime.fromisoformat(pa)
        except Exception:
            continue
        if pa_dt <= cutoff:
            out.append(e)
    return out


def list_all() -> list:
    return _load()


def get(url: str) -> dict | None:
    if not url:
        return None
    for e in _load():
        if e.get("url") == url:
            return e
    return None


def mark_prompted(url: str) -> None:
    """Record the moment the bubble was sent. Triggers the 5-minute
    overdue clock that surfaces the entry in /pending."""
    data = _load()
    for e in data:
        if e.get("url") == url:
            e["prompted_at"] = datetime.utcnow().isoformat(timespec="seconds")
            break
    try:
        _atomic_write(data)
    except Exception:
        log.exception("pending_url_decisions mark_prompted failed")


def remove(url: str) -> bool:
    if not url:
        return False
    data = _load()
    before = len(data)
    data = [e for e in data if e.get("url") != url]
    if len(data) == before:
        return False
    try:
        _atomic_write(data)
    except Exception:
        log.exception("pending_url_decisions remove failed")
        return False
    return True
