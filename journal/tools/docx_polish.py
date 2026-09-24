"""Post-process a pandoc-generated DOCX so tables read well.

- A4 page, 2.5 cm margins
- tables: content-proportional column widths, smaller font (9 pt, 8 pt for wide
  tables, 7.5 pt for very wide), no row splitting across pages, header row repeated,
  compact cell margins, thin top/bottom/header rules (journal style)
- captions: table caption paragraph kept with the table; figure image kept with its caption
- optional: East Asian body font (e.g. "맑은 고딕" or "바탕")

Usage: python docx_polish.py in.docx out.docx [--ea-font "바탕"] [--body-pt 10.5]
"""
import sys
import re
from copy import deepcopy
from docx import Document
from docx.shared import Pt, Mm, Emu
from docx.enum.section import WD_ORIENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement


def _weight(text):
    """Approximate rendered width of a cell string (Korean chars count double)."""
    w = 0.0
    for ch in text:
        o = ord(ch)
        if 0xAC00 <= o <= 0xD7A3 or 0x3000 <= o <= 0x9FFF:
            w += 2.0
        elif ch in '()%*.,':
            w += 0.5
        else:
            w += 1.0
    return w


def _set_cell_margins(tbl, top=20, bottom=20, left=60, right=60):
    tblPr = tbl._tbl.tblPr
    mar = tblPr.find(qn('w:tblCellMar'))
    if mar is None:
        mar = OxmlElement('w:tblCellMar')
        tblPr.append(mar)
    for side, val in (('top', top), ('bottom', bottom), ('left', left), ('right', right)):
        el = mar.find(qn('w:%s' % side))
        if el is None:
            el = OxmlElement('w:%s' % side)
            mar.append(el)
        el.set(qn('w:w'), str(val))
        el.set(qn('w:type'), 'dxa')


def _set_borders(tbl, n_rows):
    """Journal-style rules: top/bottom of table, and under the header row."""
    tblPr = tbl._tbl.tblPr
    borders = tblPr.find(qn('w:tblBorders'))
    if borders is None:
        borders = OxmlElement('w:tblBorders')
        tblPr.append(borders)
    for edge in ('top', 'bottom'):
        el = borders.find(qn('w:%s' % edge))
        if el is None:
            el = OxmlElement('w:%s' % edge)
            borders.append(el)
        el.set(qn('w:val'), 'single'); el.set(qn('w:sz'), '8'); el.set(qn('w:space'), '0'); el.set(qn('w:color'), '000000')
    for edge in ('left', 'right', 'insideH', 'insideV'):
        el = borders.find(qn('w:%s' % edge))
        if el is None:
            el = OxmlElement('w:%s' % edge)
            borders.append(el)
        el.set(qn('w:val'), 'nil')
    # header underline
    if n_rows:
        for cell in tbl.rows[0].cells:
            tcPr = cell._tc.get_or_add_tcPr()
            tcB = tcPr.find(qn('w:tcBorders'))
            if tcB is None:
                tcB = OxmlElement('w:tcBorders')
                tcPr.append(tcB)
            b = tcB.find(qn('w:bottom'))
            if b is None:
                b = OxmlElement('w:bottom')
                tcB.append(b)
            b.set(qn('w:val'), 'single'); b.set(qn('w:sz'), '4'); b.set(qn('w:space'), '0'); b.set(qn('w:color'), '000000')


