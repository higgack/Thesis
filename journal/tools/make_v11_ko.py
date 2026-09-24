"""Assemble manuscript_v11_ko.md (simplified Korean version) from sources/ko_v11/*.md.

Reference list: entries of manuscript_v9_ko.md (Elsevier Harvard, with DOIs) that are
cited in the v10 text. Audits: citation <-> reference list, table/figure order,
and every decimal/large number in v10 must occur in v9_ko or supplement S1 (numbers
are never new).
"""
import re, pathlib, sys
src = pathlib.Path('sources/ko_v11')
order = ['00_front.md','01_intro.md','02_theory.md','03_context.md','04_data.md','05_results.md','06_discussion.md']
body = '\n'.join((src/f).read_text(encoding='utf-8').rstrip()+'\n' for f in order)
v9 = pathlib.Path('manuscript_v9_ko.md').read_text(encoding='utf-8')
refs_v9 = [l for l in v9.split('## 참고문헌')[1].splitlines() if l.strip() and not l.startswith('#') and l.strip() != '---' and not l.startswith(' (')]

# citations in body: "Surname(2001)", "Surname and X, 2001", "Surname et al., 2001", "Surname과 X(2012)"
cited = set()
for m in re.finditer(r'([A-Z][A-Za-zÀ-ÿ\'’\-]+(?: [A-Z][a-z]+)?)(?: et al\.| and [A-Z][A-Za-zÀ-ÿ\'’\-]+|[과와] [A-Z][A-Za-zÀ-ÿ\'’\-]+)?,? ?\(?((?:19|20)\d\d[a-z]?)', body):
    cited.add((m.group(1), m.group(2)))
for m in re.finditer(r'(European Patent Office|Yole Group),? ?\(?((?:19|20)\d\d)', body):
    cited.add((m.group(1), m.group(2)))
# years listed after one author, e.g. "Lau, 2021, 2022" / "Lau(2021, 2022)"
for m in re.finditer(r'([A-Z][A-Za-z\-]+),? ?\(?((?:19|20)\d\d), ((?:19|20)\d\d)', body):
    cited.add((m.group(1), m.group(3)))
cited = {c for c in cited if c[0] not in ('PATSTAT Global','Patent Office','CPC','H01L21','H01L24')}
def key(ref):
    m = re.match(r'^(.+?),? (?:[A-Z]\.[A-Z]?\.? ?)*.*?((?:19|20)\d\d[a-z]?)\.', ref)
    first = re.match(r'^(European Patent Office|Yole Group|[A-Z][A-Za-zÀ-ÿ\'’\-]+(?: [A-Z][a-z]+)?)', ref).group(1)
    yr = re.search(r'\b((?:19|20)\d\d[a-z]?)\.', ref).group(1)
    return (first, yr)
keyed = [(key(r), r) for r in refs_v9]
kept = [r for k, r in keyed if k in cited]
missing = sorted(c for c in cited if c not in {k for k,_ in keyed})
dropped = sorted(k for k,_ in keyed if k not in cited)
appendix = (src/'07_appendix.md').read_text(encoding='utf-8')
text = body + '\n## 참고문헌 (References)\n\n' + '\n\n'.join(kept) + '\n\n---\n\n' + appendix
pathlib.Path('manuscript_v11_ko.md').write_text(text, encoding='utf-8')
print('cited', len(cited), 'kept refs', len(kept), 'dropped', len(dropped))
print('cited but not in v9 list:', missing)
fails = len(missing)
# numbering
main = body
for kind in ('표','그림'):
    first=[]
    for m in re.finditer(rf'{kind} (\d+)', main):
        n=int(m.group(1))
        if n not in first: first.append(n)
    caps=[int(m.group(1)) for m in re.finditer(rf'^\*\*{kind} (\d+)\.\*\*', main, re.M)]
    ok = first==sorted(first) and caps==sorted(caps) and set(caps)==set(first)
    print(kind, first, caps, 'OK' if ok else '!! MISMATCH'); fails += (not ok)
# numbers subset check
s1 = pathlib.Path('supplement_S1_v9_ko.md').read_text(encoding='utf-8')
s2 = pathlib.Path('supplement_S2_v9_ko.md').read_text(encoding='utf-8')
pat = r'(?<![\d.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\d])'
def nums(t): return set(x.replace(',','') for x in re.findall(pat, t))
pool = nums(v9) | nums(s1) | nums(s2) | nums(v9.replace(',','')) | nums(s1.replace(',',''))
pool |= {'2.7'}   # derived: RRR 2.687 rounded in prose
new = sorted(x for x in nums(main + appendix) if x not in pool and ('.' in x or ',' in x or len(x)>=3))
print('numbers in v10 not found in v9/S1/S2:', new); fails += len(new)
print('chars', len(main), 'FAILS', fails)
sys.exit(1 if fails else 0)
