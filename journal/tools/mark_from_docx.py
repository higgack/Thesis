"""Carry yellow highlights from a marked Word file onto a newly built Word file with (almost) the same text.

Usage:
  python3 tools/mark_from_docx.py extract MARKED.docx MARKS.json     (highlighted spans -> json)
  python3 tools/mark_from_docx.py apply BASE.docx MARKS.json OUT.docx (BASE + yellow at those spans -> OUT)

A span is (paragraph number, start, end, text), counting every w:p of the document body in order (table cells
included). When BASE has changed a paragraph (e.g. a sentence before the span was edited), the span is moved to the
occurrence of its text nearest the old position; a span whose text is no longer in the paragraph stops the run.
Only the highlight is added: the text, runs and formatting of BASE are otherwise unchanged (runs are split at the
span boundaries). Checks: the highlighted text of OUT equals the span texts, and the plain text of OUT equals BASE.
"""
import sys, json, copy
import docx
from docx.oxml.ns import qn
from docx.text.run import Run
from docx.enum.text import WD_COLOR_INDEX

def run_text(r):
    out = []
    for c in r:
        tag = c.tag
        if tag == qn('w:t'): out.append(c.text or '')
        elif tag == qn('w:tab'): out.append('\t')
        elif tag in (qn('w:br'), qn('w:cr')): out.append('\n')
        elif tag == qn('w:noBreakHyphen'): out.append('-')
    return ''.join(out)

def paras(d):
    return list(d.element.body.iter(qn('w:p')))

def runs(p):
    return [r for r in p.iter(qn('w:r'))]

def ptext(p):
    return ''.join(run_text(r) for r in runs(p))

def is_yellow(r):
    h = r.find(qn('w:rPr') + '/' + qn('w:highlight'))
    return h is not None and h.get(qn('w:val')) not in (None, 'none')

def extract(marked):
    d = docx.Document(marked); spans = []
    for i, p in enumerate(paras(d)):
        pos = 0; t = ptext(p)
        for r in runs(p):
            n = len(run_text(r))
            if is_yellow(r) and n:
                if spans and spans[-1]['para'] == i and spans[-1]['end'] == pos: spans[-1]['end'] += n
                else: spans.append({'para': i, 'start': pos, 'end': pos + n})
            pos += n
        for s in spans:
            if s['para'] == i: s['text'] = t[s['start']:s['end']]
    return spans

def split_run(r, k):
    """Split run r (rPr + one w:t) at character k; returns (left, right)."""
    kids = [c for c in r if c.tag != qn('w:rPr')]
    assert len(kids) == 1 and kids[0].tag == qn('w:t'), 'cannot split a run with tabs, breaks or fields'
    t = kids[0].text
    right = copy.deepcopy(r); r.addnext(right)
    for el, s in ((r, t[:k]), (right, t[k:])):
        w = el.find(qn('w:t')); w.text = s; w.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
    return r, right

def mark(p, s, e):
    pos = 0
    for r in runs(p):
        n = len(run_text(r))
        a, b = pos, pos + n
        if b <= s or a >= e or n == 0: pos += n; continue
        if a < s:
            _, r = split_run(r, s - a); a = s
        if b > e:
            r, _ = split_run(r, e - a); b = e
        Run(r, None).font.highlight_color = WD_COLOR_INDEX.YELLOW
        pos = b
        if b >= e: break

def apply(base, marks, out):
    d = docx.Document(base); ps = paras(d); before = [ptext(p) for p in ps]
    moved = []
    for m in marks:
        p = ps[m['para']]; t = ptext(p); s = m['start']
        if t[s:m['end']] != m['text']:
            hits = [k for k in range(len(t)) if t.startswith(m['text'], k)]
            assert hits, f"span text not found in paragraph {m['para']}: {m['text'][:60]}"
            s = min(hits, key=lambda k: abs(k - m['start'])); moved.append((m['para'], m['start'], s))
        mark(p, s, s + len(m['text']))
    d.save(out)
    # checks
    d2 = docx.Document(out); ps2 = paras(d2)
    assert [ptext(p) for p in ps2] == before, 'text changed'
    got = []
    for i, p in enumerate(ps2):
        cur = None
        for r in runs(p):
            if is_yellow(r) and run_text(r):
                if cur is not None and cur[0] == i: cur[1] += run_text(r)
                else: cur = [i, run_text(r)]; got.append(cur)
            elif run_text(r): cur = None
    want = [[m['para'], m['text']] for m in marks]
    assert got == want, ('highlight mismatch', got[:3], want[:3])
    print('marked', len(marks), 'spans; moved', moved)

if __name__ == '__main__':
    if sys.argv[1] == 'extract':
        sp = extract(sys.argv[2])
        json.dump(sp, open(sys.argv[3], 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print('spans', len(sp))
    else:
        apply(sys.argv[2], json.load(open(sys.argv[3], encoding='utf-8')), sys.argv[4])
