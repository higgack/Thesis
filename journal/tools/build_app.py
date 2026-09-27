"""Rebuild data/app.pkl (application-level dataset) from data/HB.csv.
Mirrors the original Stata collapse (HB_analysis.do): 1 row = application x CPC symbol -> 1 row = application."""
import pandas as pd, numpy as np, re
for enc in ('utf-8', 'cp949', 'latin1'):
    try:
        D = pd.read_csv('data/HB.csv', dtype=str, encoding=enc); break
    except UnicodeDecodeError:
        continue
print('encoding', enc)
D['cpc'] = D.cpc_class_symbol.str.replace(r'\s+', ' ', regex=True).str.strip()
D['grp'] = D.cpc.str.split('/').str[0].str.replace(' ', '', regex=False)   # "H01L 21/603" -> "H01L21"
D['year'] = pd.to_numeric(D.appln_filing_year, errors='coerce')
D = D[D.year.notna()].copy(); D['year'] = D.year.astype(int)
D = D.drop_duplicates(['appln_id', 'cpc'])
g = D.groupby('appln_id')
A = pd.DataFrame({
    'breadth': g.cpc.nunique(),
    'n21': g.apply(lambda x: int((x.grp == 'H01L21').sum())),
    'n24': g.apply(lambda x: int((x.grp == 'H01L24').sum())),
    'year': g.year.first(), 'title': g.appln_title.first(), 'auth': g.appln_auth.first(),
})
A['has_process'] = (A.n21 > 0).astype(int); A['has_assembly'] = (A.n24 > 0).astype(int)
A['tech_cat'] = np.select([(A.n21 > 0) & (A.n24 > 0), A.n21 > 0, A.n24 > 0], ['Integrated', 'Process', 'Assembly'], 'None')
A['breadth_adj'] = A.breadth - A.has_process - A.has_assembly
A['ccode'] = A.auth.where(A.auth.isin(['US', 'CN', 'KR', 'TW', 'JP', 'EP', 'WO']), 'Other')
A['yc'] = A.year - A.year.min()
A['period'] = np.select([A.year <= 2014, A.year <= 2019], ['–2014', '2015–2019'], '2020–2024')
A['has_2224'] = 0; A['breadth_ex'] = 0; A['breadth_ex2'] = 0
A = A.reset_index()
A.to_pickle('data/app.pkl')
print('applications', len(A), 'records', len(D), 'min year', A.year.min())
print(A.tech_cat.value_counts().to_dict(), 'has_process', int(A.has_process.sum()))
print('breadth mean', round(A.breadth.mean(), 2), 'sd', round(A.breadth.std(), 2), 'max', A.breadth.max(), 'symbols', D.cpc.nunique())
print(A.ccode.value_counts().to_dict())
