"""Search subsets of candidate edits for one paragraph.
usage: opt.py SP DOCX PARA candfile   (candfile: python list CANDS of 'old=>new' strings)
Prints the best few subsets: (max gap dash1, #lines>=1.8 dash1, max gap dash2, #edits)."""
import sys, itertools, runpy; sys.path.insert(0, sys.argv[1]); import lay, docx, edits
d = docx.Document(sys.argv[2]); pi0 = int(sys.argv[3])
C = runpy.run_path(sys.argv[4])['CANDS'][pi0]
E = [(e[0], e[1]) for e in edits.EDITS]
for pi, p, t, W, ind in lay.paras(d):
    if pi != pi0: continue
    for o, n in E: t = t.replace(o, n)
    for c in C:
        if c.split('=>')[0] not in t: print('MISSING', c)
    def score(tt):
        r = []
        for dash in (1, 2):
            lay.DASH = dash
            gs = [g for (s, w, n_, g) in lay.gaps(tt, W, ind) if g is not None]
            r.append((round(max(gs), 2) if gs else 0, sum(1 for g in gs if g >= 1.8)))
        return r
    res = []
    for k in range(0, min(len(C), 6) + 1):
        for sub in itertools.combinations(range(len(C)), k):
            tt = t; ok = True
            for i in sub:
                o, n = C[i].split('=>')
                if o not in tt: ok = False; break
                tt = tt.replace(o, n)
            if not ok: continue
            (m1, c1), (m2, c2) = score(tt)
            res.append(((c1, max(m1, 1.0) if m1 < 1.8 else m1, c2, len(sub), m2), sub, m1))
    res.sort(key=lambda x: (x[0][0], x[0][2], round(max(x[2],1.0),1), x[0][3], x[0][1]))
    for key, sub, m1 in res[:4]:
        print(f'd1 max {m1} n>=1.8 {key[0]} | d2 max {key[4]} n {key[2]} | edits {list(sub)}')
    print('baseline', score(t))
