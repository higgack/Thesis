import sys, runpy
C = runpy.run_path('cands2.py')['CANDS']
e = open('edits2.py', encoding='utf8').read().rstrip().rstrip(']').rstrip()
if e.endswith('['): e += '\n'
for spec in sys.argv[1:]:
    p, idx = spec.split(':')
    e += f' # {p}\n'
    for i in [int(x) for x in idx.split(',')]:
        o, n = C[int(p)][i].split('=>'); e += f' ({o!r}, {n!r}),\n'
e += ']\n'
open('edits2.py', 'w', encoding='utf8').write(e)
