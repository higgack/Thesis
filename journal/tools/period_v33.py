"""Thesis v33: does the breadth-integrated association (H1) hold within each filing period?
Multinomial logit (base = assembly-only) of tech_cat on breadth and filing year, estimated separately
per period. Office dummies are left out because several office cells are empty in the first period.
Writes robustness/results_v33.json and sources/ko_v33/numbers_v33.txt."""
import json, pathlib
import numpy as np, pandas as pd, statsmodels.api as sm
A = pd.read_pickle('data/app.pkl')
out = {}
for p in ['–2014', '2015–2019', '2020–2024']:
    d = A[A.period == p]
    X = sm.add_constant(d[['yc', 'breadth']].astype(float))
    y = d.tech_cat.map({'Assembly': 0, 'Integrated': 1, 'Process': 2})
    m = sm.MNLogit(y, X).fit(disp=0, maxiter=500)
    b, se, pv = m.params.loc['breadth', 0], m.bse.loc['breadth', 0], m.pvalues.loc['breadth', 0]
    out[p] = dict(n=int(len(d)), rrr=float(np.exp(b)), lo=float(np.exp(b - 1.96 * se)), hi=float(np.exp(b + 1.96 * se)), p=float(pv),
                  converged=bool(m.mle_retvals['converged']))
json.dump({'h1_by_period': out}, open('robustness/results_v33.json', 'w'), ensure_ascii=False, indent=1, sort_keys=True)
nums = set()
for v in out.values(): nums |= {f"{v['rrr']:.3f}", f"{v['lo']:.3f}", f"{v['hi']:.3f}", f"{v['p']:.3f}"}
pathlib.Path('sources/ko_v33/numbers_v33.txt').write_text('\n'.join(sorted(nums)) + '\n', encoding='utf-8')
for p, v in out.items(): print(p, v)
