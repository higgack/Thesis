# -*- coding: utf-8 -*-
"""Figure (v32): technology network of H01L21/H01L24 symbols carried by 30 or more applications.
Node size = number of applications, edge width = number of applications carrying both symbols
(edges with 10 or more shown). The four core symbols (H01L24 symbols carried by 200 or more
applications) are drawn in a darker colour."""
import itertools, json
import matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt
import koreanize_matplotlib  # noqa
import numpy as np, pandas as pd, networkx as nx
from matplotlib.lines import Line2D
plt.rcParams.update({'font.size': 10, 'figure.dpi': 300, 'savefig.dpi': 300})
A = pd.read_pickle('data/app.pkl'); A['aid'] = A.appln_id.astype(str)
for enc in ('utf-8', 'cp949', 'latin1'):
    try: D = pd.read_csv('data/HB.csv', encoding=enc); break
    except UnicodeDecodeError: pass
D['sym'] = D.cpc_class_symbol.str.replace(r'\s+', '', regex=True).str.replace('H01L', '', regex=False)
D['aid'] = D.appln_id.astype(str)
D = D.drop_duplicates(['aid', 'sym']); D = D[D.aid.isin(set(A.aid))]
CORE = set(json.load(open('robustness/results_v32.json'))['core_symbols'])
cnt = D.groupby('sym').aid.nunique()
keep = set(cnt[cnt >= 30].index)
G = nx.Graph()
for s in keep: G.add_node(s, n=int(cnt[s]))
for a, g in D.groupby('aid'):
    ss = sorted(set(g.sym) & keep)
    for u, v in itertools.combinations(ss, 2):
        G.add_edge(u, v, weight=G[u][v]['weight'] + 1 if G.has_edge(u, v) else 1)
G.remove_edges_from([(u, v) for u, v, w in G.edges(data='weight') if w < 10])
G.remove_nodes_from([n for n in list(G.nodes) if G.degree(n) == 0])
pos = nx.kamada_kawai_layout(G, weight=None)
# push apart nodes whose discs would overlap (24/80 and 24/08 share almost all neighbours)
X = np.array([pos[n] for n in G]); nodes = list(G)
upi = (X[:, 0].max() - X[:, 0].min()) / 5.6                          # data units per inch of drawing width
rad = np.array([np.sqrt(20 + 1.1 * G.nodes[n]['n']) / 2 / 72 * upi for n in nodes])
for _ in range(300):
    moved = False
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            d = X[j] - X[i]; dist = np.linalg.norm(d) + 1e-9; need = 1.25 * (rad[i] + rad[j])
            if dist < need:
                step = (need - dist) / 2 * d / dist; X[i] -= step; X[j] += step; moved = True
    if not moved: break
pos = {n: X[k] for k, n in enumerate(nodes)}
col = {n: ('#b5441b' if n in CORE else ('#f0b36b' if n.startswith('24/') else '#1f3b5c')) for n in G}
fig, ax = plt.subplots(figsize=(6.3, 5.2))
ws = np.array([w for _, _, w in G.edges(data='weight')])
nx.draw_networkx_edges(G, pos, ax=ax, width=0.3 + 4.5 * ws / ws.max(), alpha=0.35, edge_color='#8a8a8a')
order = sorted(G.nodes, key=lambda n: (n in CORE, G.nodes[n]['n']))
nx.draw_networkx_nodes(G, pos, nodelist=order, ax=ax, node_color=[col[n] for n in order],
                       node_size=[20 + 1.1 * G.nodes[n]['n'] for n in order], linewidths=0.6, edgecolors='white')
for n in G:
    x, y = pos[n]
    big = n in CORE
    r = np.sqrt(20 + 1.1 * G.nodes[n]['n']) / 2 / 72 * upi
    ax.text(x, y + r + 0.012, n, fontsize=8 if big else 6.5, ha='center', va='bottom',
            color='#8a2c0e' if big else ('#1f3b5c' if n.startswith('21/') else '#5a3a10'), fontweight='bold' if big else 'normal')
ax.axis('off')
handles = [Line2D([0], [0], marker='o', color='w', markerfacecolor='#b5441b', markersize=9, label='핵심 기술(H01L24, 출원 200건 이상)'),
           Line2D([0], [0], marker='o', color='w', markerfacecolor='#f0b36b', markersize=7, label='기타 H01L24 접합·연결 기호'),
           Line2D([0], [0], marker='o', color='w', markerfacecolor='#1f3b5c', markersize=7, label='H01L21 제조공정 기호')]
fig.legend(handles=handles, loc='lower center', ncol=2, frameon=False, fontsize=8.5)
fig.tight_layout(rect=(0, 0.08, 1, 1)); fig.savefig('figures/fig_core_network_ko.png'); plt.close(fig)
print('nodes', G.number_of_nodes(), 'edges', G.number_of_edges(), 'h21 nodes', sum(n.startswith('21/') for n in G))
