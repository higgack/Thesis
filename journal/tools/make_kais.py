"""Build the KAIS-style (한국산학기술학회논문지 형식) condensed journal version.

Sources: sources/ko_kais/*.md (author-year citations in the text).
- converts author-year citations to numbered [n] in order of first appearance
- builds a numbered reference list in the journal's format from the Harvard entries of manuscript_v24_ko.md
- audits: unresolved citations, unused references, Table/Fig order, numbers vs the v9/S1/S2/extra pool
Usage: python3 tools/make_kais.py [--base 24]
"""
import re, sys, pathlib, argparse
ap = argparse.ArgumentParser(); ap.add_argument('--base', default='24'); ap.add_argument('--src', default='sources/ko_kais'); ap.add_argument('--out', default=None); a = ap.parse_args()
src = pathlib.Path(a.src)
order = sorted(f.name for f in src.glob('0*.md'))
text = '\n'.join((src/f).read_text(encoding='utf-8').rstrip()+'\n' for f in order)
base = pathlib.Path(f'manuscript_v{a.base}_ko.md').read_text(encoding='utf-8')
harvard = [l for l in base.split('## 참고문헌')[1].split('## 부록')[0].splitlines() if re.match(r'^[A-Z]', l)]

# ---------- reference parsing (Harvard -> KAIS numbered style) ----------
def key_of(ref):
    first = re.match(r'^(European Patent Office|KnowMade|[A-Z][A-Za-zÀ-ÿ\-]+)', ref).group(1)
    yr = re.search(r', ((?:19|20)\d\d)\. ', ref).group(1)
    return (first, yr)
def authors_kais(auth):
    # "Curran, C.-S., Bröring, S., Leker, J." -> "C. S. Curran and S. Bröring and J. Leker"
    parts = re.findall(r"([A-Z][A-Za-zÀ-ÿ'\-]+(?: [A-Z][a-z]+)?), ((?:[A-Z]\.(?:-[A-Z]\.)?)+)", auth)
    out = []
    for sn, ini in parts:
        ini = re.sub(r'\s+', ' ', ini.replace('.-', '. ').replace('.', '. ')).strip()
        out.append(f"{ini} {sn}")
    return ' and '.join(out)
MANUAL = {
 ('Arden','2010'): 'W. Arden and M. Brillouët and P. Cogez and M. Graef and B. Huizing and R. Mahnkopf, "More-than-Moore" White Paper, White paper, International Technology Roadmap for Semiconductors (ITRS), 2010.',
 ('Brown','2009'): 'C. Brown and G. Linden, Chips and Change: How Crisis Reshapes the Semiconductor Industry, MIT Press, Cambridge, MA, 2009.',
 ('Cameron','2013'): 'A. C. Cameron and P. K. Trivedi, Regression Analysis of Count Data, 2nd ed., Cambridge University Press, Cambridge, 2013.',
 ('European Patent Office','2025'): 'European Patent Office, PATSTAT Global (2025 edition), Database, EPO, Vienna, 2025.',
 ('Hosmer','2013'): 'D. W. Hosmer and S. Lemeshow and R. X. Sturdivant, Applied Logistic Regression, 3rd ed., Wiley, Hoboken, NJ, 2013.',
 ('KnowMade','2024'): 'KnowMade, Hybrid Bonding Patent Landscape Analysis 2024, Industry report, KnowMade (Yole Group), Sophia Antipolis, 2024, https://www.knowmade.com/patent-analytics-services/patent-report/semiconductor-patent-landscape/semiconductor-advanced-packaging-patent-landscape/hybrid-bonding-patent-landscape-analysis-2024/',
 ('Kodama','1992'): 'F. Kodama, "Technology fusion and the new R&D", Harvard Business Review, Vol.70, No.4, pp.70-78, 1992.',
 ('Langlois','1999'): 'R. N. Langlois and W. E. Steinmueller, "The evolution of competitive advantage in the worldwide semiconductor industry, 1947–1996", in D. C. Mowery and R. R. Nelson (Eds.), Sources of Industrial Leadership: Studies of Seven Industries, Cambridge University Press, Cambridge, pp.19-78, 1999.',
 ('Macher','2004'): 'J. T. Macher and D. C. Mowery, "Vertical specialization and industry structure in high technology industries", in J. A. C. Baum and A. M. McGahan (Eds.), Business Strategy over the Industry Lifecycle (Advances in Strategic Management, Vol.21), Emerald, Bingley, pp.317-355, 2004.\nDOI: https://doi.org/10.1016/S0742-3322(04)21011-7',
 ('McFadden','1974'): 'D. McFadden, "Conditional logit analysis of qualitative choice behavior", in P. Zarembka (Ed.), Frontiers in Econometrics, Academic Press, New York, pp.105-142, 1974.',
}
def to_kais(ref):
    k = key_of(ref)
    if k in MANUAL: return MANUAL[k]
    m = re.match(r'^(.+?), ((?:19|20)\d\d)\. (.+)$', ref); auth, yr, rest = m.groups()
    m2 = re.search(r' ([A-Z][A-Za-z&:,\- ]+?) (\d+(?:–\d+)?)(?: \(([^)]+)\))?, ([^,.]+)\.(?: (https://doi\.org/\S+))?$', rest)
    assert m2, ref
    title = rest[:m2.start()].rstrip('.'); jnl, vol, no, pages, doi = m2.groups()
    pages = pages.replace('–', '-')
    pg = f"pp.{pages}" if '-' in pages else f"Article {pages}"
    s = f'{authors_kais(auth)}, "{title}", {jnl}, Vol.{vol}' + (f', No.{no}' if no else '') + f', {pg}, {yr}.'
    if doi: s += f'\nDOI: {doi}'
    return s
