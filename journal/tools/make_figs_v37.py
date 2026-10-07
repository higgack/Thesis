"""v37: redraw the period-share and predicted-type figures with the type renamed
'조립 단독형' -> '접합 단독형' (Korean only). Reuses the plotting code of
make_figures_v9.py (Fig 7 block) and make_fig_pred_types.py; writes *_v37_ko.png."""
import re
src = open('tools/make_figures_v9.py', encoding='utf-8').read().split('\n')
head = '\n'.join(src[:17]); i = next(k for k, l in enumerate(src) if l.startswith('# Fig 7'))
j = next(k for k in range(i, len(src)) if 'fig7_period_shares' in src[k])
block = '\n'.join(src[i:j + 1])
code = (head + '\n' + block).replace("'조립 단독형'", "'접합 단독형'").replace('fig7_period_shares{SUF}', 'fig7_period_shares_v37{SUF}')
exec(compile(code, 'fig7_v37', 'exec'), {})
code = open('tools/make_fig_pred_types.py', encoding='utf-8').read()
code = code.replace("'조립 단독형'", "'접합 단독형'").replace("fig_pred_types{'_ko'", "fig_pred_types_v37{'_ko'")
exec(compile(code, 'pred_v37', 'exec'), {})
print('v37 figures written')
