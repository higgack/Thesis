# -*- coding: utf-8 -*-
"""Poster v6 = v5 laid out for PowerPoint (10-12, the user submits the PPTX, not the PDF): text blocks are sized with
맑은 고딕 widths (Noto x 1.2, calibrated on the user's screenshots, check with check_fit.py) so nothing overlaps.
Poster v5 = v4 with the presenter only as author (10-12, user: the professor is on the paper as the supervisor).
Poster v4 = v3 (variant B, navy + gold) updated to the journal manuscript v64 (10-12):
the professor's title (Korean rendering + the English title), co-author and affiliation as in the paper,
research questions worded as in the paper, keywords as in the paper (no "다항 로짓"), and the template's
"P" box removed with the header art re-centred in its place. A1 template.
Usage: python3 build_v6.py TEMPLATE ASSET_DIR ICON_DIR OUT.pptx"""
import sys
import common as K
from common import *
from PIL import Image as _I

TPL, ASSET_, ICON_, OUT = sys.argv[1:5]
K.init(TPL, ASSET_, ICON_)
K.WSCALE = 1.2   # v6: estimate wraps with PowerPoint's 맑은 고딕 widths (+ margin), breaking at spaces only
# ---- v4: text of the journal manuscript v64 (common.py keeps the v59 thesis text for v1–v3)
TITLE1 = '반도체 하이브리드 본딩의'
TITLE2 = [('공정 단계 간 기술융합', 'GOLD'), (': 특허 기반 종단 분석', 'WHITE')]
TITLE_EN = 'Cross-stage technology convergence in semiconductor hybrid bonding: A patent-based longitudinal analysis'
AUTHORS = '이형규'   # v5: presenter only (v4 had 이형규 · 강민구)
K.AFFIL = AFFIL = '한양대학교 기술경영전문대학원'       # footer contact uses K.AUTHOR (이형규) and K.AFFIL
RQS = [('RQ1', '수준과 유형', '하이브리드 본딩 기술에서 제조공정과 접합 기술 간의 융합이 어느 정도로, 어떠한 형태로 나타나는가?'),
       ('RQ2', '시간적 변화', '공정 단계 간 기술융합은 시간에 따라\n어떻게 변화해 왔는가?'),   # forced break: no one-word last line
       ('RQ3', '구조적 중심 변화', '공정 단계 간 기술융합의 중심이 핵심 기술과 주변 기술 사이에서 시기에 따라 어떻게 재편되는가?')]
KEYWORDS = '기술융합 · 공정 단계 간 융합 · 하이브리드 본딩 · 첨단 패키징 · 기술 네트워크 · 지배적 설계' 
GOLDB, GOLD_L, BAND, RULE = 'D4A72C', 'FFF6DC', 'E8EEF7', 'C9D2E0'
X0, X1 = 1.6, 57.8
CW = X1 - X0
CG = 1.0
COLW = (CW - 2 * CG) / 3
COLX = [X0 + i * (COLW + CG) for i in range(3)]

# ---------------------------------------------------------------- header
HDR = 12.0
to_back(shape(MSO_SHAPE.RECTANGLE, 0, 0, W, HDR, fill=NAVY))
shape(MSO_SHAPE.RECTANGLE, 0, HDR, W, 0.3, fill=GOLDB)
K.P_BOX._element.getparent().remove(K.P_BOX._element)   # v4: the template's "P" box is not used
shape_text(shape(MSO_SHAPE.ROUNDED_RECTANGLE, X0, 1.0, 10.6, 1.25, line=GOLDB, lw=1.5, radius=0.5),
           [P((EVENT, 16, GOLDB, True))], margin=0.1)
fill_ph(153, X0, 2.6, 42.0, 5.1, [P((TITLE1, 56, WHITE, True), line=0.92),
                                  P(*[(t, 56, GOLDB if c == 'GOLD' else WHITE, True) for t, c in TITLE2], line=0.92)])
