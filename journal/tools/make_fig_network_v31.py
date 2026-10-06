# -*- coding: utf-8 -*-
"""Figure: CPC symbol co-occurrence networks, 2015–2019 vs 2020–2024 (v31).
Node colour = technical area, node size = betweenness centrality; the three H01L21 symbols
with the highest betweenness in each period are labelled."""
import itertools, os
import matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt
import koreanize_matplotlib  # noqa
import numpy as np, pandas as pd, networkx as nx
from matplotlib.lines import Line2D
LANG = os.environ.get('LANG_FIG', 'ko'); SUF = '_ko' if LANG == 'ko' else '_en'
T = (lambda ko, en: ko if LANG == 'ko' else en)
plt.rcParams.update({'font.size': 11, 'figure.dpi': 300, 'savefig.dpi': 300})
A = pd.read_pickle('data/app.pkl'); A['aid'] = A.appln_id.astype(str)
D = None
for enc in ('utf-8', 'cp949', 'latin1'):
    try: D = pd.read_csv('data/HB.csv', encoding=enc); break
    except UnicodeDecodeError: pass
D['sym'] = D.cpc_class_symbol.str.replace(r'\s+', '', regex=True); D['aid'] = D.appln_id.astype(str)
D = D.drop_duplicates(['aid', 'sym']); D = D[D.aid.isin(set(A.aid))]
D['cls'] = D.sym.str[4:6]; D['mg'] = D.sym.str.extract(r'/(\d{2})')[0].astype(int)
ASM = {48, 50, 51, 52, 53, 54, 55, 56, 57, 58, 60, 78}; EQUIP = {67, 68}
D['area'] = [('P_asm' if m in ASM else 'P_equip' if m in EQUIP else 'P_wafer') if c == '21' else 'B' for c, m in zip(D.cls, D.mg)]
AREA = dict(zip(D.sym, D.area)); SYMS = {a: sorted(g.sym.unique()) for a, g in D.groupby('aid')}
COL = {'P_wafer': '#1f3b5c', 'P_equip': '#8c8c8c', 'P_asm': '#9ec1e6', 'B': '#e3a33b'}
LAB = {'P_wafer': T('H01L21: 웨이퍼 제조공정', 'H01L21: wafer fabrication'), 'P_equip': T('H01L21: 공정 장치', 'H01L21: process equipment'),
       'P_asm': T('H01L21: 조립 단계 공정', 'H01L21: assembly-stage processes'), 'B': T('H01L24: 접합·연결', 'H01L24: bonding and connection')}
fig, axes = plt.subplots(1, 2, figsize=(6.3, 3.6))
for ax, p in zip(axes, ['2015–2019', '2020–2024']):
    G = nx.Graph()
    for a in A[A.period == p].aid:
        for s in SYMS[a]: G.add_node(s)
        for s, t in itertools.combinations(SYMS[a], 2):
            G.add_edge(s, t, weight=G[s][t]['weight'] + 1 if G.has_edge(s, t) else 1)
    G = G.subgraph(max(nx.connected_components(G), key=len)).copy()
    bt = nx.betweenness_centrality(G, normalized=True)
    pos = nx.spring_layout(G, seed=7, k=0.35, iterations=200, weight=None)
    nx.draw_networkx_edges(G, pos, ax=ax, width=0.25, alpha=0.18, edge_color='#999999')
    order = sorted(G.nodes(), key=lambda n: AREA[n] == 'B')
    nx.draw_networkx_nodes(G, pos, nodelist=order, ax=ax, node_color=[COL[AREA[n]] for n in order],
                           node_size=[8 + 900 * bt[n] for n in order], linewidths=0.3, edgecolors='white')
    top21 = [n for n in sorted(bt, key=lambda k: -bt[k]) if AREA[n] != 'B'][:3]
    cx, cy = np.mean([pos[n][0] for n in G]), np.mean([pos[n][1] for n in G])
    used = []
    for k, n in enumerate(top21):
        x, y = pos[n]; v = np.array([x - cx, y - cy]); v = v / (np.linalg.norm(v) + 1e-9)
        off = 16 * v + np.array([0, 9 if k % 2 == 0 else -9])
        for ux, uy in used:                      # push apart labels that would collide
            if abs(ux - (x * 100 + off[0])) < 40 and abs(uy - (y * 100 + off[1])) < 12: off = off + np.array([0, -16])
        used.append((x * 100 + off[0], y * 100 + off[1]))
        ax.annotate(n.replace('H01L', ''), (x, y), xytext=tuple(off), textcoords='offset points', fontsize=7.5,
                    ha='left' if off[0] >= 0 else 'right', va='center',
                    arrowprops=dict(arrowstyle='-', lw=0.5, color='#555'),
                    bbox=dict(boxstyle='round,pad=0.15', fc='white', ec='#777', lw=0.5, alpha=0.95))
    ax.set_title(f"{p} ({T('기호', 'symbols')} {G.number_of_nodes()})", fontsize=10)
    ax.axis('off')
handles = [Line2D([0], [0], marker='o', color='w', markerfacecolor=COL[k], markersize=7, label=LAB[k]) for k in ['P_wafer', 'P_equip', 'P_asm', 'B']]
fig.legend(handles=handles, loc='lower center', ncol=2, frameon=False, fontsize=8.5)
fig.tight_layout(rect=(0, 0.12, 1, 1)); fig.savefig(f'figures/fig_network{SUF}.png'); plt.close(fig)
print('saved')
