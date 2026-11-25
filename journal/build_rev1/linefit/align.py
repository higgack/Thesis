"""Map docx body paragraphs to the author's PDF lines. Produces {para_index: [line texts...]}.
usage: align.py SP DOCX lines.json out.json"""
import sys, json, re; sys.path.insert(0, sys.argv[1]); import lay, docx
d = docx.Document(sys.argv[2]); P = json.load(open(sys.argv[3], encoding='utf8'))
L = []
for pg, lines in enumerate(P, 1):
    for li, ws in enumerate(lines):
        t = ' '.join(w[4] for w in ws)
        L.append((pg, li, t, re.sub(r'\s', '', t), ws))
out = {}; pos = 0; miss = []
for pi, p, t, W, ind in lay.paras(d):
    T0 = re.sub(r'\s', '', t).replace('​', '')
    head = T0[:12]; found = None
    for k in range(pos, min(pos + 400, len(L))):
        if L[k][3].replace('​', '').startswith(head[:min(len(head), len(L[k][3]))]) and T0.startswith(L[k][3].replace('​','')):
            found = k; break
    if found is None: miss.append(pi); continue
    acc = ''; k = found; lines = []
    while k < len(L) and len(acc) < len(T0):
        s = L[k][3].replace('​', '')
        if T0.startswith(acc + s):
            acc += s; lines.append([L[k][0], L[k][1], L[k][2], [[w[0], w[2], w[4]] for w in L[k][4]]])
        elif lines:  # page header/footer or figure text in between: skip
            pass
        k += 1
        if k - found > 80: break
    if acc == T0:
        out[pi] = lines; pos = k
    else: miss.append(pi)
json.dump(out, open(sys.argv[4], 'w', encoding='utf8'), ensure_ascii=False)
print('aligned', len(out), 'missed', len(miss), miss[:30])
