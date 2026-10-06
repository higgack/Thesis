"""Multi-dimensional convergence indicators for thesis v31 (advisor's framework:
co-occurrence/Jaccard, diversity/balance, technological distance, Rao-Stirling,
E-I index, network centrality, new combinations).
Reads data/HB.csv + data/app.pkl; writes robustness/results_v31.json.
"""
import json, itertools, collections, warnings
import numpy as np, pandas as pd, networkx as nx
import statsmodels.api as sm
warnings.filterwarnings('ignore')
rng = np.random.default_rng(20261006)

# ---------------- data ----------------
A = pd.read_pickle('data/app.pkl').copy()
A['aid'] = A.appln_id.astype(str)
D = None
for enc in ('utf-8', 'cp949', 'latin1'):
    try:
        D = pd.read_csv('data/HB.csv', encoding=enc); break
    except UnicodeDecodeError:
        pass
D['sym'] = D.cpc_class_symbol.str.replace(r'\s+', '', regex=True)
D['cls'] = D.sym.str.extract(r'H01L(2[14])/')[0]
D['mg'] = D.sym.str.extract(r'/(\d{2})')[0].astype(int)
D['aid'] = D.appln_id.astype(str)
D = D.drop_duplicates(['aid', 'sym'])
D = D[D.aid.isin(set(A.aid))]
ASM = {48, 50, 51, 52, 53, 54, 55, 56, 57, 58, 60, 78}; EQUIP = {67, 68}
def area_of(cls, mg):
    if cls == '21':
        return 'P_asm' if mg in ASM else ('P_equip' if mg in EQUIP else 'P_wafer')
    return 'B_method' if mg >= 74 else 'B_struct'
D['area'] = [area_of(c, m) for c, m in zip(D.cls, D.mg)]
D['grp'] = 'H01L' + D.cls + '/' + D.mg.astype(str).str.zfill(2)        # two-digit group (coarse level)
SYMS = {a: sorted(g.sym.unique()) for a, g in D.groupby('aid')}
GRPS = {a: sorted(g.grp.unique()) for a, g in D.groupby('aid')}
AREA = dict(zip(D.sym, D.area)); CLS = dict(zip(D.sym, D.cls))
GCLS = {g: g[4:6] for g in D.grp.unique()}
per = dict(zip(A.aid, A.period)); cat = dict(zip(A.aid, A.tech_cat)); yr = dict(zip(A.aid, A.year)); off = dict(zip(A.aid, A.ccode))
PERIODS = ['–2014', '2015–2019', '2020–2024']
out = {}

# ---------------- distance maps (co-classification profiles) ----------------
def cooc_matrix(units, ids):
    idx = {u: k for k, u in enumerate(units)}
    C = np.zeros((len(units), len(units))); Nn = np.zeros(len(units))
    for a in ids:
        s = [idx[u] for u in UNITS_OF[a] if u in idx]
        for i in s: Nn[i] += 1
        for i, j in itertools.combinations(s, 2):
            C[i, j] += 1; C[j, i] += 1
    return C, Nn, idx
def cosine_rows(C):
    nrm = np.linalg.norm(C, axis=1); nrm[nrm == 0] = np.nan
    S = (C @ C.T) / np.outer(nrm, nrm)
    S = np.nan_to_num(S, nan=0.0); np.fill_diagonal(S, 1.0)
    return S
def build_map(level, ids):
    global UNITS_OF
    UNITS_OF = SYMS if level == 'sym' else GRPS
    units = sorted({u for a in ids for u in UNITS_OF[a]})
    C, Nn, idx = cooc_matrix(units, ids)
    Dist2 = 1 - cosine_rows(C)                                   # second-order cosine distance (main)
    J1 = C / (Nn[:, None] + Nn[None, :] - C); np.fill_diagonal(J1, 1.0)
    return dict(units=units, idx=idx, C=C, N=Nn, d2=np.clip(Dist2, 0, 1), dj=1 - J1)
