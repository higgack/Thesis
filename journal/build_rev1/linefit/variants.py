"""usage: variants.py SP DOCX PARA 'old=>new' ['old=>new' ...]  (each arg is one variant; ' && ' joins several replacements)"""
import sys; sys.path.insert(0, sys.argv[1]); import lay, docx, edits
d = docx.Document(sys.argv[2]); pi0 = int(sys.argv[3])
E = [(e[0], e[1]) for e in edits.EDITS]
for pi, p, t, W, ind in lay.paras(d):
    if pi != pi0: continue
    for o, n in E: t = t.replace(o, n)
    for v in sys.argv[4:]:
        tt = t
        for rep in v.split(' && '):
            o, n = rep.split('=>')
            if o not in tt: print('MISSING', o); continue
            tt = tt.replace(o, n)
        worst = []
        for dash in (1, 2):
            lay.DASH = dash
            gs = [g for (s, w, n_, g) in lay.gaps(tt, W, ind) if g is not None]
            worst.append(round(max(gs), 2) if gs else 0)
        print(worst, v)
        if '-v' in sys.argv[1:2]: pass
