"""Show flagged lines with one line of context, after applying edits. usage: ctx.py SP DOCX paras thr"""
import sys; sys.path.insert(0, sys.argv[1]); import lay, docx, edits
d = docx.Document(sys.argv[2]); want = [int(x) for x in sys.argv[3].split(',')]
thr = float(sys.argv[4]) if len(sys.argv) > 4 else 1.8
E = [(e[0], e[1]) for e in edits.EDITS]
for pi, p, t, W, ind in lay.paras(d):
    if pi not in want: continue
    for o, n in E:
        if o in t: t = t.replace(o, n)
    res = {}
    for dash in (1, 2):
        lay.DASH = dash; res[dash] = lay.gaps(t, W, ind)
    ls = res[1]; flag = set()
    for li, (s, w, n, g) in enumerate(ls):
        g2 = res[2][li][3] if li < len(res[2]) else None
        if (g and g >= thr) or (g2 and g2 >= thr): flag |= {li - 1, li, li + 1}
    print(f'### {pi} W={W}')
    last = -2
    for li in sorted(x for x in flag if 0 <= x < len(ls)):
        if li != last + 1: print('   ...')
        s, w, n, g = ls[li]; g2 = res[2][li][3] if li < len(res[2]) else None
        m = '' if not ((g and g >= thr) or (g2 and g2 >= thr)) else f'  <-- {g and round(g,2)} / d2 {g2 and round(g2,2)}'
        print(f'{li:>3} {W-w:>3} {s}{m}'); last = li
