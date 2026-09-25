# -*- coding: utf-8 -*-
"""Figure 4 (v16+): predicted co-classification type probabilities by scope and by filing year.
Reads robustness/results_v16.json; writes figures/fig_pred_types_ko.png (and _en)."""
import json, os
import numpy as np
import matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt
import koreanize_matplotlib  # noqa
LANG = os.environ.get('LANG_FIG', 'ko'); T = (lambda ko, en: ko if LANG == 'ko' else en)
out = json.load(open('robustness/results_v16.json'))
plt.rcParams.update({'font.size': 11, 'axes.spines.top': False, 'axes.spines.right': False})
cols = {T('조립 단독형', 'Assembly-only'): '#7fb3d5', T('통합형', 'Integrated'): '#1f3b5c', T('공정 단독형', 'Process-only'): '#7a7a7a'}
fig, axes = plt.subplots(1, 2, figsize=(6.3, 3.3))
bs = list(range(1, 16)); P = np.array([out['pred_breadth'][str(b)] for b in bs])
for i, (nm, c) in enumerate(cols.items()): axes[0].plot(bs, P[:, i], color=c, lw=2.8, label=nm)
axes[0].set_xlabel(T('기술범위(CPC 서브그룹 수)', 'Scope (CPC subgroups)')); axes[0].set_ylabel(T('예측확률', 'Predicted probability'))
axes[0].set_ylim(0, 1); axes[0].set_xticks([1, 3, 5, 7, 9, 11, 13, 15]); axes[0].legend(frameon=False, fontsize=9.5, loc='center right')
ys = list(range(2005, 2025)); P = np.array([out['pred_year'][str(y)] for y in ys])
for i, (nm, c) in enumerate(cols.items()): axes[1].plot(ys, P[:, i], color=c, lw=2.8, label=nm)
axes[1].set_xlabel(T('출원연도', 'Filing year')); axes[1].set_ylim(0, 1); axes[1].set_xticks([2005, 2010, 2015, 2020, 2024])
for ax in axes: ax.grid(axis='y', color='#e5e5e5', lw=0.8); ax.set_axisbelow(True); ax.tick_params(labelsize=10)
fig.tight_layout(); fig.savefig(f"figures/fig_pred_types{'_ko' if LANG == 'ko' else '_en'}.png", dpi=300); plt.close(fig)
print('figure 4 redrawn')
