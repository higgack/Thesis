# -*- coding: utf-8 -*-
"""Generate the English Supplementary material (supplement_v9_en.md) from robustness/results_v9.json:
Section S0 (Table S1 period shares, Figures S1–S2), Section S1 (full robustness estimates, Tables S2+), Section S2 (excluded titles)."""
import json
r = json.load(open('robustness/results_v9.json', encoding='utf-8'))
OFF = [('ccode_CN', 'China (CN)'), ('ccode_KR', 'Korea (KR)'), ('ccode_TW', 'Taiwan (TW)'), ('ccode_JP', 'Japan (JP)'), ('ccode_EP', 'EPO (EP)'), ('ccode_WO', 'PCT (WO)'), ('ccode_Other', 'Other')]
def star(p): return '\\*\\*\\*' if p < 0.01 else ('\\*\\*' if p < 0.05 else ('\\*' if p < 0.10 else ''))
def f3(x): return f"{x:.3f}"
counter = [1]
def tn():
    counter[0] += 1; return f"Table S{counter[0]}"

def mnl_table(key, title, note=''):
    m = r[key]; rows = [f"**{tn()}.** {title} (N = {m['n']}; log-likelihood = {m['llf']:.1f}; AIC = {m['aic']:.1f})", '',
                        '| | Integrated RRR | SE (log) | p | Process-only RRR | SE (log) | p |', '|---|:---:|:---:|:---:|:---:|:---:|:---:|']
    for v, lab in [('yc', 'Filing year (yc)'), ('breadth', 'Technological scope')] + OFF:
        a, b = m['Integrated'].get(v), m['Process'].get(v)
        if a is None: continue
        rows.append(f"| {lab} | {f3(a['rrr'])}{star(a['p'])} | ({f3(a['se_log'])}) | {a['p']:.3f} | {f3(b['rrr'])}{star(b['p'])} | ({f3(b['se_log'])}) | {b['p']:.3f} |")
    if note: rows += ['', '*Note:* ' + note]
    return '\n'.join(rows)

def period_table(key, title):
    m = r[key]; rows = [f"**{tn()}.** {title} (N = {m['n']}; log-likelihood = {m['llf']:.1f})", '', '| | Integrated RRR | p | Process-only RRR | p |', '|---|:---:|:---:|:---:|:---:|']
    for v, lab in [('breadth', 'Technological scope'), ('per_p1', 'Second period (vs first)'), ('per_p2', 'Third period (vs first)')] + OFF:
        a, b = m['Integrated'][v], m['Process'][v]
        rows.append(f"| {lab} | {f3(a['rrr'])}{star(a['p'])} | {a['p']:.3f} | {f3(b['rrr'])}{star(b['p'])} | {b['p']:.3f} |")
    w = m['wald_int_p1_vs_p2']; rows += ['', f"*Note:* Wald test of the difference between the two period coefficients in the integrated equation: χ²(1) = {w['chi2']:.1f}, p = {w['p']:.4f}."]
    return '\n'.join(rows)

def logit_table(key, title, dep='has_process (front-end orientation)'):
    m = r[key]; rows = [f"**{tn()}.** {title} (dependent variable {dep}; N = {m['n']}; log-likelihood = {m['llf']:.1f}; AIC = {m['aic']:.1f}; AUC = {m['auc']:.3f})", '', '| | Odds ratio | SE (log) | p |', '|---|:---:|:---:|:---:|']
    for v, lab in [('yc', 'Filing year (yc)'), ('breadth', 'Technological scope')] + OFF:
        a = m[v]; rows.append(f"| {lab} | {f3(a['or_'])}{star(a['p'])} | ({f3(a['se_log'])}) | {a['p']:.3f} |")
    return '\n'.join(rows)

def nb_table(key, title):
    m = r[key]; rows = [f"**{tn()}.** {title} (N = {m['n']}; α = {m['alpha']:.3f} (SE {m['alpha_se']:.3f}); log-likelihood = {m['llf']:.1f}; AIC = {m['aic']:.1f})", '', '| | IRR | SE (log) | p |', '|---|:---:|:---:|:---:|']
    for v, lab in [('yc', 'Filing year (yc)')] + OFF:
        a = m[v]; rows.append(f"| {lab} | {f3(a['irr'])}{star(a['p'])} | ({f3(a['se_log'])}) | {a['p']:.3f} |")
    return '\n'.join(rows)

