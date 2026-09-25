# -*- coding: utf-8 -*-
"""Figure 1 (v19): hybrid bonding framework in four tiers
(enabling processes -> bonding configurations -> device structures -> applications).
Writes figures/fig_framework_psp.png (ko) and figures/fig_framework_psp_en.png (en);
the previous versions are kept as *_v8.png."""
import os, shutil
import matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
import koreanize_matplotlib  # noqa
NAVY = '#1f3b5c'; LIGHT = '#f4f6f9'; EDGE = '#8a94a6'
for old in ('figures/fig_framework_psp.png', 'figures/fig_framework_psp_en.png'):
    v8 = old.replace('.png', '_v8.png')
    if os.path.exists(old) and not os.path.exists(v8): shutil.copy(old, v8)
def draw(lang):
    T = (lambda ko, en: ko if lang == 'ko' else en)
    tiers = [
        (T('기반 공정', 'Enabling processes'), T('(CPC H01L21 제조공정 클래스)', '(CPC H01L21, fabrication processes)'),
         [T('CMP 평탄화', 'CMP planarisation'), T('세정·오염 제어', 'Cleaning / contamination control'), T('표면 활성화', 'Surface activation'), T('정렬', 'Alignment'), T('저온 어닐링', 'Low-temperature anneal')]),
        (T('접합 형태', 'Bonding configurations'), T('(CPC H01L24 접합·인터커넥트 클래스)', '(CPC H01L24, bonding and interconnect)'),
         [T('웨이퍼-투-웨이퍼(W2W)', 'Wafer-to-wafer (W2W)'), T('다이-투-웨이퍼(D2W)', 'Die-to-wafer (D2W)'), T('다이-투-다이(D2D)', 'Die-to-die (D2D)')]),
        (T('소자 구조', 'Device structures'), '',
         [T('적층 다이', 'Stacked die'), T('칩렛 집적', 'Chiplet integration'), T('메모리-온-로직', 'Memory-on-logic')]),
        (T('적용 제품', 'Applications'), '',
         [T('이미지센서(CIS)', 'Image sensor (CIS)'), '3D NAND', 'HBM', T('로직 적층', 'Logic stacking'), 'MicroLED']),
    ]
    fig, ax = plt.subplots(figsize=(6.3, 5.0)); ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis('off')
    n = len(tiers); h = 18.5; gap = (100 - n * h) / (n + 1)
    for i, (title, sub, items) in enumerate(tiers):
        y0 = 100 - gap * (i + 1) - h * (i + 1)
        ax.add_patch(FancyBboxPatch((4, y0), 92, h, boxstyle='round,pad=0.4,rounding_size=1.5', fc=LIGHT, ec=EDGE, lw=1.6))
        ax.text(6, y0 + h - 3.4, title, fontsize=13, fontweight='bold', color=NAVY, va='center')
        if sub: ax.text(6 + len(title) * 3.6 + 3, y0 + h - 3.4, sub, fontsize=9.5, color='#333', va='center')
        k = len(items); w = (88 - (k - 1) * 1.6) / k
        for j, it in enumerate(items):
            x = 6 + j * (w + 1.6)
            ax.add_patch(FancyBboxPatch((x, y0 + 2.0), w, 9.0, boxstyle='round,pad=0.3,rounding_size=1.2', fc='white', ec='#222', lw=1.4))
            ax.text(x + w / 2, y0 + 6.5, it, ha='center', va='center', fontsize=10.5 if k < 5 else 9.0, color='#111')
        if i < n - 1:
            ax.add_patch(FancyArrowPatch((50, y0 - 0.4), (50, y0 - gap + 0.4), arrowstyle='-|>', mutation_scale=18, color=NAVY, lw=2.2))
    lbl = [T('가능하게 함', 'enables'), T('구현', 'realised as'), T('적용', 'used in')]
    for i in range(n - 1):
        y0 = 100 - gap * (i + 1) - h * (i + 1)
        ax.text(52.5, y0 - gap / 2, lbl[i], fontsize=10, color=NAVY, va='center')
    fig.tight_layout(); fig.savefig(f"figures/fig_framework_psp{'' if lang == 'ko' else '_en'}.png", dpi=300); plt.close(fig)
draw('ko'); draw('en'); print('figure 1 redrawn')
