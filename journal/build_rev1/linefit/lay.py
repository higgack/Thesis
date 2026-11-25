"""Line layout model of the author's Word (template setting, wordWrap 0). See wsim.py docstring.
Per paragraph: width 72 units at 11pt (79 at 10pt), first-line indent 4 units unless firstLine=0."""
import re, sys, docx
from docx.oxml.ns import qn
DASH = 1
def cw(c):
    if c == '​': return 0
    if c == '–': return DASH
    return 1 if ord(c) < 0x100 else 2
H = lambda c: '가' <= c <= '힣'
def units_of(text, minside=2):
    units = []
    for i, w in enumerate(text.split(' ')):
        cuts = [0]
        for j in range(1, len(w)):
            if w[j-1] == '​': cuts.append(j); continue
            if H(w[j-1]) and H(w[j]):
                l = len([c for c in w[:j] if H(c)]); r = len([c for c in w[j:] if H(c)])
                # count syllables within the current Hangul run only
                lm = re.search(r'[가-힣]+$', w[:j]).group(0); rm = re.match(r'[가-힣]+', w[j:]).group(0)
                if len(lm) >= minside and len(rm) >= minside: cuts.append(j)
        cuts.append(len(w))
        cuts = sorted(set(cuts))
        for k, (a, b) in enumerate(zip(cuts, cuts[1:])): units.append((w[a:b], i > 0 and k == 0))
    return units
def layout(text, W=72, indent=4, minside=2):
    lines = []; cur = ''; width = indent; nsp = 0
    for s, sp in units_of(text, minside):
        sw = sum(cw(c) for c in s); add = sw + (1 if (sp and cur) else 0)
        if cur and width + add > W:
            lines.append([cur, width, nsp, False]); cur = s; width = sw; nsp = 0; continue
        if sp and cur: cur += ' '; nsp += 1; width += 1
        cur += s; width += sw
    lines.append([cur, width, nsp, True]); return lines
def params(p):
    pPr = p._p.pPr; W = 72; ind = 4
    sizes = set()
    for r in p.runs:
        if not r.text: continue
        sz = r._r.rPr.find(qn('w:sz')) if r._r.rPr is not None else None
        sizes.add(sz.get(qn('w:val')) if sz is not None else None)
    if sizes == {'20'}: W = 79
    if pPr is not None and pPr.find(qn('w:ind')) is not None:
        e = pPr.find(qn('w:ind'))
        if e.get(qn('w:firstLine')) == '0': ind = 0
    return W, ind
def paras(d):
    for pi, p in enumerate(d.paragraphs):
        if p.style.name != '본문1' or not p.text.strip(): continue
        pPr = p._p.pPr
        jc = pPr.find(qn('w:jc')) if pPr is not None else None
        if jc is not None and jc.get(qn('w:val')) != 'both': continue
        W, ind = params(p)
        yield pi, p, re.sub(r' +', ' ', p.text.strip()), W, ind
def gaps(text, W, ind):
    out = []
    for s, w, n, last in layout(text, W, ind):
        g = None if last else (1 + (W - w) / n if n else 99)
        out.append((s, w, n, g))
    return out
def scan(d, thr=1.8):
    bad = {}
    for pi, p, t, W, ind in paras(d):
        ls = gaps(t, W, ind)
        b = [(li, round(g, 2)) for li, (s, w, n, g) in enumerate(ls) if g is not None and g >= thr]
        if b: bad[pi] = b
    return bad
def show(text, W=72, ind=4, thr=1.8):
    for s, w, n, g in gaps(text, W, ind):
        mark = '' if g is None or g < thr else f'  <-- {g:.2f}'
        print(f'{W-w:>3} {s}{mark}')