fill_ph(151, X0, 7.95, 42.0, 1.1, [P((TITLE_EN, 18, 'B9C6DD', False, True))])
fill_ph(150, X0, 9.35, 42.0, 2.0, [P((AUTHORS, 30, WHITE, True), ('   ' + AFFIL, 24, 'DCE3EE'))], anchor='m')

# header art: next-generation HBM stack joined by hybrid-bonded layers (concept)
sx, swd = 44.4, 8.6
ART_TOP = (HDR - 6.65) / 2          # v4: art block (stack + base + caption, 6.65 cm) centred in the 12 cm header
yy = ART_TOP
for k in range(4):
    shape(MSO_SHAPE.RECTANGLE, sx, yy, swd, 0.9, fill=('8FA6CC', '7F98C2', '6F8AB7', '607DAD')[k])
    if k == 0:
        text(sx, yy, swd, 0.9, [P(('DRAM 다이', 13, NAVY, True), align=C)], anchor='m')
    yy += 0.9
    shape(MSO_SHAPE.RECTANGLE, sx, yy, swd, 0.3, fill='BFE3E6')
    for m in range(8):
        shape(MSO_SHAPE.RECTANGLE, sx + 0.35 + m * (swd - 0.7 - 0.5) / 7, yy, 0.5, 0.3, fill=BOND)
    if k == 3:
        bond_y = yy + 0.15
    yy += 0.3
shape(MSO_SHAPE.RECTANGLE, sx - 0.4, yy, swd + 0.8, 1.05, fill='3E5A8A')
text(sx - 0.4, yy, swd + 0.8, 1.05, [P(('베이스(로직) 다이', 13, 'DCE3EE', True), align=C)], anchor='m')
base_y = yy + 0.5
lx = sx + swd + 0.9
ly0 = ART_TOP + 0.9 + 0.15
line(sx + swd + 0.05, ly0, lx - 0.1, ly0, GOLDB, w=1.25)
text(lx, ly0 - 0.5, X1 - lx, 3.0, [P(('하이브리드', 13, GOLDB, True)), P(('본딩 계면', 13, GOLDB, True), after=4),
                                    P(('Cu 패드 +', 12, 'DCE3EE')), P(('산화막 직접 접합', 12, 'DCE3EE'))])
text(sx - 0.4, yy + 1.15, swd + 0.8, 0.7, [P(('차세대 HBM 적층 개념도', 12, '9FB0CC'), align=C)], anchor='m')

# ---------------------------------------------------------------- scaffolding
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
    shape(MSO_SHAPE.RECTANGLE, x + 0.2, y, w - 0.4, h, fill=GOLD_L)
    icon('FaArrowRightLong', x + 0.45, y + (h - 0.75) / 2, 0.75, 'C9A227')
    txt = ''.join(r[0] for r in runs)
    size = runs[0][1]
    while size > 14 and est_lines(txt, size, w - 1.85) > 1:
        size -= 0.5
    runs = [(r[0], size) + tuple(r[2:]) for r in runs]
    text(x + 1.45, y, w - 1.75, h, [P(*runs)], anchor='m')
    return y + h + 0.25

COL_END = 64.6
for i in (1, 2):
    line(COLX[i] - CG / 2, TOP + 2.4, COLX[i] - CG / 2, COL_END, RULE, w=1.0)

