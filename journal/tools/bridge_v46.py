"""Thesis v46: where fabrication processes meet bonding, by filing period.

(1) Jaccard coefficient between each H01L21 area and H01L24 over the applications of a period:
    J = both / (n_area + n_bonding - both). Areas as in Section 4.2: packaging-stage processes
    (main groups 48, 50-58, 60, 78), processing apparatus (67, 68), wafer fabrication (the rest).
(2) Period technology networks built as in Section 4.4 (symbols as nodes, co-assignment as links):
    hubs = largest node strength, bridges = largest betweenness centrality (unweighted shortest
    paths; check: distance = 1 / weight), and each area's share of total betweenness.
Reads data/HB.csv + data/app.pkl; writes robustness/results_v46.json and sources/ko_v46/numbers_v46.txt.
"""
import json, itertools, pathlib
import pandas as pd, networkx as nx

A = pd.read_pickle('data/app.pkl').copy()
A['aid'] = A.appln_id.astype(str)
for enc in ('utf-8', 'cp949', 'latin1'):
    try:
        D = pd.read_csv('data/HB.csv', encoding=enc); break
    except UnicodeDecodeError:
        pass
D['sym'] = D.cpc_class_symbol.str.replace(r'\s+', '', regex=True).str.replace('H01L', '', regex=False)
D['aid'] = D.appln_id.astype(str)
D = D.drop_duplicates(['aid', 'sym'])
D = D[D.aid.isin(set(A.aid))]
ASM = {48, 50, 51, 52, 53, 54, 55, 56, 57, 58, 60, 78}; EQUIP = {67, 68}
def area(s):
    if s.startswith('24/'):
        return 'bonding'
    g = int(s[3:5])
    return 'packaging' if g in ASM else ('apparatus' if g in EQUIP else 'wafer')
D['area'] = D.sym.map(area)
SYMS = D.groupby('aid').sym.apply(lambda s: sorted(set(s))).to_dict()
per = dict(zip(A.aid, A.period))
PERIODS = ['–2014', '2015–2019', '2020–2024']
AREAS = ['wafer', 'apparatus', 'packaging']
out = {'h01l21_records': {a: int((D.area == a).sum()) for a in AREAS}}

# (1) area-level Jaccard with H01L24
has = D.groupby(['aid', 'area']).size().unstack(fill_value=0).gt(0)
has['period'] = has.index.map(per)
jac = {}
for p in PERIODS:
    S = has[has.period == p]; nb = int(S.bonding.sum())
    jac[p] = {}
    for a in AREAS:
        both = int((S[a] & S.bonding).sum()); na = int(S[a].sum())
        jac[p][a] = dict(both=both, n_area=na, n_bonding=nb, jaccard=both / (na + nb - both))
out['area_jaccard'] = jac

# (2) period networks: hubs and bridges
def graph(ids):
    G = nx.Graph()
    for a in ids:
        s = SYMS[a]
        G.add_nodes_from(s)
        for u, v in itertools.combinations(s, 2):
            if G.has_edge(u, v): G[u][v]['weight'] += 1
            else: G.add_edge(u, v, weight=1)
    for u, v, d in G.edges(data=True):
        d['dist'] = 1.0 / d['weight']
    return G
net = {}
for p in PERIODS:
    G = graph([a for a in SYMS if per[a] == p])
    st = dict(G.degree(weight='weight'))
    res = dict(nodes=G.number_of_nodes(), edges=G.number_of_edges(),
               hubs=[dict(sym=n, strength=int(st[n])) for n in sorted(st, key=lambda k: (-st[k], k))[:5]])
    for key, w in (('unweighted', None), ('weighted', 'dist')):
        bt = nx.betweenness_centrality(G, normalized=True, weight=w); tot = sum(bt.values())
        share = {a: sum(v for n, v in bt.items() if area(n) == a) / tot for a in AREAS + ['bonding']}
        top21 = [dict(sym=n, b=bt[n], area=area(n)) for n in sorted(bt, key=lambda k: (-bt[k], k)) if not n.startswith('24/')][:6]
        top = [dict(sym=n, b=bt[n]) for n in sorted(bt, key=lambda k: (-bt[k], k))[:5]]
        res[key] = dict(share=share, top21=top21, top=top)
    net[p] = res
out['network'] = net
json.dump(out, open('robustness/results_v46.json', 'w'), ensure_ascii=False, indent=1)

print('H01L21 records by area', out['h01l21_records'])
for p in PERIODS:
    print(p, ' '.join(f"{a}: J={jac[p][a]['jaccard']:.3f} (both {jac[p][a]['both']}, n {jac[p][a]['n_area']}, B {jac[p][a]['n_bonding']})" for a in AREAS))
for p in PERIODS:
    r = net[p]
    print(p, 'nodes', r['nodes'], 'edges', r['edges'], 'hubs', [(h['sym'], h['strength']) for h in r['hubs']])
    for key in ('unweighted', 'weighted'):
        print('  ', key, 'share', {a: round(100 * v, 1) for a, v in r[key]['share'].items()})
        print('  ', key, 'top21', [(d['sym'], round(d['b'], 4), d['area']) for d in r[key]['top21']])
        print('  ', key, 'top', [(d['sym'], round(d['b'], 4)) for d in r[key]['top']])

NUMS = set()
for p in PERIODS:
    for a in AREAS:
        d = jac[p][a]; NUMS |= {f"{d['jaccard']:.3f}", str(d['both']), str(d['n_area']), str(d['n_bonding'])}
    for key in ('unweighted', 'weighted'):
        NUMS |= {f"{100 * v:.1f}" for v in net[p][key]['share'].values()}
        NUMS |= {f"{d['b']:.3f}" for d in net[p][key]['top21']}
    NUMS |= {str(h['strength']) for h in net[p]['hubs']} | {f"{h['strength']:,}" for h in net[p]['hubs']}
pathlib.Path('sources/ko_v46').mkdir(parents=True, exist_ok=True)
pathlib.Path('sources/ko_v46/numbers_v46.txt').write_text('\n'.join(sorted(NUMS)) + '\n', encoding='utf-8')
print('numbers', len(NUMS))
