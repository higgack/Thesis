"""Convergence indicators for the hypothesis-free thesis version (v30).
Reads data/HB.csv + data/app.pkl, writes robustness/results_v30.json and prints tables.
"""
import json, itertools, collections
import numpy as np, pandas as pd, networkx as nx

A = pd.read_pickle('data/app.pkl')
D = None
for enc in ('utf-8', 'cp949', 'latin1'):
    try: D = pd.read_csv('data/HB.csv', encoding=enc); break
    except UnicodeDecodeError: pass
D['sym'] = D.cpc_class_symbol.str.replace(r'\s+', '', regex=True)
D['cls'] = D.sym.str.extract(r'H01L(2[14])/')[0]
D['mg'] = D.sym.str.extract(r'/(\d{2})')[0].astype(int)
D = D.drop_duplicates(['appln_id', 'sym'])
asm = {48, 50, 51, 52, 53, 54, 55, 56, 57, 58, 60, 78}; equip = {67, 68}
def area(r):
    if r.cls == '21':
        return 'P_asm' if r.mg in asm else ('P_equip' if r.mg in equip else 'P_wafer')
    return 'B_method' if r.mg >= 74 else 'B_struct'
D['area'] = D.apply(area, axis=1)
A = A.copy(); A.index = A.index.astype(str) if A.index.name else A.index
key = 'appln_id' if 'appln_id' in A.columns else None
A['aid'] = A.appln_id.astype(str) if key else A.index.astype(str)
D['aid'] = D.appln_id.astype(str)
per = dict(zip(A.aid, A.period)); cat = dict(zip(A.aid, A.tech_cat)); yr = dict(zip(A.aid, A.year))
D['period'] = D.aid.map(per); D['tech_cat'] = D.aid.map(cat)
out = {}
periods = ['–2014', '2015–2019', '2020–2024']

# 1. application-level shares by period
t = []
for p in periods + ['all']:
    S = A if p == 'all' else A[A.period == p]
    t.append(dict(period=p, n=int(len(S)), integrated=float((S.tech_cat == 'Integrated').mean()),
                  process=float((S.tech_cat == 'Process').mean()), assembly=float((S.tech_cat == 'Assembly').mean()),
                  has_process=float(S.has_process.mean()), breadth=float(S.breadth.mean()),
                  n21=float(S.n21.mean()), n24=float(S.n24.mean())))
out['period_shares'] = t

# 2. symbol co-occurrence network by period
def net(ids):
    G = nx.Graph(); pairs = collections.Counter()
    sub = D[D.aid.isin(ids)]
    for aid, g in sub.groupby('aid'):
        syms = sorted(g.sym.unique())
        for s in syms: G.add_node(s, cls=s[4:6])
        for a, b in itertools.combinations(syms, 2):
            pairs[(a, b)] += 1
    for (a, b), w in pairs.items(): G.add_edge(a, b, weight=w)
    cross = [(a, b, d['weight']) for a, b, d in G.edges(data=True) if a[4:6] != b[4:6]]
    wt = sum(d['weight'] for *_, d in G.edges(data=True))
    n21 = sum(1 for n, d in G.nodes(data=True) if d['cls'] == '21'); n24 = G.number_of_nodes() - n21
    # symbol occurrences share -> expected cross-pair share under random pairing
    occ = sub.cls.value_counts(normalize=True)
    exp_cross = 2 * occ.get('21', 0) * occ.get('24', 0)
    return dict(nodes=G.number_of_nodes(), nodes21=n21, nodes24=n24, edges=G.number_of_edges(),
                cross_edges=len(cross), cross_edge_share=len(cross) / max(G.number_of_edges(), 1),
                pair_weight=int(wt), cross_weight=int(sum(w for *_, w in cross)),
                cross_weight_share=sum(w for *_, w in cross) / max(wt, 1), expected_cross_share=float(exp_cross),
                density=nx.density(G)), G
out['network'] = {}
for p in periods + ['all']:
    ids = set(A.aid if p == 'all' else A[A.period == p].aid)
    r, G = net(ids); out['network'][p] = r
    if p == 'all': Gall = G

# 3. H01L21 / H01L24 technical areas vs type (application level: does app carry the area?)
has = D.groupby(['aid', 'area']).size().unstack(fill_value=0).gt(0)
has['tech_cat'] = has.index.map(cat); has['period'] = has.index.map(per)
area_tab = {}
for a in ['P_wafer', 'P_equip', 'P_asm', 'B_struct', 'B_method']:
    S = has[has[a]]
    area_tab[a] = dict(n=int(len(S)), integrated=float((S.tech_cat == 'Integrated').mean()),
                       by_period={p: dict(n=int((S.period == p).sum()), integrated=float((S[S.period == p].tech_cat == 'Integrated').mean()) if (S.period == p).any() else None) for p in periods})
