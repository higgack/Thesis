"""Technology cluster, technology network and the core-technology hypothesis (H3) for thesis v32.
Reads data/HB.csv + data/app.pkl; writes robustness/results_v32.json.

Cluster  = H01L21/H01L24 symbols ranked by the number of applications carrying them.
Network  = symbol co-classification pairs weighted by the number of applications carrying both.
Core     = H01L24 symbols carried by 200 or more applications (24/80, 24/08, 24/05, 24/03).
H3 test  = logit of integrated vs. assembly-only among applications with H01L24 symbols,
           core x filing period interaction, office dummies as controls.
"""
import json, itertools, collections, warnings
import numpy as np, pandas as pd, networkx as nx
import statsmodels.api as sm
warnings.filterwarnings('ignore')

A = pd.read_pickle('data/app.pkl').copy()
A['aid'] = A.appln_id.astype(str)
for enc in ('utf-8', 'cp949', 'latin1'):
    try:
        D = pd.read_csv('data/HB.csv', encoding=enc); break
    except UnicodeDecodeError:
        pass
D['sym'] = D.cpc_class_symbol.str.replace(r'\s+', '', regex=True).str.replace('H01L', '', regex=False)
D['aid'] = D.appln_id.astype(str)
D = D.drop_duplicates(['aid', 'sym'])
D = D[D.aid.isin(set(A.aid))]
SYMS = D.groupby('aid').sym.apply(lambda s: sorted(set(s))).to_dict()
N = len(A)
out = {'n_apps': N, 'n_symbols': int(D.sym.nunique())}

# ---------------- (a) technology cluster ----------------
cnt = D.groupby('sym').aid.nunique().sort_values(ascending=False, kind='mergesort')
cnt = cnt.iloc[np.lexsort((cnt.index.values, -cnt.values))]
out['cluster_top'] = [dict(sym=s, n=int(v), share=float(v / N)) for s, v in cnt.head(12).items()]
CORE = sorted(s for s, v in cnt.items() if v >= 200 and s.startswith('24/'))
out['core_symbols'] = CORE
out['n_symbols_ge200'] = int((cnt >= 200).sum())
out['next_after_core'] = dict(sym=cnt.index[len(CORE)], n=int(cnt.iloc[len(CORE)]))

# ---------------- (b) technology network ----------------
pc = collections.Counter()
for a, s in SYMS.items():
    for p in itertools.combinations(s, 2):
        pc[p] += 1
G = nx.Graph()
for (u, v), w in pc.items():
    G.add_edge(u, v, weight=w)
pairs = sorted(pc.items(), key=lambda kv: (-kv[1], kv[0]))
out['pairs_top'] = [dict(a=u, b=v, w=int(w)) for (u, v), w in pairs[:10]]
deg = dict(G.degree()); stren = dict(G.degree(weight='weight'))
rank = sorted(G.nodes, key=lambda n: (-stren[n], n))
out['strength_top'] = [dict(sym=n, strength=int(stren[n]), degree=int(deg[n])) for n in rank[:8]]
out['network'] = dict(nodes=G.number_of_nodes(), edges=G.number_of_edges(), density=float(nx.density(G)),
                      total_weight=int(sum(pc.values())))
core_pairs = [pc[tuple(sorted(p))] for p in itertools.combinations(CORE, 2)]
out['core_clique'] = dict(pairs=len(core_pairs), min_w=int(min(core_pairs)), max_w=int(max(core_pairs)),
                          share_of_top10=int(sum(1 for d in out['pairs_top'][:6] if d['a'] in CORE and d['b'] in CORE)))
# core-to-process ties by period: H01L21 symbols most often co-assigned with a core symbol
A['core'] = [int(bool(set(SYMS[a]) & set(CORE))) for a in A.aid]
A['ncore'] = [len(set(SYMS[a]) & set(CORE)) for a in A.aid]
A['n24nc'] = A.n24 - A.ncore
PERIODS = ['–2014', '2015–2019', '2020–2024']
ties = {}
for p in PERIODS:
    ids = A[(A.period == p) & (A.core == 1) & (A.tech_cat == 'Integrated')].aid
    c = collections.Counter(s for a in ids for s in SYMS[a] if s.startswith('21/'))
    top = sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    ties[p] = dict(n_core_integrated=int(len(ids)), top=[dict(sym=s, n=int(v)) for s, v in top])
out['core_process_ties'] = ties

