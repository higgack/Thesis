"""Spaced-repetition scheduling — SM-2 lite.

Pure functions, no I/O. `schedule()` takes the current per-note SRS
state + a self-grade and returns the next state. The dashboard's 3
review buttons map to grades:

    0  까먹음    (forgot)      — reset interval, lower ease
    1  가물가물  (shaky)       — small bump, ease unchanged
    2  기억함    (remembered)  — full SM-2 progression, raise ease

State dict keys (see notes.db `note_srs`):
    ease           float  (1.3 – 2.7, default 2.5)
    interval_days  int
    reps           int    (consecutive successful reviews)
    lapses         int    (number of times forgotten)
"""
from __future__ import annotations

EASE_MIN = 1.3
EASE_MAX = 2.7
EASE_DEFAULT = 2.5

GRADE_FORGOT = 0
GRADE_SHAKY = 1
GRADE_REMEMBER = 2


def initial_state() -> dict:
    """SRS state for a freshly synthesised note. interval 0 / next_due
    handled by the caller (store) so the note enters today's queue
    tomorrow."""
    return {
        "ease": EASE_DEFAULT,
        "interval_days": 0,
        "reps": 0,
        "lapses": 0,
    }


def _clamp_ease(e: float) -> float:
    return max(EASE_MIN, min(EASE_MAX, e))


def schedule(state: dict, grade: int) -> dict:
    """Return the next SRS state given a review grade. Does not touch
    timestamps — the store sets last_reviewed/next_due from
    interval_days."""
    ease = float(state.get("ease", EASE_DEFAULT))
    interval = int(state.get("interval_days", 0))
    reps = int(state.get("reps", 0))
    lapses = int(state.get("lapses", 0))

    if grade <= GRADE_FORGOT:
        return {
            "ease": _clamp_ease(ease - 0.2),
            "interval_days": 1,
            "reps": 0,
            "lapses": lapses + 1,
        }

    if grade == GRADE_SHAKY:
        # Mild reinforcement: nudge the interval up a little, keep ease.
        next_interval = max(1, round(interval * 1.2)) if interval else 1
        return {
            "ease": ease,
            "interval_days": next_interval,
            "reps": reps + 1,
            "lapses": lapses,
        }

    # GRADE_REMEMBER — full SM-2 progression.
    if reps == 0:
        next_interval = 1
    elif reps == 1:
        next_interval = 4
    else:
        next_interval = max(1, round(interval * ease))
    return {
        "ease": _clamp_ease(ease + 0.1),
        "interval_days": next_interval,
        "reps": reps + 1,
        "lapses": lapses,
    }