# ================================================================ 1 PROBLEM
x, w = COLX[0], COLW
y = col_header(x, w, 1, 'Problem', '연구 문제')
y = sub(x, y, w, 'Background', '연구 배경')
bg = background(18)   # v6: 18 pt (19 in v3–v5) so that column 1 still fits with 맑은 고딕 widths
bgh = est_height([''.join(r[0] for r in runs) for runs in bg], 18, w - 0.3, indent=0.6, after_pt=5) + 0.1
text(x + 0.2, y, w - 0.3, bgh, [P(*runs, bullet=('■', NAVY, 70), indent=0.6, after=5) for runs in bg])
y += bgh + 0.2
# bump vs hybrid cards with a material legend
ch_ = 7.1; gap = 1.0; cw2 = (w - 0.4 - gap) / 2
for k, hyb in enumerate((False, True)):
    cx = x + 0.2 + k * (cw2 + gap)
    shape(MSO_SHAPE.ROUNDED_RECTANGLE, cx, y, cw2, ch_, fill=WHITE, radius=0.06, shadow=True)
    text(cx + 0.3, y + 0.2, cw2 - 0.6, 0.9, [P((('하이브리드 본딩' if hyb else '기존: 솔더 범프 접합'), 17, (INTEG if hyb else MUTED), True), align=C)])
    die_stack(cx + 0.9, y + 1.15, cw2 - 1.8, hyb)
    ly = y + 4.0
    if hyb:
        items = [('BFE3E6', '산화막'), (BOND, '구리 패드'), (INTEG, '접합 계면')]
    else:
        items = [('A9AFBA', '솔더 범프'), ('8A97AD', '다이')]
    iw_ = (cw2 - 0.6) / len(items)
    for m, (col, lab) in enumerate(items):
        ix = cx + 0.3 + m * iw_
        shape(MSO_SHAPE.RECTANGLE, ix + 0.1, ly + 0.17, 0.42, 0.42, fill=col, line='9AA3B2', lw=0.5)
        text(ix + 0.62, ly, iw_ - 0.6, 0.75, [P((lab, 11, MUTED))], anchor='m')
    cap = ([('구리–구리 + 산화막–산화막', True), ('직접 접합', True), ('범프 없이 10 µm 이하 간격', False)] if hyb else
           [('다이 사이를 솔더 범프로 연결', True), ('10 µm 이하 간격은', False), ('구현하기 어려움', False)])
    text(cx + 0.2, y + 4.95, cw2 - 0.4, 1.95, [P((t, 13, TEXT if b else MUTED, b), align=C) for t, b in cap])
line(x + 0.2 + cw2 + 0.15, y + ch_ / 2, x + 0.2 + cw2 + gap - 0.15, y + ch_ / 2, INTEG, w=4, tail='triangle')
y += ch_ + 0.6
y = sub(x, y, w, 'Real World Context', '산업 맥락')
text(x + 0.2, y, w - 0.3, 0.8, [P(('하이브리드 본딩 특허 활동을 주도하는 출원인', 15, TEXT, True), ('  (KnowMade, 2024)', 13, MUTED))], anchor='m')
y += 0.95
names = ['TSMC', 'Adeia', 'YMTC', 'Intel', 'Samsung']
chw = (w - 0.4 - 4 * 0.3) / 5
for k, nm in enumerate(names):
    shape_text(shape(MSO_SHAPE.ROUNDED_RECTANGLE, x + 0.2 + k * (chw + 0.3), y, chw, 1.15, fill=WHITE, line=NAVY, lw=1.5, radius=0.5),
               [P((nm, 16, NAVY, True))], margin=0)
y += 1.4
text(x + 0.2, y, w - 0.3, 1.6, [P(('파운드리·라이선싱 기업·메모리 제조사가 함께 주도 → 접합 기술은 더 이상 후공정 전문기업만의 영역이 아니다.', 17))])
y += 1.95
y = sub(x, y, w, 'Research Gap', '연구 공백')
for i, g in enumerate(GAP):
    shape_text(shape(MSO_SHAPE.OVAL, x + 0.25, y + 0.1, 1.0, 1.0, fill=NAVY), [P((str(i + 1), 16, WHITE, True))], margin=0)
    gh = est_height([g], 18, w - 1.75) + 0.15
    text(x + 1.55, y, w - 1.7, gh, [P((g, 18))])
    y += gh + 0.3
