"""Build the post-defense revision (2026-11-13) of the thesis from manuscript_v84_ko.docx.
usage: python3 build_rev1.py IN.docx OUT.docx red|clean
Every added or rewritten passage is coloured red in 'red' mode."""
import sys, re, copy
import docx
from docx.shared import RGBColor, Pt
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.text.run import Run
from docx.text.paragraph import Paragraph

SRC, OUT, MODE = sys.argv[1], sys.argv[2], sys.argv[3]
RED_ON = MODE == 'red'
RED = RGBColor(0xFF, 0, 0)
d = docx.Document(SRC)
P = list(d.paragraphs)
W = lambda t: qn('w:' + t)

def assert_text(i, start):
    assert P[i].text.startswith(start), (i, P[i].text[:40])

# ---------- low-level helpers ----------
def redden(r_el, parent):
    if RED_ON:
        Run(r_el, parent).font.color.rgb = RED

def make_run(text, rpr=None):
    r = OxmlElement('w:r')
    if rpr is not None:
        r.append(copy.deepcopy(rpr))
    t = OxmlElement('w:t'); t.text = text; t.set(qn('xml:space'), 'preserve')
    r.append(t)
    return r

def first_rpr(p):
    for r in p._p.findall(W('r')):
        if r.find(W('t')) is not None:
            rp = r.find(W('rPr'))
            return copy.deepcopy(rp) if rp is not None else None
    return None

def append_text(p, text, red=True):
    r = make_run(text, first_rpr(p)); p._p.append(r)
    if red: redden(r, p)

def prepend_text(p, text, red=True):
    r = make_run(text, first_rpr(p))
    runs = p._p.findall(W('r'))
    runs[0].addprevious(r)
    if red: redden(r, p)

def rewrite(p, segs):
    rpr = first_rpr(p)
    for r in p._p.findall(W('r')):
        p._p.remove(r)
    for text, red in segs:
        r = make_run(text, rpr); p._p.append(r)
        if red: redden(r, p)

def trim_from(p, marker):
    """delete text from marker to end of paragraph"""
    runs = p._p.findall(W('r')); found = False
    for r in runs:
        if found:
            p._p.remove(r); continue
        t = r.find(W('t'))
        if t is not None and t.text and marker in t.text:
            t.text = t.text[:t.text.index(marker)]
            found = True
    assert found, marker

def strip_ids(el):
    for tag in ('bookmarkStart', 'bookmarkEnd', 'lastRenderedPageBreak'):
        for b in el.iter(W(tag)):
            pass
        for b in list(el.iter(W(tag))):
            b.getparent().remove(b)
    for h in el.iter(W('hyperlink')):
        if h.get(W('anchor')): del h.attrib[W('anchor')]

def clone_par(like, text_segs, after_el):
    """new paragraph with like's pPr and first run formatting"""
    new = OxmlElement('w:p')
    ppr = like._p.find(W('pPr'))
    if ppr is not None: new.append(copy.deepcopy(ppr))
    after_el.addnext(new)
    p = Paragraph(new, like._parent)
    rpr = first_rpr(like)
    for text, red in text_segs:
        r = make_run(text, rpr); new.append(r)
        if red: redden(r, p)
    return p

def redden_all(p_el, parent):
    for r in p_el.iter(W('r')):
        redden(r, parent)

def set_heading(p, text, red=True):
    ts = [t for t in p._p.iter(W('t'))]
    ts[0].text = text
    for t in ts[1:]: t.text = ''
    if red: redden_all(p._p, p)

def clone_heading(like, text, after_el):
    new = copy.deepcopy(like._p); strip_ids(new)
    after_el.addnext(new)
    p = Paragraph(new, like._parent)
    set_heading(p, text)
    return p

def clone_toc(like, title, page, after_el):
    new = copy.deepcopy(like._p); strip_ids(new)
    after_el.addnext(new)
    ts = list(new.iter(W('t')))
    ts[0].text = title
    for t in ts[1:-1]: t.text = ''
    ts[-1].text = page
    p = Paragraph(new, like._parent); redden_all(new, p)
    return p

def clone_caption(like, num, title, after_el):
    new = copy.deepcopy(like._p); strip_ids(new)
    after_el.addnext(new)
    ts = list(new.iter(W('t')))
    ts[0].text = f'표 {num}.'; ts[1].text = f' {title}.'
    for t in ts[2:]: t.text = ''
    for it in new.iter(W('instrText')):
        if '표' in (it.text or ''):
            it.text = f'표 {num}. {title}'
    p = Paragraph(new, like._parent); redden_all(new, p)
    return p

def clone_note(like, text, after_el):
    new = copy.deepcopy(like._p); strip_ids(new)
    after_el.addnext(new)
    runs = new.findall(W('r'))
    runs[1].find(W('t')).text = ' ' + text
    for r in runs[2:]: new.remove(r)
    p = Paragraph(new, like._parent); redden_all(new, p)
    return p

