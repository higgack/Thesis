"""Assemble the English (TASM-format) manuscript from the translated sections.

Usage: python3 tools/make_en_journal_v9.py [--src DIR]
Writes journal/manuscript_v9_en_TASM.md and runs consistency audits
(Korean leftovers, table/figure order, citation <-> reference list).
"""
import argparse, re, sys, pathlib

ap = argparse.ArgumentParser()
ap.add_argument('--src', default='sources/en_v9')
ap.add_argument('--out', default='manuscript_v9_en_TASM.md')
a = ap.parse_args()
src = pathlib.Path(a.src)
order = ['00_front.md','01_intro.md','02_theory.md','03_context.md','04_data.md',
         '05a_results.md','05b_results.md','06_discussion.md']
parts = [ (src/f).read_text(encoding='utf-8').rstrip()+'\n' for f in order ]
refs = pathlib.Path('refs_cad_v9.md').read_text(encoding='utf-8')
refs_body = refs.split('\n',1)[1].strip()
text = '\n'.join(parts) + '\n## References\n\n' + refs_body + '\n'
text += ('\n## Supplementary material\n\nSupplementary material (Tables S1–S28, Figures S1–S2 and the list of '
         'excluded applications) is provided in the separate file supplement_v9_en.md/.docx.\n')
pathlib.Path(a.out).write_text(text, encoding='utf-8')

fails = 0
# 1. Korean leftovers
ko = [ (i+1,l[:80]) for i,l in enumerate(text.splitlines()) if re.search(r'[가-힣]', l) ]
print('Korean lines:', len(ko)); [print('  ',x) for x in ko[:10]]; fails += len(ko)
# 2. Table/figure first-mention order
for kind in ('Table','Figure'):
    nums = [int(m.group(1)) for m in re.finditer(rf'\b{kind} (\d+)\b', text)]
    first = []
    for n in nums:
        if n not in first: first.append(n)
    caps = [int(m.group(1)) for m in re.finditer(rf'^\*\*{kind} (\d+)\.\*\*', text, re.M)]
    print(kind, 'first mentions:', first, 'captions:', caps)
    if first != sorted(first) or caps != sorted(caps) or set(caps) != set(first):
        print('  !! order/cover mismatch'); fails += 1
# 3. Citations <-> references
main = text.split('## References')[0]
ref_lines = [l for l in refs_body.splitlines() if l.strip()]
ref_keys = []
for l in ref_lines:
    m = re.match(r'^(.+?)\. (\d{4}[a-z]?)\.', l)
    if not m: print('  !! unparsed ref:', l[:60]); fails += 1; continue
    auth = m.group(1); yr = m.group(2)
    surnames = re.findall(r'(?:^|, and |, )([A-Z][A-Za-zÀ-ÿ\'’\-]+(?: [A-Z][a-zé\-]+)?), [A-Z]\.', auth)
    first = re.match(r'^([A-Z][A-Za-zÀ-ÿ\'’\-]+(?: [A-Z][a-zé\-]+)?),', auth)
    first_sn = first.group(1) if first else auth.split(',')[0]
    n = len(re.findall(r'[A-Z]\.', auth.replace('and ',''))) or 1
    n_auth = auth.count(', and ')+1 if ', and ' in auth else (1 if ', ' in auth and auth.count(',')<=2 else auth.count(',')//2+1)
    ref_keys.append((first_sn, yr, auth))
cited = set()
for m in re.finditer(r'\(([^()]*?\d{4}[a-z]?[^()]*)\)', main):
    for chunk in m.group(1).split(';'):
        chunk = re.sub(r'^\s*(?:see|see also|e\.g\.|cf\.)\s+', '', chunk.strip())
        mm = re.match(r'((?:von |de )?[A-Z][A-Za-zÀ-ÿ\'’\-]+(?: [A-Z][a-zé\-]+)?)(?:, (?:von )?[A-Z][A-Za-zÀ-ÿ\'’\-]+)*(?:,? and (?:von )?[A-Z][A-Za-zÀ-ÿ\'’\-]+)?(?: et al\.)?,? (\d{4}[a-z]?)', chunk)
        if mm: cited.add((mm.group(1), mm.group(2)))
for m in re.finditer(r'(?<![A-Za-z’\'])([A-Z][A-Za-zÀ-ÿ\-]+)(?:’s|\'s)?(?:, (?:von )?[A-Z][A-Za-zÀ-ÿ\'’\-]+)*(?:,? and (?:von )?[A-Z][A-Za-zÀ-ÿ\'’\-]+)?(?: et al\.)? \((\d{4}[a-z]?)\)', main):
    cited.add((m.group(1), m.group(2)))
refset = {(k[0], k[1]) for k in ref_keys}
missing = sorted(c for c in cited if c not in refset)
uncited = sorted((k[0],k[1]) for k in ref_keys if (k[0],k[1]) not in cited
                 and not re.search(re.escape(k[0]) + r'[^.;)]{0,60}?\(?' + k[1], main))
print('cited pairs:', len(cited), 'refs:', len(ref_keys))
print('cited but not in list:', missing)
print('in list but not cited:', uncited)
fails += len(missing) + len(uncited)
# 4. word counts
body = re.split(r'## References', main)[0]
abstract = re.search(r'## Abstract\n\n(.+?)\n\n\*\*Keywords', body, re.S).group(1)
print('abstract words:', len(abstract.split()))
mt = body.split('## 1. Introduction',1)[1].split('## Acknowledgements')[0]
mt_notab = '\n'.join(l for l in mt.splitlines() if not l.startswith('|'))
print('main text words (1. Intro–7. Conclusion, excl. table rows):', len(mt_notab.split()))
print('FAILS', fails)
sys.exit(1 if fails else 0)