out['area'] = area_tab
# 4. area-pair table within integrated apps
I = has[has.tech_cat == 'Integrated']
out['area_pairs'] = {f'{pa}|{ba}': int((I[pa] & I[ba]).sum()) for pa in ['P_wafer', 'P_equip', 'P_asm'] for ba in ['B_struct', 'B_method']}
out['area_pairs_by_period'] = {p: {f'{pa}|{ba}': int((I[I.period == p][pa] & I[I.period == p][ba]).sum()) for pa in ['P_wafer', 'P_equip', 'P_asm'] for ba in ['B_struct', 'B_method']} for p in periods}
out['n_integrated_by_period'] = {p: int((I.period == p).sum()) for p in periods}
# share of integrated apps whose H01L21 part includes wafer-fab processes, by period
out['integrated_with_wafer'] = {p: float(I[I.period == p].P_wafer.mean()) for p in periods}
out['integrated_with_wafer_all'] = float(I.P_wafer.mean())
# 5. top bridging symbols and pairs (integrated apps)
Di = D[D.tech_cat == 'Integrated']
out['top21_integrated'] = Di[Di.cls == '21'].groupby('sym').aid.nunique().sort_values(ascending=False).head(10).to_dict()
out['top24_integrated'] = Di[Di.cls == '24'].groupby('sym').aid.nunique().sort_values(ascending=False).head(10).to_dict()
cp = collections.Counter()
for aid, g in Di.groupby('aid'):
    for a in g[g.cls == '21'].sym.unique():
        for b in g[g.cls == '24'].sym.unique(): cp[(a, b)] += 1
out['top_cross_pairs'] = [dict(p21=a, p24=b, n=n) for (a, b), n in cp.most_common(10)]
# bridging centrality: H01L21 nodes with most distinct H01L24 neighbours
nb24 = {n: sum(1 for m in Gall.neighbors(n) if m[4:6] == '24') for n, d in Gall.nodes(data=True) if d['cls'] == '21'}
out['top21_by_24_neighbours'] = dict(sorted(nb24.items(), key=lambda kv: -kv[1])[:10])
json.dump(out, open('robustness/results_v30.json', 'w'), ensure_ascii=False, indent=1, default=float)
import pprint; pprint.pprint(out, width=160)

# ---------- robustness of the descriptive convergence findings ----------
import re as _re
A['tnorm'] = A.title.str.lower().str.replace(r'[^a-z0-9 ]', '', regex=True).str.replace(r'\s+', ' ', regex=True).str.strip()
FP = _re.compile(open('tools/fp_pattern.txt').read().strip()) if __import__('os').path.exists('tools/fp_pattern.txt') else None
samples = {'base': A,
           'dedup': A.sort_values('year').groupby('tnorm').head(1),
           'core': A[A.title.str.lower().str.contains(r'hybrid[ -]?bond', regex=True)],
           'le2022': A[A.year <= 2022]}
if FP is not None: samples['clean'] = A[~A.title.str.lower().str.contains(FP)]
rob = {}
for k, S in samples.items():
    ids = set(S.aid); Ii = I[I.index.isin(ids)]
    rob[k] = dict(n=int(len(S)),
                  integrated={p: float((S[S.period == p].tech_cat == 'Integrated').mean()) for p in periods},
                  n_period={p: int((S.period == p).sum()) for p in periods},
                  wafer_in_integrated={p: float(Ii[Ii.period == p].P_wafer.mean()) if (Ii.period == p).any() else None for p in periods},
                  n_int_period={p: int((Ii.period == p).sum()) for p in periods})
out['robust_desc'] = rob
json.dump(out, open('robustness/results_v30.json', 'w'), ensure_ascii=False, indent=1, default=float)
for k, v in rob.items(): print(k, v['n'], {p: round(x, 3) for p, x in v['integrated'].items()}, {p: (None if x is None else round(x, 3)) for p, x in v['wafer_in_integrated'].items()}, v['n_int_period'])

# ---------- composition table: technical areas carried by integrated applications ----------
comp = {}
for p in periods + ['all']:
    S = I if p == 'all' else I[I.period == p]
    comp[p] = dict(n=int(len(S)), **{a: float(S[a].mean()) for a in ['P_wafer', 'P_equip', 'P_asm', 'B_struct', 'B_method']},
                   wafer_only=float((S.P_wafer & ~S.P_equip & ~S.P_asm).mean()),
                   no_wafer=float((~S.P_wafer).mean()))
out['composition'] = comp
json.dump(out, open('robustness/results_v30.json', 'w'), ensure_ascii=False, indent=1, default=float)
for p, v in comp.items(): print(p, {k: (round(x * 100, 1) if isinstance(x, float) else x) for k, v2 in [(0, 0)] for k, x in v.items()})

# ---------- per-period top H01L21 symbols / cross pairs in integrated apps; within-application cross-pair share ----------
tp = {}
for p in periods:
    Dp = Di[Di.period == p]
    c = collections.Counter()
    for aid, g in Dp.groupby('aid'):
        for a in g[g.cls == '21'].sym.unique():
            for b in g[g.cls == '24'].sym.unique(): c[(a, b)] += 1
    tp[p] = dict(top21=Dp[Dp.cls == '21'].groupby('sym').aid.nunique().sort_values(ascending=False).head(5).to_dict(),
                 top_pairs=[f'{a}x{b}:{n}' for (a, b), n in c.most_common(5)])
out['top_by_period'] = tp
wc = []
for aid, g in D.groupby('aid'):
    c = g.cls.values; n = len(c)
    if n >= 2: k = (c == '21').sum(); wc.append((per[aid], k * (n - k) / (n * (n - 1) / 2)))
W = pd.DataFrame(wc, columns=['period', 'x'])
out['within_app_cross'] = {**{p: float(W[W.period == p].x.mean()) for p in periods}, 'all': float(W.x.mean())}
out['within_app_n'] = {**{p: int((W.period == p).sum()) for p in periods}, 'all': int(len(W))}
json.dump(out, open('robustness/results_v30.json', 'w'), ensure_ascii=False, indent=1, default=float)
pprint.pprint(tp, width=160); print(out['within_app_cross'], out['within_app_n'])
