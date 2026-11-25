"""List justified lines in the author's PDF whose word spaces are stretched. usage: pdfwide.py lines.json thr"""
import sys, json
P = json.load(open(sys.argv[1], encoding='utf8')); thr = float(sys.argv[2])
out = []
for pg, lines in enumerate(P, 1):
    for li, ws in enumerate(lines):
        if len(ws) < 3: continue
        h = ws[0][3] - ws[0][1]
        if ws[-1][2] < 494: continue           # not reaching right margin -> last line / centered
        if ws[0][0] < 95 or ws[0][0] > 125: continue  # not body column
        gaps = [ws[i+1][0] - ws[i][2] for i in range(len(ws) - 1)]
        norm = 5.5 if h > 10.5 else 5.0
        r = sum(gaps) / len(gaps) / norm
        if r >= thr: out.append((pg, li, round(r, 2), round(h, 1), ' '.join(w[4] for w in ws)))
for o in out: print(o)
print(len(out), 'lines')
