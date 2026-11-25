import sys, json, re, itertools; sys.path.insert(0, sys.argv[1]); import lay2, lay, docx
d = docx.Document(sys.argv[2]); A = json.load(open(sys.argv[3], encoding='utf8'))
paras = {pi: (t, W, ind) for pi, p, t, W, ind in lay.paras(d)}
def breaks_pdf(lines): 
    acc = 0; out = []
    for l in lines[:-1]: acc += len(re.sub(r'\s', '', l[2]).replace('​','')); out.append(acc)
    return out
def breaks_model(t, W, ind):
    cap = (36073 if W == 72 else 39680) + EXTRA; inds = 2000 if ind else 0
    T = t.replace('​', '​')
    ls = lay2.layout(t, cap, inds); out = []
    for a, b in ls[:-1]:
        out.append(len(re.sub(r'\s', '', t[:b]).replace('​','')))
    return out
def score(verbose=False):
    ok = 0; bad = []
    for k, lines in A.items():
        pi = int(k); t, W, ind = paras[pi]
        bp, bm = breaks_pdf(lines), breaks_model(t, W, ind)
        if bp == bm: ok += 1
        else: bad.append((pi, bp, bm))
    return ok, bad
EXTRA = 0
for ex in (0, 30, 60, 100, 150, 200, 300):
    EXTRA = ex; print(ex, score()[0])
EXTRA = int(sys.argv[4]) if len(sys.argv) > 4 else 0
ok, bad = score(); print('match', ok, 'of', len(A))
for pi, bp, bm in bad:
    t, W, ind = paras[pi]; T0 = re.sub(r'\s', '', t)
    i = next((i for i in range(min(len(bp), len(bm))) if bp[i] != bm[i]), min(len(bp), len(bm)))
    print(pi, 'line', i, 'pdf:', (T0[(bp[i-1] if i else 0):bp[i]][-12:] + '|' + T0[bp[i]:bp[i]+6]) if i < len(bp) else '-', ' model:', (T0[(bm[i-1] if i else 0):bm[i]][-12:] + '|' + T0[bm[i]:bm[i]+6]) if i < len(bm) else '-')