ALL = list(A.aid)
MAP = build_map('sym', ALL)
out['map'] = dict(n_symbols=len(MAP['units']),
                  isolated=int((MAP['C'].sum(1) == 0).sum()))

def app_measures(a, M, level='sym', dkey='d2'):
    units = SYMS[a] if level == 'sym' else GRPS[a]
    cl = CLS if level == 'sym' else GCLS
    n = len(units); ii = [M['idx'][u] for u in units if u in M['idx']]
    d = M[dkey]
    res = dict(n=n)
    if len(ii) >= 2:
        pairs = list(itertools.combinations(range(len(ii)), 2))
        dd = [d[ii[p], ii[q]] for p, q in pairs]
        res['disparity'] = float(np.mean(dd))
        p = 1.0 / len(ii)
        res['rs'] = float(2 * p * p * np.sum(dd))              # RS = sum_i sum_j p_i p_j d_ij
        cross = [d[ii[p], ii[q]] for p, q in pairs if cl[units[p]] != cl[units[q]]]
        within = len(pairs) - len(cross)
        res['cross_disparity'] = float(np.mean(cross)) if cross else np.nan
        res['ei_app'] = (len(cross) - within) / len(pairs)
    else:
        res.update(disparity=np.nan, rs=0.0, cross_disparity=np.nan, ei_app=np.nan)
    return res

# area entropy / Simpson and class balance
def area_div(a):
    ar = [AREA[s] for s in SYMS[a]]; n = len(ar)
    p = np.array(list(collections.Counter(ar).values())) / n
    H = float(-(p * np.log(p)).sum()); simpson = float(1 - (p ** 2).sum())
    s21 = sum(CLS[s] == '21' for s in SYMS[a]) / n
    bal = 0.0 if s21 in (0, 1) else float(-(s21 * np.log(s21) + (1 - s21) * np.log(1 - s21)) / np.log(2))
    return dict(H_area=H, simpson_area=simpson, n_areas=len(set(ar)), class_balance=bal)

rows = []
for a in ALL:
    r = dict(aid=a, period=per[a], tech_cat=cat[a], year=yr[a], ccode=off[a])
    r.update(app_measures(a, MAP)); r.update(area_div(a))
    rows.append(r)
R = pd.DataFrame(rows)
R.to_pickle('data/app_measures_v31.pkl')   # per-application values stay with the git-ignored raw data

def mean_by(df, col, by='period'):
    g = df.groupby(by)[col].mean()
    return {k: float(v) for k, v in g.items()}

# ---------------- 1. co-occurrence strength: Jaccard / cosine ----------------
cls_j = {}
for p in PERIODS + ['all']:
    S = A if p == 'all' else A[A.period == p]
    I = int((S.tech_cat == 'Integrated').sum()); P = int((S.tech_cat == 'Process').sum()); As = int((S.tech_cat == 'Assembly').sum())
    n21, n24 = I + P, I + As
    cls_j[p] = dict(N=int(len(S)), I=I, N21=n21, N24=n24, jaccard=I / (n21 + n24 - I), cosine=I / np.sqrt(n21 * n24))
out['class_jaccard'] = cls_j
has = D.groupby(['aid', 'area']).size().unstack(fill_value=0).gt(0)
has['B'] = has[['B_struct', 'B_method']].any(axis=1)
has['period'] = has.index.map(per)
aj = {}
for p in PERIODS + ['all']:
    S = has if p == 'all' else has[has.period == p]
    aj[p] = {}
    for a_ in ['P_wafer', 'P_equip', 'P_asm']:
        both = int((S[a_] & S.B).sum()); na = int(S[a_].sum()); nb = int(S.B.sum())
        aj[p][a_] = dict(both=both, n_area=na, n_B=nb, jaccard=both / (na + nb - both), cosine=both / np.sqrt(na * nb) if na and nb else np.nan,
                         p_B_given_area=both / na if na else np.nan)