def clone_table(tbl_like, rows, after_el):
    new = copy.deepcopy(tbl_like._tbl); strip_ids(new)
    trs = new.findall(W('tr'))
    header, tmpl, last = trs[0], trs[1], trs[-1]
    for tr in trs[1:]: new.remove(tr)
    after_el.addnext(new)
    from docx.table import Table
    def fill(tr, vals):
        for tc, v in zip(tr.findall(W('tc')), vals):
            ps = tc.findall(W('p'))
            for extra in ps[1:]: tc.remove(extra)
            p_el = ps[0]
            rpr = None
            for r in p_el.findall(W('r')):
                if rpr is None and r.find(W('rPr')) is not None:
                    rpr = copy.deepcopy(r.find(W('rPr')))
                p_el.remove(r)
            for h in p_el.findall(W('hyperlink')): p_el.remove(h)
            r = make_run(v, rpr); p_el.append(r)
    fill(header, rows[0])
    for k, vals in enumerate(rows[1:]):
        src = last if k == len(rows) - 2 else tmpl   # keep bottom border on last row
        tr = copy.deepcopy(src); fill(tr, vals); new.append(tr)
    t = Table(new, tbl_like._parent)
    for r in new.iter(W('r')): redden(r, t)
    return new

# ---------- 0. sanity anchors (v84 indices) ----------
assert_text(64, '반도체 제조는'); assert_text(70, '반도체 산업은 반세기'); assert_text(72, '하이브리드 본딩에서 나타나는')
assert_text(73, '본 연구의 목적은'); assert_text(75, '세 연구 질문에 대한'); assert_text(76, '본 논문의 구성은')
assert_text(82, '앞에서 검토한 선행연구는'); assert_text(100, '기술범위는 기술융합의'); assert_text(101, '제5절 이론적 틀')
assert_text(103, '표 1.'); assert_text(120, '주:'); assert_text(143, 'H01L24 기호를 보유한')
assert_text(173, '제5장 제2절에서 확인한'); assert_text(193, '표 15는'); assert_text(196, '본 장에서는')
assert_text(206, '제2절 실무적·정책적'); assert_text(207, '기업과 정책의 측면'); assert_text(208, '시기별 분석에서')
assert_text(210, '본 연구에는 다음과'); assert_text(213, '그동안 기술융합은'); assert_text(215, '공정 단계 간 융합은 한 방향')
assert_text(216, '본 연구의 학술적 기여는'); assert_text(268, 'Sternitzke'); assert_text(269, 'Whang'); assert_text(274, 'Ziedonis')
assert_text(287, 'Front-end device'); assert_text(289, 'Cross-stage convergence thus'); assert_text(66, '이상의 결과에 비추어')
assert_text(10, '제4절 특허 기반'); assert_text(11, '제5절 이론적 틀'); assert_text(28, '제2절 실무적·정책적')
assert_text(38, '표 1.'); assert_text(53, '표 16.')
TBL1 = d.tables[3]
assert TBL1.rows[0].cells[0].text == '연구 질문'

# ---------- 1. renumber tables 1-16 -> 2-17 (new 표 1 in 제2장 제5절, new 표 18 in 제6장 제2절) ----------
FINAL = {1:1,2:0,3:1,4:0,5:0,6:1,7:1,8:1,9:0,10:1,11:1,12:0,13:1,14:0,15:0,16:1,17:1,18:1}
PAIRS = {'은':'는','는':'은','을':'를','를':'을','과':'와','와':'과'}
def fix(m):
    n = int(m.group(1)) + 1
    paren, part = m.group(2), m.group(3)
    if part:
        want = {('은','는'):('은','는'), ('을','를'):('을','를'), ('과','와'):('과','와')}
        for (a, b) in want:
            if part in (a, b):
                part = a if FINAL[n] else b
    return f'표 {n}{paren}{part}'
pat = re.compile(r'표 (\d+)(\)?)(은 |는 |을 |를 |과 |와 )?')
def fix2(m):
    n = int(m.group(1)) + 1
    paren, part = m.group(2), m.group(3) or ''
    if part and not paren:   # after ')' the particle belongs to the noun before the parenthesis
        ch = part[0]
        group = {'은':('은','는'),'는':('은','는'),'을':('을','를'),'를':('을','를'),'과':('과','와'),'와':('과','와')}[ch]
        ch = group[0] if FINAL[n] else group[1]
        part = ch + ' '
    return f'표 {n}{paren}{part}'
body = d.element.body
cnt = 0
for el in list(body.iter(W('t'))) + list(body.iter(W('instrText'))):
    if el.text and pat.search(el.text):
        el.text, k = pat.subn(fix2, el.text); cnt += k
print('renumbered refs:', cnt)

# ---------- 2. 국문요지 ----------
prepend_text(P[64], '반도체는 2024년 한국 전체 수출의 약 5분의 1을 차지한 핵심 산업이며, 미세화가 한계에 이르면서 성능 경쟁의 축은 여러 칩을 쌓고 연결하는 첨단 패키징으로 옮겨 가고 있다. ')
append_text(P[66], ' 실무적으로 이 결과는 하이브리드 본딩의 핵심 접합 설계가 이미 수렴하였고 혁신의 중심이 다이 지지·봉지·공정 장치와 같은 주변 공정으로 이동하고 있으므로, 후발 기업의 기회는 핵심 원리의 재발명보다 주변 공정과 장비·소재 역량에 있음을 시사한다. 학술적으로는 산업 간 융합의 결과를 사후적으로 측정해 온 기술융합 연구를 한 기술의 공정 단계 경계와 개별 발명 수준으로 옮겨, 융합의 형성–확산–재편 과정을 실증하였다는 데 의의가 있다.')

