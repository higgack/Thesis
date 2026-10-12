# -*- coding: utf-8 -*-
"""Variant B: Problem–Solution–Evaluation three-column poster (navy + gold), A1 template.
Usage: python3 build_B.py TEMPLATE ASSET_DIR ICON_DIR OUT.pptx"""
import sys
import common as K
from common import *

TPL, ASSET_, ICON_, OUT = sys.argv[1:5]
K.init(TPL, ASSET_, ICON_)
GOLDB, GOLD_L, BAND = 'D4A72C', 'FFF6DC', 'E8EEF7'
X0, X1 = 1.6, 57.8
CW = X1 - X0
CG = 1.0
COLW = (CW - 2 * CG) / 3
COLX = [X0 + i * (COLW + CG) for i in range(3)]

# ---------------------------------------------------------------- header
HDR = 12.0
to_back(shape(MSO_SHAPE.RECTANGLE, 0, 0, W, HDR, fill=NAVY))
shape(MSO_SHAPE.RECTANGLE, 0, HDR, W, 0.3, fill=GOLDB)
to_front(K.P_BOX)
shape_text(shape(MSO_SHAPE.ROUNDED_RECTANGLE, X0, 1.0, 10.6, 1.25, line=GOLDB, lw=1.5, radius=0.5),
           [P((EVENT, 16, GOLDB, True))], margin=0.1)
fill_ph(153, X0, 2.65, 50.0, 4.9, [P((TITLE1, 52, WHITE, True), line=0.92), P((TITLE2a + TITLE2b, 52, GOLDB, True), line=0.92)])
fill_ph(151, X0, 7.75, 50.0, 1.1, [P((TITLE_EN, 19, 'B9C6DD', False, True))])
fill_ph(150, X0, 9.2, 50.0, 2.0, [P((AUTHOR, 30, WHITE, True), ('   ' + AFFIL, 24, 'DCE3EE'))], anchor='m')

# ---------------------------------------------------------------- column scaffolding
TOP = HDR + 1.0
def col_header(x, w, num, en, ko, y=TOP, h=2.0):
    shape(MSO_SHAPE.RECTANGLE, x, y, w, h, fill=NAVY)
    shape_text(shape(MSO_SHAPE.OVAL, x + 0.5, y + 0.3, 1.4, 1.4, fill=GOLDB), [P((str(num), 22, NAVY, True))], margin=0)
    text(x + 2.3, y, w - 2.6, h, [P((en, 26, WHITE, True), ('  ·  ' + ko, 20, 'C8D3E6'))], anchor='m')
    return y + h + 0.6

def sub(x, y, w, en, ko):
    shape(MSO_SHAPE.RECTANGLE, x, y, w, 1.15, fill=BAND)
    shape(MSO_SHAPE.RECTANGLE, x + 0.25, y + 0.3, 0.3, 0.55, fill=NAVY)
    text(x + 0.85, y, w - 1.0, 1.15, [P((en, 19, NAVY, True), ('  ·  ' + ko, 18, NAVY))], anchor='m')
    return y + 1.6

def takeaway(x, y, w, runs, h=1.5):
    icon('FaArrowRightLong', x + 0.1, y + (h - 0.8) / 2, 0.8, 'C9A227')
    text(x + 1.15, y, w - 1.2, h, [P(*runs)], anchor='m')
    return y + h + 0.2

# separators between columns
COL_END = 64.6
for i in (1, 2):
    line(COLX[i] - CG / 2, TOP + 2.4, COLX[i] - CG / 2, COL_END, 'C9D2E0', w=1.0)

