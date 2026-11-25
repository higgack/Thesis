"""Apply EDITS (list of (old,new)) from edits.py to paragraph texts and report layouts.
usage: tryedits.py SP DOCX [paras-to-show] [thr]"""
import sys, re; sys.path.insert(0, sys.argv[1]); import lay, docx, importlib
import edits; E = edits.EDITS
d = docx.Document(sys.argv[2])
show = [int(x) for x in sys.argv[3].split(',')] if len(sys.argv) > 3 and sys.argv[3] else []
thr = float(sys.argv[4]) if len(sys.argv) > 4 else 1.8
alltext = '\n'.join(p.text for p in d.paragraphs)
E = [(e[0], e[1], len(e) > 2) for e in E]
for old, new, many in E:
    n = alltext.count(old)
    if n == 0 or (n > 1 and not many): print('!! not unique/missing', n, repr(old))
res = {}
for dash in (1, 2):
    lay.DASH = dash; bad = {}
    for pi, p, t, W, ind in lay.paras(d):
        for old, new, many in E:
            if old in t: t = t.replace(old, new)
        if pi in show and dash == 1:
            print(f'### {pi}'); lay.show(t, W, ind, thr); print()
        ls = lay.gaps(t, W, ind)
        b = [round(g, 2) for (s, w, n, g) in ls if g is not None and g >= thr]
        if b: bad[pi] = b
    res[dash] = bad
print('dash1:', sum(len(v) for v in res[1].values()), 'lines in', len(res[1]), 'paras | dash2:', sum(len(v) for v in res[2].values()))
print('remaining dash1:', dict(list(res[1].items())))
print('extra under dash2:', {k: v for k, v in res[2].items() if res[1].get(k) != v})
