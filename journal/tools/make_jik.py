"""Journal of Innovation & Knowledge (JIK, Elsevier) format of the English manuscript: manuscript_v<N>_en.md ->
manuscript_v<N>_en_JIK (anonymized manuscript), title_page_v<N>_en_JIK and highlights_v<N>_en_JIK (md + docx).
Same version number as the English text it formats (journal variants keep the number, cf. v9_en_TASM, v24_ko_KAIS).

Usage: python3 tools/make_jik.py 66

Inputs: manuscript_v<N>_en.md; sources/en_v<N>_jik/ title_page.md, highlights.md, back_matter.md, jel.md
JIK requirements applied (guide for authors; the journal uses double-anonymized review):
  - title page as a separate file (title, authors, affiliations, corresponding author, e-mail, acknowledgements,
    funding, declaration of competing interest, CRediT statement); the manuscript keeps no author names/affiliations
  - abstract <= 250 words; 1-6 keywords; JEL codes after the keywords
  - references and in-text citations in APA 7th edition: reference list converted with tools/refs_convert.py
    (books, chapters, reports and web pages in APA_MANUAL below); parenthetical citations use "&" and list the works
    alphabetically, narrative citations keep "and"
  - appendix tables numbered Table A.1, A.2, B.1 (Elsevier style); reference list with a 0.5-inch hanging indent (APA)
  - data availability and the generative-AI declaration as unnumbered sections before the references
  - highlights: 3-5 bullets of at most 85 characters including spaces, in a separate file
Statements only the authors can confirm (funding, competing interest, CRediT, data availability, AI use) are
yellow ([...]{.mark} in the sources).
Audits (any failure -> exit 1): citations <-> references both ways and the same (first author, year) set as v<N>_en;
no " and " left in parenthetical citations; parenthetical lists alphabetical; reference list in APA order; no author
name, affiliation or e-mail in the manuscript; abstract words; keyword count; highlight count and lengths; the text of
the manuscript equals v<N>_en apart from the author block, the citation format, the appendix table numbers and the
added JEL line and back matter.
"""
import re, sys, pathlib, unicodedata, difflib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import refs_convert

VER = sys.argv[1] if len(sys.argv) > 1 else '66'
src = pathlib.Path(f'sources/en_v{VER}_jik')
en = pathlib.Path(f'manuscript_v{VER}_en.md').read_text(encoding='utf-8')
OUT_MS, OUT_TP, OUT_HL = (f'manuscript_v{VER}_en_JIK', f'title_page_v{VER}_en_JIK', f'highlights_v{VER}_en_JIK')

APA_MANUAL = {
 'Applied Materials, 2025.': 'Applied Materials. (2025). *Kinex integrated die-to-wafer hybrid bonding system* [Press release, in Korean; written by J. Kim]. Retrieved October 12, 2026, from https://www.appliedmaterials.com/kr/ko/newsroom/press-releases/251126-next-gen-semi-equipment-ko.html',
 'Arden, W.': 'Arden, W., Brillouët, M., Cogez, P., Graef, M., Huizing, B., & Mahnkopf, R. (2010). *"More-than-Moore" white paper*. International Technology Roadmap for Semiconductors (ITRS).',
 'Brown, C., Linden': 'Brown, C., & Linden, G. (2009). *Chips and change: How crisis reshapes the semiconductor industry*. MIT Press.',
 'European Patent Office': 'European Patent Office. (2025). *PATSTAT Global* (2025 edition) [Database]. EPO.',
 'Hosmer, D.W.': 'Hosmer, D. W., Lemeshow, S., & Sturdivant, R. X. (2013). *Applied logistic regression* (3rd ed.). Wiley.',
 'KnowMade, 2024.': 'KnowMade. (2024). *Hybrid bonding patent landscape analysis 2024* [Industry report]. KnowMade (Yole Group). https://www.knowmade.com/patent-analytics-services/patent-report/semiconductor-patent-landscape/semiconductor-advanced-packaging-patent-landscape/hybrid-bonding-patent-landscape-analysis-2024/',
 'Langlois, R.N.': 'Langlois, R. N., & Steinmueller, W. E. (1999). The evolution of competitive advantage in the worldwide semiconductor industry, 1947–1996. In D. C. Mowery & R. R. Nelson (Eds.), *Sources of industrial leadership: Studies of seven industries* (pp. 19–78). Cambridge University Press.',
 'Macher, J.T.': 'Macher, J. T., & Mowery, D. C. (2004). Vertical specialization and industry structure in high technology industries. In J. A. C. Baum & A. M. McGahan (Eds.), *Business strategy over the industry lifecycle* (Advances in Strategic Management, Vol. 21, pp. 317–355). Emerald. https://doi.org/10.1016/S0742-3322(04)21011-7',
 'McFadden, D.': 'McFadden, D. (1974). Conditional logit analysis of qualitative choice behavior. In P. Zarembka (Ed.), *Frontiers in econometrics* (pp. 105–142). Academic Press.',
}