def polish_table(tbl, text_width_twips):
    rows = tbl.rows
    if not rows:
        return
    ncols = max(len(r.cells) for r in rows)
    # column content weights
    maxw = [1.0] * ncols
    for ri, r in enumerate(rows):
        for j, c in enumerate(r.cells[:ncols]):
            txt = c.text.strip()
            full = _weight(txt)
            tokens = re.split(r'\s+', txt)
            longest = max((_weight(t) for t in tokens), default=1.0)
            if ri == 0:
                # header cells may wrap: count them at a discount
                w = max(longest, 0.55 * full)
            elif len(tokens) <= 3 or full <= 18:
                # short data cells must never wrap
                w = full
            else:
                w = min(full, longest + 8.0)
            maxw[j] = max(maxw[j], w)
    # font size by width pressure
    if ncols >= 9:
        sz = 15   # 7.5 pt
    elif ncols >= 7:
        sz = 16   # 8 pt
    elif ncols >= 5:
        sz = 17   # 8.5 pt
    else:
        sz = 18   # 9 pt
    # longest unbreakable token per column -> minimum width in twips (never wrap a token)
    longest_tok = [1.0] * ncols
    for r in rows:
        for j, c in enumerate(r.cells[:ncols]):
            for tok in re.split(r'\s+', c.text.strip()):
                longest_tok[j] = max(longest_tok[j], _weight(tok))
    def widths_for(sz_):
        pt = sz_ / 2.0
        mins = [int(w * 0.5 * pt * 20 + 160) for w in longest_tok]
        total = sum(maxw)
        prop = [int(text_width_twips * w / total) for w in maxw]
        ws = [max(a, b) for a, b in zip(prop, mins)]
        s = sum(ws)
        if s > text_width_twips:
            # shrink only the slack above the minimums
            slack = [a - b for a, b in zip(ws, mins)]
            excess = s - text_width_twips
            tot_slack = sum(slack)
            if tot_slack > 0:
                ws = [a - int(excess * (sl / tot_slack)) for a, sl in zip(ws, slack)]
        return ws, sum(ws)
    widths, s = widths_for(sz)
    while s > text_width_twips and sz > 13:
        sz -= 1
        widths, s = widths_for(sz)
    # apply grid + cell widths
    tblPr = tbl._tbl.tblPr
    lay = tblPr.find(qn('w:tblLayout'))
    if lay is None:
        lay = OxmlElement('w:tblLayout')
        tblPr.append(lay)
    lay.set(qn('w:type'), 'fixed')
    tblW = tblPr.find(qn('w:tblW'))
    if tblW is None:
        tblW = OxmlElement('w:tblW')
        tblPr.append(tblW)
    tblW.set(qn('w:type'), 'dxa'); tblW.set(qn('w:w'), str(sum(widths)))
    grid = tbl._tbl.find(qn('w:tblGrid'))
    if grid is not None:
        for gc in list(grid):
            grid.remove(gc)
        for w in widths:
            gc = OxmlElement('w:gridCol'); gc.set(qn('w:w'), str(w)); grid.append(gc)
    for r in rows:
        trPr = r._tr.get_or_add_trPr()
        if trPr.find(qn('w:cantSplit')) is None:
            trPr.append(OxmlElement('w:cantSplit'))
        for j, c in enumerate(r.cells[:ncols]):
            tcPr = c._tc.get_or_add_tcPr()
            tcW = tcPr.find(qn('w:tcW'))
            if tcW is None:
                tcW = OxmlElement('w:tcW'); tcPr.append(tcW)
            tcW.set(qn('w:type'), 'dxa'); tcW.set(qn('w:w'), str(widths[j]))
            for p in c.paragraphs:
                pf = p.paragraph_format
                pf.space_before = Pt(1); pf.space_after = Pt(1); pf.line_spacing = 1.0
                pPr = p._p.get_or_add_pPr()
                if pPr.find(qn('w:keepNext')) is None:
                    pPr.append(OxmlElement('w:keepNext'))
                for run in p.runs:
                    rPr = run._r.get_or_add_rPr()
                    for tag in ('w:sz', 'w:szCs'):
                        el = rPr.find(qn(tag))
                        if el is None:
                            el = OxmlElement(tag); rPr.append(el)
                        el.set(qn('w:val'), str(sz))
                if not p.runs:
                    pass
    # note rows (only the first cell has text) span the full width
    for r in rows[1:]:
        cells = r.cells
        if len(cells) > 1 and cells[0].text.strip() and all(not c.text.strip() for c in cells[1:]):
            try:
                merged = cells[0].merge(cells[-1])
                for p in merged.paragraphs:
                    p.paragraph_format.alignment = None
                    pPr = p._p.get_or_add_pPr()
                    jc = pPr.find(qn('w:jc'))
                    if jc is not None:
                        pPr.remove(jc)
            except Exception:
                pass
    _set_cell_margins(tbl)
    _set_borders(tbl, len(rows))
    # header repeat
    trPr = rows[0]._tr.get_or_add_trPr()
    if trPr.find(qn('w:tblHeader')) is None:
        trPr.append(OxmlElement('w:tblHeader'))


def _is_caption(p, kind):
    t = p.text.strip()
    return t.startswith(kind + ' ') and (re.match(r'^%s \d+\.' % kind, t) is not None or re.match(r'^<%s \d+>' % kind, t) is not None or re.match(r'^%s \d+[.:]' % kind, t) is not None)


def polish(path_in, path_out, ea_font=None, body_pt=None):
    doc = Document(path_in)
    for s in doc.sections:
        s.page_width = Mm(210); s.page_height = Mm(297)
        s.left_margin = s.right_margin = Mm(25)
        s.top_margin = s.bottom_margin = Mm(25)
    text_width = int((210 - 50) / 25.4 * 1440)  # twips
    for tbl in doc.tables:
        polish_table(tbl, text_width)
    # captions: keep table caption with table, image with figure caption
    body = doc.element.body
    children = list(body)
    for i, el in enumerate(children):
        if el.tag == qn('w:p'):
            txt = ''.join(t.text or '' for t in el.iter(qn('w:t'))).strip()
            has_img = el.find('.//' + qn('w:drawing')) is not None
            nxt = children[i + 1] if i + 1 < len(children) else None
            if (re.match(r'^(\*\*)?(표|Table|<표) ?\d+', txt) and nxt is not None and nxt.tag == qn('w:tbl')) or has_img:
                pPr = el.find(qn('w:pPr'))
                if pPr is None:
                    pPr = OxmlElement('w:pPr'); el.insert(0, pPr)
                if pPr.find(qn('w:keepNext')) is None:
                    pPr.append(OxmlElement('w:keepNext'))
    # optional fonts
    if ea_font or body_pt:
        styles = doc.styles
        for name in ('Normal', 'Body Text', 'First Paragraph', 'Compact'):
            try:
                st = styles[name]
            except KeyError:
                continue
            rPr = st.element.get_or_add_rPr()
            if ea_font:
                rFonts = rPr.find(qn('w:rFonts'))
                if rFonts is None:
                    rFonts = OxmlElement('w:rFonts'); rPr.append(rFonts)
                rFonts.set(qn('w:eastAsia'), ea_font)
            if body_pt and name != 'Compact':
                st.font.size = Pt(body_pt)
    doc.save(path_out)


if __name__ == '__main__':
    args = sys.argv[1:]
    ea = None; bp = None
    if '--ea-font' in args:
        i = args.index('--ea-font'); ea = args[i + 1]; del args[i:i + 2]
    if '--body-pt' in args:
        i = args.index('--body-pt'); bp = float(args[i + 1]); del args[i:i + 2]
    polish(args[0], args[1], ea, bp)
    print('polished ->', args[1])
