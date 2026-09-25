# -*- coding: utf-8 -*-
"""Redraw figures 2–7 with Korean labels from robustness/results_v9.json."""
import json
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import koreanize_matplotlib  # noqa
import numpy as np, os
LANG = os.environ.get('LANG_FIG', 'ko')
SUF = '_ko' if LANG == 'ko' else '_en'
T = (lambda ko, en: ko if LANG == 'ko' else en)
r = json.load(open('robustness/results_v9.json', encoding='utf-8'))
plt.rcParams.update({'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False, 'figure.dpi': 200})
OFF = ['US', 'CN', 'KR', 'TW', 'JP', 'EP', 'WO', 'Other']
LAB = {'US': T('미국','US'), 'CN': T('중국','CN'), 'KR': T('한국','KR'), 'TW': T('대만','TW'), 'JP': T('일본','JP'), 'EP': 'EPO', 'WO': 'PCT', 'Other': T('기타','Other')}
NAVY = '#1f3b5c'

# Fig 2: applications by year
yc = {int(k): v for k, v in r['year_counts'].items()}
years = list(range(min(yc), max(yc) + 1)); vals = [yc.get(y, 0) for y in years]
fig, ax = plt.subplots(figsize=(7.5, 4))
ax.bar(years, vals, color=NAVY, width=0.8)
ax.set_xlabel(T('출원연도','Filing year')); ax.set_ylabel(T('출원 건수','Number of applications')); ax.set_xlim(1966, 2025)
ax.set_ylim(0, max(vals) * 1.14)
ax.axvspan(2022.5, 2024.5, color='#bbbbbb', alpha=0.35, lw=0)
ax.text(2023.5, max(vals) * 1.07, T('공개 시차','publication lag'), ha='center', va='center', fontsize=8.5, color='#333', bbox=dict(boxstyle='round,pad=0.25', fc='white', ec='#999', lw=0.6))
fig.tight_layout(); fig.savefig(f'figures/fig2_applications_by_year{SUF}.png'); plt.close(fig)

# Fig 3: OLS residuals vs fitted
fig, ax = plt.subplots(figsize=(6.5, 4.2))
ax.scatter(r['ols_resid']['fitted'], r['ols_resid']['resid'], s=8, color=NAVY, alpha=0.7)
ax.axhline(0, color='#b22222', lw=1); ax.set_xlabel(T('선형 예측값','Linear prediction')); ax.set_ylabel(T('잔차','Residual'))
fig.tight_layout(); fig.savefig(f'figures/fig3_ols_residuals{SUF}.png'); plt.close(fig)

# Fig 4: predicted breadth by office (NB, at means)
p = r['fig4_pred']; x = np.arange(len(OFF))
fig, ax = plt.subplots(figsize=(7, 4))
ax.errorbar(x, [p[o]['pred'] for o in OFF], yerr=[[p[o]['pred'] - p[o]['lo'] for o in OFF], [p[o]['hi'] - p[o]['pred'] for o in OFF]], fmt='o', color=NAVY, capsize=3)
ax.set_xticks(x); ax.set_xticklabels([LAB[o] for o in OFF]); ax.set_xlabel(T('출원청','Filing office')); ax.set_ylabel(T('예측 기술범위(서브그룹 수)','Predicted scope (number of subgroups)'))
fig.tight_layout(); fig.savefig(f'figures/fig4_predicted_breadth{SUF}.png'); plt.close(fig)

# Fig 5: ROC
fig, ax = plt.subplots(figsize=(4.8, 4.5))
ax.plot(r['roc']['fpr'], r['roc']['tpr'], color=NAVY, lw=1.5); ax.plot([0, 1], [0, 1], '--', color='#888', lw=1)
ax.set_xlabel(T('1 − 특이도','1 − specificity')); ax.set_ylabel(T('민감도','Sensitivity')); ax.set_xlim(0, 1); ax.set_ylim(0, 1)
fig.tight_layout(); fig.savefig(f'figures/fig5_roc{SUF}.png'); plt.close(fig)

# Fig 6: average predicted probability of process-only
m = r['fig6_margins']
fig, ax = plt.subplots(figsize=(7, 4))
ax.errorbar(x, [m[o]['mean'] for o in OFF], yerr=[[m[o]['mean'] - m[o]['lo'] for o in OFF], [m[o]['hi'] - m[o]['mean'] for o in OFF]], fmt='o', color=NAVY, capsize=3)
ax.set_xticks(x); ax.set_xticklabels([LAB[o] for o in OFF]); ax.set_xlabel(T('출원청','Filing office')); ax.set_ylabel(T('공정 단독형 평균 예측확률','Average predicted probability of process-only')); ax.set_ylim(0, None)
fig.tight_layout(); fig.savefig(f'figures/fig6_predicted_process_only{SUF}.png'); plt.close(fig)

# Fig 7: period shares
pc = r['period_counts']; periods = ['–2014', '2015–2019', '2020–2024']
PL = {'–2014': T('–2014','to 2014'), '2015–2019': '2015–2019', '2020–2024': '2020–2024'}
N = {q: sum(pc[t][q] for t in pc) for q in periods}
fig, ax = plt.subplots(figsize=(7, 4.2))
bottom = np.zeros(3)
for t, lab, col in [('Process', T('공정 단독형','Process-only'), '#8c8c8c'), ('Integrated', T('통합형(경계 넘기)','Integrated (boundary-spanning)'), NAVY), ('Assembly', T('조립 단독형','Assembly-only'), '#9ec1e6')]:
    sh = np.array([pc[t][q] / N[q] * 100 for q in periods]); ax.bar(periods, sh, bottom=bottom, color=col, label=lab, width=0.55)
    for i, v in enumerate(sh): ax.text(i, bottom[i] + v / 2, f'{v:.1f}%', ha='center', va='center', fontsize=8, color='white' if t != 'Assembly' else '#222')
    bottom += sh
ax.set_xticks(range(3)); ax.set_xticklabels([f'{PL[q]}\n(N = {N[q]})' for q in periods]); ax.set_ylabel(T('비중(%)','Share (%)')); ax.set_ylim(0, 100)
ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.18), ncol=3, frameon=False, fontsize=8)
fig.tight_layout(); fig.savefig(f'figures/fig7_period_shares{SUF}.png'); plt.close(fig)
print('figures written')