# ================================================================ 1 PROBLEM
x, w = COLX[0], COLW
y = col_header(x, w, 1, 'Problem', '연구 문제')
y = sub(x, y, w, 'Background', '연구 배경')
tb = text(x + 0.2, y, w - 0.3, 7.4, [P(*runs, bullet=('■', NAVY, 70), indent=0.6, after=5, line=1.0) for runs in background(19)])
y += 7.5
bump_vs_hybrid(x + 0.2, y, w - 0.4, 6.6, gap=1.0, size=(17, 14))
y += 7.2
y = sub(x, y, w, 'Real World Context', '산업 맥락')
text(x + 0.2, y, w - 0.3, 3.6, [
    P(('하이브리드 본딩 특허 활동을 주도하는 출원인: ', 17), ('TSMC, Adeia, YMTC, Intel, Samsung', 17, TEXT, True), (' (KnowMade, 2024)', 15, MUTED), after=3, line=1.0),
    P(('파운드리·라이선싱 기업·메모리 제조사가 함께 주도 → 접합 기술은 더 이상 후공정 전문기업만의 영역이 아니다.', 17), line=1.0)])
y += 3.9
y = sub(x, y, w, 'Research Gap', '연구 공백')
for i, g in enumerate(GAP):
    shape_text(shape(MSO_SHAPE.OVAL, x + 0.25, y + 0.1, 1.0, 1.0, fill=NAVY), [P((str(i + 1), 16, WHITE, True))], margin=0)
    gh = (2.2, 2.85, 2.2)[i]
    text(x + 1.55, y, w - 1.7, gh, [P((g, 18), line=1.0)])
    y += gh
y += 0.2
cq = shape(MSO_SHAPE.RECTANGLE, x + 0.2, y, w - 0.4, 3.6, fill=GOLD_L, line=GOLDB, lw=2.0)
text(x + 0.7, y + 0.15, w - 1.4, 3.3, [
    P(('핵심 질문', 16, 'A07800', True), after=2),
    P(('공정 단계 간 기술 경계의 융합을 개별 특허 수준에서 확인할 수 있는가? 그 융합은 시간에 따라 어떻게 전개되는가?', 18, NAVY, True), line=1.0)], anchor='m')
y += 4.0
y = sub(x, y, w, 'Research Questions', '연구 질문')
for tag, ttl, q in RQS:
    shape(MSO_SHAPE.RECTANGLE, x + 0.2, y, w - 0.4, 3.4, fill=WHITE, line='C9D2E0', lw=1.0)
    shape_text(shape(MSO_SHAPE.RECTANGLE, x + 0.2, y, 2.9, 3.4, fill=NAVY), [P((tag, 22, GOLDB, True))], margin=0)
    text(x + 3.45, y + 0.1, w - 3.9, 3.2, [P((ttl, 19, NAVY, True), after=1), P((q, 17), line=1.0)], anchor='m')
    y += 3.65
print('col1 end', round(y, 2))

# ================================================================ 2 SOLUTION
x = COLX[1]
y = col_header(x, w, 2, 'Solution', '연구 설계')
y = sub(x, y, w, 'Data', '자료')
tw = (w - 0.4 - 2 * 0.4) / 3
for k, (num, lab) in enumerate([('928', '관련 특허출원(건)'), ('262', 'CPC 서브그룹 기호(개)'), ('8', '출원청(개)')]):
    tx = x + 0.2 + k * (tw + 0.4)
    shape(MSO_SHAPE.RECTANGLE, tx, y, tw, 3.6, fill=NAVY)
    text(tx, y + 0.25, tw, 2.0, [P((num, 48, GOLDB, True), align=C)], anchor='m')
    text(tx, y + 2.3, tw, 1.0, [P((lab, 14, 'DCE3EE'), align=C)], anchor='m')
y += 3.9
text(x + 0.2, y, w - 0.3, 3.0, [
    P(('EPO PATSTAT Global에서 CPC ', 17), ('H01L21(제조공정 분류)', 17, PROC, True), (' 또는 ', 17), ('H01L24(접합·인터커넥트 분류)', 17, BOND, True),
      (' 기호를 가진 출원 중 제목에 hybrid bonding, direct bonding, Cu–Cu 등이 포함된 출원 (1968–2024)', 17), line=1.0)])