out['area_jaccard'] = aj
# symbol-pair Jaccard (cross-class), within period, min count 5
pj = {}
for p in PERIODS + ['all']:
    ids = ALL if p == 'all' else list(A[A.period == p].aid)
    cnt = collections.Counter(); pc = collections.Counter()
    for a in ids:
        for s in SYMS[a]: cnt[s] += 1
        for s, t in itertools.combinations(SYMS[a], 2):
            if CLS[s] != CLS[t]:
                pc[(s, t) if CLS[s] == '21' else (t, s)] += 1
    lst = [(s, t, n, n / (cnt[s] + cnt[t] - n)) for (s, t), n in pc.items() if n >= 5]
    lst.sort(key=lambda x: -x[3])
    pj[p] = [dict(p21=s, p24=t, n=n, n21=cnt[s], n24=cnt[t], jaccard=j) for s, t, n, j in lst[:8]]
out['pair_jaccard_top'] = pj

# ---------------- 2. diversity / balance ----------------
out['diversity'] = {
    'H_area_by_type': mean_by(R, 'H_area', 'tech_cat'),
    'H_area_by_period': mean_by(R, 'H_area'),
    'H_area_integrated_by_period': mean_by(R[R.tech_cat == 'Integrated'], 'H_area'),
    'simpson_area_integrated_by_period': mean_by(R[R.tech_cat == 'Integrated'], 'simpson_area'),
    'n_areas_integrated_by_period': mean_by(R[R.tech_cat == 'Integrated'], 'n_areas'),
    'class_balance_integrated_by_period': mean_by(R[R.tech_cat == 'Integrated'], 'class_balance'),
    'class_balance_integrated_all': float(R[R.tech_cat == 'Integrated'].class_balance.mean()),
    'H_area_integrated_all': float(R[R.tech_cat == 'Integrated'].H_area.mean()),
}
# field-level (period) concentration over symbols and areas
fl = {}
for p in PERIODS + ['all']:
    sub = D if p == 'all' else D[D.aid.map(per) == p]
    ps = sub.sym.value_counts(normalize=True).values; pa = sub.area.value_counts(normalize=True)
    fl[p] = dict(HHI_sym=float((ps ** 2).sum()), H_sym=float(-(ps * np.log(ps)).sum()), n_sym=int(sub.sym.nunique()),
                 H_area=float(-(pa * np.log(pa)).sum()), area_shares={k: float(v) for k, v in pa.items()})
out['field_diversity'] = fl

# ---------------- 3–4. distance, Rao-Stirling ----------------
out['distance'] = {
    'disparity_by_type': mean_by(R, 'disparity', 'tech_cat'),
    'rs_by_type': mean_by(R, 'rs', 'tech_cat'),
    'cross_disparity_integrated_by_period': mean_by(R[R.tech_cat == 'Integrated'], 'cross_disparity'),
    'rs_integrated_by_period': mean_by(R[R.tech_cat == 'Integrated'], 'rs'),
    'disparity_integrated_by_period': mean_by(R[R.tech_cat == 'Integrated'], 'disparity'),
    'rs_all_by_period': mean_by(R, 'rs'),
    'cross_disparity_integrated_all': float(R[R.tech_cat == 'Integrated'].cross_disparity.mean()),
    'rs_integrated_all': float(R[R.tech_cat == 'Integrated'].rs.mean()),
}
# mean distance from each H01L21 area to H01L24 symbols (frequency-weighted, pooled map)
u = MAP['units']; Nn = MAP['N']; d2 = MAP['d2']
i24 = [k for k, s in enumerate(u) if CLS[s] == '24']
area_dist = {}
for a_ in ['P_wafer', 'P_equip', 'P_asm']:
    ia = [k for k, s in enumerate(u) if AREA[s] == a_]
    w = np.outer(Nn[ia], Nn[i24]); area_dist[a_] = float((d2[np.ix_(ia, i24)] * w).sum() / w.sum())