refmap = {key_of(r): r for r in harvard}

# ---------- citation conversion ----------
numbers = {}          # key -> n
def num(k):
    if k not in refmap: raise KeyError(k)
    if k not in numbers: numbers[k] = len(numbers) + 1
    return numbers[k]
def fmt(ns):
    ns = sorted(set(ns)); out = []; i = 0
    while i < len(ns):
        j = i
        while j + 1 < len(ns) and ns[j+1] == ns[j] + 1: j += 1
        out.append(f'{ns[i]}-{ns[j]}' if j - i >= 2 else ','.join(str(x) for x in ns[i:j+1])); i = j + 1
    return '[' + ','.join(out) + ']'
NAME = r"(?:European Patent Office|KnowMade|[A-Z][A-Za-zÀ-ÿ\-]+)"
CHUNK = re.compile(rf"^\s*({NAME})(?: and {NAME}| et al\.)?,\s*((?:19|20)\d\d(?:,\s*(?:19|20)\d\d)*)\s*$")
def conv_paren(m):
    inner = m.group(1); ns = []
    for chunk in inner.split(';'):
        cm = CHUNK.match(chunk)
        if not cm: return m.group(0)   # not a citation group
        for yr in re.findall(r'(?:19|20)\d\d', cm.group(2)): ns.append(num((cm.group(1), yr)))
    return fmt(ns)
text = re.sub(r'\(([^()]*?(?:19|20)\d\d[^()]*?)\)', conv_paren, text)
# narrative: "Name과 Name(2012)", "Name et al.(2010)", "Name(1963)", "Lau (2021, 2022)"
def conv_narr(m):
    name = m.group(1); yrs = re.findall(r'(?:19|20)\d\d', m.group(3))
    return f"{m.group(0)[:m.start(3)-m.start(0)-1].rstrip()}{fmt([num((name, y)) for y in yrs])}"
text = re.sub(rf"({NAME})((?:[과와] {NAME}| et al\.)?)\s?\(((?:19|20)\d\d(?:, (?:19|20)\d\d)*)\)", conv_narr, text)
left = [x for x in re.findall(r'[A-Z][a-z]+(?: et al\.| and [A-Z][a-z]+)?,? \(?(?:19|20)\d\d\)?', text) if not x.startswith('Global')]
refs_out = [to_kais(refmap[k]) for k, n in sorted(numbers.items(), key=lambda kv: kv[1])]
ref_block = '\n\n'.join(f'[{i+1}] {r}' for i, r in enumerate(refs_out))
out = text + '\n## References\n\n' + ref_block + '\n'
pathlib.Path(a.out or f'manuscript_v{a.base}_ko_KAIS.md').write_text(out, encoding='utf-8')

# ---------- audits ----------
fails = 0
print('references used:', len(numbers), 'of', len(refmap))
unused = sorted(k for k in refmap if k not in numbers); print('unused (dropped from list):', unused)
print('unconverted author-year leftovers:', left[:10]); fails += len(left)
for kind in ('Table', 'Fig\\.'):
    first = []
    for m in re.finditer(rf'{kind} (\d+)', text):
        n = int(m.group(1)); first.append(n) if n not in first else None
    caps = [int(m.group(1)) for m in re.finditer(rf'^\*\*{kind} (\d+)\.\*\*', text, re.M)]
    ok = first == sorted(first) and caps == sorted(caps) and set(caps) == set(first)
    print(kind.replace('\\', ''), first, caps, 'OK' if ok else '!! MISMATCH'); fails += (not ok)
pat = r'(?<![\d./\[])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\d\]])'
def nums(t): return set(x.replace(',', '') for x in re.findall(pat, t))
pool = nums(base)
for f in ('supplement_S1_v9_ko.md', 'supplement_S2_v9_ko.md'): pool |= nums(pathlib.Path(f).read_text(encoding='utf-8'))
extra = pathlib.Path(f'sources/ko_v{a.base}/numbers_extra.txt')
if extra.exists(): pool |= set(extra.read_text().split())
pool |= {'2.7', '1.96'} | set(re.findall(r'^#+ (\d+\.\d+)', text, re.M))
body = re.sub(r'\[\d+(?:[-,]\d+)*\]', '', text)   # drop citation numbers
new = sorted(x for x in nums(body) if x not in pool and ('.' in x or len(x) >= 3))
print('numbers not in pool:', new); fails += len(new)
ko = [l[:60] for l in ref_block.splitlines() if re.search(r'[가-힣]', l)]
print('Korean in references:', ko); fails += len(ko)
print('chars (body, excl. tables):', len('\n'.join(l for l in text.splitlines() if not l.startswith('|'))), 'FAILS', fails)
sys.exit(1 if fails else 0)
