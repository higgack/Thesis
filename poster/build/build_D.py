# -*- coding: utf-8 -*-
"""Variant D: dark, bold poster (big English section words, neon data colours), A1 template.
Usage: python3 build_D.py TEMPLATE ASSET_DIR ICON_DIR OUT.pptx"""
import sys
import common as K
from common import *
from PIL import Image as _I

TPL, ASSET_, ICON_, OUT = sys.argv[1:5]
K.init(TPL, ASSET_, ICON_)
BG, CARD, CLINE = '0B1222', '131E36', '25385E'
FG, FG2 = 'E6ECF7', 'A9B6CF'
CYAN, COP, VIO, BLU, YLW = '38BDF8', 'FB923C', 'A78BFA', '60A5FA', 'FACC15'
X0, X1 = 1.6, 57.8
CW = X1 - X0
BOTTOM = 80.1

bg = shape(MSO_SHAPE.RECTANGLE, 0, 0, W, BOTTOM, fill=BG); to_back(bg)
# faint pad-array texture (top right)
for i in range(9):
    for j in range(7):
        shape(MSO_SHAPE.ROUNDED_RECTANGLE, 40.6 + i * 2.0, 3.6 + j * 1.0, 1.1, 0.55, fill=COP, alpha=max(0.05, 0.32 - 0.03 * (8 - i) - 0.03 * j), radius=0.2)
to_front(K.P_BOX)

def spaced(tb, spc=300):
    for r in tb._element.iter(qn('a:rPr')): r.set('spc', str(spc))
    return tb

# ---------------------------------------------------------------- title
spaced(shape_text(shape(MSO_SHAPE.ROUNDED_RECTANGLE, X0, 1.0, 14.2, 1.25, line=CYAN, lw=1.5, radius=0.5),
                  [P(('2026 HANYANG MOT RESEARCH DAY', 15, CYAN, True))], margin=0.1), 150)
fill_ph(153, X0, 2.7, 50.0, 4.9, [P((TITLE1, 54, WHITE, True), line=0.92), P((TITLE2a, 54, COP, True), (TITLE2b, 54, WHITE, True), line=0.92)])
ph = fill_ph(151, X0, 7.8, 50.0, 1.0, [P((TITLE_EN.upper(), 15, '8FA3C7', True))])
spaced(ph, 100)
fill_ph(150, X0, 9.1, 50.0, 1.9, [P((AUTHOR, 30, WHITE, True), ('   ' + AFFIL, 22, FG2))], anchor='m')

# ---------------------------------------------------------------- hero KPIs
ky = 11.9
kw = (CW - 2 * 0.9) / 3
for k, (num, lab, col) in enumerate([('928', '하이브리드 본딩 관련 특허출원 (1968–2024)', COP), ('45.8%', '두 공정 분류를 함께 가진 통합형 비중', VIO), ('3국면', '형성기 → 확산기 → 재편기', CYAN)]):
    kx = X0 + k * (kw + 0.9)
    shape(MSO_SHAPE.ROUNDED_RECTANGLE, kx, ky, kw, 4.6, fill=CARD, line=col, lw=2.0, radius=0.08)
    text(kx, ky + 0.25, kw, 2.9, [P((num, 70, col, True), align=C, line=0.9)], anchor='m')
    text(kx + 0.4, ky + 3.25, kw - 0.8, 1.1, [P((lab, 17, FG), align=C)], anchor='m')

def band(y, num, en, ko):
    tb = text(X0, y, 40, 2.2, [P((num + '  ', 44, CYAN, True), (en, 44, WHITE, True), ('   ' + ko, 22, FG2))], anchor='m')
    for r in list(tb._element.iter(qn('a:rPr')))[:2]: r.set('spc', '400')
    return y + 2.4

def card(x, y, w, h, line_col=CLINE):
    return shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h, fill=CARD, line=line_col, lw=1.25, radius=0.04)

def h3(x, y, w, s_, col=CYAN, size=20):
    text(x, y, w, 1.0, [P((s_, size, col, True))], anchor='m')
    return y + 1.15

