"""Review surface — the "체화" loop on top of the note store.

Thin layer the dashboard / Telegram review UI calls:
  - review_queue(): notes due today (next_due <= today)
  - questions_for(): active-recall questions for a note (answers hidden
    in the UI until revealed)
  - grade(): apply a self-grade (0 까먹음 / 1 가물가물 / 2 기억함) →
    reschedule via SM-2 lite and log the review
"""
from __future__ import annotations

from . import store, srs

# Re-export grade constants so callers don't reach into srs.
FORGOT = srs.GRADE_FORGOT      # 0
SHAKY = srs.GRADE_SHAKY        # 1
REMEMBER = srs.GRADE_REMEMBER  # 2


def review_queue() -> list[dict]:
    """Notes whose next review is due (or overdue) today."""
    return store.due_notes()


def questions_for(note_id: str) -> list[dict]:
    note = store.get_note(note_id)
    return note["questions"] if note else []


def grade(note_id: str, grade_value: int) -> dict | None:
    """Record a self-assessment and reschedule. Returns the new SRS
    state (ease/interval/next_due/...) or None for an unknown note."""
    if grade_value not in (FORGOT, SHAKY, REMEMBER):
        raise ValueError(f"grade must be 0/1/2, got {grade_value!r}")
    return store.record_review(note_id, grade_value)


def stats() -> dict:
    return store.stats()