y += 0.1
cqh = 3.4
shape(MSO_SHAPE.RECTANGLE, x + 0.2, y, w - 0.4, cqh, fill=GOLD_L, line=GOLDB, lw=2.0)
text(x + 0.7, y + 0.1, w - 1.4, cqh - 0.2, [
    P(('핵심 질문', 16, 'A07800', True), after=2),
    P(('공정 단계 간 기술 경계의 융합을 개별 특허 수준에서 확인할 수 있는가? 그 융합은 시간에 따라 어떻게 전개되는가?', 18, NAVY, True))], anchor='m')
y += cqh + 0.4
y = sub(x, y, w, 'Research Questions', '연구 질문')
rqh = (COL_END - 0.1 - y - 2 * 0.25) / 3
for tag, ttl, q in RQS:
    shape(MSO_SHAPE.RECTANGLE, x + 0.2, y, w - 0.4, rqh, fill=WHITE, line=RULE, lw=1.0)
    shape_text(shape(MSO_SHAPE.RECTANGLE, x + 0.2, y, 2.9, rqh, fill=NAVY), [P((tag, 22, GOLDB, True))], margin=0)
    text(x + 3.45, y + 0.1, w - 3.9, rqh - 0.2, [P((ttl, 19, NAVY, True), after=1)] + [P((qq, 17)) for qq in q.split('\n')], anchor='m')
    y += rqh + 0.25
print('col1 end', round(y, 2), 'rq card h', round(rqh, 2))

# ================================================================ 2 SOLUTION
x = COLX[1]
y = col_header(x, w, 2, 'Solution', '연구 설계')
y = sub(x, y, w, 'Data', '자료')
tw = (w - 0.4 - 2 * 0.4) / 3
for k, (num, lab) in enumerate([('928', '관련 특허출원(건)'), ('262', 'CPC 서브그룹 기호(개)'), ('8', '출원청(개)')]):
    tx = x + 0.2 + k * (tw + 0.4)
    shape(MSO_SHAPE.RECTANGLE, tx, y, tw, 4.0, fill=NAVY)
    text(tx, y + 0.3, tw, 2.2, [P((num, 52, GOLDB, True), align=C)], anchor='m')
    text(tx, y + 2.6, tw, 1.0, [P((lab, 13, 'DCE3EE'), align=C)], anchor='m')
y += 4.3
text(x + 0.2, y, w - 0.3, 3.0, [
    P(('EPO PATSTAT Global에서 CPC ', 17), ('H01L21(제조공정 분류)', 17, PROC, True), (' 또는 ', 17), ('H01L24(접합·인터커넥트 분류)', 17, BOND, True),
      (' 기호를 가진 출원 중 제목에 hybrid bonding, direct bonding, Cu–Cu 등이 포함된 출원 (1968–2024)', 17))])
y += 3.3
y = sub(x, y, w, 'Measurement', '공동분류 유형화')
vw = w - 1.0
_vw, _vh = _I.open(f'{ASSET_}/venn.png').size
picture(f'{ASSET_}/venn.png', x + 0.5, y, vw)
y += vw * _vh / _vw + 0.2
text(x + 0.2, y, w - 0.3, 1.0, [P(('통합형', 16, INTEG, True), (' = 두 분류가 함께 부여된, 공정 단계의 경계를 넘는 발명', 16), align=C)], anchor='m')
y += 1.6
y = sub(x, y, w, 'Core Technology', '핵심 기술 식별')
nw_ = w - 1.4; nh = nw_ * 1196 / 1621
picture(f'{ASSET_}/network_nolegend.png', x + 0.7, y, nw_, nh)
y += nh + 0.1
for k, (col, lab) in enumerate([(CORE, '핵심 기술'), ('EFB26A', '기타 접합 기호'), ('1F3A5F', '제조공정 기호')]):
    lx_ = x + 0.9 + k * (w - 1.8) / 3
    shape(MSO_SHAPE.OVAL, lx_, y + 0.2, 0.45, 0.45, fill=col)
    text(lx_ + 0.6, y, (w - 1.8) / 3 - 0.6, 0.85, [P((lab, 14))], anchor='m')