# ---------- 3. 제1장 서론 ----------
body_like = P[71]
intro0 = clone_par(body_like, [('반도체는 한국 경제에서 비중이 가장 큰 산업 가운데 하나이다. 2024년 반도체 수출은 1,419억 달러로 전체 수출 6,838억 달러의 약 20.8%를 차지하였으며, 그 증가는 고대역폭 메모리(HBM)와 같은 고부가 메모리 제품이 이끌었다(산업통상자원부, 2025). 인공지능 가속기와 고성능 컴퓨팅의 수요가 커지면서 반도체의 성능 경쟁은 회로 선폭을 줄이는 미세화뿐 아니라 여러 칩을 쌓고 연결하는 첨단 패키징에서도 이루어지고 있다(Lau, 2022). 첨단 패키징 기술이 어떠한 방향으로 발전하는지는 반도체 기업의 투자와 역량 배분을 좌우하는 문제가 되었다.', True)], P[69]._p)

clone_par(body_like, [('여러 첨단 패키징 기술 가운데 하이브리드 본딩에 주목하는 이유는 세 가지이다. 첫째, 하이브리드 본딩은 특정 제품에 한정된 기술이 아니다. CMOS 이미지센서와 3D NAND의 웨이퍼 단위 접합에서 먼저 상용화된 뒤 HBM, 칩렛, 3D 로직의 다이 단위 접합으로 적용 범위가 넓어지고 있어(Lau, 2021, 2022), 메모리와 로직을 가리지 않고 반도체 전반의 성능을 좌우하는 기반 기술이 되고 있다. 둘째, 접합 자체는 후공정에 속하지만 그 성패는 평탄화, 표면 활성화, 정밀 정렬과 같은 전공정의 제조공정 역량에 달려 있어, 반도체 산업의 오랜 분업 경계인 전공정과 후공정의 관계가 어떻게 바뀌는지를 관찰하기에 적합하다. 셋째, 웨이퍼 직접 접합의 원천 개념은 수십 년 전에 등장하였으나 관련 출원이 급증한 것은 2010년대 후반 이후이므로(제4장 제3절), 지금은 이 기술을 둘러싼 산업의 구도가 형성되고 있는 시점이다. 이 구도는 한국 기업에 특히 중요하다. KnowMade(2024)에 따르면 하이브리드 본딩 특허 활동을 주도하는 출원인은 TSMC, Adeia, YMTC, Intel, Samsung으로, 원천기술을 보유한 라이선싱 기업과 해외 파운드리·메모리 기업이 다수를 차지한다. 차세대 HBM 적층에 하이브리드 본딩을 도입하려는 국내 메모리 기업과 이를 뒷받침하는 장비·소재 기업에게는, 핵심 특허가 이미 선점된 상황에서 어느 영역에 역량을 집중해야 하는가가 실무적으로 중요한 질문이 된다. 본 연구는 하이브리드 본딩 특허에 나타난 공정 단계 간 기술융합의 전개를 분석하여 이 질문에 대한 근거를 제시하고자 한다.', True)], P[71]._p)

rewrite(P[72], [('이러한 산업적 중요성에 비해 학술적 이해는 충분하지 않다. 기술융합 연구는 서로 구분되던 기술 영역이 중첩되고 결합하는 과정을 오랫동안 다루어 왔으나(Rosenberg, 1963; Kodama, 1992; Curran and Leker, 2011; Sick and Bröring, 2022), 주된 관심은 서로 다른 산업이나 기술 분야 간의 중첩에 있었고, 한 산업 안에서 공정 단계의 경계를 넘는 융합과 그 내용의 시간적 변화는 실증적으로 충분히 다루어지지 않았다. 선행연구의 구체적인 검토와 본 연구의 차별성은 제2장 제5절에서 정리한다.', True)])

trim_from(P[73], '공동분류를 통해 융합을 포착하는 것은')
append_text(P[73], '이를 통해 과거 출원의 변화를 기술하는 데 그치지 않고, 관찰된 변화에 비추어 기업이 앞으로 어느 영역에 역량을 집중해야 하는지에 대한 시사점과 전망을 도출하고자 한다.')

rewrite(P[75], [('본 연구의 기여는 학술과 실무의 두 측면에서 요약할 수 있다. 학술적으로 본 연구는 산업 간 융합의 결과를 분야 수준에서 측정해 온 기술융합 연구를 한 기술의 공정 단계 경계와 개별 발명 수준으로 옮겨, 융합이 형성–확산–재편의 국면을 거치며 그 내용과 중심이 바뀌는 과정을 실증한다(제2장 제5절). 실무적으로는 핵심 접합 설계가 수렴한 뒤 혁신의 중심이 다이 지지·봉지·공정 장치와 같은 주변 공정으로 이동하였다는 결과를 바탕으로, 후발 기업과 장비·소재 기업이 역량을 집중할 영역과 향후 기술 전개에 대한 전망을 제시한다(제6장 제2절).', True)])