y += 3.3
y = sub(x, y, w, 'Measurement', '공동분류 유형화')
vw = w - 1.0
picture(f'{ASSET_}/venn.png', x + 0.5, y, vw)
from PIL import Image as _I; _vw, _vh = _I.open(f'{ASSET_}/venn.png').size
y += vw * _vh / _vw + 0.2
text(x + 0.2, y, w - 0.3, 1.0, [P(('통합형', 16, INTEG, True), (' = 두 분류가 함께 부여된, 공정 단계의 경계를 넘는 발명', 16), align=C)], anchor='m')
y += 1.4
y = sub(x, y, w, 'Core Technology', '핵심 기술 식별')
nw_ = w - 1.4; nh = nw_ * 1196 / 1621
picture(f'{ASSET_}/network_nolegend.png', x + 0.7, y, nw_, nh)
y += nh + 0.1
for k, (col, lab) in enumerate([(CORE, '핵심 기술'), ('EFB26A', '기타 접합 기호'), ('1F3A5F', '제조공정 기호')]):
    lx = x + 0.9 + k * (w - 1.8) / 3
    shape(MSO_SHAPE.OVAL, lx, y + 0.2, 0.45, 0.45, fill=col)
    text(lx + 0.6, y, (w - 1.8) / 3 - 0.6, 0.85, [P((lab, 14))], anchor='m')
y += 1.0
text(x + 0.2, y, w - 0.3, 1.9, [P(('기술 클러스터(출원 200건 이상 기호)와 기술 네트워크로 핵심 접합 기호 ', 16), ('24/03·05·08·80', 16, CORE, True),
                                  (' 식별 → 접합 출원의 81.3%가 보유', 16), line=1.0)])
y += 2.2
y = sub(x, y, w, 'Models', '분석 모형')
table(x + 0.2, y, [4.3, 4.6, 5.0, 3.17][:4], [
    ['분석', '종속변수', '모형', '연구 질문'],
    ['유형 구성', '공동분류 유형(3범주)', '다항 로짓', 'RQ1·RQ2'],
    ['핵심 기술 × 시기', '통합형 여부', '이항 로짓', 'RQ3'],
    ['융합 구조', '영역별 결합·연결', 'Jaccard · 매개 중심성', 'RQ3']], 1.85, size=16, head_size=16)
y += 4 * 1.85 + 0.3
print('col2 end', round(y, 2))

# ================================================================ 3 EVALUATION
x = COLX[2]
y = col_header(x, w, 3, 'Evaluation', '분석 결과')
y = sub(x, y, w, 'RQ1', '융합의 수준과 유형')
for k, (num, lab, col) in enumerate([('45.8%', '통합형 = 최다 유형', INTEG), ('37.2%', '좁은 정의에서도', '9A72E8'), ('1.40배', '기술범위 1 증가 시 RRR', NAVY)]):
    tx = x + 0.2 + k * (tw + 0.4)
    shape(MSO_SHAPE.RECTANGLE, tx, y, tw, 3.3, fill=WHITE, line=col, lw=2.0)
    text(tx, y + 0.2, tw, 1.8, [P((num, 38, col, True), align=C)], anchor='m')
    text(tx, y + 2.1, tw, 0.9, [P((lab, 14, TEXT), align=C)], anchor='m')
