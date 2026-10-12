# -*- coding: utf-8 -*-
"""English figures for manuscript_v64_en (the English version of the Korean submission v64).

Same data and layout as the Korean figures of v64, with English labels in American spelling and the
terms used in the English text. Writes figures/*_en_v64.png and leaves the older *_en.png files alone.
  Figure 1  fig_framework_psp_en_v64.png        (Korean: fig_framework_psp.png, tools/make_fig_framework_v19.py)
  Figure 2  fig_d2w_flow_amat_ko.png is reused as is (its labels are already in English)
  Figure 3  fig2_applications_by_year_en_v64.png (Korean: fig2_applications_by_year_ko.png, tools/make_figures_v9.py)
  Figure 4  fig_core_network_en_v64.png          (Korean: fig_core_network_v58_ko.png, tools/make_fig_core_network_v58.py:
            the published v32 network drawing with an English legend strip pasted below y = 1360 px)
  Figure 5  fig_pred_types_en_v64.png            (Korean: fig_pred_types_v37_ko.png, tools/make_fig_pred_types.py)
  Figure 6  fig7_period_shares_en_v64.png        (Korean: fig7_period_shares_v37_ko.png, tools/make_figures_v9.py)
Usage (from journal/): python3 tools/make_figs_en_v64.py
"""
import json
import numpy as np
import matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
from matplotlib.lines import Line2D
from PIL import Image
import koreanize_matplotlib  # noqa  (registers NanumGothic, the font of the node labels, for the Figure 4 legend)

plt.rcParams.update({'font.family': 'Liberation Sans', 'font.size': 11, 'axes.spines.top': False,
                     'axes.spines.right': False, 'figure.dpi': 300, 'savefig.dpi': 300, 'axes.linewidth': 1.0,
                     'xtick.labelsize': 10, 'ytick.labelsize': 10})
NAVY = '#1f3b5c'; LIGHT = '#f4f6f9'; EDGE = '#8a94a6'
r9 = json.load(open('robustness/results_v9.json', encoding='utf-8'))
r16 = json.load(open('robustness/results_v16.json', encoding='utf-8'))

# Figure 1: enabling processes -> bonding configurations -> device structures -> applications
tiers = [('Enabling processes', '(CPC H01L21: fabrication-process class)',
          ['CMP\nplanarization', 'Cleaning and\ncontamination\ncontrol', 'Surface\nactivation', 'Alignment', 'Low-temperature\nannealing']),
         ('Bonding configurations', '(CPC H01L24: bonding and interconnect class)',
          ['Wafer-to-wafer (W2W)', 'Die-to-wafer (D2W)', 'Die-to-die (D2D)']),
         ('Device structures', '', ['Stacked dies', 'Chiplet integration', 'Memory-on-logic']),
         ('Applications', '', ['Image sensors\n(CIS)', '3D NAND', 'HBM', 'Logic\nstacking', 'MicroLED'])]
fig, ax = plt.subplots(figsize=(6.3, 5.6)); ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis('off')
n = len(tiers); h = 18.5; gap = (100 - n * h) / (n + 1)
for i, (title, sub, items) in enumerate(tiers):
    y0 = 100 - gap * (i + 1) - h * (i + 1)
    ax.add_patch(FancyBboxPatch((4, y0), 92, h, boxstyle='round,pad=0.4,rounding_size=1.5', fc=LIGHT, ec=EDGE, lw=1.6))
    t = ax.text(6, y0 + h - 3.4, title, fontsize=12.5, fontweight='bold', color=NAVY, va='center')
    if sub:   # subtitle right after the title, as in the Korean figure (anchored to the title's box at draw time)
        ax.annotate(sub, xy=(1, 0.5), xycoords=t, xytext=(8, 0), textcoords='offset points', fontsize=9.5, color='#333', va='center')
    k = len(items); w = (88 - (k - 1) * 1.6) / k
    for j, it in enumerate(items):
        x = 6 + j * (w + 1.6)
        ax.add_patch(FancyBboxPatch((x, y0 + 2.0), w, 9.0, boxstyle='round,pad=0.3,rounding_size=1.2', fc='white', ec='#222', lw=1.4))
        ax.text(x + w / 2, y0 + 6.5, it, ha='center', va='center', fontsize=10 if k < 5 else 8.5, color='#111', linespacing=1.1)
    if i < n - 1:
        ax.add_patch(FancyArrowPatch((50, y0 - 0.4), (50, y0 - gap + 0.4), arrowstyle='-|>', mutation_scale=18, color=NAVY, lw=2.2))
for i, lb in enumerate(['enable', 'implemented as', 'applied to']):
    y0 = 100 - gap * (i + 1) - h * (i + 1)
    ax.text(52.5, y0 - gap / 2, lb, fontsize=10, color=NAVY, va='center')
fig.tight_layout(); fig.savefig('figures/fig_framework_psp_en_v64.png'); plt.close(fig)