rewrite(P[76], [('본 논문의 구성은 다음과 같다. 제2장에서는 이론적 배경을 검토하고 ', False),
                ('선행연구와 대비한 본 연구의 차별성을 정리한 뒤, ', True),
                ('이를 바탕으로 연구 질문의 이론적 틀을 제시하며, 제3장에서는 하이브리드 본딩 기술과 산업을 개관한다. 제4장에서는 자료와 측정 및 분석 방법을 설명하고, 제5장에서는 세 연구 질문에 따른 분석 결과를 제시한다. 제6장에서는 ', False),
                ('이론적 함의, 실무적 시사점과 전망, 연구의 한계를', True),
                (' 논의하며, 제7장에서 결론을 맺는다.', False)])

# ---------- 4. 제2장: 제1절 끝 문장, 신설 제5절, 기존 제5절 -> 제6절 ----------
t82 = P[82].text
key = '제2장 제5절에서 이를 종합하여 연구 질문별 이론적 근거와 분석 방법을 정리한다.'
assert t82.endswith(key)
rewrite(P[82], [(t82[:-len(key)], False),
                ('제2장 제5절에서 선행연구와 대비한 본 연구의 차별성과 학술적 공헌을 정리한 뒤, 제2장 제6절에서', True),
                (' 이를 종합하여 연구 질문별 이론적 근거와 분석 방법을 정리한다.', False)])

h5 = clone_heading(P[101], '제5절 선행연구 검토와 본 연구의 차별성', P[100]._p)
a = clone_par(P[102], [('앞의 네 절에서 검토한 선행연구와 비교하여 본 연구의 위치를 정리하면 다음과 같다. 기술융합 연구는 서로 구분되던 기술 영역이 중첩되고 결합하는 과정을 오랫동안 다루어 왔으나(Rosenberg, 1963; Kodama, 1992; Curran and Leker, 2011; Sick and Bröring, 2022), 정보통신과 가전(Hacklin et al., 2009), 식품과 제약(Bröring et al., 2006), 나노기술과 바이오기술(No and Park, 2010)의 사례에서 보듯 주된 관심은 서로 다른 산업이나 기술 분야 간의 중첩에 있었다. 한 산업 안에서 가치사슬의 단계를 가로지르는 융합을 다룬 연구는 드물다. 가치사슬 상·하류 기업 간의 특허 인용을 분석한 Karvonen과 Kässi(2012)가 가장 가까운 선례이지만, 이 연구도 하나의 기술이 공정 단계의 경계를 넘는지를 특허 단위에서 검정하지는 않았다. 공동분류를 통해 융합을 포착하는 것은 Curran과 Leker(2011), Kwon et al.(2020) 등에서 활용된 표준적인 방법이나, 이들 연구는 공동분류를 분야 수준의 지수로 집계하였다. 융합의 시간적 변화를 다룬 Jeong et al.(2015)과 Yun과 Geum(2019) 역시 분야 쌍의 융합 정도가 어떻게 변화하는지에 초점을 두었으며, 융합을 단계적 과정으로 보는 이론(Hacklin et al., 2010)에 비해 하나의 기술 안에서 융합의 내용과 중심이 어떻게 바뀌는지에 관한 실증은 부족하다. 공정 단계의 경계를 넘는 발명이 누가 무엇을 생산하고 가치를 어떻게 배분하는지를 규정하는 산업의 틀(Jacobides et al., 2006)을 변화시키는 기술적 출발점이 될 수 있다는 점을 고려하면, 이러한 연구 공백은 보완되어야 한다. 표 1은 대표적인 선행연구와 본 연구를 융합의 범위와 분석 단위, 측정 방법, 시간 차원에서 비교한 것이다.', True)], h5._p)
cap1 = clone_caption(P[103], 1, '선행연구와 본 연구의 비교', a._p)
t1 = clone_table(TBL1, [
    ['연구', '융합의 범위와 분석 단위', '측정 방법', '시간 차원'],
    ['Curran and Leker (2011)', '산업 간(영양기능식품, 정보통신); 기술 분야', '특허 공동분류 기반 지표', '융합 정도의 추세'],
    ['Karvonen and Kässi (2012)', '가치사슬 상·하류 산업 간; 기업·산업', '특허 인용', '가치 시스템 변화 속 인용 관계'],
    ['Jeong et al. (2015)', '기술 분야 간; 분야 쌍', '공동분류 네트워크', '융합의 전개 단계'],
    ['Yun and Geum (2019)', '기술 분류 간(헬스케어 서비스); 분류 쌍', '공동분류의 변동성·지속성 지표', '분류 쌍별 융합의 동태'],
    ['Kwon et al. (2020)', '산업 간; 기술 분야', '대규모 특허 공동분류', '산업 융합의 예측'],
    ['본 연구', '한 산업 내 공정 단계 간(제조공정–접합); 개별 특허', '공동분류 유형, 이산선택 모형, 기술 클러스터·네트워크', '유형 구성과 융합 중심의 국면별 변화'],
], cap1._p)
n1 = clone_note(P[120], '저자 정리. 각 연구의 사례와 측정 방법은 본문 제2장 제1–4절의 검토에 따른다.', t1)
b = clone_par(P[104], [('이러한 비교에서 본 연구의 학술적 공헌은 세 가지로 정리된다. 첫째, 기술융합의 분석 범위를 산업 간 또는 기술 분야 간 융합에서 동일 산업 내 서로 다른 제조공정 단계 간 융합으로 넓혀, 기술융합이 산업 경계뿐 아니라 생산공정의 기술적 경계에서도 나타나는 현상임을 개별 특허 수준에서 보인다. 둘째, 공동분류를 분야 수준의 지수로 집계하는 대신 개별 특허의 유형으로 재구성함으로써, 어떠한 발명이 경계를 넘는지와 그 유형별 구성이 시간에 따라 어떻게 변화하는지를 이산선택 모형으로 추정한다. 이는 융합을 정태적 결합이 아니라 형성, 확산, 재편의 국면을 거치는 동태적 과정으로 다루는 것이다. 셋째, 융합의 양적 변화와 함께 핵심 접합 기술과 제조공정 기술의 결합 관계가 시기별로 어떻게 달라지는지를 추적하여, 기술융합의 동태적 전개를 기술 아키텍처의 핵심–주변 관계와 기술 순환의 관점에서 이해할 수 있는 실증적 근거를 제시한다.', True)], n1._p)
c = clone_par(P[104], [('이러한 공헌은 기술경영 연구의 흐름에서도 의미를 가진다. 기존 기술융합 연구의 상당수는 산업 간 융합이 일어난 결과를 거시적·사후적으로 측정하였다. 그러나 단계 모형(Hacklin et al., 2010)에 따르면 산업 융합은 지식과 기술의 융합에 뒤이어 나타나므로, 산업 수준의 변화를 이해하려면 그에 앞서 개별 기술의 공정 수준에서 지식이 어떻게 결합하는지를 관찰할 필요가 있다. 기업 사례 연구가 산업 구조의 변화를 이해하는 출발점이 되듯, 한 기술의 공정 단계 경계를 깊이 들여다보는 기술 사례 연구는 산업 수준의 융합을 설명하는 미시적 기초가 될 수 있으며, 본 연구는 하이브리드 본딩을 그러한 사례로 삼는다.', True)], b._p)
set_heading(P[101], '제6절 이론적 틀과 연구 질문')

