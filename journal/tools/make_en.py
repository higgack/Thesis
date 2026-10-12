"""Assemble manuscript_v<N>_en.md, the English version of the Korean submission manuscript_v<N>_ko.md,
from sources/en_v<N>/*.md (same version number: language variants keep the number, cf. v9_ko / v9_en_TASM).

Usage: python3 tools/make_en.py 64

References: the list of manuscript_v<N>_ko.md (same entries and order); an entry in sources/en_v<N>/refs_override.md
replaces the entry with the same first author and year (the Korean-language web page, cited in English).
Audits (any failure -> exit 1):
  - citations in the English text <-> reference list, both directions, and the same (first author, year) set
    as the citations of the Korean text
  - Table/Figure numbering: first mentions ascending, captions ascending, the two sets equal (as in make_easy_ko.py)
  - numbers: the set of numbers with a decimal point, a thousands separator or 3+ digits is the same in the English
    and the Korean manuscript (main text + appendix); count differences are printed for review
  - no Hangul left in the English manuscript
"""
import re, sys, pathlib, collections
VER = sys.argv[1] if len(sys.argv) > 1 else '64'
src = pathlib.Path(f'sources/en_v{VER}')
order = ['00_front.md', '01_intro.md', '02_theory.md', '03_context.md', '04_data.md', '05_results.md', '06_discussion.md']
body = '\n'.join((src / f).read_text(encoding='utf-8').rstrip() + '\n' for f in order)
appendix = (src / '07_appendix.md').read_text(encoding='utf-8')
ko = pathlib.Path(f'manuscript_v{VER}_ko.md').read_text(encoding='utf-8')
ko_body, rest = ko.split('## 참고문헌 (References)')
ko_refs_txt, ko_app = rest.split('\n## 부록\n')
ko_app = '## 부록\n' + ko_app
refs = [l.strip() for l in ko_refs_txt.splitlines() if l.strip()]

def key(ref):
    first = re.match(r'^(European Patent Office|Applied Materials|[A-Z][A-Za-zÀ-ÿ\'’\-]+(?: [A-Z][a-z]+)?)', ref).group(1)
    yr = re.search(r'\b((?:19|20)\d\d[a-z]?)\.', ref).group(1)
    return (first, yr)
over = {key(l): l.strip() for l in (src / 'refs_override.md').read_text(encoding='utf-8').splitlines() if l.strip()}
refs = [over.get(key(r), r) for r in refs]
assert all(k in {key(r) for r in refs} for k in over), 'override without a matching entry'
text = body + '\n## References\n\n' + '\n\n'.join(refs) + '\n\n' + appendix
pathlib.Path(f'manuscript_v{VER}_en.md').write_text(text, encoding='utf-8')
fails = 0

# --- citations (English): "Surname (2001)", "Surname et al. (2001)", "Surname and Other (2012)", "(Surname, 2001; ...)",
#     "Kodama's (1992)", "Lau (2021, 2022)"
NOT_NAMES = {'In', 'From', 'Of', 'By', 'Up', 'To', 'For', 'The', 'Table', 'Tables', 'Figure', 'Section', 'Sections', 'Appendix',
             'Step', 'Global', 'Patent Office', 'PATSTAT Global', 'CPC', 'H01L21', 'H01L24', 'Formation', 'Diffusion',
             'Reconfiguration', 'Baseline', 'Integrated', 'RQ1', 'RQ2', 'RQ3', 'Relative', 'Second', 'Third', 'First'}
def cites_en(t):
    out = set()
    for m in re.finditer(r"([A-Z][A-Za-zÀ-ÿ'’\-]+(?: [A-Z][a-z]+)?)(?: et al\.| and [A-Z][A-Za-zÀ-ÿ'’\-]+)?,? ?\(?((?:19|20)\d\d[a-z]?)", t):
        out.add((re.sub(r"['’]s$", '', m.group(1)), m.group(2)))
    for m in re.finditer(r'(European Patent Office|Applied Materials),? ?\(?((?:19|20)\d\d)', t):
        out.add((m.group(1), m.group(2)))
    for m in re.finditer(r"([A-Z][A-Za-z\-]+)(?:['’]s)?,? ?\(?((?:19|20)\d\d), ((?:19|20)\d\d)", t):
        out.add((m.group(1), m.group(3)))
    return {c for c in out if c[0] not in NOT_NAMES}