out['area_to_bonding_distance'] = area_dist
# cross-disparity in integrated apps by H01L21 content (wafer vs non-wafer)
Ri = R[R.tech_cat == 'Integrated'].copy()
Ri['has_wafer'] = Ri.aid.map(lambda a: any(AREA[s] == 'P_wafer' for s in SYMS[a]))
out['cross_disparity_by_wafer'] = {str(k): float(v) for k, v in Ri.groupby('has_wafer').cross_disparity.mean().items()}
# leave-period-out map: distances for period t from apps outside t
lpo = {}
for p in PERIODS:
    M = build_map('sym', list(A[A.period != p].aid))
    vals = [app_measures(a, M)['cross_disparity'] for a in A[(A.period == p) & (A.tech_cat == 'Integrated')].aid]
    vals = [v for v in vals if not np.isnan(v)]
    lpo[p] = dict(mean=float(np.mean(vals)), n=len(vals))
out['cross_disparity_lpo'] = lpo
# coarse level (two-digit groups)
MAPG = build_map('grp', ALL)
RG = pd.DataFrame([dict(aid=a, period=per[a], tech_cat=cat[a], **app_measures(a, MAPG, 'grp')) for a in ALL])
out['coarse'] = dict(cross_disparity_integrated_by_period=mean_by(RG[RG.tech_cat == 'Integrated'], 'cross_disparity'),
                     rs_integrated_by_period=mean_by(RG[RG.tech_cat == 'Integrated'], 'rs'))
# Jaccard-distance variant (first-order)
RJ = pd.DataFrame([dict(aid=a, period=per[a], tech_cat=cat[a], **app_measures(a, MAP, 'sym', 'dj')) for a in ALL])
out['jaccard_distance_variant'] = dict(cross_disparity_integrated_by_period=mean_by(RJ[RJ.tech_cat == 'Integrated'], 'cross_disparity'))

# ---------------- 5–6. network: E-I, density, clustering, centrality ----------------
def period_graph(ids, level='sym'):
    G = nx.Graph(); units_of = SYMS if level == 'sym' else GRPS; cl = CLS if level == 'sym' else GCLS
    for a in ids:
        us = units_of[a]
        for s in us: G.add_node(s, cls=cl[s], area=AREA.get(s, ''))
        for s, t in itertools.combinations(us, 2):
            if G.has_edge(s, t): G[s][t]['weight'] += 1
            else: G.add_edge(s, t, weight=1)
    return G
def ei(G, labels=None, weighted=False):
    lab = labels if labels is not None else {n: d['cls'] for n, d in G.nodes(data=True)}
    E = I = 0.0
    for s, t, d in G.edges(data=True):
        w = d['weight'] if weighted else 1
        if lab[s] != lab[t]: E += w
        else: I += w
    return (E - I) / (E + I) if E + I else np.nan
def ei_perm(G, B=2000):
    nodes = list(G.nodes()); labs = np.array([G.nodes[n]['cls'] for n in nodes]); obs = ei(G)
    sims = []
    for _ in range(B):
        perm = rng.permutation(labs); sims.append(ei(G, dict(zip(nodes, perm))))
    sims = np.array(sims)
    return dict(observed=float(obs), expected=float(sims.mean()), p_lower=float((sims <= obs).mean()))