# ---------- 5. 제4장 제4절 -> 제5장 제3절 연결 ----------
append_text(P[143], ' 이렇게 식별한 네 기호(24/80, 24/08, 24/05, 24/03)는 핵심 기술 보유(core) 변수의 정의에 그대로 사용되며(표 5), 제5장 제3절에서는 이 변수를 이용하여 핵심 기술을 포함한 출원이 시기별로 제조공정 지식을 함께 포함하는 정도를 비교한다(표 12).')
append_text(P[173], ' 분석은 다음의 순서로 연결된다. 제4장 제4절의 기술 클러스터(표 6)와 기술 네트워크(표 7, 그림 4)에서 출원 수와 연결 강도가 가장 큰 접합 기호 네 개를 핵심 기술로 식별하고, 이 가운데 하나 이상을 보유한 접합 출원을 핵심 기술 보유 출원으로 정의하였다. 이어 시기마다 핵심 기술 보유 출원과 그렇지 않은 출원의 통합형 승산비를 비교하고(표 12), 그 결과를 제조공정 영역별 결합 강도와 시기별 기술 네트워크의 매개 중심성(표 13)으로 다시 확인하였다.')
append_text(P[193], ' 제6장의 이론적 함의와 실무적 시사점은 이 표에 정리한 결과를 근거로 하며, 각 논의에 근거가 되는 표 번호를 함께 표기하였다.')