# ---------------- (c) H3: core x period among bonding applications ----------------
OFF = ['CN', 'KR', 'TW', 'JP', 'EP', 'WO', 'Other']
FP = open('tools/fp_pattern.txt').read().strip()
A['tnorm'] = A.title.str.lower().str.replace(r'[^a-z0-9 ]', '', regex=True).str.replace(r'\s+', ' ', regex=True).str.strip()
ASM = {48, 50, 51, 52, 53, 54, 55, 56, 57, 58, 60, 78}
strict = set(a for a, s in SYMS.items() if any(x.startswith('21/') and int(x[3:5]) not in ASM for x in s))
A['tcs'] = np.where(A.aid.isin(strict) & (A.has_assembly == 1), 'Integrated',
                    np.where(A.has_assembly == 1, 'Assembly', 'Process'))

def design(df, extra=()):
    X = pd.DataFrame(index=df.index)
    X['core'] = df.core.astype(float)
    X['p2'] = (df.period == '2015–2019').astype(float); X['p3'] = (df.period == '2020–2024').astype(float)
    X['core_p2'] = X.core * X.p2; X['core_p3'] = X.core * X.p3
    for e in extra:
        X[e] = df[e].astype(float)
    for o in OFF:
        X['cc_' + o] = (df.ccode == o).astype(float)
    X = X.loc[:, X.std() > 0]
    return sm.add_constant(X)

def h3(df, y='integ', extra=(), cov='HC1', groups=None):
    df = df.copy()
    X = design(df, extra)
    kw = dict(cov_type='cluster', cov_kwds={'groups': groups}) if cov == 'cluster' else dict(cov_type='HC1')
    m = sm.Logit(df[y].astype(float), X).fit(disp=0, maxiter=200, **kw)
    b, V = m.params, m.cov_params()
    res = dict(n=int(len(df)), n_integ=int(df[y].sum()), pseudo_r2=float(m.prsquared))
    for p, terms in [('–2014', ['core']), ('2015–2019', ['core', 'core_p2']), ('2020–2024', ['core', 'core_p3'])]:
        L = np.zeros(len(b)); L[[list(b.index).index(t) for t in terms]] = 1
        est = float(L @ b); se = float(np.sqrt(L @ V.values @ L)); z = est / se
        from scipy.stats import norm
        res[p] = dict(or_=float(np.exp(est)), lo=float(np.exp(est - 1.96 * se)), hi=float(np.exp(est + 1.96 * se)), p=float(2 * norm.sf(abs(z))))
    for t in ['core_p2', 'core_p3']:
        res[t] = dict(ratio=float(np.exp(b[t])), p=float(m.pvalues[t]))
    w = m.wald_test('core_p2 = 0, core_p3 = 0', scalar=True)
    res['wald_chi2'] = float(w.statistic); res['wald_p'] = float(w.pvalue)
    for e in extra:
        res[e] = dict(or_=float(np.exp(b[e])), p=float(m.pvalues[e]))
    return res

B = A[A.has_assembly == 1].copy(); B['integ'] = (B.tech_cat == 'Integrated').astype(int)
out['h3_sample'] = dict(n=int(len(B)), integrated=int(B.integ.sum()), assembly=int((1 - B.integ).sum()),
                        core=int(B.core.sum()), core_share=float(B.core.mean()))
out['core_share_by_period'] = {p: dict(n=int((B.period == p).sum()), core=int(B[B.period == p].core.sum()),
                                       share=float(B[B.period == p].core.mean())) for p in PERIODS}
cells = {}
for p in PERIODS:
    for c in (1, 0):
        g = B[(B.period == p) & (B.core == c)]
        cells[f'{p}|{c}'] = dict(n=int(len(g)), integ=int(g.integ.sum()), share=float(g.integ.mean()))
out['h3_cells'] = cells
out['h3_all'] = {c: dict(n=int((B.core == c).sum()), share=float(B[B.core == c].integ.mean())) for c in (1, 0)}
out['h3_base'] = h3(B)
out['h3_size'] = h3(B, extra=('n24nc',))
out['h3_cluster'] = h3(B, cov='cluster', groups=pd.factorize(B.tnorm)[0])
C = B[~B.title.str.lower().str.contains(FP, regex=True)]; out['h3_clean'] = h3(C)
out['h3_le2022'] = h3(B[B.year <= 2022])
Dd = A.sort_values(['year', 'aid']).groupby('tnorm').head(1); Dd = Dd[Dd.has_assembly == 1].copy()
Dd['integ'] = (Dd.tech_cat == 'Integrated').astype(int); out['h3_dedup'] = h3(Dd)
S = A[A.has_assembly == 1].copy(); S['integ'] = (S.tcs == 'Integrated').astype(int); out['h3_strict'] = h3(S)
B2 = B.copy(); B2['core'] = [int(bool(set(SYMS[a]) & {'24/08', '24/80'})) for a in B2.aid]; out['h3_core2'] = h3(B2)
Hc = B[B.title.str.lower().str.contains(r'hybrid[ -]?bond', regex=True)]
out['h3_hybrid_title_cells'] = {p: {c: dict(n=int(((Hc.period == p) & (Hc.core == c)).sum()),
                                            share=(float(Hc[(Hc.period == p) & (Hc.core == c)].integ.mean()) if ((Hc.period == p) & (Hc.core == c)).sum() else None))
                                    for c in (1, 0)} for p in PERIODS}