y += 3.6
text(x + 0.2, y, w - 0.3, 0.8, [P(('기술범위(CPC 서브그룹 수)별 예측확률', 15, NAVY, True))])
chart_pred(x, y + 0.7, w, 9.0, size=14)
y += 9.8
y = takeaway(x, y, w, [('세부 기술이 다양한 출원일수록 통합형 (RRR 1.403***)', 17, NAVY, True)])
y = sub(x, y + 0.1, w, 'RQ2', '시간적 변화')
text(x + 0.2, y, w - 0.3, 0.8, [P(('시기별 공동분류 유형 구성비 (%)', 15, NAVY, True))])
chart_period(x, y + 0.7, w, 10.6, size=15, label_size=17)
y += 11.4
y = takeaway(x, y, w, [('통합형 31.3% → 62.2% → 44.3%, 무게중심은 제조공정 → 접합', 17, NAVY, True)])
y = sub(x, y + 0.1, w, 'RQ3', '구조적 중심 변화')
text(x + 0.2, y, w - 0.3, 0.8, [P(('핵심 기술 보유 → 통합형 승산비 (95% CI)', 15, NAVY, True))])
y = or_plot(x + 0.2, y + 0.9, w - 0.4, label_w=2.6, rs=1.1, size=17)
text(x + 0.2, y, w - 0.3, 0.8, [P(('제조공정 영역별 접합 기호와의 결합 강도 (Jaccard)', 15, NAVY, True))])
chart_jaccard(x, y + 0.7, w, 7.6, size=13)
y += 8.4
y = takeaway(x, y, w, [('결합 중심: 웨이퍼 제조공정 → 공정 장치·패키징 단계 공정', 17, NAVY, True)])
print('col3 end', round(y, 2))

# ================================================================ 4 CONCLUSION band
by = COL_END + 0.6
bh = 79.9 - by
shape(MSO_SHAPE.RECTANGLE, X0, by, CW, 2.0, fill=NAVY)
shape_text(shape(MSO_SHAPE.OVAL, X0 + 0.5, by + 0.3, 1.4, 1.4, fill=GOLDB), [P(('4', 22, NAVY, True))], margin=0)
text(X0 + 2.3, by, 40, 2.0, [P(('Conclusion & Implications', 26, WHITE, True), ('  ·  결론 및 시사점', 20, 'C8D3E6'))], anchor='m')
cy = by + 2.6
phw = 12.3; step = phw - 0.3
for i, (nm, yrs, col, l1, l2) in enumerate(PHASES):
    kind = MSO_SHAPE.PENTAGON if i == 0 else MSO_SHAPE.CHEVRON
    shape_text(shape(kind, X0 + i * step, cy, phw, 2.1, fill=col), [P((nm + '  ', 24, WHITE, True), (yrs, 18, WHITE))], margin=0.9)
    text(X0 + i * step + 0.5, cy + 2.25, phw - 1.4, 2.7, [P((l1, 18, col, True), after=1), P((l2, 16), line=1.0)])
cw_ = 3 * step + 0.3
qy = cy + 5.2
qh = 79.9 - qy - 0.1
shape(MSO_SHAPE.RECTANGLE, X0, qy, cw_, qh, fill=NAVY)
icon('FaQuoteLeft', X0 + 0.5, qy + 0.45, 1.0, 'FFFFFF')
text(X0 + 1.9, qy, cw_ - 2.4, qh, [P((CONCL[0], 24, WHITE, True, True), (CONCL[1], 24, GOLDB, True, True), (CONCL[2], 24, WHITE, True, True), line=1.05)], anchor='m')
ix = X0 + cw_ + 0.9; iw = X1 - ix
ih = (79.9 - cy - 0.4) / 2
for k, (ic, ttl, body) in enumerate([('FaGraduationCap', '학술적 시사점', IMPL_ACAD), ('FaIndustry', '실무적 시사점', IMPL_PRAC)]):
    yy = cy + k * (ih + 0.3)
    shape(MSO_SHAPE.RECTANGLE, ix, yy, iw, ih, fill=GOLD_L, line=GOLDB, lw=1.5)
    icon_circle(ic, ix + 0.35, yy + 0.3, 1.3, NAVY)
    text(ix + 1.95, yy + 0.25, iw - 2.2, 1.4, [P((ttl, 18, NAVY, True))], anchor='m')
    text(ix + 0.4, yy + 1.7, iw - 0.75, ih - 1.8, [P((body, 17), line=1.05)])

footer()
save(OUT)