fails = 0
def fail(msg):
    global fails
    fails += 1
    print('  !!', msg)

# --- split the English manuscript
front, rest = en.split('## Abstract', 1)
abstract_part, body = rest.split('\n---\n', 1)          # abstract + keywords | main text
main, refs_app = body.split('## References', 1)
refs_txt, appendix = refs_app.split('\n## Appendix', 1)
appendix = '## Appendix' + appendix
title = front.strip().split('\n')[0]
assert title.startswith('**Cross-stage') and 'HyungKyu' in front

# --- references -> APA 7
ref_lines = [l.strip() for l in refs_txt.splitlines() if l.strip()]
refs_convert.MANUAL = {}
apa = []
for l in ref_lines:
    k = next((k for k in APA_MANUAL if l.startswith(k)), None)
    if k:
        apa.append(APA_MANUAL[k]); continue
    _, a, un = refs_convert.convert('## References\n' + l)
    if un: fail('reference not converted: ' + l[:80])
    apa.append(a[0])
assert len(apa) == len(ref_lines)

# --- in-text citations: parenthetical groups -> APA ("&", alphabetical)
NAME = r"(?:European Patent Office|Applied Materials|[A-Z][A-Za-zÀ-ÿ'’\-]+)"
ITEM = re.compile(rf"^({NAME})( et al\.| (?:and|&) [A-Z][A-Za-zÀ-ÿ'’\-]+)?, ((?:19|20)\d\d[a-z]?(?:, (?:19|20)\d\d[a-z]?)*)$")
def fold(s):
    return ''.join(c for c in unicodedata.normalize('NFD', s) if not unicodedata.combining(c)).lower()
def group_items(g):
    pre = re.match(r'^(source: |e\.g\., |see )?', g).group(0)
    items = g[len(pre):].split('; ')
    if all(ITEM.match(i) for i in items):
        return pre, items
    return None
def apa_group(m):
    g = m.group(1); r = group_items(g)
    if r is None:
        return m.group(0)
    pre, items = r
    items = [i.replace(' and ', ' & ') for i in items]
    items.sort(key=lambda i: (fold(ITEM.match(i).group(1)), ITEM.match(i).group(3)))
    return '(' + pre + '; '.join(items) + ')'
PAREN = re.compile(r'\(([^()]*)\)')
main_jik = PAREN.sub(apa_group, main)
appendix_jik = PAREN.sub(apa_group, appendix)

# --- appendix tables: Table A1 -> Table A.1 (also "Tables A1 and A2")
def app_tables(t):
    t = re.sub(r'\b(Tables? )([A-Z])(\d+)\b', r'\1\2.\3', t)
    return re.sub(r'(Tables [A-Z]\.\d+ and )([A-Z])(\d+)\b', r'\1\2.\3', t)