# ================================================================ 01 PROBLEM
y = band(17.0, '01', 'PROBLEM', '왜 이 연구인가')
ph_h = 13.8
hw = (CW - 0.9) / 2
card(X0, y, hw, ph_h)
yy = h3(X0 + 0.7, y + 0.5, hw - 1.4, '배경')
dark_bg = [[(r[0], r[1], (FG if len(r) < 3 or r[2] == TEXT else ({PROC: BLU, BOND: COP}.get(r[2], r[2]))), r[3] if len(r) > 3 else False) for r in it] for it in background(18)]
text(X0 + 0.7, yy, hw - 1.4, 7.0, [P(*it, bullet=('▪', CYAN, 100), indent=0.6, after=4) for it in dark_bg])
bump_vs_hybrid(X0 + 0.7, y + ph_h - 5.7, hw - 1.4, 5.2, card_fill='1B2948', title_muted=FG2, fg=FG, sub=FG2, shadow=False, gap=1.0, size=(17, 14))
rx = X0 + hw + 0.9
card(rx, y, hw, ph_h)
yy = h3(rx + 0.7, y + 0.5, hw - 1.4, '연구 공백')
text(rx + 0.7, yy, hw - 1.4, 4.2, [P((g, 17, FG), bullet=('▪', CYAN, 100), indent=0.6, after=3) for g in GAP])
yy += 4.4
yy = h3(rx + 0.7, yy, hw - 1.4, '연구 질문')
for tag, ttl, q in RQS:
    shape_text(shape(MSO_SHAPE.ROUNDED_RECTANGLE, rx + 0.7, yy, 2.8, 2.0, fill='1D4ED8', radius=0.15), [P((tag, 20, WHITE, True))], margin=0)
    text(rx + 3.8, yy, hw - 4.6, 2.0, [P((ttl, 18, YLW, True)), P((q, 16, FG))], anchor='m')
    yy += 2.15
y += ph_h + 0.8

# ================================================================ 02 SOLUTION
y = band(y, '02', 'SOLUTION', '어떻게 분석했는가')
so_h = 12.6
sw = 17.0
for i, (ic, ttl, ls) in enumerate(STEPS):
    sy = y + i * (so_h + 0.3 - 0.3 * 0) / 4
    sh_ = (so_h - 3 * 0.3) / 4
    sy = y + i * (sh_ + 0.3)
    card(X0, sy, sw, sh_)
    icon_circle(ic, X0 + 0.35, sy + (sh_ - 1.5) / 2, 1.5, '1D4ED8')
    text(X0 + 2.15, sy + 0.05, sw - 2.4, sh_ - 0.1, [P(('STEP %d  ' % (i + 1), 13, CYAN, True), (ttl, 18, WHITE, True))] + [P((l, 14, FG2)) for l in ls], anchor='m')