net = {}; brokers = {}
for p in PERIODS + ['all']:
    ids = ALL if p == 'all' else list(A[A.period == p].aid)
    G = period_graph(ids)
    Gc = G.subgraph(max(nx.connected_components(G), key=len)).copy()
    bt = nx.betweenness_centrality(G, normalized=True)
    deg = dict(G.degree())
    tot = sum(bt.values())
    bshare = {a_: float(sum(v for n, v in bt.items() if G.nodes[n]['area'] == a_) / tot) for a_ in ['P_wafer', 'P_equip', 'P_asm', 'B_struct', 'B_method']}
    eip = ei_perm(G, 2000)
    net[p] = dict(nodes=G.number_of_nodes(), edges=G.number_of_edges(), density=nx.density(G),
                  clustering=nx.average_clustering(G), giant_share=Gc.number_of_nodes() / G.number_of_nodes(),
                  ei=eip['observed'], ei_expected=eip['expected'], ei_p=eip['p_lower'],
                  ei_weighted=float(ei(G, weighted=True)),
                  ei_app_mean=float(R[(R.period == p) if p != 'all' else slice(None)].ei_app.mean()) if p != 'all' else float(R.ei_app.mean()),
                  betweenness_share=bshare)
    brokers[p] = dict(top_betweenness=[(n, round(v, 4), G.nodes[n]['area']) for n, v in sorted(bt.items(), key=lambda kv: -kv[1])[:8]],
                      top21_betweenness=[(n, round(v, 4), G.nodes[n]['area']) for n, v in sorted(bt.items(), key=lambda kv: -kv[1]) if G.nodes[n]['cls'] == '21'][:6],
                      top_degree=[(n, deg[n], G.nodes[n]['area']) for n in sorted(deg, key=lambda k: -deg[k])[:6]])
out['network'] = net; out['brokers'] = brokers
# coarse-level E-I
out['network_coarse'] = {p: dict(ei=float(ei(period_graph(ALL if p == 'all' else list(A[A.period == p].aid), 'grp')))) for p in PERIODS + ['all']}

# ---------------- 7. dynamics: new cross-class combinations, persistence ----------------
def cross_pairs(ids):
    s = set()
    for a in ids:
        for x, y in itertools.combinations(SYMS[a], 2):
            if CLS[x] != CLS[y]: s.add((x, y) if CLS[x] == '21' else (y, x))
    return s
seen = set(); dyn = {}
pairs_by_p = {p: cross_pairs(list(A[A.period == p].aid)) for p in PERIODS}
for p in PERIODS:
    cur = pairs_by_p[p]; new = cur - seen
    ints = list(A[(A.period == p) & (A.tech_cat == 'Integrated')].aid)
    with_new = sum(1 for a in ints if any(((x, y) if CLS[x] == '21' else (y, x)) in new for x, y in itertools.combinations(SYMS[a], 2) if CLS[x] != CLS[y]))
    new_area = collections.Counter(AREA[x] for x, _ in new)
    dyn[p] = dict(distinct_cross_pairs=len(cur), new_pairs=len(new), new_share=len(new) / len(cur) if cur else np.nan,
                  integrated=len(ints), integrated_with_new=with_new, integrated_with_new_share=with_new / len(ints) if ints else np.nan,
                  new_pairs_per_integrated=len(new) / len(ints) if ints else np.nan,
                  new_by_area={k: int(v) for k, v in sorted(new_area.items())},
                  new_by_area_share={k: v / len(new) for k, v in sorted(new_area.items())} if new else {})
    seen |= cur
# persistence: pairs present in 2015–2019 that reappear in 2020–2024
p1, p2 = pairs_by_p['2015–2019'], pairs_by_p['2020–2024']
new1519 = p1 - pairs_by_p['–2014']
dyn['persistence'] = dict(pairs_1519=len(p1), reappear_2024=len(p1 & p2), share=len(p1 & p2) / len(p1),
                          new1519=len(new1519), new1519_reappear=len(new1519 & p2), new1519_share=len(new1519 & p2) / len(new1519),
                          by_area={a_: dict(n=sum(AREA[x] == a_ for x, _ in p1), reappear=sum(AREA[x] == a_ for x, _ in (p1 & p2))) for a_ in ['P_wafer', 'P_equip', 'P_asm']})
# yearly cumulative distinct cross pairs (for a figure)
cum = set(); yearly = {}
for y in sorted(A.year.unique()):
    cur = cross_pairs(list(A[A.year == y].aid)); newy = cur - cum; cum |= cur
    yearly[int(y)] = dict(new=len(newy), cumulative=len(cum), integrated=int(((A.year == y) & (A.tech_cat == 'Integrated')).sum()))
