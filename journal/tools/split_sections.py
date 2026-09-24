"""Split a markdown manuscript into section files keyed by top-level/second-level headings.
Usage: python split_sections.py manuscript.md outdir
Writes outdir/NN_<slug>.md and outdir/index.json (ordered list of {file, heading}).
"""
import sys, os, re, json
src, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)
text = open(src, encoding='utf-8').read()
lines = text.split('\n')
# split at any '## ' heading (keep '### ' inside the parent section)
sections = []
cur = {'heading': 'FRONT', 'lines': []}
for ln in lines:
    if ln.startswith('## '):
        sections.append(cur)
        cur = {'heading': ln[3:].strip(), 'lines': [ln]}
    else:
        cur['lines'].append(ln)
sections.append(cur)
index = []
for i, s in enumerate(sections):
    slug = re.sub(r'[^0-9A-Za-z가-힣]+', '_', s['heading'])[:40].strip('_')
    fn = f'{i:02d}_{slug}.md'
    open(os.path.join(out, fn), 'w', encoding='utf-8').write('\n'.join(s['lines']).strip() + '\n')
    index.append({'file': fn, 'heading': s['heading'], 'chars': len('\n'.join(s['lines']))})
json.dump(index, open(os.path.join(out, 'index.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
for e in index: print(e)