vx = X0 + sw + 0.9
vw = 20.0
card(vx, y, vw, so_h)
text(vx, y + 0.3, vw, 0.9, [P(('CPC 공동분류로 본 세 유형 (N = 928)', 18, WHITE, True), align=C)], anchor='m')
iw_, ih_ = _I.open(f'{ASSET_}/venn_dark.png').size
vimg = vw - 1.4
picture(f'{ASSET_}/venn_dark.png', vx + 0.7, y + 1.4, vimg, vimg * ih_ / iw_)
text(vx + 0.4, y + so_h - 1.2, vw - 0.8, 1.0, [P(('통합형', 15, VIO, True), (' = 공정 단계의 경계를 넘는 발명', 15, FG), align=C)], anchor='m')
cx = vx + vw + 0.9
cwid = X1 - cx
card(cx, y, cwid, so_h)
text(cx, y + 0.3, cwid, 0.9, [P(('핵심 접합 기술', 18, WHITE, True), align=C)], anchor='m')
pw = (cwid - 1.4 - 0.5) / 2
for k, code in enumerate(['24/03', '24/05', '24/08', '24/80']):
    px = cx + 0.7 + (k % 2) * (pw + 0.5); py = y + 1.6 + (k // 2) * 1.9
    shape_text(shape(MSO_SHAPE.ROUNDED_RECTANGLE, px, py, pw, 1.5, fill='3A1C10', line=COP, lw=2.0, radius=0.5), [P(('H01L ' + code, 20, COP, True))], margin=0)
text(cx, y + 5.6, cwid, 2.4, [P(('81.3%', 54, COP, True), align=C, line=0.9)], anchor='m')
text(cx + 0.5, y + 8.0, cwid - 1.0, 2.0, [P(('접합 출원 763건 중 핵심 기술을 포함한 비중', 15, FG), align=C)], anchor='m')
text(cx + 0.5, y + 9.9, cwid - 1.0, 2.4, [P(('기술 클러스터(출원 200건 이상 기호)와 기술 네트워크의 연결 강도로 식별', 13, FG2), align=C)], anchor='m')
y += so_h + 0.8

# ================================================================ 03 EVALUATION
y = band(y, '03', 'EVALUATION', '무엇을 발견했는가')
ev_h = 17.2
rw = (CW - 2 * 0.9) / 3
heads = [('RQ1', '융합은 예외가 아니다', VIO), ('RQ2', '형성 → 확산 → 재편', CYAN), ('RQ3', '중심 이동: 핵심 → 주변', COP)]
tk_h = 3.0
for k, (tag, hd, col) in enumerate(heads):
    x = X0 + k * (rw + 0.9)
    card(x, y, rw, ev_h, line_col=col)
    shape_text(shape(MSO_SHAPE.ROUNDED_RECTANGLE, x + 0.5, y + 0.45, 2.4, 1.3, fill=col, radius=0.25), [P((tag, 20, BG, True))], margin=0)
    text(x + 3.2, y + 0.4, rw - 3.6, 1.4, [P((hd, 21, WHITE, True))], anchor='m')
    shape(MSO_SHAPE.ROUNDED_RECTANGLE, x + 0.4, y + ev_h - tk_h - 0.4, rw - 0.8, tk_h, fill='1B2948', radius=0.08)
def tk(x, items):
    text(x + 0.7, y + ev_h - tk_h - 0.3, rw - 1.3, tk_h - 0.2, [P(*it, bullet=('▪', YLW, 100), indent=0.6, after=2) for it in items], anchor='m')
x = X0
text(x + 0.5, y + 2.1, rw - 1.0, 2.4, [P(('45.8%', 50, VIO, True), ('  통합형', 17, FG, True), line=0.9), P(('좁은 정의에서도 37.2%', 15, FG2))])
text(x + 0.5, y + 4.8, rw - 1.0, 0.8, [P(('기술범위별 유형 예측확률', 15, FG2, True))])
chart_pred(x + 0.2, y + 5.5, rw - 0.4, ev_h - 5.5 - tk_h - 0.6, size=13, dark=True)
tk(x, [[('세부 기술이 다양할수록 통합형', 16, WHITE, True)], [('기술범위 1 증가 → RRR 1.403***', 15, FG)], [('기술범위 3 → 0.26, 8 → 0.68', 15, FG)]])
x = X0 + rw + 0.9
text(x + 0.5, y + 2.1, rw - 1.0, 0.8, [P(('시기별 유형 구성비 (%)', 15, FG2, True))])
chart_period(x + 0.2, y + 2.8, rw - 0.4, ev_h - 2.8 - tk_h - 0.6, size=13, label_size=16, dark=True,
             cats=('형성기', '확산기', '재편기'))
tk(x, [[('통합형 31.3% → 62.2% → 44.3%', 16, WHITE, True)], [('공정 단독형 매년 8.7% 감소 (RRR 0.913***)', 15, FG)], [('무게중심: 제조공정 → 접합', 15, FG)]])
x = X0 + 2 * (rw + 0.9)
text(x + 0.5, y + 2.1, rw - 1.0, 0.8, [P(('핵심 기술 → 통합형 승산비 (95% CI)', 15, FG2, True))])
yb = or_plot(x + 0.4, y + 2.9, rw - 0.8, label_w=2.4, rs=0.95, dark=True, size=16)
text(x + 0.5, yb, rw - 1.0, 0.8, [P(('제조공정 영역별 결합 강도 (Jaccard)', 15, FG2, True))])
chart_jaccard(x + 0.2, yb + 0.7, rw - 0.4, y + ev_h - tk_h - 0.5 - (yb + 0.7), size=12, dark=True)
tk(x, [[('재편기에는 핵심 기술의 우위가 확인되지 않음', 16, WHITE, True)], [('핵심 기술 비중 53.5% → 88.0%', 15, FG)], [('웨이퍼 제조공정 → 공정 장치·패키징 단계 공정', 15, FG)]])
y += ev_h + 0.8

# ================================================================ 04 TAKEAWAY
y = band(y, '04', 'TAKEAWAY', '결론과 시사점')
phw = 12.3; step = phw - 0.3
cols = ['1D4ED8', '6D28D9', 'EA580C']
for i, ((nm, yrs, _c, l1, l2), col) in enumerate(zip(PHASES, cols)):
    kind = MSO_SHAPE.PENTAGON if i == 0 else MSO_SHAPE.CHEVRON
    shape_text(shape(kind, X0 + i * step, y, phw, 1.8, fill=col), [P((nm + '  ', 22, WHITE, True), (yrs, 16, WHITE))], margin=0.9)
    text(X0 + i * step + 0.5, y + 1.95, phw - 1.4, 2.3, [P((l1, 16, (BLU, VIO, COP)[i], True)), P((l2, 14, FG2))])
cw_ = 3 * step + 0.3
qy = y + 4.4
text(X0, qy, cw_, BOTTOM - qy - 0.4, [P((CONCL[0], 19, WHITE, True), (CONCL[1], 19, YLW, True), (CONCL[2], 19, WHITE, True))], anchor='m')
ix = X0 + cw_ + 0.9; iw = X1 - ix
ih = (BOTTOM - y - 0.4 - 0.3) / 2
for k, (ic, ttl, body) in enumerate([('FaGraduationCap', '학술적 시사점', IMPL_ACAD), ('FaIndustry', '실무적 시사점', IMPL_PRAC)]):
    yy = y + k * (ih + 0.3)
    card(ix, yy, iw, ih)
    icon_circle(ic, ix + 0.35, yy + 0.3, 1.2, '1D4ED8')
    text(ix + 1.85, yy + 0.25, iw - 2.1, 1.3, [P((ttl, 17, YLW, True))], anchor='m')
    text(ix + 0.4, yy + 1.55, iw - 0.75, ih - 1.6, [P((body, 13, FG))])
print('end', round(y, 2))

footer()
save(OUT)