dyn['yearly'] = yearly
out['dynamics'] = dyn

# ---------------- RQ3: offices ----------------
OFF = ['US', 'CN', 'KR', 'TW', 'JP', 'EP', 'WO', 'Other']
offt = {}
for o in OFF:
    S = R[R.ccode == o]; Si = S[S.tech_cat == 'Integrated']
    offt[o] = dict(n=int(len(S)), H_area=float(S.H_area.mean()), rs=float(S.rs.mean()), disparity=float(S.disparity.mean()),
                   cross_disparity_int=float(Si.cross_disparity.mean()) if len(Si) else np.nan, class_balance_int=float(Si.class_balance.mean()) if len(Si) else np.nan)
out['offices'] = offt
R['yc'] = R.year - 1968
R['ccode'] = pd.Categorical(R.ccode, categories=OFF)
def ols(df, y):
    X = sm.add_constant(pd.get_dummies(df[['yc', 'ccode']], columns=['ccode'], drop_first=True).astype(float))
    m = sm.OLS(df[y].astype(float), X, missing='drop').fit(cov_type='HC1')
    return {k: dict(b=float(m.params[k]), se=float(m.bse[k]), p=float(m.pvalues[k])) for k in ['yc', 'ccode_CN', 'ccode_KR', 'ccode_TW', 'ccode_EP', 'ccode_WO']} | dict(n=int(m.nobs), r2=float(m.rsquared))
out['office_ols'] = dict(H_area=ols(R, 'H_area'), rs=ols(R, 'rs'), disparity=ols(R[R.n >= 2], 'disparity'),
                         cross_disparity_int=ols(R[R.tech_cat == 'Integrated'], 'cross_disparity'))

json.dump(out, open('robustness/results_v31.json', 'w'), ensure_ascii=False, indent=1, default=lambda o: None if (isinstance(o, float) and np.isnan(o)) else (o.item() if hasattr(o, 'item') else str(o)))
print('done')

# ================= supplementary checks =================
A['tnorm'] = A.title.str.lower().str.replace(r'[^a-z0-9 ]', '', regex=True).str.replace(r'\s+', ' ', regex=True).str.strip()
tgrp = dict(zip(A.aid, pd.factorize(A.tnorm)[0]))
# (a) period differences in cross-distance and RS among integrated applications
Ri = R[R.tech_cat == 'Integrated'].copy(); Ri['grp'] = Ri.aid.map(tgrp)
def period_ols(df, y):
    X = sm.add_constant(pd.get_dummies(df.period, drop_first=False)[['–2014', '2020–2024']].astype(float))  # base = 2015–2019
    m1 = sm.OLS(df[y].astype(float), X, missing='drop').fit(cov_type='HC1')
    ok = df[y].notna()
    m2 = sm.OLS(df.loc[ok, y].astype(float), X[ok]).fit(cov_type='cluster', cov_kwds={'groups': df.loc[ok, 'grp']})
    return {k: dict(b=float(m1.params[k]), p_hc1=float(m1.pvalues[k]), p_cluster=float(m2.pvalues[k])) for k in ['–2014', '2020–2024']}
out['period_tests_integrated'] = dict(cross_disparity=period_ols(Ri, 'cross_disparity'), rs=period_ols(Ri, 'rs'),
                                      H_area=period_ols(Ri, 'H_area'), class_balance=period_ols(Ri, 'class_balance'))
# (b) cross-pair distance by H01L21 area, by period (pairs observed in integrated applications)
cp = []
for a in A[A.tech_cat == 'Integrated'].aid:
    for x, y in itertools.combinations(SYMS[a], 2):
        if CLS[x] != CLS[y]:
            s21, s24 = (x, y) if CLS[x] == '21' else (y, x)
            cp.append(dict(period=per[a], area=AREA[s21], d=MAP['d2'][MAP['idx'][s21], MAP['idx'][s24]]))
