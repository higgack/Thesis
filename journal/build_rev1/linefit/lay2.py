"""Calibrated line model of the author's Word (from the author's PDF, 2026-11-25).
Widths in 1/1000 em. HY신명조 Latin widths from the embedded font; Hangul 1000; space 500."""
import json, re, os
_F = json.load(open(os.path.join(os.path.dirname(__file__), 'fontw.json'), encoding='utf8'))['BCDEEE+H2mjsM:ansi']
SPECIAL = {'₀': 400, '–': 500, '—': 500, '−': 500, 'ö': 500, 'ä': 500, 'ë': 500, 'µ': 500, '​': 0, '\xa0': 500, ' ': 500}
H = lambda c: '가' <= c <= '힣'
def cw(c):
    if c in SPECIAL: return SPECIAL[c]
    if H(c): return 1000
    if c in _F and _F[c] > 0: return _F[c]
    return 1000
NO_START = set(',.)]}%:;!?’”·')
NO_END = set('([{‘“')
HANG = set(',.')
RULES = dict(h_open=True, close_h=True, dash_h=True, h_dash=True, latin_h=True, h_latin=True, digit_h=True, h_digit=True, slash=False, mid=True)
def can_break(a, b):
    """break allowed between chars a and b (no space)"""
    if b in NO_START or a in NO_END: return False
    if GENERIC:
        if a in '–—\u200b': return True
        if a == '-' and b.isalpha(): return True
        return wide_ctx(a) or wide_ctx(b)
    if a == '​': return True
    if H(a) and H(b): return True
    if H(a) and b in '([': return RULES['h_open']
    if a in ')]' and H(b): return RULES['close_h']
    if a in '–—': return RULES['dash_h']
    if H(a) and b in '–—': return RULES['h_dash']
    if a.isalpha() and a.isascii() and H(b): return RULES['latin_h']
    if H(a) and b.isascii() and b.isalpha(): return RULES['h_latin']
    if a.isdigit() and H(b): return RULES['digit_h']
    if H(a) and b.isdigit(): return RULES['h_digit']
    if a == '/' : return RULES['slash']
    if a == '·' and H(b): return RULES['mid']
    return False
SH, SL = 500, 250
GENERIC = True
def wide_ctx(c): return H(c) or (c not in SPECIAL and not c.isascii() and cw(c) >= 1000)
def sw(text, i):
    a = text[i-1] if i > 0 else ''; b = text[i+1] if i + 1 < len(text) else ''
    return SH if (a and wide_ctx(a)) or (b and wide_ctx(b)) else SL
def width(text, a, b):
    return sum((sw(text, i) if text[i] == ' ' else cw(text[i])) for i in range(a, b))
def layout(text, cap=36073, indent=2000):
    """returns list of (start, end) char indices per line (end exclusive, trailing space excluded)"""
    n = len(text); lines = []; start = 0; first = True
    while start < n:
        limit = cap - (indent if first else 0)
        w = 0; last_ok = None; i = start
        while i < n:
            c = text[i]
            if i > start and (c == ' ' or can_break(text[i-1], c)):
                last_ok = i  # break before i (if c is space, the space is dropped)
            w += sw(text, i) if c == ' ' else cw(c)
            if w > limit:
                # hanging punctuation: allow if this char is , or . and it is a valid end
                if c in HANG and (i + 1 >= n or text[i+1] == ' ' or can_break(c, text[i+1])):
                    i += 1; last_ok = i; break
                break
            i += 1
        else:
            lines.append((start, n)); break
        end = last_ok if last_ok and last_ok > start else i
        e2 = end
        while e2 > start and text[e2-1] == ' ': e2 -= 1
        lines.append((start, e2))
        start = end
        while start < n and text[start] == ' ': start += 1
        first = False
    return lines
def gaps(text, cap=36073, indent=2000):
    """per line: (line text, slack in 1/1000 em, n spaces, gap ratio or None for last line)"""
    out = []; ls = layout(text, cap, indent)
    for k, (a, b) in enumerate(ls):
        s = text[a:b]; w = width(text, a, b) + (indent if k == 0 else 0)
        nsp = s.count(' '); last = k == len(ls) - 1
        slack = cap - w
        spw = [sw(text, i) for i in range(a, b) if text[i] == ' ']
        g = None if last else ((sum(spw) + max(slack, 0)) / sum(spw) if nsp else 99)
        out.append((s, slack, nsp, g))
    return out