def cites_ko(t):   # the Korean regex of make_easy_ko.py
    out = set()
    for m in re.finditer(r'([A-Z][A-Za-zÀ-ÿ\'’\-]+(?: [A-Z][a-z]+)?)(?: et al\.| and [A-Z][A-Za-zÀ-ÿ\'’\-]+|[과와] [A-Z][A-Za-zÀ-ÿ\'’\-]+)?,? ?\(?((?:19|20)\d\d[a-z]?)', t):
        out.add((m.group(1), m.group(2)))
    for m in re.finditer(r'(European Patent Office|Applied Materials|Yole Group),? ?\(?((?:19|20)\d\d)', t):
        out.add((m.group(1), m.group(2)))
    for m in re.finditer(r'([A-Z][A-Za-z\-]+),? ?\(?((?:19|20)\d\d), ((?:19|20)\d\d)', t):
        out.add((m.group(1), m.group(3)))
    return {c for c in out if c[0] not in ('PATSTAT Global', 'Patent Office', 'CPC', 'H01L21', 'H01L24', 'Global', 'Materials')}
refkeys = {key(r) for r in refs}
def trim(cs):   # 'Whereas Kapoor (2013)': a sentence word caught by the two-word surname pattern
    return {(n.split()[-1], y) if ' ' in n and (n, y) not in refkeys and (n.split()[-1], y) in refkeys else (n, y) for n, y in cs}
cen = trim(cites_en(body + appendix)); cko = trim(cites_ko(ko_body + ko_app))
missing = sorted(cen - refkeys); uncited = sorted(refkeys - cen)
print('references', len(refs), '| cited (en)', len(cen), '| cited (ko)', len(cko))
print('cited but not in the reference list:', missing); fails += len(missing)
print('references never cited:', uncited); fails += len(uncited)
print('cited in en only:', sorted(cen - cko), '| in ko only:', sorted(cko - cen)); fails += len(cen ^ cko)

# --- Table/Figure numbering
for kind in ('Table', 'Figure'):
    first = []
    for m in re.finditer(rf'{kind}s? (\d+)', body):
        n = int(m.group(1))
        if n not in first: first.append(n)
    caps = [int(m.group(1)) for m in re.finditer(rf'^\*\*{kind} (\d+)\.\*\*', body, re.M)]
    ok = first == sorted(first) and caps == sorted(caps) and set(caps) == set(first)
    print(kind, first, caps, 'OK' if ok else '!! MISMATCH'); fails += (not ok)
# "Tables 12 and 13" style plural mentions are covered by the plural pattern above (first number only)

# --- numbers, English vs Korean (main text + appendix; references excluded)
pat = r'(?<![\d./])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\d])'
def nums(t):
    return collections.Counter(x for x in re.findall(pat, t) if '.' in x or ',' in x or len(x) >= 3)
nen = nums(body + appendix); nko = nums(ko_body + ko_app)
only_en = sorted(set(nen) - set(nko)); only_ko = sorted(set(nko) - set(nen))
print('numbers only in en:', only_en); print('numbers only in ko:', only_ko); fails += len(only_en) + len(only_ko)
diff = {x: (nen[x], nko[x]) for x in set(nen) | set(nko) if nen[x] != nko[x]}
print('count differences (en, ko) for review:', dict(sorted(diff.items())))

# --- leftovers
hangul = re.findall(r'[가-힣]+', text)
print('Hangul left:', hangul[:10]); fails += len(hangul)
words = len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’\-]*", body))
print('words (front + sections 1-7, incl. tables and captions):', words, '| FAILS', fails)
sys.exit(1 if fails else 0)
