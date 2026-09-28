# -*- coding: utf-8 -*-
"""Figure: H01L21 technical areas carried by integrated applications, by period (v30)."""
import json, os
import matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt
import koreanize_matplotlib  # noqa
import numpy as np
LANG = os.environ.get('LANG_FIG', 'ko'); SUF = '_ko' if LANG == 'ko' else '_en'
T = (lambda ko, en: ko if LANG == 'ko' else en)
plt.rcParams.update({'font.size': 11, 'axes.spines.top': False, 'axes.spines.right': False, 'figure.dpi': 300, 'savefig.dpi': 300, 'axes.linewidth': 1.0, 'xtick.labelsize': 10, 'ytick.labelsize': 10})
r = json.load(open('robustness/results_v30.json', encoding='utf-8'))['composition']
periods = ['–2014', '2015–2019', '2020–2024']
PL = {'–2014': T('–2014', 'to 2014'), '2015–2019': '2015–2019', '2020–2024': '2020–2024'}
areas = [('P_wafer', T('웨이퍼 제조공정', 'Wafer fabrication'), '#1f3b5c'),
         ('P_asm', T('조립 단계 공정', 'Assembly-stage processes'), '#9ec1e6'),
         ('P_equip', T('공정 장치', 'Process equipment'), '#8c8c8c')]
fig, ax = plt.subplots(figsize=(6.3, 3.9))
x = np.arange(3); w = 0.26
for k, (a, lab, col) in enumerate(areas):
    v = [r[p][a] * 100 for p in periods]
    b = ax.bar(x + (k - 1) * w, v, width=w, color=col, label=lab)
    for xi, vi in zip(x + (k - 1) * w, v):
        ax.text(xi, vi + 1.5, f'{vi:.1f}', ha='center', va='bottom', fontsize=9)
ax.set_xticks(x); ax.set_xticklabels([f"{PL[p]}\n({T('통합형', 'integrated')} N = {r[p]['n']})" for p in periods])
ax.set_ylabel(T('통합형 출원 중 비중(%)', 'Share of integrated applications (%)')); ax.set_ylim(0, 108)
ax.grid(axis='y', color='#e5e5e5', lw=0.8); ax.set_axisbelow(True)
ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.22), ncol=3, frameon=False, fontsize=10)
fig.tight_layout(); fig.savefig(f'figures/fig_composition{SUF}.png'); plt.close(fig)
print('saved')
