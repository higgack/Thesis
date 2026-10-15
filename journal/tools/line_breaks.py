"""Break opportunities inside long Latin/number tokens that left a stretched line in Word (thesis track, v81+).

Usage: python3 tools/line_breaks.py USER.docx OUT.docx

The user marked four lines in yellow (on v80) whose word spaces Word had stretched: in each, the next token was a long
unbreakable Latin or number string (Steinmueller(1999)는, Frenken(2006)의, (2224/08145,, (21/67121),) that did not
fit at the end of the line, so the justified line was spread over its few spaces. This adds invisible break
opportunities inside those tokens and changes nothing else:
  - English names: optional hyphens (Word shows the hyphen only when it breaks the word there):
      Stein-muel-ler, Fren-ken
  - CPC symbols: a no-width optional break (U+200B) after the slash: (2224/|08145, (21/|67121)
The user's yellow marks are removed (they marked the problem lines; they are not part of the thesis).
Audit (exit 1 on failure): every edit is applied exactly once, and the visible text of every paragraph is unchanged
(the paragraph text minus U+200B equals the input).
"""
import copy, sys
import docx
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

ZWSP = '​'
# (unique paragraph start, token, split points inside the token, kind)
EDITS = [
    ('선행연구의 첫째 특징과 관련하여', 'Steinmueller', (5, 9), 'hyphen'),     # Stein-muel-ler
    ('한편 하이브리드 본딩의 핵심 기술 식별과', 'Frenken', (4,), 'hyphen'),          # Fren-ken
    ('기술 클러스터. 표 5에는', '(2224/08145', (6,), 'zwsp'),               # (2224/|08145
    ('이러한 이동은 개별 기호를 넘어', '(21/67121)', (4,), 'zwsp'),             # (21/|67121)
]


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


def main(src, out):
    d = docx.Document(src)
    body = d.element.body
    # the user's yellow marks
    nh = 0
    for h in list(body.iter(qn('w:highlight'))):
        h.getparent().remove(h); nh += 1
    before = [ptext(p) for p in body.iter(qn('w:p'))]
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
    print(f'removed {nh} user highlights; break opportunities added in {done} places; paragraphs with changed text: {len(changed)}')
    sys.exit(0 if done == len(EDITS) and not changed else 1)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