main_jik, appendix_jik = app_tables(main_jik), app_tables(appendix_jik)

# --- assemble
jel = (src / 'jel.md').read_text(encoding='utf-8').strip()
back = (src / 'back_matter.md').read_text(encoding='utf-8').strip()
ms = (title + '\n\n---\n\n## Abstract' + abstract_part.rstrip() + '\n\n' + jel + '\n\n---\n' + main_jik.rstrip() + '\n\n'
      + back + '\n\n## References\n\n' + '\n\n'.join(apa) + '\n\n' + appendix_jik)
pathlib.Path(OUT_MS + '.md').write_text(ms, encoding='utf-8')
tp = (src / 'title_page.md').read_text(encoding='utf-8')
pathlib.Path(OUT_TP + '.md').write_text(tp, encoding='utf-8')
hl = (src / 'highlights.md').read_text(encoding='utf-8')
pathlib.Path(OUT_HL + '.md').write_text(hl, encoding='utf-8')

# ================= audits
print('== audits')
# title page title = manuscript title
if tp.strip().split('\n')[0] != title: fail('title page title differs from the manuscript title')
# anonymization
for pat in ('HyungKyu', 'Mingu', 'Kang', 'Hanyang', 'naver', 'higgack', 'jiangmq', '@'):
    if pat in ms: fail(f'identifying text in the manuscript: {pat}')
# abstract, keywords
ab = abstract_part.split('**Keywords:**')[0].strip()
n_ab = len(ab.split()); print('abstract words', n_ab)
if n_ab > 250: fail('abstract over 250 words')
kw = abstract_part.split('**Keywords:**')[1].strip().split(';')
print('keywords', len(kw), [k.strip() for k in kw])
if not 1 <= len(kw) <= 6: fail('keywords not 1-6')
if any(re.search(r'\b(and|of)\b', k) for k in kw): fail('keyword with "and"/"of"')
# highlights
hls = [l[2:].strip() for l in hl.splitlines() if l.startswith('- ')]
print('highlights', len(hls), [len(h) for h in hls])
if not 3 <= len(hls) <= 5 or any(len(h) > 85 for h in hls): fail('highlights: 3-5 bullets of <= 85 characters')
# citations: parenthetical groups
text_part = main_jik + appendix_jik
groups = [g for g in PAREN.findall(text_part) if group_items(g)]
left = [g for g in groups if ' and ' in g]
if left: fail(f'"and" left in parenthetical citations: {left}')
unsorted = [g for g in groups if (lambda it: it != sorted(it, key=lambda i: (fold(ITEM.match(i).group(1)), ITEM.match(i).group(3))))(group_items(g)[1])]
if unsorted: fail(f'parenthetical list not alphabetical: {unsorted}')
print('parenthetical citation groups', len(groups), '(with "&":', sum('&' in g for g in groups), ')')
# citations <-> references
def cites(t):
    out = set()
    for m in re.finditer(rf"({NAME})(?: et al\.| (?:and|&) [A-Z][A-Za-zÀ-ÿ'’\-]+)?(?:['’]s)?,? \(?((?:19|20)\d\d[a-z]?)((?:, (?:19|20)\d\d[a-z]?)*)", t):
        name = re.sub(r"['’]s$", '', m.group(1))   # Kodama's (1992)
        out.add((name, m.group(2)))
        for y in re.findall(r'(?:19|20)\d\d[a-z]?', m.group(3)): out.add((name, y))
    NOT = {'In', 'From', 'Of', 'By', 'Up', 'To', 'For', 'The', 'Table', 'Tables', 'Figure', 'Section', 'Sections', 'Appendix',
           'Step', 'Global', 'CPC', 'Formation', 'Diffusion', 'Reconfiguration', 'Baseline', 'Integrated', 'Relative',
           'Second', 'Third', 'First', 'January', 'October', 'November', 'Retrieved'}
    return {c for c in out if c[0] not in NOT}
