"""All estimates behind manuscript v9 (baseline + robustness), from journal/data/app.pkl and HB.csv.
Writes journal/robustness/results_v9.json and prints the numbers used in the text.
"""
import json, re, warnings
import numpy as np, pandas as pd
import statsmodels.api as sm
from scipy import stats
warnings.filterwarnings('ignore')

A = pd.read_pickle('data/app.pkl')
D = pd.read_csv('data/HB.csv')
A['ccode'] = pd.Categorical(A.ccode, categories=['US', 'CN', 'KR', 'TW', 'JP', 'EP', 'WO', 'Other'])
OFF = ['CN', 'KR', 'TW', 'JP', 'EP', 'WO', 'Other']
out = {}

def design(df, cols=('yc', 'breadth', 'ccode')):
    X = pd.get_dummies(df[list(cols)], columns=['ccode'], drop_first=True).astype(float)
    return sm.add_constant(X)

def mnl(df, breadth='breadth', cov=None, groups=None):
    X = design(df); X = X.rename(columns={'breadth': 'breadth'})
    if breadth != 'breadth':
        X['breadth'] = df[breadth].astype(float).values
    y = df.tech_cat.map({'Assembly': 0, 'Integrated': 1, 'Process': 2})
    kw = {}
    if cov == 'cluster': kw = dict(cov_type='cluster', cov_kwds={'groups': groups})
    m = sm.MNLogit(y, X).fit(disp=0, maxiter=300, **kw)
    res = {}
    for k, name in ((0, 'Integrated'), (1, 'Process')):
        res[name] = {v: dict(rrr=float(np.exp(m.params.loc[v, k])), se_log=float(m.bse.loc[v, k]),
                             p=float(m.pvalues.loc[v, k]), b=float(m.params.loc[v, k])) for v in X.columns if v != 'const'}
    res['llf'] = float(m.llf); res['aic'] = float(m.aic); res['n'] = int(len(df))
    return m, res

def logit(df, y='has_process', breadth='breadth', cov=None, groups=None):
    X = design(df)
    if breadth != 'breadth': X['breadth'] = df[breadth].astype(float).values
    kw = dict(cov_type='cluster', cov_kwds={'groups': groups}) if cov == 'cluster' else {}
    m = sm.Logit(df[y].astype(float), X).fit(disp=0, **kw)
    res = {v: dict(or_=float(np.exp(m.params[v])), se_log=float(m.bse[v]), p=float(m.pvalues[v])) for v in X.columns if v != 'const'}
    from sklearn.metrics import roc_auc_score
    res['auc'] = float(roc_auc_score(df[y], m.predict(X))); res['llf'] = float(m.llf); res['aic'] = float(m.aic); res['n'] = int(len(df))
    return m, res

def nb(df, cov=None, groups=None):
    X = design(df, ('yc', 'ccode'))
    kw = dict(cov_type='cluster', cov_kwds={'groups': groups}) if cov == 'cluster' else (dict(cov_type='HC1') if cov == 'HC1' else {})
    m = sm.NegativeBinomial(df.breadth.astype(float), X).fit(disp=0, maxiter=300, **kw)
    res = {v: dict(irr=float(np.exp(m.params[v])), se_log=float(m.bse[v]), p=float(m.pvalues[v]), b=float(m.params[v])) for v in X.columns if v != 'const'}
    res['alpha'] = float(m.params['alpha']); res['alpha_se'] = float(m.bse['alpha']); res['llf'] = float(m.llf); res['aic'] = float(m.aic); res['n'] = int(len(df))
    return m, res

