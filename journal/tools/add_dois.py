"""Append verified DOIs to reference lines lacking one.
Usage: python add_dois.py manuscript.md doi_map.json > out.md   (in-place with --inplace)
doi_map: {first-40-chars-of-reference-line: "10.xxxx/..."}
"""
import sys, json, re
src = sys.argv[1]; mp = json.load(open(sys.argv[2], encoding='utf-8'))
inplace = '--inplace' in sys.argv
t = open(src, encoding='utf-8').read()
head, sep, refs = t.partition('## 참고문헌 (References)')
if not sep:
    head, sep, refs = t.partition('## References')
out = []; added = 0
for ln in refs.split('\n'):
    s = ln.strip()
    if s and not s.startswith('#') and 'doi.org' not in s:
        doi = mp.get(s[:40])
        if doi:
            ln = s + ' https://doi.org/' + doi; added += 1
    out.append(ln)
res = head + sep + '\n'.join(out)
if inplace:
    open(src, 'w', encoding='utf-8').write(res)
else:
    sys.stdout.write(res)
print(f'DOIs added: {added}', file=sys.stderr)
