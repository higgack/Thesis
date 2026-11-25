"""usage: addedits.py PARA i,j,k  -> append chosen candidates of PARA from cands.py to edits.py"""
import sys, runpy
C = runpy.run_path('cands.py')['CANDS'][int(sys.argv[1])]
idx = [int(x) for x in sys.argv[2].split(',')] if sys.argv[2] else []
e = open('edits.py', encoding='utf8').read().rstrip().rstrip(']')
e += f'\n # {sys.argv[1]}\n'
for i in idx:
    o, n = C[i].split('=>'); e += f' ({o!r}, {n!r}),\n'
e += ']\n'
open('edits.py', 'w', encoding='utf8').write(e)