# ---------- 6. 제6장 ----------
rewrite(P[196], [('본 장에서는 제5장의 분석 결과를 바탕으로 이론적 함의와 ', False), ('실무적 시사점, 향후 전망을', True), (' 논의하고, 연구의 한계와 향후 연구 방향을 제시한다.', False)])
set_heading(P[206], '제2절 실무적 시사점과 전망')
p0 = clone_par(P[207], [('특허 종단분석의 실무적 의미는 과거의 변화를 기술하는 데 있지 않고, 관찰된 변화의 방향에 비추어 기업이 지금 어디에 역량을 집중해야 하는지를 판단하는 근거를 제공하는 데 있다. 표 18은 제5장의 주요 결과를 근거와 함께 실무적 시사점으로 연결한 것이며, 이하에서는 대상별 시사점, 향후 전망, 정책적 시사점의 순서로 논의한다.', True)], P[206]._p)
cap18 = clone_caption(P[103], 18, '주요 결과와 실무적 시사점의 연결', p0._p)
t18 = clone_table(TBL1, [
    ['주요 결과(근거)', '의미', '주된 대상', '시사점'],
    ['통합형 45.8%(좁은 정의 37.2%); 기술범위가 넓을수록 통합형(표 10)', '접합 역량만으로는 부족하며 인접 공정 지식이 함께 결합됨', '메모리·파운드리·조립·테스트 기업', '직접 수행하지 않는 평탄화·표면 화학·정렬 공정의 지식까지 확보(Brusoni et al., 2001; Kapoor and Adner, 2012)'],
    ['재편기 통합형 62.2%→44.3% 감소, 접합 단독형 23.0%→45.4% 증가(표 11)', '접합과 제조공정 사이의 인터페이스가 안정됨', '후발 진입 기업', '핵심 원리의 재발명보다 정해진 인터페이스를 전제로 한 응용·주변 공정 발명에 집중'],
    ['핵심 기술 포함 비중 53.5%→78.7%→88.0%; 핵심 기술 승산비 11.071→2.452→0.658(비유의)(표 12)', '핵심 접합 설계의 수렴(지배적 설계)', '국내 메모리·장비 기업', '핵심 원리는 라이선스·협력으로 확보하고, 차별화는 보완 자산과 주변 공정에서 추구(Teece, 1986; Jacobides et al., 2006)'],
    ['공정 장치 Jaccard 0.046→0.059→0.176(출원 11→89건), 패키징 단계 공정 0.194→0.234→0.268(43→139건); 재편기 공정 장치의 매개 중심성 비중 26.1%(표 13)', '장비와 다이 단위 공정을 매개로 공정 지식이 확산', '장비·소재·부품 기업, 정책', '다이 이송·정렬·조립 장치와 패키징 단계 공정의 역량과 특허 확보(Rosenberg, 1963)'],
    ['재편기 핵심 기술과 함께 부여된 제조공정: 임시 기판 지지(21/6835, 48건), 봉지용 임시 기판(21/568, 43건)(제5장 제3절)', '혁신의 중심이 다이를 다루고 패키지로 완성하는 공정으로 이동', 'HBM 적층을 준비하는 메모리 기업', '얇은 다이의 지지·박리·핸들링, 적층, 봉지 공정 역량 확보'],
], cap18._p)
n18 = clone_note(P[120], '대상과 시사점은 분석 결과에서 도출한 해석이다. 자료에 출원인 정보가 없어 기업별 위치는 분석하지 않았다(제6장 제3절). 화살표는 형성기→확산기→재편기 순서이며, 출원 수는 확산기→재편기이다.', t18)
# 기존 두 문단 뒤에 근거 표기 + 새 문단 (기존 문단은 표 뒤로 그대로 이어짐)
t207 = P[207].text; k207 = '기업과 정책의 측면에서도 몇 가지 함의를 도출할 수 있다. '
assert t207.startswith(k207)
rewrite(P[207], [('먼저 메모리 기업, 파운드리, 조립·테스트 전문업체에 대한 시사점이다. ', True), (t207[len(k207):], False)])
append_text(P[207], ' 이 결과(표 10, 표 18)는 이들 기업이 자신이 직접 수행하지 않는 인접 공정의 지식까지 유지해야 한다는 점을 시사한다. Brusoni et al.(2001)이 보였듯이 구성요소 사이의 상호의존을 예측하기 어려운 기술 전환기에는 외부에서 조달하는 구성요소에 관한 지식이 시스템을 통합하는 역량을 좌우한다.')
append_text(P[208], ' 재편기에 핵심 기술을 포함한 출원의 통합형 비중(48.4%)과 승산비(0.658, 비유의; 표 12), 그리고 임시 기판 지지(21/6835)와 봉지(21/568) 기호의 부상(제5장 제3절)이 이 해석의 근거이다.')
q1 = clone_par(P[208], [('후발 기업에 대한 시사점은 핵심 설계의 수렴에서 나온다. 접합 출원 가운데 핵심 기술을 포함한 출원의 비중은 형성기 53.5%에서 재편기 88.0%로 높아졌고, 핵심 기술을 포함한 출원이 경계를 더 자주 넘는 경향은 재편기에 확인되지 않았다(표 12). 지배적 설계가 확립된 이후에는 핵심 원리를 둘러싼 경쟁보다 정해진 설계를 전제로 한 점진적 개선과 공정 혁신이 중심이 된다는 기술 순환 이론(Utterback and Abernathy, 1975; Anderson and Tushman, 1990)에 비추어 보면, 이미 수렴한 핵심 접합 원리에서 새로운 원천 특허를 확보할 여지는 줄어들었다고 볼 수 있다. 또한 KnowMade(2024)는 원천기술을 보유한 라이선싱 기업을 하이브리드 본딩 특허 활동의 주요 출원인으로 제시하고 있다. 혁신에서 이익을 얻는 주체가 핵심 기술의 보유자만이 아니라 보완 자산을 갖춘 기업일 수 있다는 Teece(1986)와 Jacobides et al.(2006)의 논의를 적용하면, 후발 기업은 핵심 원리를 라이선스나 협력으로 확보하는 한편 양산 수율과 처리량을 좌우하는 다이 지지, 핸들링, 적층, 봉지와 같은 주변 공정에서 특허 포트폴리오와 공정 역량을 구축하는 전략을 고려할 수 있다. 세분화된 기술 시장에서 넓은 특허 포트폴리오가 협상력의 원천이 된다는 반도체 산업의 선행연구(Hall and Ziedonis, 2001; Ziedonis, 2004)도 이러한 방향을 뒷받침한다.', True)], P[208]._p)
q2 = clone_par(P[208], [('장비·소재·부품 기업에 대한 시사점도 분명하다. 재편기에 접합과 가장 강하게 결합한 제조공정 영역은 패키징 단계 공정이었고, 공정 장치와 접합 기호의 결합 강도는 형성기 0.046에서 재편기 0.176으로 높아졌으며, 두 기호를 함께 가진 출원은 확산기 11건에서 재편기 89건으로 늘었다. 같은 시기 기술 네트워크에서 공정 장치 기호는 전체 매개 중심성의 26.1%를 차지하여, 접합과 제조공정을 잇는 위치가 장비 쪽으로 옮겨 갔다(표 13). 19세기 공작기계 산업이 여러 산업의 공통 공정 지식을 축적하며 독립된 전문 산업으로 성장하였다는 Rosenberg(1963)의 관찰에 비추어 보면, 하이브리드 본딩에서도 다이 이송·정렬·조립 장치와 임시 접합·박리 공정을 공급하는 장비·소재 기업이 공정 지식 확산의 매개가 될 가능성이 크다. 국내 장비·소재 기업에게 이 영역은 핵심 원리 특허의 선점 여부와 관계없이 진입할 수 있는 기회가 될 수 있다.', True)], q1._p)
q3 = clone_par(P[208], [('이상의 결과에 기술 순환 이론을 적용하면 향후 전개를 다음과 같이 전망할 수 있다. 지배적 설계가 확립된 이후 혁신의 중심은 핵심 원리에서 공정과 주변 요소로 옮겨 가며(Utterback and Abernathy, 1975; Murmann and Frenken, 2006), 하이브리드 본딩의 적용 범위는 웨이퍼 단위 접합에서 HBM과 칩렛의 다이 단위 접합으로 넓어지고 있다(Lau, 2021, 2022). 이를 고려하면 앞으로의 출원은 핵심 접합 원리보다 다이 지지·박리·핸들링, 다이 단위 정렬과 적층, 봉지, 그리고 이를 구현하는 공정 장치에서 더 많이 나타날 것으로 예상되며, 경쟁의 축도 핵심 원리에서 수율과 처리량, 장비의 생산성으로 옮겨 갈 것으로 보인다. 다만 이 전망은 관찰된 추세와 이론에 근거한 것이며, 2023–2024년 출원은 공개 시차로 일부만 관찰되었다는 점(그림 3)을 고려하여 후속 자료로 확인할 필요가 있다.', True)], q2._p)
q4 = clone_par(P[208], [('정책 측면에서는 두 가지를 제안할 수 있다. 첫째, 첨단 패키징에 대한 지원의 초점을 접합 원리 자체보다 다이 핸들링, 임시 접합·박리, 봉지 공정과 이를 구현하는 장비·소재로 넓힐 필요가 있다. 융합의 중심이 이동한 곳이 이 영역이기 때문이다(표 13). 둘째, 하이브리드 본딩 특허 활동을 라이선싱 기업과 해외 파운드리·메모리 기업이 주도하는 구도(KnowMade, 2024)에서는 원천특허의 라이선스 구도를 파악하여 대응하는 것과 함께, 국내 기업이 주변 공정에서 특허 포트폴리오를 구축하도록 지원하는 것이 실효적일 수 있다. 이러한 시사점은 융합의 중심 이동이라는 결과에 기초한 해석이므로, 기업별 특허 보유 구도는 출원인 자료를 결합한 후속 분석으로 보완되어야 한다(제6장 제3절).', True)], q3._p)

