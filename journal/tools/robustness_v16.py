"""Additional estimates for manuscript v16 (core hybrid-bonding subsample, fit statistics,
predicted probabilities by scope / year / office, per-period CPC symbol counts).
Reads data/app.pkl; writes robustness/results_v16.json and figures/fig_pred_types_ko.png.
"""
import json, warnings
import numpy as np, pandas as pd, statsmodels.api as sm
warnings.filterwarnings('ignore')
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt; import koreanize_matplotlib  # noqa
A = pd.read_pickle('data/app.pkl')
A['ccode'] = pd.Categorical(A.ccode, categories=['US','CN','KR','TW','JP','EP','WO','Other'])
def design(df, cols=('yc','breadth','ccode')):
    X = pd.get_dummies(df[list(cols)], columns=['ccode'], drop_first=True).astype(float); return sm.add_constant(X)
def mnl(df):
    X = design(df); y = df.tech_cat.map({'Assembly':0,'Integrated':1,'Process':2})
    m = sm.MNLogit(y, X).fit(disp=0, maxiter=300)
    res = {nm: {v: dict(rrr=float(np.exp(m.params.loc[v,k])), se_log=float(m.bse.loc[v,k]), p=float(m.pvalues.loc[v,k])) for v in X.columns if v!='const'} for k,nm in ((0,'Integrated'),(1,'Process'))}
    res['llf']=float(m.llf); res['n']=int(len(df)); return m, X, res
def nb(df):
    X = design(df, ('yc','ccode')); m = sm.NegativeBinomial(df.breadth.astype(float), X).fit(disp=0, maxiter=300)
    return {v: dict(irr=float(np.exp(m.params[v])), se_log=float(m.bse[v]), p=float(m.pvalues[v])) for v in X.columns if v!='const'} | {'n': int(len(df))}
def logit(df):
    X = design(df); m = sm.Logit(df.has_process.astype(float), X).fit(disp=0)
    return m, {v: dict(or_=float(np.exp(m.params[v])), se_log=float(m.bse[v]), p=float(m.pvalues[v])) for v in X.columns if v!='const'} | {'llf': float(m.llf), 'n': int(len(df))}
out = {}
# --- fit statistics of baseline logit / MNL ---
m0, X0, base = mnl(A); l0, lb = logit(A)
ynull = A.tech_cat.map({'Assembly':0,'Integrated':1,'Process':2})
mn = sm.MNLogit(ynull, np.ones((len(A),1))).fit(disp=0); ln = sm.Logit(A.has_process.astype(float), np.ones((len(A),1))).fit(disp=0)
out['fit'] = dict(mnl_llf=float(m0.llf), mnl_ll0=float(mn.llf), mnl_lr=float(2*(m0.llf-mn.llf)), mnl_df=int(m0.df_model), mnl_pr2=float(1-m0.llf/mn.llf),
                  logit_llf=float(l0.llf), logit_ll0=float(ln.llf), logit_lr=float(2*(l0.llf-ln.llf)), logit_df=int(l0.df_model), logit_pr2=float(1-l0.llf/ln.llf))
# --- core hybrid-bonding subsample (title contains 'hybrid bond') ---
core = A[A.title.str.lower().str.contains(r'hybrid[ -]?bond', regex=True)].copy()
_,_,mc = mnl(core); _, lc = logit(core)
out['core'] = dict(n=int(len(core)), pre2015=int((core.year<2015).sum()), shares={k: round(float(v),4) for k,v in core.tech_cat.value_counts(normalize=True).items()},
                   has_process=round(float(core.has_process.mean()),4), mnl=mc, nb=nb(core), logit={k:v for k,v in lc.items()})
# --- predicted type probabilities (average predictive margins) ---
def margins(setter):
    X = X0.copy(); setter(X); P = np.asarray(m0.predict(X)); return [float(x) for x in P.mean(axis=0)]  # [Assembly, Integrated, Process]
def set_office(X, off):
    for o in ['CN','KR','TW','JP','EP','WO','Other']: X[f'ccode_{o}'] = 0.0
    if off != 'US': X[f'ccode_{off}'] = 1.0
out['pred_breadth'] = {str(b): margins(lambda X, b=b: X.__setitem__('breadth', float(b))) for b in range(1, 16)}
out['pred_year'] = {str(y): margins(lambda X, y=y: X.__setitem__('yc', float(y-1968))) for y in range(2005, 2025)}
out['pred_office'] = {o: margins(lambda X, o=o: set_office(X, o)) for o in ['US','CN','KR','TW','JP','EP','WO','Other']}
out['contrasts'] = dict(breadth_3=out['pred_breadth']['3'], breadth_8=out['pred_breadth']['8'], year_2010=out['pred_year']['2010'], year_2022=out['pred_year']['2022'],
                        US=out['pred_office']['US'], CN=out['pred_office']['CN'])
# --- per-period CPC symbol counts ---
per = {}
for p, g in A.groupby('period'):
    per[str(p)] = dict(n=int(len(g)), mean_n24=float(g.n24.mean()), mean_n21=float(g.n21.mean()), share_any24=float((g.n24>0).mean()), share_any21=float((g.n21>0).mean()))
out['period_symbols'] = per
json.dump(out, open('robustness/results_v16.json','w'), indent=1, ensure_ascii=False)
# --- figure: predicted type probabilities by scope and by year ---
plt.rcParams.update({'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False, 'figure.dpi': 200})
cols = {'조립 단독형':'#9ecae1', '통합형':'#1f3b5c', '공정 단독형':'#8c8c8c'}
fig, axes = plt.subplots(1, 2, figsize=(9, 3.6))
bs = list(range(1,16)); P = np.array([out['pred_breadth'][str(b)] for b in bs])
for i,(nm,c) in enumerate(cols.items()): axes[0].plot(bs, P[:,i], color=c, lw=2, label=nm)
axes[0].set_xlabel('기술범위(CPC 서브그룹 수)'); axes[0].set_ylabel('예측확률'); axes[0].set_ylim(0,1); axes[0].legend(frameon=False, fontsize=8)
ys = list(range(2005,2025)); P = np.array([out['pred_year'][str(y)] for y in ys])
for i,(nm,c) in enumerate(cols.items()): axes[1].plot(ys, P[:,i], color=c, lw=2, label=nm)
axes[1].set_xlabel('출원연도'); axes[1].set_ylim(0,1); axes[1].set_xticks([2005,2010,2015,2020,2024])
fig.tight_layout(); fig.savefig('figures/fig_pred_types_ko.png'); plt.close(fig)
print(json.dumps({k: out[k] for k in ('fit','contrasts','period_symbols')}, indent=1)[:2500])
print('core', out['core']['n'], out['core']['shares'], out['core']['has_process'])
for eq in ('Integrated','Process'): print(' core', eq, {k:(round(v['rrr'],3), round(v['p'],3)) for k,v in mc[eq].items() if k in ('yc','breadth','ccode_CN','ccode_JP')})
print(' core nb CN', round(out['core']['nb']['ccode_CN']['irr'],3), round(out['core']['nb']['ccode_CN']['p'],3), 'yc', round(out['core']['nb']['yc']['irr'],3), round(out['core']['nb']['yc']['p'],3))
