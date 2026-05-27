"""
Auto-blocked URL hosts.

Tracks hosts whose body extraction failed repeatedly. After
THRESHOLD consecutive failures, future ingest_url calls for that
host return immediately with status='blocked' — no fetch, no
trafilatura attempt, no Jina fallback, no failure log entry.
Counter resets to zero the moment any URL on that host extracts
successfully, so a transient outage doesn't permanently kill a
good site.

Persistence: data/auto_blocked_hosts.json, atomic write (.tmp →
rename) + .bak retained on overwrite. On load, JSON parse errors
fall through to empty dict — safer than crashing the bot import.

Schema:
    {
      "<host>": {
          "count": int,             # consecutive failure count
          "last_url": str,          # most recent failed URL (≤200 char)
          "last_at": str            # ISO 8601 UTC timestamp
      },
      ...
    }
"""
import json
import logging
import os
from datetime import datetime
from urllib.parse import urlparse

from .. import config

log = logging.getLogger(__name__)

_PATH = config.DATA_DIR / "auto_blocked_hosts.json"
_THRESHOLD = int(os.getenv("URL_BLOCKLIST_THRESHOLD", "2"))

# Hosts we never auto-block, no matter how many extractions miss.
# Naver blogs are JS-rendered: even with the PostView rewrite a given
# post can extract empty, and a couple of those used to nuke the WHOLE
# host (every Naver blog silently skipped). For these we keep trying —
# a real miss just lands in /failed (visible, retryable) instead of
# disappearing. This also neutralises any pre-existing stored block.
_NEVER_BLOCK = {"blog.naver.com", "m.blog.naver.com"}

_BLOCKED: dict[str, dict] = {}
_loaded = False


def _atomic_write(data: dict) -> None:
    tmp = _PATH.with_suffix(_PATH.suffix + ".tmp")
    bak = _PATH.with_suffix(_PATH.suffix + ".bak")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    if _PATH.exists():
        try:
            os.replace(_PATH, bak)
        except OSError:
            pass
    os.replace(tmp, _PATH)


def _load_once() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not _PATH.exists():
        return
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _BLOCKED.update(data)
    except Exception:
        log.exception("url_blocklist: load failed (using empty)")


def _host_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def is_blocked(url: str) -> bool:
    """True if this host has crossed the failure threshold. Cheap —
    just a dict lookup after the one-time JSON load on first call."""
    _load_once()
    h = _host_of(url)
    if not h or h in _NEVER_BLOCK:
        return False
    rec = _BLOCKED.get(h)
    return bool(rec and rec.get("count", 0) >= _THRESHOLD)


def record_failure(url: str) -> None:
    """Bump the failure counter for this host. Called by
    pipeline.ingest_url when body extraction returns empty."""
    _load_once()
    h = _host_of(url)
    if not h or h in _NEVER_BLOCK:
        return
    rec = _BLOCKED.get(h, {"count": 0})
    rec["count"] = int(rec.get("count", 0)) + 1
    rec["last_url"] = (url or "")[:200]
    rec["last_at"] = datetime.utcnow().isoformat(timespec="seconds")
    _BLOCKED[h] = rec
    try:
        _atomic_write(_BLOCKED)
    except Exception:
        log.exception("url_blocklist: persist failed (in-memory only)")
    if rec["count"] >= _THRESHOLD:
        log.info(
            "url_blocklist: host %s reached threshold (%d failures), "
            "auto-blocked", h, rec["count"],
        )


def record_success(url: str) -> None:
    """Reset the counter for this host. Called by pipeline.ingest_url
    on any successful body extraction so a transient outage doesn't
    permanently kill a good site."""
    _load_once()
    h = _host_of(url)
    if not h or h not in _BLOCKED:
        return
    del _BLOCKED[h]
    try:
        _atomic_write(_BLOCKED)
    except Exception:
        log.exception("url_blocklist: reset persist failed")


def list_all() -> list[dict]:
    """All tracked hosts with their counter / last failure metadata.
    Used by /blocked_hosts command."""
    _load_once()
    out = []
    for h, rec in _BLOCKED.items():
        out.append({
            "host": h,
            "count": int(rec.get("count", 0)),
            "last_url": rec.get("last_url", ""),
            "last_at": rec.get("last_at", ""),
            "is_blocked": (h not in _NEVER_BLOCK
                           and int(rec.get("count", 0)) >= _THRESHOLD),
        })
    out.sort(key=lambda r: (-r["count"], r["host"]))
    return out


def reset_all() -> int:
    """Wipe every entry. Returns the number cleared. Used by
    /reset_blocked_hosts when the user wants to retry previously
    failed sites (e.g. after a paywall switched policies)."""
    _load_once()
    n = len(_BLOCKED)
    _BLOCKED.clear()
    try:
        _atomic_write(_BLOCKED)
    except Exception:
        log.exception("url_blocklist: clear persist failed")
    return n


def reset_one(host: str) -> bool:
    """Remove a single host's entry. Returns True if it existed."""
    _load_once()
    h = (host or "").strip().lower()
    if not h or h not in _BLOCKED:
        return False
    del _BLOCKED[h]
    try:
        _atomic_write(_BLOCKED)
    except Exception:
        log.exception("url_blocklist: single reset persist failed")
    return True