append_text(P[210], ' 특히 자료에 출원인 정보가 없으므로, 제6장 제2절의 실무적 시사점 가운데 핵심 특허의 보유 구도나 국내 기업의 위치에 관한 부분은 산업 보고서(KnowMade, 2024)에 의존하였다. 출원인 자료를 결합하여 핵심 기술 출원의 주체와 기업 유형별 전략을 분석하는 것, 시기별 대표 특허의 발명 내용을 정성적으로 검토하거나 텍스트 분석으로 토픽 간 관계를 살펴보는 것은 본 연구의 해석을 보강할 후속 과제이다.')

# ---------- 7. 제7장 결론 ----------
prepend_text(P[213], '반도체의 성능 경쟁이 첨단 패키징으로 옮겨 가는 가운데, 본 연구는 그 중심 기술인 하이브리드 본딩에서 전공정과 후공정의 경계가 어떻게 융합되어 왔는지를 밝히고, 이를 근거로 기업이 앞으로 어디에 역량을 집중해야 하는지를 제시하고자 하였다. ')
clone_par(P[215], [('이 결론은 실무적으로 다음을 의미한다. 하이브리드 본딩의 핵심 접합 설계는 이미 수렴하였고 혁신의 중심은 다이 지지·봉지·공정 장치와 같은 주변 공정으로 이동하고 있으므로, 후발 기업의 기회는 핵심 원리의 재발명보다 주변 공정과 장비·소재 역량에 있다. 차세대 HBM 적층을 준비하는 국내 메모리 기업은 접합 원리뿐 아니라 얇은 다이를 지지하고 적층하며 봉지하는 공정까지 포괄하는 역량을, 장비·소재 기업은 다이 핸들링, 임시 접합·박리, 봉지 공정의 장비와 소재를 우선 과제로 삼을 필요가 있다. 지배적 설계 이후의 기술 순환에 비추어 볼 때 앞으로의 경쟁은 이러한 주변 공정의 수율과 생산성에서 이루어질 가능성이 크다(제6장 제2절).', True)], P[215]._p)
append_text(P[216], ' 요약하면, 본 연구는 산업 간 융합의 결과를 사후적으로 측정해 온 기술융합 연구를 한 기술의 공정 단계 경계와 개별 발명 수준으로 옮겨, 산업 수준의 융합에 앞서 나타나는 미시적 융합의 형성–확산–재편 과정을 실증하였다.')