# Figure 3: applications by filing year
yc = {int(k): v for k, v in r9['year_counts'].items()}
years = list(range(min(yc), max(yc) + 1)); vals = [yc.get(y, 0) for y in years]
fig, ax = plt.subplots(figsize=(6.3, 3.6))
ax.bar(years, vals, color=NAVY, width=0.8)
ax.set_xlabel('Filing year'); ax.set_ylabel('Number of applications'); ax.set_xlim(1966, 2025)
ax.set_ylim(0, max(vals) * 1.14)
ax.axvspan(2022.5, 2024.5, color='#bbbbbb', alpha=0.35, lw=0)
ax.text(2023.5, max(vals) * 1.07, 'Publication lag', ha='right', va='center', fontsize=10, color='#333',
        bbox=dict(boxstyle='round,pad=0.25', fc='white', ec='#999', lw=0.8))
fig.tight_layout(); fig.savefig('figures/fig2_applications_by_year_en_v64.png'); plt.close(fig)

# Figure 4: English legend strip on the published network drawing (same figure size and legend placement as v58)
fig, ax = plt.subplots(figsize=(6.3, 5.2)); ax.axis('off')
handles = [Line2D([0], [0], marker='o', color='w', markerfacecolor='#b5441b', markersize=9, label='Core technologies (H01L24, ≥200 applications)'),
           Line2D([0], [0], marker='o', color='w', markerfacecolor='#f0b36b', markersize=7, label='Other H01L24 bonding and interconnect symbols'),
           Line2D([0], [0], marker='o', color='w', markerfacecolor='#1f3b5c', markersize=7, label='H01L21 fabrication-process symbols')]
fig.legend(handles=handles, loc='lower center', ncol=2, frameon=False, prop={'family': 'NanumGothic', 'size': 8.5})   # same font as the node labels
fig.tight_layout(rect=(0, 0.08, 1, 1)); fig.savefig('/tmp/fig_core_network_legend_en_v64.png'); plt.close(fig)
old = Image.open('figures/fig_core_network_ko.png').convert('RGB')    # v32 drawing (published layout)
new = Image.open('/tmp/fig_core_network_legend_en_v64.png').convert('RGB')
assert old.size == new.size, (old.size, new.size)
CUT = 1360
out = old.copy(); out.paste(new.crop((0, CUT, new.width, new.height)), (0, CUT))
out.save('figures/fig_core_network_en_v64.png', dpi=(300, 300))

# Figure 5: predicted type probabilities by technological scope and by filing year
cols = {'Bonding-only': '#7fb3d5', 'Integrated': '#1f3b5c', 'Process-only': '#7a7a7a'}
fig, axes = plt.subplots(1, 2, figsize=(6.3, 3.3))
bs = list(range(1, 16)); P = np.array([r16['pred_breadth'][str(b)] for b in bs])
for i, (nm, c) in enumerate(cols.items()): axes[0].plot(bs, P[:, i], color=c, lw=2.8, label=nm)
axes[0].set_xlabel('Technological scope (CPC subgroups)'); axes[0].set_ylabel('Predicted probability')
axes[0].set_ylim(0, 1); axes[0].set_xticks([1, 3, 5, 7, 9, 11, 13, 15]); axes[0].legend(frameon=False, fontsize=9.5, loc='center right')
ys = list(range(2005, 2025)); P = np.array([r16['pred_year'][str(y)] for y in ys])
for i, (nm, c) in enumerate(cols.items()): axes[1].plot(ys, P[:, i], color=c, lw=2.8, label=nm)
axes[1].set_xlabel('Filing year'); axes[1].set_ylim(0, 1); axes[1].set_xticks([2005, 2010, 2015, 2020, 2024])
for a in axes: a.grid(axis='y', color='#e5e5e5', lw=0.8); a.set_axisbelow(True); a.tick_params(labelsize=10)
fig.tight_layout(); fig.savefig('figures/fig_pred_types_en_v64.png'); plt.close(fig)

# Figure 6: type shares in the three filing periods
pc = r9['period_counts']; periods = ['–2014', '2015–2019', '2020–2024']
PL = {'–2014': '1968–2014', '2015–2019': '2015–2019', '2020–2024': '2020–2024'}
N = {q: sum(pc[t][q] for t in pc) for q in periods}
fig, ax = plt.subplots(figsize=(6.3, 4.0))
bottom = np.zeros(3)
for t, lab, col in [('Process', 'Process-only', '#8c8c8c'), ('Integrated', 'Integrated (boundary-spanning)', NAVY), ('Assembly', 'Bonding-only', '#9ec1e6')]:
    sh = np.array([pc[t][q] / N[q] * 100 for q in periods]); ax.bar(periods, sh, bottom=bottom, color=col, label=lab, width=0.55)
    for i, v in enumerate(sh): ax.text(i, bottom[i] + v / 2, f'{v:.1f}%', ha='center', va='center', fontsize=10, color='white' if t != 'Assembly' else '#222')
    bottom += sh
ax.set_xticks(range(3)); ax.set_xticklabels([f'{PL[q]}\n(N = {N[q]})' for q in periods]); ax.set_ylabel('Share (%)'); ax.set_ylim(0, 100)
ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.2), ncol=3, frameon=False, fontsize=10)
fig.tight_layout(); fig.savefig('figures/fig7_period_shares_en_v64.png'); plt.close(fig)
print('English figures for v64_en written')
