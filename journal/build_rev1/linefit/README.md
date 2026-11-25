# Line-fit work in progress (2026-11-25)

Goal: remove justified lines whose word spaces stretch to about twice normal or more in the author's Word,
by small wording edits (no red marking, author's choice).

- `lay.py`: line-break model. HY신명조 11pt, ASCII/Latin-1 half width, 72 units per line (79 at 10pt),
  first-line indent 4 units, Korean words split only with >= 2 syllables on each side.
  Validated on the author's screenshots, but the split rule and the width of the en dash are not certain.
- `edits.py`: candidate wording edits found so far (not yet applied to the build).
- `cands.py` / `opt.py`: per-paragraph candidate edits and a subset search.

Next step: calibrate `lay.py` on a PDF exported from the author's Word, then finish and move the edits into
`build_rev1.py`.