# ---------- baseline ----------
m0, out['mnl_base'] = mnl(A)
l0, out['logit_base'] = logit(A)
nb0, out['nb_base'] = nb(A)
Xn = design(A, ('yc', 'ccode'))
po = sm.Poisson(A.breadth.astype(float), Xn).fit(disp=0)
ols = sm.OLS(A.breadth.astype(float), Xn).fit(); olsr = sm.OLS(A.breadth.astype(float), Xn).fit(cov_type='HC1')
from statsmodels.stats.diagnostic import het_breuschpagan
lm, lmp, _, _ = het_breuschpagan(ols.resid, Xn)
u2 = ols.resid ** 2; aux = sm.OLS(u2, Xn).fit(); bp_orig = aux.ess / (2 * u2.mean() ** 2)
mu = po.predict(Xn); pearson = float((((A.breadth - mu) ** 2) / mu).sum() / (len(A) - Xn.shape[1]))
out['diag'] = dict(bp_koenker_lm=float(lm), bp_koenker_p=float(lmp), bp_orig_chi2=float(bp_orig), bp_orig_p=float(1 - stats.chi2.cdf(bp_orig, 8)),
                   lr_pois_nb=float(2 * (nb0.llf - po.llf)), poisson_pearson_df=pearson, ols_jp_p=float(ols.pvalues['ccode_JP']), ols_jp_p_hc1=float(olsr.pvalues['ccode_JP']),
                   poisson_hc1_se_cn=float(sm.Poisson(A.breadth.astype(float), Xn).fit(disp=0, cov_type='HC1').bse['ccode_CN']),
                   poisson_hc1_se_jp=float(sm.Poisson(A.breadth.astype(float), Xn).fit(disp=0, cov_type='HC1').bse['ccode_JP']))
nbh, out['nb_hc1'] = nb(A, cov='HC1')

# ---------- adjusted breadth ----------
_, out['mnl_adj'] = mnl(A, breadth='breadth_adj'); _, out['logit_adj'] = logit(A, breadth='breadth_adj')
# ---------- 2010+ ----------
A10 = A[A.year >= 2010]; _, out['mnl_2010'] = mnl(A10); _, out['logit_2010'] = logit(A10); _, out['nb_2010'] = nb(A10)
# JP big-family filings (breadth 21, 2010+)
big = A10[(A10.ccode == 'JP') & (A10.breadth == 21)].index
_, out['nb_2010_nojp21'] = nb(A10.drop(big)); out['jp21_n'] = int(len(big))
jp = A[A.ccode == 'JP']; out['jp_pre2010'] = int((jp.year < 2010).sum()); out['jp_2010_types'] = A10[A10.ccode == 'JP'].tech_cat.value_counts().to_dict()
out['other_2010_types'] = A10[A10.ccode == 'Other'].tech_cat.value_counts().to_dict()
# ---------- period dummies ----------
def mnl_period(df, b1=2014, b2=2019):
    df = df.copy(); df['per'] = pd.cut(df.year, [0, b1, b2, 2100], labels=['p0', 'p1', 'p2'])
    X = pd.get_dummies(df[['breadth', 'ccode', 'per']], columns=['ccode', 'per'], drop_first=True).astype(float); X = sm.add_constant(X)
    y = df.tech_cat.map({'Assembly': 0, 'Integrated': 1, 'Process': 2})
    m = sm.MNLogit(y, X).fit(disp=0, maxiter=300)
    r = {name: {v: dict(rrr=float(np.exp(m.params.loc[v, k])), p=float(m.pvalues.loc[v, k]), se_log=float(m.bse.loc[v, k])) for v in X.columns if v != 'const'} for k, name in ((0, 'Integrated'), (1, 'Process'))}
    # Wald test p1 vs p2 for Integrated (k=0)
    names = list(X.columns); i1, i2 = names.index('per_p1'), names.index('per_p2')
    b = m.params.values.flatten(order='F'); V = m.cov_params().values
    K = len(names); idx1, idx2 = i1, i2  # equation 0 block first
    diff = b[idx1] - b[idx2]; var = V[idx1, idx1] + V[idx2, idx2] - 2 * V[idx1, idx2]
    r['wald_int_p1_vs_p2'] = dict(chi2=float(diff ** 2 / var), p=float(1 - stats.chi2.cdf(diff ** 2 / var, 1)))
    r['n'] = int(len(df)); r['llf'] = float(m.llf); r['counts'] = df.per.value_counts().to_dict()
    return r