y += 1.0
text(x + 0.2, y, w - 0.3, 2.3, [P(('기술 클러스터(출원 200건 이상 기호)와 기술 네트워크로 핵심 접합 기호 ', 16), ('24/03·05·08·80', 16, CORE, True),
                                  (' 식별 → 접합 출원의 81.3%가 보유', 16))])
y += 2.5
y = sub(x, y, w, 'Models', '분석 방법')
rows = [['연구 질문', '분석', '방법'],
        ['RQ1', '융합의 수준과 유형', '유형 분포 · 다항 로짓'],
        ['RQ2', '시간적 변화', '시기별 분포 · 다항 로짓'],
        ['RQ3', '구조적 중심 변화', '핵심 기술 × 시기 이항 로짓,\nJaccard 계수 · 매개 중심성']]
rh_ = [1.45, 1.75, 1.75, 2.4]
table(x + 0.2, y, [3.2, 6.0, 8.47], rows, rh_, size=15, head_size=15, bold_cols=(0,),
      cell_color={(1, 0): NAVY, (2, 0): NAVY, (3, 0): NAVY})
y += sum(rh_) + 0.3
print('col2 end', round(y, 2))

# ================================================================ 3 EVALUATION
x = COLX[2]
y = col_header(x, w, 3, 'Evaluation', '분석 결과')
y = sub(x, y, w, 'RQ1', '융합의 수준과 유형')
for k, (num, lab, col) in enumerate([('45.8%', '통합형 = 최다 유형', INTEG), ('37.2%', '좁은 정의에서도', '9A72E8'), ('1.40배', '기술범위 1 증가 시 RRR', NAVY)]):
    tx = x + 0.2 + k * (tw + 0.4)
    shape(MSO_SHAPE.RECTANGLE, tx, y, tw, 3.3, fill=WHITE, line=col, lw=2.0)
    text(tx, y + 0.2, tw, 1.8, [P((num, 38, col, True), align=C)], anchor='m')
    text(tx, y + 2.1, tw, 0.9, [P((lab, 13, TEXT), align=C)], anchor='m')
y += 3.6
text(x + 0.2, y, w - 0.3, 0.8, [P(('기술범위(CPC 서브그룹 수)별 예측확률', 15, NAVY, True))])
chart_pred(x, y + 0.7, w, 8.6, size=14)
y += 9.4
y = takeaway(x, y, w, [('세부 기술이 다양한 출원일수록 통합형 (RRR 1.403***)', 17, NAVY, True)])
y = sub(x, y + 0.1, w, 'RQ2', '시간적 변화')
text(x + 0.2, y, w - 0.3, 0.8, [P(('시기별 공동분류 유형 구성비 (%)', 15, NAVY, True))])
chart_period(x, y + 0.7, w, 10.4, size=15, label_size=17, cats=('형성기\n–2014', '확산기\n2015–2019', '재편기\n2020–2024'))
y += 11.2
y = takeaway(x, y, w, [('통합형 31.3% → 62.2% → 44.3%, 무게중심은 제조공정 → 접합', 17, NAVY, True)])
y = sub(x, y + 0.1, w, 'RQ3', '구조적 중심 변화')
text(x + 0.2, y, w - 0.3, 0.8, [P(('핵심 기술 보유 → 통합형 승산비 (95% CI)', 15, NAVY, True))])
y = or_plot(x + 0.2, y + 0.9, w - 0.4, label_w=2.6, rs=1.05, size=17, ref_note='1 = 차이 없음')
text(x + 0.2, y, w - 0.3, 0.8, [P(('제조공정 영역별 접합 기호와의 결합 강도 (Jaccard)', 15, NAVY, True))])
jh = COL_END - 0.1 - 1.75 - (y + 0.7)
chart_jaccard(x, y + 0.7, w, jh, size=13)
y += 0.7 + jh + 0.1
y = takeaway(x, y, w, [('결합 중심: 웨이퍼 제조공정 → 공정 장치·패키징 단계 공정', 17, NAVY, True)])
print('col3 end', round(y, 2), 'jaccard h', round(jh, 2))