def ref_key(r):
    m = re.match(r"^(European Patent Office|Applied Materials|KnowMade|[^,]+),.*?\((\d{4}[a-z]?)\)|^(European Patent Office|Applied Materials|KnowMade)\. \((\d{4})\)", r)
    return (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
refkeys = [ref_key(r) for r in apa]
c_jik = cites(text_part); c_en = cites(main + appendix)
miss = c_jik - set(refkeys); unused = set(refkeys) - c_jik
print('citations', len(c_jik), 'references', len(refkeys))
if miss: fail(f'cited but not in references: {sorted(miss)}')
if unused: fail(f'references not cited: {sorted(unused)}')
if c_jik != c_en: fail(f'citation set differs from v{VER}_en: {sorted(c_jik ^ c_en)}')
# APA order of the reference list: first author surname, then authors/initials, single-author first, then year
def apa_sort_key(r):
    head = r.split(' (')[0]
    names = [fold(x) for x in re.split(r', (?:& )?', head)]
    return (names, re.search(r'\((\d{4})', r).group(1))
order_ok = [apa_sort_key(a) for a in apa] == sorted(apa_sort_key(a) for a in apa)
print('reference list in APA order:', order_ok)
if not order_ok: fail('reference list not in APA order')
# text equality with the English manuscript, apart from the intended changes
def norm(t):
    t = PAREN.sub(lambda m: '(CIT)' if group_items(m.group(1)) else m.group(0), t)
    t = re.sub(r'\b(Tables? [A-Z])\.?(\d+)', r'\1\2', t)
    return re.sub(r'(Tables [A-Z]\d+ and [A-Z])\.?(\d+)', r'\1\2', t)
a_ = norm(abstract_part + '\n---\n' + main + appendix).split()
b_ = norm(ms.split('## Abstract', 1)[1].replace(jel, '').replace(back, '').split('## References')[0]
          + '## Appendix' + ms.split('\n## Appendix', 1)[1]).split()
diff = [d for d in difflib.ndiff(a_, b_) if d[:1] in '+-']
print('word differences vs v%s_en (beyond citations/table numbers/JEL/back matter):' % VER, diff[:20])
if diff: fail('unexpected text differences')
# the citation items are the same multiset (ignoring and/& and order)
it = lambda t: sorted(i.replace(' & ', ' and ') for g in PAREN.findall(t) if group_items(g) for i in group_items(g)[1])
if it(main + appendix) != it(text_part): fail('parenthetical citation items changed')
print('FAILS', fails)
if fails: sys.exit(1)

# ================= docx
import pypandoc, docx, subprocess, docx_polish
def build(stem, polish_args):
    pypandoc.convert_file(stem + '.md', 'docx', outputfile=stem + '.docx', extra_args=['--resource-path=.:figures', '--dpi=300'])
    subprocess.run([sys.executable, 'tools/docx_polish.py', stem + '.docx', stem + '.docx', '--caption-pt', '10'] + polish_args, check=True)
    d = docx.Document(stem + '.docx'); cp = d.core_properties
    in_refs = False
    for para in d.paragraphs:
        if para.style.name.startswith('Heading'):
            in_refs = para.text.strip() == 'References'
        elif in_refs and para.text.strip():   # APA reference list: 0.5-inch hanging indent
            docx_polish._indent(para, 720, 720)
        if para.text.startswith('* Corresponding author'):   # title page: same Times New Roman as the e-mail line above
            for r in para.runs: docx_polish._tnr(r)
            docx_polish._indent(para, 240)
    cp.author = ''; cp.last_modified_by = ''; cp.title = ''; cp.comments = ''
    d.save(stem + '.docx')
build(OUT_MS, ['--front-black', '--line-numbers'])
build(OUT_TP, ['--front-black'])
build(OUT_HL, [])
print('docx ->', OUT_MS + '.docx,', OUT_TP + '.docx,', OUT_HL + '.docx')
