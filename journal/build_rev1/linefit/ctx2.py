import sys; sys.path.insert(0, sys.argv[1]); from common2 import *
P, d = load(sys.argv[2]); E = runpy.run_path(sys.argv[3])['EDITS']
want = [int(x) for x in sys.argv[4].split(',')]; full = len(sys.argv) > 5
for pi in want:
    t, cap, ind = P[pi]; t = apply(t, E); rs = gapr(t, cap, ind)
    flag = {i for i, (s, sl, r) in enumerate(rs) if r and r >= THR}
    show = set(range(len(rs))) if full else {j for i in flag for j in (i-1, i, i+1) if 0 <= j < len(rs)}
    print(f'### {pi}')
    last = -2
    for i in sorted(show):
        if i != last + 1: print('   ...')
        s, sl, r = rs[i]; print(f'{i:>3} {sl:>6} {s}' + (f'  <-- {r:.2f}' if r and r >= THR else '')); last = i