# ================================================================ 4 CONCLUSION band
by = COL_END + 0.6
shape(MSO_SHAPE.RECTANGLE, X0, by, CW, 2.0, fill=NAVY)
shape_text(shape(MSO_SHAPE.OVAL, X0 + 0.5, by + 0.3, 1.4, 1.4, fill=GOLDB), [P(('4', 22, NAVY, True))], margin=0)
text(X0 + 2.3, by, 40, 2.0, [P(('Conclusion & Implications', 26, WHITE, True), ('  ·  결론 및 시사점', 20, 'C8D3E6'))], anchor='m')
cy = by + 2.5
END = 79.95
phw = 12.3; step = phw - 0.3
for i, (nm, yrs, col, l1, l2) in enumerate(PHASES):
    kind = MSO_SHAPE.PENTAGON if i == 0 else MSO_SHAPE.CHEVRON
    shape_text(shape(kind, X0 + i * step, cy, phw, 2.2, fill=col), [P((nm + '  ', 25, WHITE, True), (yrs, 18, WHITE))], margin=0.9)
    text(X0 + i * step + 0.5, cy + 2.35, phw - 1.4, 2.8, [P((l1, 19, col, True), after=1), P((l2, 16))])
cw_ = 3 * step + 0.3
qy = cy + 5.35
kh = 1.0
qh = END - qy - kh - 0.25
shape(MSO_SHAPE.RECTANGLE, X0, qy, cw_, qh, fill=NAVY)
icon('FaQuoteLeft', X0 + 0.6, qy + 0.4, 1.0, 'FFFFFF')
text(X0 + 2.0, qy, cw_ - 2.6, qh, [P((CONCL[0], 26, WHITE, True, True), (CONCL[1], 26, GOLDB, True, True), (CONCL[2], 26, WHITE, True, True), line=1.05)], anchor='m')
text(X0, END - kh, cw_, kh, [P(('Keywords  ', 15, NAVY, True), (KEYWORDS, 15, MUTED))], anchor='m')
ix = X0 + cw_ + 0.9; iw = X1 - ix
imps = [('FaGraduationCap', '학술적 시사점', ['분석 범위를 산업 간 → 공정 단계 간 융합으로 확장 (RQ1)', '융합을 유형 구성이 변하는 동태적 과정으로 분석 (RQ2)', '핵심–주변 관계·기술 순환 관점의 실증 근거 (RQ3)']),
        ('FaFlask', '방법론적 기여', ['공동분류를 개별 특허의 유형으로 재구성', '기술 클러스터·네트워크로 식별한 핵심 기술과 연결']),
        ('FaIndustry', '실무적 시사점', ['HBM 등 하이브리드 본딩 경쟁력은 접합 원리 하나에 좌우되지 않을 수 있음', '평탄화·표면 화학·정렬 계측과 얇은 다이를 다루고 적층하는 공정 역량까지 필요'])]
need = [1.55 + est_height(body, 15, iw - 0.6, indent=0.5, after_pt=2) for _, _, body in imps]
spare = (END - cy - 2 * 0.25) - sum(need)
assert spare > 0, ('implication boxes do not fit', spare)
yy = cy
for k, (ic, ttl, body) in enumerate(imps):
    ih = need[k] + spare / 3
    shape(MSO_SHAPE.RECTANGLE, ix, yy, iw, ih, fill=GOLD_L, line=GOLDB, lw=1.5)
    icon_circle(ic, ix + 0.3, yy + 0.25, 1.1, NAVY)
    text(ix + 1.6, yy + 0.2, iw - 1.8, 1.2, [P((ttl, 17, NAVY, True))], anchor='m')
    text(ix + 0.35, yy + 1.45, iw - 0.6, ih - 1.5, [P((b, 15), bullet=('•', NAVY), indent=0.5, after=2) for b in body])
    yy += ih + 0.25

footer()
save(OUT)
