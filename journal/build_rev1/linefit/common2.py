import sys, re, json, itertools, runpy
import lay, lay2, docx
THR = 1.5
def load(docx_path):
    d = docx.Document(docx_path)
    return {pi: (t, (36073 if W == 72 else 39680), (2000 if ind else 0)) for pi, p, t, W, ind in lay.paras(d)}, d
def apply(t, E):
    for e in E:
        o, n = e[0], e[1]
        if o in t: t = t.replace(o, n)
    return t
def ratios(t, cap, ind):
    out = []
    for s, slack, nsp, g in lay2.gaps(t, cap, ind):
        if g is None: out.append((s, slack, None)); continue
        # ratio relative to natural spaces (500 near Hangul, 333 otherwise) to compare with PDF metric
        nat = 0; 
        i0 = t.find(s)
        out.append((s, slack, g))
    return out
def gapr(t, cap, ind):
    """ratio = (natural space total + slack) / natural total, natural = 500 near Hangul else 333"""
    res = []
    ls = lay2.layout(t, cap, ind)
    for k, (a, b) in enumerate(ls):
        s = t[a:b]; last = k == len(ls) - 1
        w = sum((lay2.sw(t, i) if t[i] == ' ' else lay2.cw(t[i])) for i in range(a, b)) + (ind if k == 0 else 0)
        nat = sum((500 if lay2.sw(t, i) == lay2.SH else 333) for i in range(a, b) if t[i] == ' ')
        used = sum(lay2.sw(t, i) for i in range(a, b) if t[i] == ' ')
        slack = cap - w
        r = None if last or not nat else (used + slack) / nat
        res.append((s, slack, r))
    return res