out['mnl_period'] = mnl_period(A); out['mnl_period_b1'] = mnl_period(A, 2013, 2018); out['mnl_period_b2'] = mnl_period(A, 2015, 2020)
# ---------- exclude 2023-24 ----------
A22 = A[A.year <= 2022]; _, out['mnl_le2022'] = mnl(A22); _, out['logit_le2022'] = logit(A22); _, out['nb_le2022'] = nb(A22)
s = A[A.year >= 2023]
out['recent'] = dict(cn_process=int(((s.ccode == 'CN') & (s.tech_cat == 'Process')).sum()), cn_n=int((s.ccode == 'CN').sum()),
                     us_process=int(((s.ccode == 'US') & (s.tech_cat == 'Process')).sum()), us_n=int((s.ccode == 'US').sum()),
                     mean_breadth_2324=float(s.breadth.mean()), mean_breadth_by_year={int(k): float(v) for k, v in A.groupby('year').breadth.mean().loc[2019:2024].items()})
# ---------- family proxy: normalized title ----------
A['tnorm'] = A.title.str.lower().str.replace(r'[^a-z0-9 ]', '', regex=True).str.replace(r'\s+', ' ', regex=True).str.strip()
vc = A.tnorm.value_counts()
groups = pd.factorize(A.tnorm)[0]
out['family'] = dict(unique_titles=int(A.tnorm.nunique()), repeated_titles=int((vc > 1).sum()), rows_in_repeated=int(vc[vc > 1].sum()), max_repeat=int(vc.max()),
                     titles_multi_office=int((A.groupby('tnorm').ccode.nunique() > 1).sum()))
pairs = []
for t, g in A.groupby('tnorm'):
    us = g[g.ccode == 'US']; cn = g[g.ccode == 'CN']
    if len(us) and len(cn):
        pairs.append((us.breadth.iloc[0] == cn.breadth.iloc[0], us.tech_cat.iloc[0] == cn.tech_cat.iloc[0], us.breadth.iloc[0] > cn.breadth.iloc[0]))
pairs = np.array(pairs); out['family']['us_cn_pairs'] = int(len(pairs)); out['family']['same_breadth'] = int(pairs[:, 0].sum()); out['family']['same_type'] = int(pairs[:, 1].sum()); out['family']['cn_narrower'] = int(pairs[:, 2].sum())
_, out['mnl_cluster'] = mnl(A, cov='cluster', groups=groups); _, out['logit_cluster'] = logit(A, cov='cluster', groups=groups); _, out['nb_cluster'] = nb(A, cov='cluster', groups=groups)
dedup = A.sort_values(['year']).groupby('tnorm').head(1)
out['dedup_shares'] = dedup.tech_cat.value_counts(normalize=True).round(4).to_dict(); out['dedup_counts'] = dedup.tech_cat.value_counts().to_dict()
_, out['mnl_dedup'] = mnl(dedup); _, out['logit_dedup'] = logit(dedup); _, out['nb_dedup'] = nb(dedup)
# ---------- strict H01L21 definition ----------
D['grp'] = D.cpc_class_symbol.str.replace(' ', '').str.split('/').str[0]; D['sub'] = D.cpc_class_symbol.str.replace(' ', '').str.split('/').str[1]
h21 = D[D.grp == 'H01L21'].copy(); h21['mg'] = h21['sub'].str.extract(r'^(\d{2})')[0].astype(int)
asm = {48, 50, 51, 52, 53, 54, 55, 56, 57, 58, 60, 78}; equip = {67, 68}
out['h21'] = dict(records=int(len(h21)), assembly_stage=int(h21.mg.isin(asm).sum()), equipment=int(h21.mg.isin(equip).sum()), wafer=int((~h21.mg.isin(asm | equip)).sum()),
                  top_maingroups=h21.mg.value_counts().head(12).to_dict())
