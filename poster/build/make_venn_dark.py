# Dark-background version of the three-type Venn diagram (variant D). Usage: python3 make_venn_dark.py OUT.png
import sys
import matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Circle
from matplotlib import font_manager as fm
fp = fm.FontProperties(fname='/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
fb = fm.FontProperties(fname='/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc')
PROC, INTEG, BOND = '#60A5FA', '#A78BFA', '#FB923C'
fig, ax = plt.subplots(figsize=(7.2, 4.0), dpi=300)
ax.set_xlim(0, 7.2); ax.set_ylim(0, 4.0); ax.axis('off')
r = 1.75; cl = (2.55, 1.95); cr = (4.65, 1.95)
ax.add_patch(Circle(cl, r, fc='#14284D', ec=PROC, lw=3)); ax.add_patch(Circle(cr, r, fc='#3A2414', ec=BOND, lw=3))
ov = Circle(cr, r, fc='#2E1F5C', ec='none'); ax.add_patch(ov); ov.set_clip_path(Circle(cl, r, transform=ax.transData))
ax.add_patch(Circle(cl, r, fc='none', ec=PROC, lw=3)); ax.add_patch(Circle(cr, r, fc='none', ec=BOND, lw=3))
def t(x, y, s, size, color, bold=False): ax.text(x, y, s, ha='center', va='center', fontsize=size, color=color, fontproperties=fb if bold else fp)
t(1.75, 2.25, '공정 단독형', 13, PROC, True); t(1.75, 1.6, '165건\n(17.8%)', 12, '#E6ECF7')
t(3.6, 2.25, '통합형', 14, INTEG, True); t(3.6, 1.6, '425건\n(45.8%)', 12, '#E6ECF7')
t(5.45, 2.25, '접합 단독형', 13, BOND, True); t(5.45, 1.6, '338건\n(36.4%)', 12, '#E6ECF7')
t(1.2, 3.85, 'H01L21 (제조공정 분류)', 11.5, PROC, True); t(6.0, 3.85, 'H01L24 (접합·인터커넥트 분류)', 11.5, BOND, True)
fig.savefig(sys.argv[1], transparent=True, bbox_inches='tight', pad_inches=0.02)
