"""Extract per-character advance widths (1/1000 em) for fonts in a Word PDF. usage: pdffontw.py file.pdf out.json"""
import re, sys, zlib, json
b = open(sys.argv[1], 'rb').read()
objs = {int(m.group(1)): m.group(2) for m in re.finditer(rb'(\d+) 0 obj(.*?)endobj', b, re.S)}
def stream(o):
    m = re.search(rb'stream\r?\n(.*?)\r?\nendstream', o, re.S)
    data = m.group(1)
    return zlib.decompress(data) if b'/FlateDecode' in o else data
# expand object streams
for n, o in list(objs.items()):
    if b'/Type/ObjStm' in o:
        N = int(re.search(rb'/N (\d+)', o).group(1)); F = int(re.search(rb'/First (\d+)', o).group(1))
        s = stream(o); hdr = list(map(int, s[:F].split()))
        offs = [(hdr[i], hdr[i+1]) for i in range(0, 2*N, 2)]
        for k, (num, off) in enumerate(offs):
            end = offs[k+1][1] if k + 1 < N else len(s) - F
            objs[num] = s[F+off:F+end]
def ref(o, key):
    m = re.search(rb'/' + key + rb' (\d+) 0 R', o); return int(m.group(1)) if m else None
def arr(o):
    o = o.strip(); return o
res = {}
for n, o in objs.items():
    if not re.search(rb"/Type\s*/Font", o) or b"/BaseFont" not in o or b"CIDFontType" in o: continue
    name = re.search(rb'/BaseFont\s*/([^/\s>]+)', o).group(1).decode()
    if b'/Type0' in o:
        desc = objs[int(re.search(rb'/DescendantFonts\s*\[?\s*(\d+) 0 R', o).group(1))]
        m_ = re.match(rb'\s*\[\s*(\d+) 0 R\s*\]', desc)
        if m_: desc = objs[int(m_.group(1))]
        dw = re.search(rb'/DW (\d+)', desc); dw = int(dw.group(1)) if dw else 1000
        wref = ref(desc, b'W'); wtxt = objs[wref] if wref else re.search(rb'/W\s*(\[.*\])', desc, re.S).group(1)
        toks = re.findall(rb'\[|\]|[\d.]+', wtxt)
        widths = {}; i = 0
        # parse W array: c [w1 w2 ...] or c1 c2 w
        toks = toks[1:-1] if toks and toks[0] == b'[' else toks
        while i < len(toks):
            c = int(toks[i]); i += 1
            if toks[i] == b'[':
                i += 1; k = 0
                while toks[i] != b']': widths[c + k] = float(toks[i]); i += 1; k += 1
                i += 1
            else:
                c2 = int(toks[i]); w = float(toks[i+1]); i += 2
                for c_ in range(c, c2 + 1): widths[c_] = w
        tu = stream(objs[ref(o, b'ToUnicode')]).decode('latin1')
        m2u = {}
        for blk in re.findall(r'beginbfchar(.*?)endbfchar', tu, re.S):
            for a, u in re.findall(r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', blk):
                m2u[int(a, 16)] = bytes.fromhex(u).decode('utf-16-be')
        for blk in re.findall(r'beginbfrange(.*?)endbfrange', tu, re.S):
            for a, z, u in re.findall(r'<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>', blk):
                for k in range(int(a, 16), int(z, 16) + 1):
                    m2u[k] = chr(int(u, 16) + k - int(a, 16))
        res[name + ':cid'] = {u: widths.get(c, dw) for c, u in m2u.items()}
    else:
        fc = int(re.search(rb'/FirstChar (\d+)', o).group(1))
        wref = ref(o, b'Widths'); wtxt = objs[wref] if wref else re.search(rb'/Widths\s*(\[.*?\])', o, re.S).group(1)
        ws = [float(x) for x in re.findall(rb'[\d.]+', wtxt)]
        cp1252 = bytes(range(fc, fc + len(ws))).decode('cp1252', errors='replace')
        res[name + ':ansi'] = {ch: w for ch, w in zip(cp1252, ws)}
res = {k: {c: w for c, w in v.items() if not any(0xD800 <= ord(x) <= 0xDFFF for x in c)} for k, v in res.items()}
json.dump(res, open(sys.argv[2], 'w', encoding='utf8'), ensure_ascii=False, indent=0)
for k, v in res.items(): print(k, len(v), ''.join(c for c in v if not ('가' <= c <= '힣'))[:120])