d = r['diag']; pc = r['period_counts']; periods = ['–2014', '2015–2019', '2020–2024']; N = {q: sum(pc[t][q] for t in pc) for q in periods}
def row(q): return f"| {q if q != '–2014' else 'to 2014'} | {pc['Assembly'][q]} ({pc['Assembly'][q]/N[q]*100:.1f}%) | {pc['Integrated'][q]} ({pc['Integrated'][q]/N[q]*100:.1f}%) | {pc['Process'][q]} ({pc['Process'][q]/N[q]*100:.1f}%) | {N[q]} | {(pc['Integrated'][q]+pc['Process'][q])/N[q]*100:.1f} |"
S = f"""# Supplementary material

Front-end/back-end technological convergence in semiconductor hybrid bonding patents: Determinants of boundary-spanning patents and jurisdictional strategy differentiation

> Source of the robustness analyses in Section 5.4 of the main text. Raw data: 5,277 application–CPC records (928 applications) extracted from EPO PATSTAT. Estimation: Python statsmodels (NegativeBinomial, Logit, MNLogit; identical specifications to Stata `nbreg`/`logit`/`mlogit`). The baseline estimates (main-text Tables 4–6) match the original Stata analysis to three decimal places. Standard errors in the tables below are those of the log-scale coefficients (the parentheses in main-text Tables 5 and 6 are delta-method standard errors on the ratio scale), and significance is from z tests on the log scale. \\* p < 0.10, \\*\\* p < 0.05, \\*\\*\\* p < 0.01. The office reference is the US office, and the outcome base category of the multinomial logit is assembly-only.

## S0. Period distribution and diagnostic figures

**Table S1.** Strategy-type distribution by period (N = 928; row shares in parentheses).

| Filing period | Assembly-only | Integrated (boundary-spanning) | Process-only | N | Front-end oriented (%) |
|---|---|---|---|---:|---:|
{row('–2014')}
{row('2015–2019')}
{row('2020–2024')}

![](figures/fig3_ols_residuals_en.png)

**Figure S1.** OLS residuals versus fitted values (N = 928).

![](figures/fig5_roc_en.png)

**Figure S2.** ROC curve of the front-end-orientation logit (AUC = 0.707).

## S1. Full robustness estimates

### S1.0 Data structure and diagnostics

- The CPC symbols in the extracted file belong to two classes only, H01L21/\\* and H01L24/\\* (262 unique symbols). Other classes (for example the H01L2224 indexing codes) were not retained at extraction, so technological scope is the number of unique subgroups within the two classes.
- Main-group composition of the {r['h21']['records']:,} H01L21 records: wafer-fabrication groups {r['h21']['wafer']}, equipment and handling groups (21/67–21/68) {r['h21']['equipment']}, assembly-stage groups (21/48, 21/50–21/58, 21/60, 21/78) {r['h21']['assembly_stage']}.
- OLS residual heteroskedasticity: Breusch–Pagan (all regressors) χ²(8) = {d['bp_orig_chi2']:.1f}, p < 0.001; Koenker version LM = {d['bp_koenker_lm']:.1f}, p < 0.001. The auxiliary regression on fitted values only (Stata `estat hettest` default) does not reject (χ²(1) = 0.90, p = 0.34).
- Poisson versus negative binomial: boundary likelihood-ratio test χ̄²(01) = {d['lr_pois_nb']:.1f}, p < 0.001; Poisson Pearson χ²/df = {d['poisson_pearson_df']:.2f}.
- Heteroskedasticity-robust (HC1) standard errors: OLS Japan coefficient p = {d['ols_jp_p']:.3f} → {d['ols_jp_p_hc1']:.3f}; Poisson robust standard errors China {d['poisson_hc1_se_cn']:.3f}, Japan {d['poisson_hc1_se_jp']:.3f}.

### S1.1 Baseline models (identical to main-text Tables 4–6)

{nb_table('nb_base', 'Negative binomial (technological scope)')}

{logit_table('logit_base', 'Binary logit')}

{mnl_table('mnl_base', 'Multinomial logit')}

### S1.2 Heteroskedasticity-robust and family (title) clustered standard errors

{nb_table('nb_hc1', 'Negative binomial, HC1 robust standard errors')}

{nb_table('nb_cluster', 'Negative binomial, title-clustered standard errors')}

{logit_table('logit_cluster', 'Binary logit, title-clustered standard errors')}

{mnl_table('mnl_cluster', 'Multinomial logit, title-clustered standard errors', 'Point estimates equal the baseline. Number of title clusters = 473.')}

### S1.3 Adjusted scope (breadth_adj = breadth − has_process − has_assembly)

{logit_table('logit_adj', 'Binary logit, adjusted scope')}

{mnl_table('mnl_adj', 'Multinomial logit, adjusted scope')}

### S1.4 Post-2010 subsample

{nb_table('nb_2010', 'Negative binomial, 2010 onwards')}

{nb_table('nb_2010_nojp21', 'Negative binomial, 2010 onwards excluding the four Japanese-office filings with scope 21')}

{logit_table('logit_2010', 'Binary logit, 2010 onwards')}

{mnl_table('mnl_2010', 'Multinomial logit, 2010 onwards', 'The 15 Japanese-office applications from 2010 onwards comprise one assembly-only, 13 integrated and one process-only application; the 10 "other" applications contain no process-only filing, so the Other → process-only coefficient is not identified (quasi-complete separation; the value shown is meaningless).')}

### S1.5 Period-dummy specification and boundary sensitivity

{period_table('mnl_period', 'Multinomial logit, period dummies (to 2014 / 2015–2019 / 2020–2024)')}

{period_table('mnl_period_b1', 'Boundaries one year earlier (to 2013 / 2014–2018 / 2019–2024)')}

{period_table('mnl_period_b2', 'Boundaries one year later (to 2015 / 2016–2020 / 2021–2024)')}

### S1.6 Exclusion of 2023–2024 (publication-lag truncation)

{nb_table('nb_le2022', 'Negative binomial, filings up to 2022')}

{logit_table('logit_le2022', 'Binary logit, filings up to 2022')}

{mnl_table('mnl_le2022', 'Multinomial logit, filings up to 2022')}

Among 2023–2024 filings, {r['recent']['cn_process']} of the {r['recent']['cn_n']} Chinese-office applications and {r['recent']['us_process']} of the {r['recent']['us_n']} US-office applications are process-only. Mean scope by filing year: {', '.join(f"{k}: {v:.2f}" for k, v in r['recent']['mean_breadth_by_year'].items())}.

### S1.7 Family de-duplication (earliest filing per normalised title, N = {r['mnl_dedup']['n']})

Type distribution: assembly-only {r['dedup_counts']['Assembly']} ({r['dedup_shares']['Assembly']*100:.1f}%), integrated {r['dedup_counts']['Integrated']} ({r['dedup_shares']['Integrated']*100:.1f}%), process-only {r['dedup_counts']['Process']} ({r['dedup_shares']['Process']*100:.1f}%). Among the {r['family']['us_cn_pairs']} US–China pairs with the same title, scope is identical in {r['family']['same_breadth']} pairs and strategy type in {r['family']['same_type']}; the Chinese filing is narrower in {r['family']['cn_narrower']} pairs.

{nb_table('nb_dedup', 'Negative binomial, de-duplicated')}

{logit_table('logit_dedup', 'Binary logit, de-duplicated')}

{mnl_table('mnl_dedup', 'Multinomial logit, de-duplicated')}

### S1.8 Strict front-end definition (assembly-stage main groups excluded, N = {r['mnl_strict']['n']})

Type distribution: assembly-only {r['strict_counts']['Assembly']}, integrated {r['strict_counts']['Integrated']}, process-only {r['strict_counts']['Process']} ({r['strict_counts']['None']} applications with no symbol left in either class excluded). Front-end orientation {r['strict_hps_share']*100:.1f}%. Period shares (assembly/integrated/process): to 2014 {r['strict_period_shares']['Assembly']['–2014']*100:.1f}/{r['strict_period_shares']['Integrated']['–2014']*100:.1f}/{r['strict_period_shares']['Process']['–2014']*100:.1f}; 2015–2019 {r['strict_period_shares']['Assembly']['2015–2019']*100:.1f}/{r['strict_period_shares']['Integrated']['2015–2019']*100:.1f}/{r['strict_period_shares']['Process']['2015–2019']*100:.1f}; 2020–2024 {r['strict_period_shares']['Assembly']['2020–2024']*100:.1f}/{r['strict_period_shares']['Integrated']['2020–2024']*100:.1f}/{r['strict_period_shares']['Process']['2020–2024']*100:.1f}.

{logit_table('logit_strict', 'Binary logit, strict definition', 'has_process (strict definition)')}

{mnl_table('mnl_strict', 'Multinomial logit, strict definition')}

### S1.9 Exclusion of irrelevant uses of the search terms (N = {r['mnl_clean']['n']}; list in Section S2)

{nb_table('nb_clean', 'Negative binomial, irrelevant uses excluded')}

{logit_table('logit_clean', 'Binary logit, irrelevant uses excluded')}

{mnl_table('mnl_clean', 'Multinomial logit, irrelevant uses excluded')}

### S1.10 Indirect check of the independence of irrelevant alternatives

Binary logit of integrated against assembly-only in the sample without process-only applications (N = {r['logit_int_vs_asm']['n']}): technological scope OR {r['logit_int_vs_asm']['breadth']['or_']:.3f}, filing year {r['logit_int_vs_asm']['yc']['or_']:.3f}, China {r['logit_int_vs_asm']['ccode_CN']['or_']:.3f}, Korea {r['logit_int_vs_asm']['ccode_KR']['or_']:.3f}, Taiwan {r['logit_int_vs_asm']['ccode_TW']['or_']:.3f}, Japan {r['logit_int_vs_asm']['ccode_JP']['or_']:.3f}, EPO {r['logit_int_vs_asm']['ccode_EP']['or_']:.3f}, PCT {r['logit_int_vs_asm']['ccode_WO']['or_']:.3f}, other {r['logit_int_vs_asm']['ccode_Other']['or_']:.3f}. These are close to the integrated equation of main-text Table 6 (1.403, 0.993, 1.205, 1.166, 1.312, 1.046, 1.307, 1.126, 2.326).

### S1.11 Counts used in the classification-practice check

- Of the {r['classif']['h24_subgroups']} H01L24 subgroups, {r['classif']['h24_pre2010']} are assigned to applications filed before 2010. Of the {r['classif']['post2015_apps_with_h24']} applications with bonding symbols filed from 2015 onwards, {r['classif']['share_with_old_sym']*100:.1f}% carry at least one pre-existing symbol; {r['classif']['share_records_old_sym']*100:.1f}% of the H01L24 records in that period are assigned to pre-existing symbols.
- H01L24 symbols per application: {', '.join(f"{k} {v:.2f}" for k, v in r['symbols_by_period']['n24'].items())}; H01L21 symbols per application: {', '.join(f"{k} {v:.2f}" for k, v in r['symbols_by_period']['n21'].items())}; share of applications with a bonding symbol: {', '.join(f"{k} {v*100:.1f}%" for k, v in r['symbols_by_period']['has24'].items())}.

### S1.12 Predicted values behind main-text Figures 3 and 4

- Figure 3 (negative binomial, filing year fixed at the sample mean yc = {r['yc_mean']:.1f}): {', '.join(f"{k} {v['pred']:.2f} [{v['lo']:.2f}, {v['hi']:.2f}]" for k, v in r['fig4_pred'].items())}.
- Figure 4 (multinomial logit, average predicted probabilities): {', '.join(f"{k} {v['mean']:.3f} [{v['lo']:.3f}, {v['hi']:.3f}]" for k, v in r['fig6_margins'].items())}. Predictions with covariates fixed at their means (for reference): {', '.join(f"{k} {v['atmeans']:.3f}" for k, v in r['fig6_margins'].items())}.

## S2. Applications excluded as irrelevant uses of the search terms

> Basis of Section 4.1 and the eighth robustness item of Section 5.4 in the main text. Applications whose titles contain the following terms were judged irrelevant uses of "direct bonding": ceramic, DBC (direct bonded copper), alumina, zirconia and aluminium nitride substrates; copper alloys ("copper alloy having … direct bonding property"); diamond-to-molybdenum bonding; laser precursors; electrostatic chucks; power-module and power-device substrates; heat spreaders; direct bonded metal or copper substrates; direct bond circuit assemblies; and sp3–sp2 "hybrid bonding" in the chemical sense. Total {r['fp']['n']} applications ({r['fp']['pre2015']} filed before 2015). By office: {', '.join(f"{k} {v}" for k, v in r['fp']['by_office'].items())}. By type: {', '.join(f"{k} {v}" for k, v in r['fp']['by_type'].items())}.

| Filing year | Office | Scope | Strategy type | Title |
|---|---|---:|---|---|
"""
for x in r['fp']['titles']:
    S += f"| {x['year']} | {x['office']} | {x['breadth']} | {x['type']} | {x['title']} |\n"
open('supplement_v9_en.md', 'w', encoding='utf-8').write(S)
print('written supplement_v9_en.md; last table', counter[0])
