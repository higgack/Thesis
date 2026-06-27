"""DART 일일 인입 캡 — 자동 채널이 DART 링크를 쏟아내도 하루 N건까지만
인입하도록 막는 가드. KST 자정에 리셋. 한도 도달 시 한 번만 텔레그램 알림
(should_alert가 one-shot 플래그로 중복 방지). 상태는 작은 JSON 파일.

`DART_DAILY_MAX`(.env, 기본 30) = 하루 허용 건수.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config

_KST = timezone(timedelta(hours=9))
_PATH = Path(config.DATA_DIR) / "dart_cap.json"


def _max() -> int:
    try:
        return max(0, int(os.getenv("DART_DAILY_MAX", "30")))
    except Exception:
        return 30


def _today() -> str:
    return datetime.now(_KST).strftime("%Y-%m-%d")


def _load() -> dict:
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    if d.get("date") != _today():
        d = {"date": _today(), "count": 0, "alerted": False}
    return d


def _save(d: dict) -> None:
    try:
        tmp = _PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        os.replace(tmp, _PATH)
    except Exception:
        pass


def over_cap() -> bool:
    """True if today's DART ingest count already reached the cap."""
    return _load().get("count", 0) >= _max()


def record() -> None:
    """Count one successful DART ingest today."""
    d = _load()
    d["count"] = d.get("count", 0) + 1
    _save(d)


def should_alert() -> tuple[bool, int, int]:
    """Returns (fire, count, max). `fire` is True exactly once per KST day —
    the first time the count has reached the cap and no alert was sent yet.
    Sets the alerted flag so the per-minute job won't spam."""
    d = _load()
    mx = _max()
    if mx and d.get("count", 0) >= mx and not d.get("alerted"):
        d["alerted"] = True
        _save(d)
        return True, d.get("count", 0), mx
    return False, d.get("count", 0), mx
