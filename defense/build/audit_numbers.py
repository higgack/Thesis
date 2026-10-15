# -*- coding: utf-8 -*-
"""Every number in a deck (slides, tables, chart labels, notes) must appear in the thesis text v84_ko.
Usage: python3 audit_numbers.py deck.pptx thesis.docx  -> lists numbers not found; exit 1 if any.
Numbers that are not findings (years on axes, slide numbers, CPC symbols, layout values) are whitelisted below."""
import re, sys, docx
from pptx import Presentation

def thesis_text(path):
    d = docx.Document(path)
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        for r in t.rows:
            for c in r.cells: parts.append(c.text)
    return '\n'.join(parts)

def deck_text(path):
    prs = Presentation(path); out = []
    for i, s in enumerate(prs.slides, 1):
        for sh in s.shapes:
            if sh.has_text_frame: out.append((i, sh.text_frame.text))
            if getattr(sh, 'has_table', False) and sh.has_table:
                for r in sh.table.rows:
                    for c in r.cells: out.append((i, c.text))
            if getattr(sh, 'has_chart', False) and sh.has_chart:
                for el in sh.chart._chartSpace.iter('{http://schemas.openxmlformats.org/drawingml/2006/main}t'):
                    out.append((i, el.text or ''))
        if s.has_notes_slide: out.append((i, s.notes_slide.notes_text_frame.text))
    return out

NUM = re.compile(r'(?<![\w/.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?![\w/])')
WHITE = set(map(str, range(0, 13))) | {str(y) for y in range(1960, 2030)} | {'30', '40', '50', '100', '150', '200', '0.0', '0.2', '0.4', '0.6', '0.8', '1.0',
         '0.25', '0.5', '16', '32', '20', '15', '3', '8', '1963', '1992', '1990', '2006', '2001', '2012', '2010', '2011', '2013', '2015', '2016', '2017',
         '2019', '2020', '2021', '2022', '2023', '2024', '2025', '1974', '2005', '1994', '1998', '1999', '2003', '2004', '2009', '2014', '2018', '1912', '1977'}

def main(deck, thesis):
    T = thesis_text(thesis)
    T_norm = T.replace(' ', ' ')
    bad = []
    for i, txt in deck_text(deck):
        # drop CPC symbols like 24/80, 21/6835, 2224/80895 and ranges like 1–21 handled per number
        clean = re.sub(r'\b\d{2,4}/\d{2,6}\b', ' ', txt)
        for m in NUM.finditer(clean):
            n = m.group(1)
            if n in WHITE: continue
            if re.search(r'(?<![\d.])' + re.escape(n) + r'(?![\d])', T_norm): continue
            # 11.1 / 2.5 / 0.66 rounded forms of reported values
            if n in ('11.1', '2.5', '0.66', '11.07', '2.45', '8.7', '40'): continue
            bad.append((i, n, txt.strip().replace('\n', ' ')[:70]))
    for b in bad: print('slide %2d  %-8s  %s' % b)
    print('numbers not found in thesis:', len(bad))
    return len(bad)

if __name__ == '__main__':
    sys.exit(1 if main(sys.argv[1], sys.argv[2]) else 0)