CP = pd.DataFrame(cp)
out['cross_pair_distance_by_area'] = {p: {a_: dict(mean=float(g.d.mean()), n=int(len(g))) for a_, g in CP[CP.period == p].groupby('area')} for p in PERIODS}
out['cross_pair_share_by_area'] = {p: {a_: float(v) for a_, v in CP[CP.period == p].area.value_counts(normalize=True).items()} for p in PERIODS}
# (c) coarse-level E-I with permutation
out['network_coarse'] = {p: ei_perm(period_graph(ALL if p == 'all' else list(A[A.period == p].aid), 'grp'), 2000) for p in PERIODS + ['all']}
# (d) robustness samples
FP = open('tools/fp_pattern.txt').read().strip()
SAMPLES = {'clean': A[~A.title.str.lower().str.contains(FP, regex=True)],
           'core': A[A.title.str.lower().str.contains(r'hybrid[ -]?bond', regex=True)],
           'le2022': A[A.year <= 2022]}
rob = {}
for k, S in SAMPLES.items():
    rk = dict(n=int(len(S)))
    hs = has[has.index.isin(set(S.aid))]
    rk['area_jaccard'] = {}
    for p in ['2015–2019', '2020–2024']:
        Sp = hs[hs.period == p]
        rk['area_jaccard'][p] = {a_: float((Sp[a_] & Sp.B).sum() / (Sp[a_].sum() + Sp.B.sum() - (Sp[a_] & Sp.B).sum())) for a_ in ['P_wafer', 'P_equip', 'P_asm']}
    rk['betweenness_share'] = {}; rk['ei'] = {}
    for p in PERIODS:
        ids = list(S[S.period == p].aid)
        if len(ids) < 10: continue
        G = period_graph(ids); bt = nx.betweenness_centrality(G, normalized=True); tot = sum(bt.values())
        rk['betweenness_share'][p] = {a_: float(sum(v for n, v in bt.items() if G.nodes[n]['area'] == a_) / tot) for a_ in ['P_wafer', 'P_equip', 'P_asm']}
        rk['ei'][p] = ei_perm(G, 1000)
    Rs = R[R.aid.isin(set(S.aid)) & (R.tech_cat == 'Integrated')]
    rk['cross_disparity'] = mean_by(Rs, 'cross_disparity')
    rob[k] = rk
out['robust_v31'] = rob
# (e) pair-level Jaccard after keeping one application per title
dedup_ids = set(A.sort_values('year').groupby('tnorm').head(1).aid)
pjd = {}
for p in ['2015–2019', '2020–2024']:
    ids = [a for a in A[A.period == p].aid if a in dedup_ids]
    cnt = collections.Counter(); pc = collections.Counter()
    for a in ids:
        for s in SYMS[a]: cnt[s] += 1
        for s, t in itertools.combinations(SYMS[a], 2):
            if CLS[s] != CLS[t]: pc[(s, t) if CLS[s] == '21' else (t, s)] += 1
    lst = sorted([(s, t, n, n / (cnt[s] + cnt[t] - n)) for (s, t), n in pc.items() if n >= 5], key=lambda x: -x[3])
    pjd[p] = [dict(p21=s, p24=t, n=n, jaccard=round(j, 3)) for s, t, n, j in lst[:6]]
out['pair_jaccard_top_dedup'] = pjd
# (f) how many distinct titles stand behind the top full-sample pair (21/44 x 24/28)
ids_pair = [a for a in ALL if 'H01L21/44' in SYMS[a] and 'H01L24/28' in SYMS[a]]
out['pair_2144_2428'] = dict(n_apps=len(ids_pair), n_titles=int(A[A.aid.isin(ids_pair)].tnorm.nunique()),
                             offices=A[A.aid.isin(ids_pair)].ccode.value_counts().to_dict(), years=sorted(A[A.aid.isin(ids_pair)].year.unique().tolist()))