# ---------- 8. 참고문헌 ----------
r1 = clone_par(P[268], [('Teece, D.J., 1986. Profiting from technological innovation: Implications for integration, collaboration, licensing and public policy. Research Policy 15 (6), 285–305. https://doi.org/10.1016/0048-7333(86)90027-2', True)], P[268]._p)
clone_par(P[268], [('Utterback, J.M., Abernathy, W.J., 1975. A dynamic model of process and product innovation. Omega 3 (6), 639–656. https://doi.org/10.1016/0305-0483(75)90068-7', True)], r1._p)
clone_par(P[274], [('산업통상자원부, 2025. 2024년 12월 및 연간 수출입 동향 [보도자료]. 산업통상자원부, 세종.', True)], P[274]._p)

# ---------- 9. ABSTRACT ----------
prepend_text(P[287], 'Semiconductors accounted for about one-fifth of Korea’s exports in 2024, and as transistor scaling slows, competition is shifting toward advanced packaging that stacks and connects chips. ')
append_text(P[289], ' Practically, because the core bonding design has converged and innovation has moved to peripheral processes such as die support, encapsulation, and processing apparatus, latecomers’ opportunities lie less in reinventing the core principle than in peripheral processes and equipment and materials capabilities. Academically, the study moves convergence research from ex post, field-level measurement of inter-industry convergence to the process-stage boundary of a single technology at the level of individual inventions.')

# ---------- 10. 목차 ----------
clone_toc(P[10], '제5절 선행연구 검토와 본 연구의 차별성', '14', P[10]._p)
def set_toc_title(p, title):
    ts = list(p._p.iter(W('t'))); ts[0].text = title
    for t in ts[1:-1]: t.text = ''
    for r in ts[0].getparent().getparent().iter(W('r')) if False else []: pass
    redden(ts[0].getparent(), p)
set_toc_title(P[11], '제6절 이론적 틀과 연구 질문')
set_toc_title(P[28], '제2절 실무적 시사점과 전망')
# 표 목차: 첫 항목(P[38])은 TOC 필드 시작을 품고 있으므로 그 제목을 새 표 1로 바꾸고, 기존 표 1(→표 2)은 그 뒤에 복제
old_first = [t for t in P[38]._p.iter(W('t'))][0].text   # already renumbered to 표 2.
newp = clone_toc(P[39], old_first, '14', P[38]._p)
for r in newp._p.iter(W('r')):
    rp = r.find(W('rPr'))
    if rp is not None and rp.find(W('color')) is not None: rp.remove(rp.find(W('color')))
set_toc_title(P[38], '표 1. 선행연구와 본 연구의 비교')
clone_toc(P[52], '표 18. 주요 결과와 실무적 시사점의 연결', '43', P[53]._p)

# ---------- 11. 서식: 본문 스타일 통일·줄간격 확대 ----------
fp = d.styles['First Paragraph']
for p in d.paragraphs:
    if p.style.name == 'First Paragraph':
        p.style = d.styles['본문1']
# 학과 배포 양식(3-2 학위논문 word 양식)의 본문 문단 설정을 그대로 적용:
# 줄간격 '배수 1.08'(w:line=259, auto), 문단 뒤 8pt(w:after=160). 기존 v84는 '고정 17.6pt'·문단 뒤 0.
sp_el = d.styles['본문1'].element.get_or_add_pPr()
spacing = sp_el.find(W('spacing'))
if spacing is None:
    spacing = OxmlElement('w:spacing'); sp_el.insert(0, spacing)
for k in list(spacing.attrib): del spacing.attrib[k]
spacing.set(W('after'), '160'); spacing.set(W('line'), '259'); spacing.set(W('lineRule'), 'auto')

# update fields (TOC) on open
st = d.settings.element
if st.find(W('updateFields')) is None:
    uf = OxmlElement('w:updateFields'); uf.set(W('val'), 'true'); st.append(uf)

d.save(OUT)
print('saved', OUT)
