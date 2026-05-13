"""Backfill missing/hash-only titles in meta.db.

Some old docs were ingested when the title extraction was less robust,
ending up with `title = doc_id` (16-char hex) or empty `title`. These
render as "[1] b4f84b63550b8e24" in answer source listings.

This script scans for problem docs and derives a sensible title from:
  1. Source filename (for `local:` PDFs/DOCXs/etc.) — strips path,
     dedupes common prefixes like "[하나증권] ", strips extension.
  2. URL path tail (for `http(s)://` sources) — last meaningful slug.
  3. First non-empty line of the summary (for `tg-msg:` or unknown).
  4. Falls back to keeping the existing title only if nothing else
     produces ≥3 Korean/English chars.

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


# A title qualifies as "needs fix" if it's empty, all-hex (looks like a
# doc_id), or shorter than the minimum readable threshold. We use the
# same Korean/English keyword check as `_looks_like_title` in
# `src/ingest/loaders.py` so the rule stays consistent across ingest +
# backfill.
_HEX_RE = re.compile(r"^[0-9a-f]{12,32}$")
_KEYWORD_RE = re.compile(r"[A-Za-z가-힣]{2,}")
_MIN_TITLE_CHARS = 4


def _is_problem(title: str) -> bool:
    t = (title or "").strip()
    if not t:
        return True
    if _HEX_RE.match(t):
        return True
    if len(t) < _MIN_TITLE_CHARS:
        return True
    # No meaningful word characters — only punctuation/digits/whitespace.
    return not _KEYWORD_RE.search(t)


def _derive_from_source(source: str) -> str:
    """Best-effort title from a source string. Returns '' if nothing
    useful can be extracted."""
    if not source:
        return ""
    s = source.strip()
    # local:<path/to/file.ext> — strip prefix, take basename without ext
    if s.startswith("local:"):
        name = Path(s[len("local:"):]).name
        stem = re.sub(r"\.(pdf|docx?|pptx?|xlsx?|txt|md|hwp)$", "", name,
                      flags=re.IGNORECASE)
        return stem.strip()
    # http(s) URL — last path segment, decoded slug
    if s.startswith("http://") or s.startswith("https://"):
        try:
            parsed = urlparse(s)
            tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
            tail = re.sub(r"[._-]+", " ", tail).strip()
            if tail and _KEYWORD_RE.search(tail):
                return tail[:120]
            host = parsed.netloc.replace("www.", "")
            return host or ""
        except Exception:
            return ""
    # tg-msg:<id>:<hash> or tg-doc:<id> — no useful info, caller falls
    # back to summary first line
    return ""


def _derive_from_summary(summary: str) -> str:
    """First non-empty line of the summary, truncated. Skip lines that
    look like markdown headers (`#`) or all-emoji prefixes."""
    if not summary:
        return ""
    for raw in summary.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Strip leading markdown header / bullet markers
        line = re.sub(r"^(#+\s*|\*\s+|\d+\.\s+)", "", line).strip()
        if len(line) < _MIN_TITLE_CHARS:
            continue
        if not _KEYWORD_RE.search(line):
            continue
        return line[:120]
    return ""


def _derive_title(doc: dict) -> str:
    """Pick the best available replacement title for a problem doc."""
    candidate = _derive_from_source(doc.get("source") or "")
    if candidate and _KEYWORD_RE.search(candidate):
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
