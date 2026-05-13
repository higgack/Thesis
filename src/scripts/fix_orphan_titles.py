"""Backfill missing/hash-only titles in meta.db.

Some old docs were ingested when the title extraction was less robust,
ending up with `title = doc_id` (16-char hex), empty, BOM-only, or
broken fragments like "받)" / "b>" / "26년 04월 27일". These render as
"[1] b4f84b63550b8e24" or similar in answer source listings.

This script ONLY touches clearly-broken titles. Short-but-valid Korean
(네이버, 메쥬, 알멕) and CJK titles (高盛示警...) are preserved. The
classifier is intentionally conservative — false negatives are fine,
false positives downgrade good titles.

Replacement title is derived from:
  1. Source filename (for `local:` PDFs/DOCXs/etc.) — strips path,
     strips extension.
  2. URL path tail (for `http(s)://` sources) — only when the slug has
     clear word structure (separators); random alphanumeric IDs are
     skipped so we don't trade '네이버' for 'puVjeDAU9RPJcsLAoMknt6S'.
  3. First non-empty line of the summary (for `tg-msg:` / `tg-photo:` /
     unknown), with markdown markers and emoji prefixes stripped.

Idempotent: rerun is safe. Preview by default; pass `--confirm` to
write.

Run:
  docker exec thesis-bot-1 python3 -m src.scripts.fix_orphan_titles
  docker exec thesis-bot-1 python3 -m src.scripts.fix_orphan_titles --confirm
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

from .. import config


# Clearly-broken title patterns. Conservative on purpose — we'd rather
# leave a debatable title alone than downgrade a real one.
_HEX_RE = re.compile(r"^[0-9a-f]{12,32}$")
_BOM = "﻿"
# Must have at least one run of 2+ word chars (Korean, English,
# Chinese/Japanese ideographs). CJK is included so Chinese titles like
# "高盛示警全球輕油..." don't get nuked.
_WORD_RE = re.compile(r"[A-Za-z가-힣一-鿿]{2,}")
# Just a date, no actual title content (e.g. "26년 04월 27일").
_DATE_ONLY_RE = re.compile(
    r"^\d{2,4}[년.\-/\s]+\d{1,2}[월.\-/\s]+\d{1,2}[일.\s]*$"
)
# Trailing-symbol fragments produced by sloppy first-line extractors:
# "받)", "급)", "b>", "X", "X)" etc.
_FRAGMENT_RE = re.compile(r"^[A-Za-z가-힣]?[\s]*[)>\]}<>}]+$")


def _is_problem(title: str) -> bool:
    t = (title or "").strip().strip(_BOM)
    if not t:
        return True
    if _HEX_RE.match(t):
        return True
    if _DATE_ONLY_RE.match(t):
        return True
    if _FRAGMENT_RE.match(t):
        return True
    # No 2+ consecutive word chars anywhere = punctuation/symbol garbage.
    return not _WORD_RE.search(t)


def _derive_from_source(source: str) -> str:
    """Best-effort title from a source string. Returns '' if nothing
    useful can be extracted."""
    if not source:
        return ""
    s = source.strip()
    # local:<path/to/file.ext> — strip prefix, take basename without ext
    if s.startswith("local:"):
        name = Path(s[len("local:"):]).name
        stem = re.sub(
            r"\.(pdf|docx?|pptx?|xlsx?|txt|md|hwp|csv)$", "", name,
            flags=re.IGNORECASE,
        )
        return stem.strip()
    # http(s) URL — last path segment, but only if it looks like a
    # human-readable slug (has separators or short word). Random
    # alphanumeric IDs like "puVjeDAU9RPJcsLAoMknt6S" get skipped so
    # the caller can fall through to the summary instead.
    if s.startswith("http://") or s.startswith("https://"):
        try:
            parsed = urlparse(s)
            tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
            if not tail:
                return ""
            # Strip extension and language suffix
            tail = re.sub(r"\.(html?|php|aspx?|do)$", "", tail,
                          flags=re.IGNORECASE)
            # Reject if no separator AND looks like an ID (long, mixed)
            has_sep = any(c in tail for c in ("_", "-"))
            if not has_sep and len(tail) > 12:
                return ""
            tail_clean = re.sub(r"[._-]+", " ", tail).strip()
            if tail_clean and _WORD_RE.search(tail_clean):
                return tail_clean[:120]
            return ""
        except Exception:
            return ""
    # tg-msg:<id>:<hash> or tg-doc:<id> — no useful info, caller falls
    # back to summary first line
    return ""


# Leading clutter on the first line of summaries: markdown bullets,
# numbering, status emojis, BOM, ▪︎/●/▶ — strip these so we get the
# actual title fragment.
_SUMMARY_PREFIX_RE = re.compile(
    r"^[\s﻿#*\-•▪︎●▫️▶▸📌🟢🔴⚪✅📋📊🚀🔥📂📰🐦💎⚡❌📝🎯>]+"
)


def _derive_from_summary(summary: str) -> str:
    """First non-empty line of the summary, with markdown / emoji noise
    stripped. Returns '' if nothing yields a usable title."""
    if not summary:
        return ""
    for raw in summary.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Strip leading symbols / emojis / markdown
        line = _SUMMARY_PREFIX_RE.sub("", line).strip()
        # Unwrap **bold** so we don't get "**파일 형식**: PNG ..."
        line = re.sub(r"\*\*([^*]+)\*\*", r"\1", line).strip()
        # Also strip standalone/unbalanced `**` left over from summary
        # snippets like "**금융 및 경제 동향**:" where leading was already
        # stripped by _SUMMARY_PREFIX_RE.
        line = line.replace("**", "").strip()
        # Strip leading "Field: " prefix where Field is a short label
        line = re.sub(r"^[가-힣A-Za-z]{1,8}\s*:\s*", "", line).strip()
        # Strip "1. " / "1) " numbering
        line = re.sub(r"^\d+[.)\s]+\s*", "", line).strip()
        if len(line) < 4:
            continue
        if not _WORD_RE.search(line):
            continue
        return line[:120]
    return ""


def _derive_title(doc: dict) -> str:
    """Pick the best available replacement title for a problem doc."""
    candidate = _derive_from_source(doc.get("source") or "")
    if candidate and _WORD_RE.search(candidate):
        return candidate
    candidate = _derive_from_summary(doc.get("summary") or "")
    if candidate:
        return candidate
    return ""  # caller leaves doc untouched


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm", action="store_true",
        help="Apply the changes. Without this flag, only previews.",
    )
    args = parser.parse_args()

    db_path = config.DATA_DIR / "meta.db"
    if not db_path.exists():
        print(f"meta.db not found at {db_path}", file=sys.stderr)
        return 1

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, source, type, title, substr(summary, 1, 2000) as summary "
        "FROM documents"
    ).fetchall()

    problems: list[tuple[str, str, str]] = []  # (doc_id, old_title, new_title)
    untouchable: list[str] = []  # doc_ids where nothing better could be derived
    for row in rows:
        if not _is_problem(row["title"] or ""):
            continue
        new_title = _derive_title(dict(row))
        if not new_title:
            untouchable.append(row["id"])
            continue
        if new_title == (row["title"] or "").strip():
            continue
        problems.append((row["id"], row["title"] or "", new_title))

    if not problems and not untouchable:
        print("✓ no problem titles found — nothing to do.")
        return 0

    print(f"scanned {len(rows)} documents")
    print(f"  fixable: {len(problems)}")
    print(f"  unfixable (kept as-is): {len(untouchable)}")
    if untouchable:
        print(f"  unfixable doc_ids (first 5): {untouchable[:5]}")

    # Preview up to 20 changes so the diff is reviewable but not flooding
    # the terminal.
    preview = problems[:20]
    print(f"\npreview of changes ({len(preview)}/{len(problems)}):")
    for doc_id, old, new in preview:
        print(f"  [{doc_id}]")
        print(f"    OLD: {old!r}")
        print(f"    NEW: {new!r}")
    if len(problems) > len(preview):
        print(f"  ... and {len(problems) - len(preview)} more")

    if not args.confirm:
        print(
            f"\n(dry run) re-run with --confirm to apply "
            f"{len(problems)} updates."
        )
        return 0

    print(f"\napplying {len(problems)} updates ...")
    with con:
        for doc_id, _, new in problems:
            con.execute(
                "UPDATE documents SET title=? WHERE id=?",
                (new, doc_id),
            )
    print(f"✓ updated {len(problems)} documents.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
