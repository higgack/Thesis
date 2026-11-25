import sys; sys.path.insert(0, sys.argv[1]); from common2 import *
P, d = load(sys.argv[2]); E = runpy.run_path(sys.argv[3])['EDITS']
thr = float(sys.argv[4]) if len(sys.argv) > 4 else THR
alltext = '\n'.join(p.text for p in d.paragraphs)
for e in E:
    n = alltext.count(e[0])
    if n == 0 or (n > 1 and len(e) < 3): print('!! not unique/missing', n, repr(e[0]))
bad = {}
for pi, (t, cap, ind) in P.items():
    t = apply(t, E)
    rs = [round(r, 2) for (s, sl, r) in gapr(t, cap, ind) if r and r >= thr]
    if rs: bad[pi] = rs
print('lines >=', thr, ':', sum(len(v) for v in bad.values()), 'in', len(bad), 'paras'); print(bad)