json.dump(out, open('robustness/results_v32.json', 'w'), ensure_ascii=False, indent=1, sort_keys=True)
print('core', CORE, 'next', out['next_after_core'])
print('pairs', [(d['a'], d['b'], d['w']) for d in out['pairs_top']])
print('strength', [(d['sym'], d['strength'], d['degree']) for d in out['strength_top']])
print('network', out['network'], out['core_clique'])
print('ties', json.dumps(ties, ensure_ascii=False))
print('sample', out['h3_sample'], out['core_share_by_period'])
print('cells', json.dumps(cells, ensure_ascii=False))
for k in ['h3_base', 'h3_size', 'h3_cluster', 'h3_clean', 'h3_le2022', 'h3_dedup', 'h3_strict', 'h3_core2']:
    r = out[k]
    print(k, r['n'], ' | '.join(f"{p}: {r[p]['or_']:.3f} [{r[p]['lo']:.3f}, {r[p]['hi']:.3f}] p={r[p]['p']:.4f}" for p in PERIODS),
          f"| int p2 {r['core_p2']['ratio']:.3f} p={r['core_p2']['p']:.4f} p3 {r['core_p3']['ratio']:.3f} p={r['core_p3']['p']:.4f} wald {r['wald_chi2']:.2f} p={r['wald_p']:.4f} R2 {r['pseudo_r2']:.3f}")
print('hybrid-title cells', out['h3_hybrid_title_cells'])

# ---------------- number pool for the v32 manuscript audit ----------------
import pathlib
NUMS = set()
def f3(x): return f'{x:.3f}'
for k in [k for k in out if k.startswith('h3_') and isinstance(out[k], dict) and 'n' in out[k] and '–2014' in out[k]]:
    r = out[k]; NUMS |= {str(r['n']), str(r['n_integ']), f"{r['wald_chi2']:.2f}", f3(r['pseudo_r2'])}
    for p in PERIODS: NUMS |= {f3(r[p]['or_']), f3(r[p]['lo']), f3(r[p]['hi']), f3(r[p]['p'])}
    for t in ['core_p2', 'core_p3']: NUMS |= {f3(r[t]['ratio']), f3(r[t]['p'])}
    for e in ('n24nc',):
        if e in r: NUMS |= {f3(r[e]['or_'])}
for c in out['h3_cells'].values(): NUMS |= {str(c['n']), str(c['integ']), f"{100 * c['share']:.1f}"}
for v in out['core_share_by_period'].values(): NUMS |= {str(v['n']), str(v['core']), f"{100 * v['share']:.1f}"}
NUMS |= {str(out['h3_sample']['n']), str(out['h3_sample']['core']), f"{100 * out['h3_sample']['core_share']:.1f}"}
for d in out['cluster_top']: NUMS |= {str(d['n']), f"{100 * d['share']:.1f}"}
for d in out['pairs_top']: NUMS.add(str(d['w']))
for d in out['strength_top']: NUMS |= {f"{d['strength']:,}", str(d['strength'])}
for v in out['core_process_ties'].values():
    for d in v['top']: NUMS.add(str(d['n']))
# full-CPC tallies of the same 928 applications (author's separate count, term paper Tables 6-7)
FULL = {'2224/80895': 504, '2224/80896': 488, '2224/08145': 436, '25/50': 363, '25/0657': 274, '2224/05647': 239, '2224/80357': 205}
NUMS |= {str(v) for v in FULL.values()} | {f'{100 * v / N:.1f}' for v in FULL.values()}
NUMS |= {'0.15', '460', '434', '420', '418', '395', '392', '379', '368', '2224', '200', '11.1', '2.5', '1990', '2001', '2003', '2006', '2008'}
pathlib.Path('sources/ko_v32/numbers_v32.txt').write_text('\n'.join(sorted(NUMS)) + '\n', encoding='utf-8')
print('numbers', len(NUMS))
