# -*- coding: utf-8 -*-
"""Variant C: classic academic two-column poster (light banner, pill section tabs, numbered figures/tables).
Usage: python3 build_C.py TEMPLATE ASSET_DIR ICON_DIR OUT.pptx"""
import sys
import common as K
from common import *
from PIL import Image as _I

TPL, ASSET_, ICON_, OUT = sys.argv[1:5]
K.init(TPL, ASSET_, ICON_)
INK, BLUE, HEAD, ROW2 = '1B2F6B', '2F5DA8', '3D6AB0', 'EAF1FB'
X0, X1 = 1.6, 57.8
CG = 1.6
COLW = (X1 - X0 - CG) / 2
XL, XR = X0, X0 + COLW + CG
BODY = 20

# ---------------------------------------------------------------- banner
bn = picture(f'{ASSET_}/banner_classic.png', 0, 0, W, 14.5); to_back(bn)
to_front(K.P_BOX)
picture(f'{ASSET_}/emblem.png', 2.0, 1.6, 6.6, 6.6 * 189 / 187)
fill_ph(153, 9.5, 1.3, 41.0, 5.6, [P((TITLE1, 54, INK, True), align=C, line=0.95), P((TITLE2a + TITLE2b, 54, INK, True), align=C, line=0.95)])
fill_ph(151, 9.5, 7.1, 41.0, 1.3, [P((TITLE_EN, 21, BLUE, True), align=C)])
bar = shape(MSO_SHAPE.ROUNDED_RECTANGLE, 6.0, 9.4, 47.4, 2.0, fill='E6EEF9', radius=0.5); to_back(bar); to_back(bn)
fill_ph(150, 6.0, 9.4, 47.4, 2.0, [P((AUTHOR, 26, INK, True), ('   |   ' + AFFIL + '   |   ' + EMAIL, 22, INK), align=C)], anchor='m')
text(6.0, 11.7, 47.4, 1.0, [P((EVENT, 16, BLUE, True), align=C)], anchor='m')

# ---------------------------------------------------------------- building blocks
def tab(x, y, title, w=None):
    w = w or COLW
    pill = shape(MSO_SHAPE.ROUNDED_RECTANGLE, x + 0.9, y + 0.15, w - 0.9, 2.0, fill=WHITE, line=INK, lw=2.25, radius=0.5)
    shape(MSO_SHAPE.OVAL, x, y, 2.3, 2.3, fill=WHITE, line=INK, lw=2.25)
    icon_circle('FaMagnifyingGlass', x + 0.25, y + 0.25, 1.8, BLUE, pad=0.27)
    text(x + 2.9, y + 0.15, w - 3.4, 2.0, [P((title, 28, '111111', True))], anchor='m')
    return y + 2.7

def bullets(x, y, w, items, size=BODY, after=6, gap=0.35):
    """items: list of run-lists; returns new y"""
    plain = [''.join(r[0] for r in it) for it in items]
    h = est_height(plain, size, w - 0.4, indent=0.8, after_pt=after) + 0.2
    text(x + 0.4, y, w - 0.4, h, [P(*it, bullet=('■', '111111', 80), indent=0.8, after=after, line=1.0) for it in items])
    return y + h + gap

def caption(x, y, w, s_):
    text(x, y, w, 0.9, [P((s_, 16, INK, True), align=C)], anchor='m')
    return y + 1.1

def b(t, col=INK): return (t, BODY, col, True)
def n(t): return (t, BODY)

# ================================================================ LEFT
y = 15.6
y = tab(XL, y, '연구 배경 및 목적')
items = [[(r[0], BODY) + tuple(r[2:]) for r in it] for it in background(BODY)]
items.append([n('본 연구는 '), b('하이브리드 본딩 관련 특허출원 928건의 CPC 공동분류'), n('로 공정 단계 간 기술융합의 수준과 유형, 시간적 변화, 구조적 중심 변화를 분석한다.')])
y = bullets(XL, y, COLW, items)
fw = 17.0; fh = fw * 1500 / 1890
picture(f'{ASSET_}/framework.png', XL + (COLW - fw) / 2, y, fw, fh)
y = caption(XL, y + fh + 0.1, COLW, '<그림 1> 하이브리드 본딩의 기반 공정, 접합 형태, 소자 구조, 적용 제품')
y += 0.5
y = tab(XL, y, '연구 질문')
y = bullets(XL, y, COLW, [[b(f'{tag} ({ttl}) '), n(q)] for tag, ttl, q in RQS])
y += 0.3
y = tab(XL, y, '자료 및 분석 방법')
y = bullets(XL, y, COLW, [
    [b('자료: '), n('EPO PATSTAT Global에서 CPC H01L21 또는 H01L24 기호와 제목 키워드(hybrid bonding, direct bonding, Cu–Cu 등)로 추출한 출원 928건(1968–2024)')],
    [b('측정: '), n('두 분류의 공동분류를 공정 단계 간 기술융합의 대리측정치로 사용(표 1)')],
    [b('핵심 기술: '), n('기술 클러스터와 기술 네트워크로 핵심 접합 기호 24/03·05·08·80 식별(그림 2)')],
    [b('분석: '), n('다항 로짓(RQ1·RQ2), 핵심 기술 × 시기 이항 로짓(RQ3), 영역별 Jaccard 계수·매개 중심성(RQ3)')]])
