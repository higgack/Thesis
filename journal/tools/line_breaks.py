"""Break opportunities inside long Latin/number tokens that left a stretched line in Word (thesis track, v81+).

Usage: python3 tools/line_breaks.py USER.docx OUT.docx [SET]      (SET = 81 (default) or 84)

v81: the user marked four lines in yellow (on v80) whose word spaces Word had stretched: in each, the next token was a
long unbreakable Latin or number string (Steinmueller(1999)는, Frenken(2006)의, (2224/08145,, (21/67121),) that did
not fit at the end of the line, so the justified line was spread over its few spaces.
v84: two more such lines marked on v83: "… 이 가운데" before "Caviggioli(2016)는" (paragraph "특정 영역에서 개발된
기술이…") and "… 응용 융합(applicational" before "convergence)" (paragraph "앞서 언급한 Hacklin et al.(2010)의…").
Besides those two tokens, the English words after them in the same two paragraphs get break points too, because the
lines below a fixed line move up and another long word could then land at a line end.
This adds invisible break opportunities inside the tokens and changes nothing else:
  - English words and names: optional hyphens (Word shows the hyphen only when it breaks the word there), e.g.
      Stein-muel-ler, Fren-ken, Ca-vig-gioli, con-ver-gence, in-dus-tri-al, Cur-ran, Le-ker, Hack-lin
  - CPC symbols: a no-width optional break (U+200B) after the slash: (2224/|08145, (21/|67121)
The user's yellow marks are removed (they marked the problem lines; they are not part of the thesis), and the runs
the marks had split are joined again when their formatting is identical (only in the marked paragraphs). From v84
on, only word/document.xml is replaced; every other part of the user's file is kept byte for byte.
Audit (exit 1 on failure): every edit is applied exactly once, and the visible text of every paragraph is unchanged
(the paragraph text minus U+200B equals the input).
"""
import copy, sys
import docx
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree

ZWSP = '​'
# (unique paragraph start, token (unique in the paragraph), split points inside the token, kind)
SETS = {
    '81': [
        ('선행연구의 첫째 특징과 관련하여', 'Steinmueller', (5, 9), 'hyphen'),     # Stein-muel-ler
        ('한편 하이브리드 본딩의 핵심 기술 식별과', 'Frenken', (4,), 'hyphen'),          # Fren-ken
        ('기술 클러스터. 표 5에는', '(2224/08145', (6,), 'zwsp'),               # (2224/|08145
        ('이러한 이동은 개별 기호를 넘어', '(21/67121)', (4,), 'zwsp'),             # (21/|67121)
    ],
    '84': [
        ('특정 영역에서 개발된 기술이', 'Caviggioli', (2, 5), 'hyphen'),                  # Ca-vig-gioli
        ('앞서 언급한 Hacklin et al.(2010)의', 'applicational convergence', (17, 20), 'hyphen'),   # con-ver-gence
        ('앞서 언급한 Hacklin et al.(2010)의', 'industrial convergence', (2, 5, 8, 14, 17), 'hyphen'),  # in-dus-tri-al con-ver-gence
        ('앞서 언급한 Hacklin et al.(2010)의', 'Curran et al.(2010)', (3,), 'hyphen'),         # Cur-ran
        ('앞서 언급한 Hacklin et al.(2010)의', 'Curran과 Leker(2011)', (3, 10), 'hyphen'),     # Cur-ran, Le-ker
        ('앞서 언급한 Hacklin et al.(2010)의', 'Hacklin et al.(2009)', (4,), 'hyphen'),        # Hack-lin
        ('앞서 언급한 Hacklin et al.(2010)의', '(Curran and Leker, 2011)', (4, 14), 'hyphen'),  # Cur-ran, Le-ker
    ],
}


def merge_runs(pars):
    """Join adjacent runs that hold only one w:t and have identical formatting (undoes splits made by the marks)."""
    n = 0
    for p in pars:
        prev = None
        for r in list(p):
            if r.tag != qn('w:r'):
                prev = None; continue
            kids = [k for k in r if k.tag != qn('w:rPr')]
            if len(kids) != 1 or kids[0].tag != qn('w:t'):
                prev = None; continue
            if prev is not None:
                a, b = prev.find(qn('w:rPr')), r.find(qn('w:rPr'))
                sa = etree.tostring(a) if a is not None else b''
                sb = etree.tostring(b) if b is not None else b''
                if sa == sb:
                    t = prev.find(qn('w:t'))
                    t.text = (t.text or '') + (r.find(qn('w:t')).text or '')
                    t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                    p.remove(r); n += 1
                    continue
            prev = r
    return n