# office OLS with title-cluster SEs (robustness)
def ols_cl(df, y):
    df = df[df[y].notna()].copy(); df['grp'] = df.aid.map(tgrp)
    X = sm.add_constant(pd.get_dummies(df[['yc', 'ccode']], columns=['ccode'], drop_first=True).astype(float))
    m = sm.OLS(df[y].astype(float), X).fit(cov_type='cluster', cov_kwds={'groups': df.grp})
    return dict(b=float(m.params['ccode_CN']), p=float(m.pvalues['ccode_CN']))
out['office_ols_cluster'] = dict(H_area=ols_cl(R, 'H_area'), rs=ols_cl(R, 'rs'), disparity=ols_cl(R[R.n >= 2], 'disparity'))
json.dump(out, open('robustness/results_v31.json', 'w'), ensure_ascii=False, indent=1, default=lambda o: None if (isinstance(o, float) and np.isnan(o)) else (o.item() if hasattr(o, 'item') else str(o)))
for k in ['period_tests_integrated', 'cross_pair_distance_by_area', 'cross_pair_share_by_area', 'network_coarse', 'robust_v31', 'pair_jaccard_top_dedup', 'pair_2144_2428', 'office_ols_cluster']:
    print('==', k); print(json.dumps(out[k], ensure_ascii=False, default=str)[:2500])

# (g) coarse-level betweenness share by H01L21 area (two-digit groups)
GAREA = {g: area_of(g[4:6], int(g[-2:])) for g in D.grp.unique()}
cb = {}
for p in PERIODS:
    G = period_graph(list(A[A.period == p].aid), 'grp'); bt = nx.betweenness_centrality(G, normalized=True); tot = sum(bt.values())
    cb[p] = {a_: float(sum(v for n, v in bt.items() if GAREA[n] == a_) / tot) for a_ in ['P_wafer', 'P_equip', 'P_asm']}
out['coarse_betweenness_share'] = cb
json.dump(out, open('robustness/results_v31.json', 'w'), ensure_ascii=False, indent=1, default=lambda o: None if (isinstance(o, float) and np.isnan(o)) else (o.item() if hasattr(o, 'item') else str(o)))
print('coarse betweenness', json.dumps(cb))

# (h) derived values quoted in the text
pa = out['dynamics']['persistence']['by_area']
out['persistence_share_by_area'] = {a_: v['reappear'] / v['n'] for a_, v in pa.items()}
gaps = {'base': {p: out['network'][p]['ei'] - out['network'][p]['ei_expected'] for p in PERIODS},
        'coarse': {p: out['network_coarse'][p]['observed'] - out['network_coarse'][p]['expected'] for p in PERIODS}}
for k in ['clean', 'core', 'le2022']:
    gaps[k] = {p: v['observed'] - v['expected'] for p, v in out['robust_v31'][k]['ei'].items()}
out['ei_gaps'] = gaps
out['ei_gap_p'] = {'base': {p: out['network'][p]['ei_p'] for p in PERIODS}, 'coarse': {p: out['network_coarse'][p]['p_lower'] for p in PERIODS},
                   **{k: {p: v['p_lower'] for p, v in out['robust_v31'][k]['ei'].items()} for k in ['clean', 'core', 'le2022']}}
out['coarse_groups'] = dict(total=int(D.grp.nunique()), h21=int(D[D.cls == '21'].grp.nunique()), h24=int(D[D.cls == '24'].grp.nunique()))
out['n_apps_ge2_symbols'] = int((R.n >= 2).sum())
json.dump(out, open('robustness/results_v31.json', 'w'), ensure_ascii=False, indent=1, default=lambda o: None if (isinstance(o, float) and np.isnan(o)) else (o.item() if hasattr(o, 'item') else str(o)))
print('persistence', {k: round(v, 3) for k, v in out['persistence_share_by_area'].items()})
print('gaps', {k: {p: round(x, 3) for p, x in v.items()} for k, v in gaps.items()})
print('coarse groups', out['coarse_groups'], 'apps>=2', out['n_apps_ge2_symbols'])