y = caption(XL, y, COLW, '<표 1> 공동분류 유형의 정의와 분포 (N = 928)')
table(XL + 0.5, y, [5.2, 14.4, 6.2], [
    ['유형', '정의', '출원 수 (비중)'],
    ['접합 단독형', 'H01L24(접합·인터커넥트 분류) 기호만 보유', '338 (36.4%)'],
    ['통합형', '두 분류의 기호를 함께 보유 = 경계를 넘는 발명', '425 (45.8%)'],
    ['공정 단독형', 'H01L21(제조공정 분류) 기호만 보유', '165 (17.8%)']], 1.45, head_fill=HEAD, size=17, head_size=17, alt_fill=ROW2,
    bold_cols=(0,), cell_color={(1, 0): BOND, (2, 0): INTEG, (3, 0): PROC})
y += 4 * 1.45 + 0.6
nh = 79.6 - 1.1 - y
nw_ = nh * 1621 / 1196
if nw_ > COLW - 1.0:
    nw_ = COLW - 1.0; nh = nw_ * 1196 / 1621
picture(f'{ASSET_}/network_nolegend.png', XL + (COLW - nw_) / 2, y, nw_, nh)
y = caption(XL, y + nh + 0.1, COLW, '<그림 2> 기술 네트워크: 핵심 접합 기호(적갈색)가 허브 — 접합 출원의 81.3%가 보유')
print('left end', round(y, 2))

# ================================================================ RIGHT
y = 15.6
y = tab(XR, y, '분석 결과 ① 융합의 수준과 유형 (RQ1)')
y = bullets(XR, y, COLW, [
    [n('통합형이 '), b('45.8%', INTEG), n('로 가장 큰 비중을 차지하였으며, 패키징 단계 공정을 제외한 좁은 정의에서도 37.2%였다.')],
    [n('세부 분류가 하나 증가할 때마다 통합형일 상대위험이 40% 높아졌다(RRR 1.403***).')],
    [n('기술범위 3인 출원의 통합형 예측확률은 0.26, 8인 출원은 0.68이다(그림 3).')]])
chart_pred(XR + 0.4, y, COLW - 0.8, 8.2, size=15)
y = caption(XR, y + 8.3, COLW, '<그림 3> 기술범위에 따른 유형별 예측확률 (다항 로짓)')
y += 0.3
y = tab(XR, y, '분석 결과 ② 시간적 변화 (RQ2)')
y = bullets(XR, y, COLW, [
    [n('통합형 비중은 '), b('31.3% → 62.2% → 44.3%', INTEG), n('로 확산기에 정점에 이른 뒤 낮아졌다(표 2).')],
    [n('공정 단독형의 상대위험은 매년 약 8.7% 감소하였다(RRR 0.913***). 출원의 무게중심은 제조공정에서 접합 쪽으로 이동하였다.')]])
y = caption(XR, y, COLW, '<표 2> 시기별 공동분류 유형 분포 (행 비중, %)')
hi = {(1, 3): PROC, (2, 2): INTEG, (3, 1): BOND}
table(XR + 0.5, y, [7.4, 4.6, 4.6, 4.6, 3.0], [
    ['시기', '접합 단독형', '통합형', '공정 단독형', 'N'],
    ['형성기 (–2014)', '25.1', '31.3', '43.6', '179'],
    ['확산기 (2015–2019)', '23.0', '62.2', '14.8', '209'],
    ['재편기 (2020–2024)', '45.4', '44.3', '10.4', '540']], 1.4, head_fill=HEAD, size=17, head_size=17, alt_fill=ROW2,
    cell_color=hi, cell_bold=set(hi))
y += 4 * 1.4 + 0.6
y = tab(XR, y, '분석 결과 ③ 구조적 중심 변화 (RQ3)')
y = bullets(XR, y, COLW, [
    [n('핵심 기술을 포함한 접합 출원의 통합형 승산비는 '), b('11.07 → 2.45 → 0.66', CORE), n('으로 낮아져 재편기에는 유의하지 않았다(표 3).')],
    [n('접합 출원 중 핵심 기술 비중은 53.5% → 78.7% → 88.0%로 높아졌다.')],
    [n('접합과 가장 강하게 결합한 제조공정 영역은 웨이퍼 제조공정에서 패키징 단계 공정으로 바뀌었다(그림 4).')]])
y = caption(XR, y, COLW, '<표 3> 핵심 기술 보유 여부별 통합형 비중과 승산비 (접합 출원 N = 763)')
table(XR + 0.5, y, [5.2, 5.6, 5.6, 7.8], [
    ['시기', '핵심 기술 보유', '핵심 기술 없음', '승산비 (95% CI)'],
    ['형성기', '79.6%', '27.7%', '11.07*** (4.21–29.11)'],
    ['확산기', '77.1%', '57.9%', '2.45** (1.16–5.19)'],
    ['재편기', '48.4%', '56.9%', '0.66 (0.37–1.16)']], 1.4, head_fill=HEAD, size=17, head_size=17, alt_fill=ROW2)
y += 4 * 1.4 + 0.3
chart_jaccard(XR + 0.4, y, COLW - 0.8, 6.8, size=14)
y = caption(XR, y + 6.9, COLW, '<그림 4> 제조공정 영역별 접합 기호와의 결합 강도 (Jaccard)')
y += 0.3
y = tab(XR, y, '결론')
y = bullets(XR, y, COLW, [
    [(CONCL[0], BODY, INK, True), (CONCL[1], BODY, CORE, True), (CONCL[2], BODY, INK, True)],
    [b('학술적 시사점: '), n(IMPL_ACAD + '하였다.')],
    [b('실무적 시사점: '), n('하이브리드 본딩 경쟁력은 접합 원리 하나에 좌우되지 않을 수 있으며, 평탄화·표면 화학·정렬 계측과 얇은 다이를 다루고 적층하는 공정 역량이 함께 필요할 수 있다.')]])
print('right end', round(y, 2))

footer(head=INK)
save(OUT)