strict_ids = set(h21[~h21.mg.isin(asm)].appln_id.astype(str))
A['hps'] = [1 if str(i) in strict_ids else 0 for i in A.index]
A['tcs'] = np.where((A.hps == 1) & (A.has_assembly == 1), 'Integrated', np.where(A.hps == 1, 'Process', np.where(A.has_assembly == 1, 'Assembly', 'None')))
out['strict_counts'] = A.tcs.value_counts().to_dict(); out['strict_hps_share'] = float(A.hps.mean()); out['only_assembly_stage_h21'] = int(((A.has_process == 1) & (A.hps == 0)).sum())
S = A[A.tcs != 'None'].copy(); S['tech_cat'] = S.tcs; S['has_process'] = S.hps
_, out['mnl_strict'] = mnl(S); _, out['logit_strict'] = logit(S)
S['per'] = pd.cut(S.year, [0, 2014, 2019, 2100], labels=['–2014', '2015–2019', '2020–2024'])
out['strict_period_shares'] = pd.crosstab(S.per, S.tech_cat, normalize='index').round(4).to_dict()
# ---------- false positives (irrelevant uses of 'direct bonding') ----------
FP = r"ceramic|dbc|alumina|zirconia|aluminum nitride|molybdenum|copper alloy having|precursor|electrostatic chuck|power (device|module|semiconductor)|heat spreader|terminal pin|direct bond circuit assembly|direct bond(ed|ing)? (of )?metal|direct bonded copper|direct bond copper|thick film printed cooler|sp3"
t = A.title.str.lower(); A['fp'] = t.str.contains(FP, regex=True)
fp = A[A.fp]
out['fp'] = dict(n=int(len(fp)), pre2010=int((fp.year < 2010).sum()), pre2015=int((fp.year <= 2014).sum()), by_office=fp.ccode.value_counts().to_dict(), by_type=fp.tech_cat.value_counts().to_dict(),
                 titles=[dict(year=int(r.year), office=str(r.ccode), breadth=int(r.breadth), type=str(r.tech_cat), title=str(r.title)) for r in fp.sort_values('year').itertuples()])
C = A[~A.fp]; _, out['mnl_clean'] = mnl(C); _, out['logit_clean'] = logit(C); _, out['nb_clean'] = nb(C)
out['clean_n'] = int(len(C)); out['clean_shares'] = C.tech_cat.value_counts(normalize=True).round(4).to_dict()
out['clean_first_year'] = int(C.year.min()); out['pre2010_soi'] = int(((C.year < 2010) & C.title.str.lower().str.contains('silicon|soi|wafer')).sum()); out['pre2010_clean'] = int((C.year < 2010).sum())
# ---------- IIA check: drop process-only, binary logit integrated vs assembly ----------
R = A[A.tech_cat != 'Process'].copy(); R['integ'] = (R.tech_cat == 'Integrated').astype(int)
_, out['logit_int_vs_asm'] = logit(R, y='integ')
# ---------- classification practice ----------
d24 = D[D.grp == 'H01L24'].copy(); first = d24.merge(A[['year']], left_on='appln_id', right_index=True) if False else None
D['year'] = D.appln_id.astype(str).map(A.year)
d24 = D[D.grp == 'H01L24']; first_year = d24.groupby('cpc_class_symbol').year.min()
old_syms = set(first_year[first_year <= 2009].index)
post = d24[d24.year >= 2015]; apps_post = post.groupby('appln_id').cpc_class_symbol.apply(lambda s: any(x in old_syms for x in s))
out['classif'] = dict(h24_subgroups=int(d24.cpc_class_symbol.nunique()), h24_pre2010=int(len(old_syms)), post2015_apps_with_h24=int(len(apps_post)), share_with_old_sym=float(apps_post.mean()),
                      share_records_old_sym=float(post.cpc_class_symbol.isin(old_syms).mean()))
