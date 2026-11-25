import sys; sys.path.insert(0, sys.argv[1]); from common2 import *
P, d = load(sys.argv[2]); E = runpy.run_path(sys.argv[3])['EDITS']; pi0 = int(sys.argv[4])
C = runpy.run_path(sys.argv[5])['CANDS'][pi0]
t0, cap, ind = P[pi0]; t0 = apply(t0, E)
for c in C:
    if c.split('=>')[0] not in t0: print('MISSING', c)
def score(t):
    rs = [r for (s, sl, r) in gapr(t, cap, ind) if r]
    return (sum(1 for r in rs if r >= 1.6), sum(1 for r in rs if r >= 1.5), round(max(rs), 2) if rs else 0)
res = []
for k in range(0, min(len(C), 4) + 1):
    for sub in itertools.combinations(range(len(C)), k):
        t = t0; ok = True
        for i in sub:
            o, n = C[i].split('=>')
            if o not in t: ok = False; break
            t = t.replace(o, n)
        if ok: res.append((score(t), len(sub), sub))
res.sort(key=lambda x: (x[0][0], x[0][1], x[1], x[0][2]))
print('baseline', score(t0))
for sc, n, sub in res[:5]: print(sc, list(sub))