def ptext(p):
    return ''.join(t.text or '' for t in p.iter(qn('w:t')))


def split_run_at(r, at):
    """Split run r with a single w:t at character offset `at`; return the second run."""
    t = r.find(qn('w:t')); s = t.text or ''
    r2 = copy.deepcopy(r)
    for x in list(r2):
        if x.tag != qn('w:rPr') and x.tag != qn('w:t'):
            r2.remove(x)
    t.text = s[:at]; r2.find(qn('w:t')).text = s[at:]
    for x in (t, r2.find(qn('w:t'))):
        x.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
    r.addnext(r2)
    return r2


def soft_hyphen_run(after):
    r = copy.deepcopy(after)
    for x in list(r):
        if x.tag != qn('w:rPr'):
            r.remove(x)
    r.append(OxmlElement('w:softHyphen'))
    after.addnext(r)
    return r


def main(src, out, which='81'):
    EDITS = SETS[which]
    d = docx.Document(src)
    body = d.element.body
    # the user's yellow marks
    nh = 0
    marked = []                                          # paragraphs that held a mark
    for h in list(body.iter(qn('w:highlight'))):
        par = next(a for a in h.iterancestors() if a.tag == qn('w:p'))
        if par not in marked:
            marked.append(par)
        rPr = h.getparent(); rPr.remove(h); nh += 1
        if which != '81' and len(rPr) == 0 and not rPr.attrib:
            rPr.getparent().remove(rPr)
    nm = merge_runs(marked) if which != '81' else 0     # v81 output was made without joining; keep it reproducible
    before = [ptext(p).replace(ZWSP, '') for p in body.iter(qn('w:p'))]
    done = 0
    for start, token, cuts, kind in EDITS:
        ps = [p for p in body.iter(qn('w:p')) if ptext(p).startswith(start)]
        assert len(ps) == 1, (start, len(ps))
        p = ps[0]
        assert ptext(p).count(token) == 1, (token, ptext(p).count(token))
        # locate the run holding the token (the token must not straddle runs)
        runs = [r for r in p.iter(qn('w:r')) if r.find(qn('w:t')) is not None]
        hit = [r for r in runs if token in (r.find(qn('w:t')).text or '') and len(r.findall(qn('w:t'))) == 1]
        assert len(hit) == 1, (token, len(hit))
        r = hit[0]
        s = r.find(qn('w:t')).text
        base = s.index(token)
        if kind == 'zwsp':
            k = base + cuts[0]
            r.find(qn('w:t')).text = s[:k] + ZWSP + s[k:]
            r.find(qn('w:t')).set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        else:
            for c in sorted(cuts, reverse=True):     # later cut first, so the earlier offsets stay valid
                split_run_at(r, base + c)              # r keeps the text before the cut
                soft_hyphen_run(r)                     # optional hyphen between the two parts
        done += 1
    # audit: visible text unchanged
    after = [ptext(p).replace(ZWSP, '') for p in body.iter(qn('w:p'))]
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b] + ([-1] if len(before) != len(after) else [])
    d.save(out)
    if which != '81':
        # keep every other part of the user's file byte for byte: only word/document.xml is replaced
        import io, zipfile
        doc = zipfile.ZipFile(out).read('word/document.xml')
        zin = zipfile.ZipFile(src); buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zout:
            for info in zin.infolist():
                data = doc if info.filename == 'word/document.xml' else zin.read(info.filename)
                zout.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
        open(out, 'wb').write(buf.getvalue())
    print(f'set v{which}: removed {nh} user highlights, joined {nm} split runs; break opportunities added in {done} '
          f'tokens; paragraphs with changed text: {len(changed)}')
    sys.exit(0 if done == len(EDITS) and not changed else 1)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2], *(sys.argv[3:4] or ['81']))
