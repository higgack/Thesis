# Line fit for justified body text (2026-11-25)

Purpose: remove justified lines whose word spaces stretch noticeably, using small wording edits only.
Fonts, sizes, character spacing, line spacing, margins and alignment stay as in the department template.

How it was done
1. The author exported the thesis to PDF from their own Word (Microsoft 365). That PDF is not in the repo.
2. `pdflines.py` / `pdfwide.py` read word positions from that PDF and measure the space width on each line.
3. `pdffontw.py` reads glyph widths from the fonts embedded in the PDF (`fontw.json`).
4. `lay2.py` is a line-break model of the author's Word, calibrated against the PDF with `align.py` + `calib.py`
   (132 of 142 body paragraphs break exactly as in the PDF; the rest differ by one or two characters):
   Hangul 1 em, Latin from the HY신명조 width table, space 0.5 em next to Hangul and compressible to 0.25 em
   between Latin words, Korean words may break at any syllable, hanging '.' and ','.
5. `edits2.py` holds the chosen edits (copied into `build_rev1.py` section 10c as LINEFIT);
   `cands2.py` + `opt2.py` were used to pick the smallest edit set per paragraph.

Targets: no Korean line with spaces at 1.5x normal or more, no English line at about 2.2x or more.
The edits do not change any paragraph's line count, so the page breaks match the author's PDF.

`lay.py`, `edits.py`, `cands.py` and the other files without a `2` are an earlier, uncalibrated attempt.
