"""
yt-dlp health tracker.

Tracks ONE thing: can yt-dlp still extract from YouTube at all? A
"failure" here means `extract_info` itself failed (broken extractor,
dead Deno/EJS runtime, YouTube anti-bot change nightly hasn't caught up
to). It does NOT mean "this video had no captions" — caption-less clips
are a normal, expected miss recorded as SUCCESS, because yt-dlp did its
job. (Conflating the two used to drive the 24h failure rate to 100%
from a few subtitle-less videos and fire a false "yt-dlp 작동 이상"
alert while yt-dlp was perfectly healthy.)

Computes a rolling failure rate over a 24-hour window; the bot fires an
actionable alert when it crosses the threshold. NOTE on the most
common real-world cause: this VM runs on a GCP (datacenter) IP, and
YouTube increasingly blocks datacenter IPs outright with "Sign in to
confirm you're not a bot" — that is an IP-reputation failure, NOT a
yt-dlp/Deno problem, and re-deploying does NOT fix it (it usually
self-clears in hours to a day). The Dockerfile does best-effort pull
the latest yt-dlp nightly on every build (after `COPY src`), which
only helps the rarer case of a genuine extractor break. The alert text
(bot._check_yt_dlp_health) explains how to tell the two apart.

State file: data/yt_dlp_health.json
Schema:
    {
      "attempts": [
        {"ts": "2026-05-15T12:34:56", "success": true},
        ...up to _MAX_ATTEMPTS entries (ring buffer-like)
      ]
    }

Why a separate module instead of inlining in bot.py: the recorder
needs to be called from src/ingest/loaders.py (worker / non-bot
context) and we want zero coupling to ContextTypes / Telegram.
Pure stdlib JSON + atomic write.
"""
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

_PATH = config.DATA_DIR / "yt_dlp_health.json"
_MAX_ATTEMPTS = int(os.getenv("YTDLP_HEALTH_MAX_ATTEMPTS", "50"))
_WINDOW_MINUTES = int(os.getenv("YTDLP_HEALTH_WINDOW_MIN", "1440"))   # 24h
_FAIL_RATE_THRESHOLD = float(os.getenv("YTDLP_HEALTH_FAIL_RATE", "0.7"))
_MIN_SAMPLES = int(os.getenv("YTDLP_HEALTH_MIN_SAMPLES", "5"))


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


def _load() -> dict:
    if not _PATH.exists():
        return {"attempts": []}
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return d
    except Exception:
        log.exception("yt_dlp_health load failed (using empty)")
    return {"attempts": []}


def record_attempt(success: bool) -> None:
    """Append one attempt to the ring buffer. Trimmed to
    _MAX_ATTEMPTS to bound disk use; the window check uses the
    timestamps regardless of position, so older entries just age out
    of the rate calculation naturally."""
    try:
        d = _load()
        attempts = list(d.get("attempts") or [])
        attempts.append({
            "ts": datetime.utcnow().isoformat(timespec="seconds"),
            "success": bool(success),
        })
        # Keep only the most recent _MAX_ATTEMPTS so the file doesn't
        # grow unbounded under heavy ingest.
        if len(attempts) > _MAX_ATTEMPTS:
            attempts = attempts[-_MAX_ATTEMPTS:]
        d["attempts"] = attempts
        _atomic_write(d)
    except Exception:
        log.exception("yt_dlp_health record_attempt failed")


def failure_rate() -> tuple[float, int]:
    """Compute (failure_rate, total_in_window) over the last
    _WINDOW_MINUTES (default 24h). Returns (0.0, 0) when no
    attempts in the window."""
    d = _load()
    cutoff = datetime.utcnow() - timedelta(minutes=_WINDOW_MINUTES)
    fails = 0
    total = 0
    for entry in (d.get("attempts") or []):
        ts = entry.get("ts") or ""
        try:
            ts_dt = datetime.fromisoformat(ts)
        except Exception:
            continue
        if ts_dt < cutoff:
            continue
        total += 1
        if not entry.get("success"):
            fails += 1
    if total == 0:
        return 0.0, 0
    return fails / total, total


def is_unhealthy() -> bool:
    """True when rate >= threshold AND we have enough samples to
    trust the rate. Used by the periodic alert check."""
    rate, total = failure_rate()
    return total >= _MIN_SAMPLES and rate >= _FAIL_RATE_THRESHOLD


def status_summary() -> dict:
    """Return current health info for /audit or debugging."""
    rate, total = failure_rate()
    return {
        "rate": rate,
        "total": total,
        "threshold": _FAIL_RATE_THRESHOLD,
        "min_samples": _MIN_SAMPLES,
        "window_min": _WINDOW_MINUTES,
        "unhealthy": is_unhealthy(),
    }