A['per'] = pd.cut(A.year, [0, 2014, 2019, 2100], labels=['–2014', '2015–2019', '2020–2024'])
out['symbols_by_period'] = dict(n24=A.groupby('per').n24.mean().round(3).to_dict(), n21=A.groupby('per').n21.mean().round(3).to_dict(), has24=A.groupby('per').has_assembly.mean().round(4).to_dict())
# ---------- predictions for figures ----------
Xb = design(A); ybase = A.tech_cat.map({'Assembly': 0, 'Integrated': 1, 'Process': 2})
mm = sm.MNLogit(ybase, Xb).fit(disp=0, maxiter=300)
def avg_margins(mm, X, office):
    Xo = X.copy()
    for o in OFF: Xo['ccode_' + o] = 1.0 if o == office else 0.0
    return float(np.asarray(mm.predict(Xo))[:, 2].mean())
def atmeans(mm, X, office):
    xm = X.mean().to_frame().T
    for o in OFF: xm['ccode_' + o] = 1.0 if o == office else 0.0
    return float(np.asarray(mm.predict(xm))[0, 2])
rng = np.random.default_rng(0); draws = rng.multivariate_normal(mm.params.values.flatten(order='F'), mm.cov_params().values, size=1000)
pm = {}
for office in ['US'] + OFF:
    pt = avg_margins(mm, Xb, office); sims = []
    for dr in draws:
        m2 = mm; P = dr.reshape(mm.params.shape, order='F')
        Xo = Xb.copy()
        for o in OFF: Xo['ccode_' + o] = 1.0 if o == office else 0.0
        eta = np.column_stack([np.zeros(len(Xo)), Xo.values @ P[:, 0], Xo.values @ P[:, 1]]); e = np.exp(eta - eta.max(1, keepdims=True)); pr = e / e.sum(1, keepdims=True)
        sims.append(pr[:, 2].mean())
    pm[office] = dict(mean=float(pt), lo=float(np.percentile(sims, 2.5)), hi=float(np.percentile(sims, 97.5)), atmeans=atmeans(mm, Xb, office))
out['fig6_margins'] = pm
# NB at means predictions with delta-method CI
nbX = design(A, ('yc', 'ccode')); nbm = sm.NegativeBinomial(A.breadth.astype(float), nbX).fit(disp=0, maxiter=300)
V = nbm.cov_params().loc[nbX.columns, nbX.columns].values; pb = {}
for office in ['US'] + OFF:
    xm = nbX.mean().to_frame().T
    for o in OFF: xm['ccode_' + o] = 1.0 if o == office else 0.0
    eta = float((xm.values @ nbm.params[nbX.columns].values).item()); se = float(np.sqrt((xm.values @ V @ xm.values.T).item())); pb[office] = dict(pred=float(np.exp(eta)), lo=float(np.exp(eta - 1.96 * se)), hi=float(np.exp(eta + 1.96 * se)))
out['fig4_pred'] = pb; out['yc_mean'] = float(A.yc.mean())
from sklearn.metrics import roc_curve
fpr, tpr, _ = roc_curve(A.has_process, l0.predict(design(A))); out['roc'] = dict(fpr=fpr.round(4).tolist(), tpr=tpr.round(4).tolist())
out['ols_resid'] = dict(fitted=ols.fittedvalues.round(4).tolist(), resid=ols.resid.round(4).tolist())
out['year_counts'] = {int(k): int(v) for k, v in A.year.value_counts().sort_index().items()}
out['period_counts'] = pd.crosstab(A.per, A.tech_cat).to_dict()
json.dump(out, open('robustness/results_v9.json', 'w'), ensure_ascii=False, indent=1, default=str)

# ---------- print summary ----------
def rr(r, eq, v): x = r[eq][v]; return f"{x['rrr']:.3f} (p={x['p']:.3f})"
for k in ['mnl_base', 'mnl_adj', 'mnl_2010', 'mnl_le2022', 'mnl_dedup', 'mnl_strict', 'mnl_clean', 'mnl_cluster']:
    r = out[k]; print(f"{k:12s} N={r['n']}: b→I {rr(r,'Integrated','breadth')} b→P {rr(r,'Process','breadth')} yc→I {rr(r,'Integrated','yc')} yc→P {rr(r,'Process','yc')} CN→P {rr(r,'Process','ccode_CN')} JP→P {rr(r,'Process','ccode_JP')} KR→P {rr(r,'Process','ccode_KR')} TW→P {rr(r,'Process','ccode_TW')} WO→P {rr(r,'Process','ccode_WO')}")
for k in ['nb_base', 'nb_hc1', 'nb_2010', 'nb_2010_nojp21', 'nb_le2022', 'nb_dedup', 'nb_clean', 'nb_cluster']:
    r = out[k]; print(f"{k:14s} N={r['n']}: yc {r['yc']['irr']:.4f} (p={r['yc']['p']:.3f}) CN {r['ccode_CN']['irr']:.3f} (p={r['ccode_CN']['p']:.4f}) JP {r['ccode_JP']['irr']:.3f} (p={r['ccode_JP']['p']:.3f}) KR {r['ccode_KR']['irr']:.3f} WO {r['ccode_WO']['irr']:.3f} (p={r['ccode_WO']['p']:.3f})")
for k in ['logit_base', 'logit_adj', 'logit_le2022', 'logit_dedup', 'logit_strict', 'logit_clean', 'logit_cluster', 'logit_int_vs_asm']:
    r = out[k]; print(f"{k:16s} N={r['n']}: breadth OR {r['breadth']['or_']:.3f} (p={r['breadth']['p']:.4f}) yc {r['yc']['or_']:.3f} CN {r['ccode_CN']['or_']:.3f} (p={r['ccode_CN']['p']:.3f}) AUC {r['auc']:.3f}")
print('diag', {k: round(v, 3) for k, v in out['diag'].items()})
print('period wald', out['mnl_period']['wald_int_p1_vs_p2'], '| b1(2013/2018):', {(e, k): (round(out['mnl_period_b1'][e][k]['rrr'], 3), round(out['mnl_period_b1'][e][k]['p'], 3)) for e in ['Integrated', 'Process'] for k in ['per_p1', 'per_p2']})
print('   b2(2015/2020):', {(e, k): (round(out['mnl_period_b2'][e][k]['rrr'], 3), round(out['mnl_period_b2'][e][k]['p'], 3)) for e in ['Integrated', 'Process'] for k in ['per_p1', 'per_p2']})
print('   base:', {(e, k): (round(out['mnl_period'][e][k]['rrr'], 3), round(out['mnl_period'][e][k]['p'], 3)) for e in ['Integrated', 'Process'] for k in ['per_p1', 'per_p2']})
print('recent', out['recent']); print('family', out['family']); print('dedup', out['dedup_counts'], out['dedup_shares'])
print('h21', {k: v for k, v in out['h21'].items() if k != 'top_maingroups'}, 'strict', out['strict_counts'], round(out['strict_hps_share'], 3), 'only-asm', out['only_assembly_stage_h21'])
print('strict period', out['strict_period_shares'])
print('fp', {k: v for k, v in out['fp'].items() if k != 'titles'}, 'clean N', out['clean_n'], out['clean_shares'], 'first year', out['clean_first_year'], 'pre2010 clean', out['pre2010_clean'])
print('classif', out['classif']); print('symbols', out['symbols_by_period'])
print('fig6', {k: (round(v['mean'], 3), round(v['lo'], 3), round(v['hi'], 3), round(v['atmeans'], 3)) for k, v in pm.items()})
print('fig4', {k: (round(v['pred'], 2), round(v['lo'], 2), round(v['hi'], 2)) for k, v in pb.items()}, 'yc mean', round(out['yc_mean'], 2))
print('jp', out['jp_pre2010'], out['jp_2010_types'], 'other 2010+', out['other_2010_types'], 'jp21', out['jp21_n'])
