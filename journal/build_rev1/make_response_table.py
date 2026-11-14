# 심사의견 반영표 생성 (교수님께 이메일 송부용)
# 형식: 학과에서 돌려본 반영표 예시와 같은 6열 표
#   목차 | 피드백 항목 | 수정 내용 | 수정 위치 | 상태 | 교수님
# 글꼴 HY신명조 8pt, 머리행 회색(A6A6A6) 굵게, 같은 장은 '목차' 칸을 세로 병합.
# 쪽수는 수정본(clean)을 LibreOffice로 변환해 찾은 값이며, Word에서 필드 갱신 후 한 번 확인할 것.
# 사용: python3 -I make_response_table.py OUT.docx
import sys, docx
from docx.shared import Pt, Mm
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT

FONT = 'HY신명조'
YU, KIM, KANG = '유승호', '김영민', '강민구'

# (목차, 피드백 항목, 수정 내용(줄 목록), 수정 위치(줄 목록), 상태, 교수님(줄 목록))
ROWS = [
 ('국문요지 / ABSTRACT', '연구의 중요성 강조',
  ['국문요지·ABSTRACT 첫 문장에 연구의 중요성 추가:',
   '"반도체는 2024년 한국 전체 수출의 약 5분의 1을 차지한 핵심 산업이며, 미세화가 한계에 이르면서 성능 경쟁의 축은 여러 칩을 쌓고 연결하는 첨단 패키징으로 옮겨 가고 있다."'],
  ['국문요지 (p.iv)', 'ABSTRACT (p.56)'], '완료', [YU]),
 ('국문요지 / ABSTRACT', '가장 큰 기여를 한 줄로',
  ['국문요지·ABSTRACT 끝에 기여를 학술·실무 각 한 줄로 추가:',
   '• 학술: 산업 간 융합의 결과를 분야 수준에서 측정해 온 기술융합 연구를 한 기술의 공정 단계 경계와 개별 발명 수준으로 옮김',
   '• 실무: 핵심 접합 설계는 이미 수렴했고 혁신의 중심이 다이 지지·봉지·공정 장치 등 주변 공정으로 이동 → 후발 기업의 기회는 핵심 원리의 재발명보다 주변 공정·장비·소재에 있음'],
  ['국문요지 (p.iv)', 'ABSTRACT (p.57)'], '완료', [KIM, YU]),

 ('제1장 서론', '서론은 실무적·산업적 중요성 중심으로',
  ['제1장 첫머리에 두 문단 신설:',
   '• 산업 비중: 2024년 반도체 수출 1,419억 달러(전체 수출의 약 20.8%), HBM과 첨단 패키징이 성장을 이끎(산업통상자원부, 2025)',
   '• 하이브리드 본딩을 택한 이유 세 가지: ① 특정 제품이 아니라 CMOS 이미지센서·3D NAND·HBM·로직 적층에 두루 쓰이는 기반 기술, ② 전공정과 후공정의 경계를 관찰하기에 적합, ③ 지금이 산업 구도가 형성되는 시점',
   '• 한국 기업(메모리·장비)에게 갖는 의미'],
  ['제1장 (p.1)'], '완료', [YU]),
 ('제1장 서론', '학술적 공헌은 문헌연구에서 다룰 것',
  ['서론의 학술적 공백 서술을 한 문단으로 축소하고, 상세 내용은 제2장 제5절(신설)로 이동',
   '논문 구성 문단에 제2장 제5절 신설과 제6장 제2절(실무적 시사점과 전망) 반영'],
  ['제1장 (p.2–3)'], '완료', [YU]),
 ('제1장 서론', '과거 변화 서술에 그치지 말 것',
  ['연구 목적 문단 끝에 추가:',
   '"과거 출원의 변화를 기술하는 데 그치지 않고, 관찰된 변화에 비추어 기업이 앞으로 어느 영역에 역량을 집중해야 하는지에 대한 시사점과 전망을 도출하고자 한다."'],
  ['제1장 (p.2)'], '완료', [YU]),
 ('제1장 서론', '가장 큰 기여를 한 줄로',
  ['기여 문단을 학술·실무 두 문장으로 다시 씀(국문요지·결론과 같은 표현)'],
  ['제1장 (p.2)'], '완료', [KIM, YU]),

 ('제2장 이론적 배경', '선행연구 대비 본 연구의 학술적 공헌을 별도 부분으로',
  ['제2장 제5절 "선행연구 검토와 본 연구의 차별성" 신설:',
   '• 표 1 "선행연구와 본 연구의 비교" 추가: Curran and Leker(2011), Karvonen and Kässi(2012), Jeong et al.(2015), Yun and Geum(2019), Kwon et al.(2020)과 본 연구를 융합의 범위·분석 단위, 측정 방법, 시간 차원으로 비교',
   '• 학술적 공헌 세 가지: ① 분석 범위를 산업 간에서 동일 산업 내 공정 단계 간으로 확장, ② 분석 단위를 분야에서 개별 발명으로, ③ 융합 중심의 국면별 이동을 실증',
   '• 산업 수준 융합에 앞서는 미시적 융합 사례로서의 의미(Hacklin et al., 2010)',
   '기존 제5절은 제6절로, 표 번호는 한 칸씩 뒤로(표 1–16 → 표 2–17) 조정, 제2장 도입부 안내 문장 갱신'],
  ['제2장 제5절', '(p.12–14)', '제2장 도입부 (p.6)'], '완료', [YU]),

 ('제4장 자료와 방법', '분석과 결론 사이의 연결고리',
  ['제4장 제4절 끝에 다음 분석으로 이어지는 연결 추가:',
   '"이렇게 식별한 네 기호(24/80, 24/08, 24/05, 24/03)는 핵심 기술 보유(core) 변수의 정의에 그대로 사용되며(표 5), 제5장 제3절에서는 이 변수를 이용하여 …"'],
  ['제4장 제4절', '(p.26)'], '완료', [KIM]),

 ('제5장 결과', '분석과 결론 사이의 연결고리',
  ['제5장 제3절 첫 문단에 분석 흐름 추가:',
   '기술 클러스터(표 6)·기술 네트워크(표 7, 그림 4) → 핵심 기술 네 기호 식별 → 시기별 승산비(표 12) → 결합 강도·매개 중심성(표 13)'],
  ['제5장 제3절', '(p.33)'], '완료', [KIM]),
 ('제5장 결과', '근거가 데이터에서 나왔음이 보이게',
  ['결과 요약표(표 16) 뒤에 추가:',
   '"제6장의 이론적 함의와 실무적 시사점은 이 표에 정리한 결과를 근거로 하며, 각 논의에 근거가 되는 표 번호를 함께 표기하였다."'],
  ['제5장 제4절', '(p.38)'], '완료', [KIM]),

 ('제6장 논의', '미래 전망과 실무 가이드라인 제시',
  ['제6장 제2절을 "실무적 시사점과 전망"으로 확대:',
   '• 도입 문단: 종단분석의 의미는 과거 서술이 아니라 지금 어디에 역량을 집중할지 판단하는 근거에 있음',
   '• 대상별 시사점: ① 메모리·파운드리·조립·테스트 기업(인접 공정 지식 유지), ② 후발 기업(정해진 인터페이스를 전제로 한 응용·주변 공정 발명), ③ 장비·소재·부품 기업(다이 이송·정렬·조립 장치, 패키징 단계 공정)',
   '• 향후 전망: 기술 순환 이론에 따라 혁신의 중심이 다이 핸들링·임시 접합·박리·봉지 공정으로 이동',
   '• 정책적 시사점 두 가지',
   '제6장 도입부에 실무적 시사점·전망을 다룬다는 안내 추가'],
  ['제6장 제2절', '(p.42–44)', '제6장 도입부 (p.39)'], '완료', [YU]),
 ('제6장 논의', '분석과 결론 사이의 연결고리',
  ['표 18 "주요 결과와 실무적 시사점의 연결" 신설:',
   '주요 결과(근거 표) → 의미 → 주된 대상 → 시사점 5개 행',
   '모든 시사점 문장에 근거 표 번호 표기(표 10, 11, 12, 13, 18)'],
  ['제6장 제2절', '(p.42–43)'], '완료', [KIM]),
 ('제6장 논의', '주장은 선행연구 인용으로 뒷받침',
  ['새로 쓴 실무·산업 주장마다 근거 문헌 표기:',
   '• 인접 공정 지식: Brusoni et al.(2001), Kapoor and Adner(2012)',
   '• 지배적 설계 이후 혁신의 이동: Utterback and Abernathy(1975), Anderson and Tushman(1990), Murmann and Frenken(2006)',
   '• 보완 자산: Teece(1986), Jacobides et al.(2006)',
   '• 장비를 매개로 한 확산: Rosenberg(1963)',
   '참고문헌 3편 추가: Teece(1986), Utterback and Abernathy(1975), 산업통상자원부(2025)'],
  ['제1장, 제6장 제2절', '참고문헌 (p.52–53)'], '완료', [YU]),
 ('제6장 논의', '실무적 시사점의 해석 범위',
  ['한계에 추가: 자료에 출원인 정보가 없어 기업별 위치는 분석하지 않았고, 핵심 특허 보유 구도는 산업 보고서(KnowMade, 2024)에 의존함',
   '향후 연구: 출원인 자료를 결합한 분석, 정성적 사례·텍스트 분석'],
  ['제6장 제3절', '(p.45)'], '완료', [YU]),

 ('제7장 결론', '그래서 무엇을 하고자 했는가 / 가치가 드러나게',
  ['• 결론 첫 문장에 연구 목적과 중요성 추가',
   '• "이 결론은 실무적으로 다음을 의미한다"로 시작하는 문단 신설: 기업이 앞으로 무엇을 해야 하는가',
   '• 결론 끝에 기여 한 줄 요약 추가: 산업 수준 융합에 앞서 나타나는 미시적 융합의 형성–확산–재편 과정을 실증'],
  ['제7장', '(p.46–47)'], '완료', [KIM, YU]),

 ('논문 전체', '폰트·줄간격을 학과 배포 양식에 맞출 것',
  ['학과 배포 양식(3-2 학위논문 word 양식)과 대조:',
   '• 본문 줄간격: 고정 17.6pt, 문단 뒤 0pt → 양식 값(배수 1.08, 문단 뒤 8pt)으로 변경',
   '• 변환 과정에서 남은 별도 본문 스타일을 없애고 본문 스타일 하나로 통일',
   '• 글꼴(HY신명조 11pt), 장·절 제목(16pt·13pt), 여백(좌우 35mm, 상하 40mm), 배치 순서, 쪽번호 체계는 양식과 같음을 확인'],
  ['논문 전체'], '완료', [KIM]),
 ('논문 전체', '주관적 표현 지양, 근거에 기반한 서술',
  ['발표 표현에 관한 말씀으로, 원고 전체를 다시 점검함:',
   '• "제 생각에는" 등 1인칭 주관 표현 없음을 확인',
   '• 새로 쓴 논의·시사점은 분석 결과(표 번호)와 선행연구 인용을 근거로 서술'],
  ['논문 전체'], '확인', [KANG]),
]

HEAD = ['목차', '피드백 항목', '수정 내용', '수정 위치', '상태', '교수님']
WIDTHS = [1217, 1466, 3266, 1134, 797, 762]  # dxa, 예시 표와 같음


def set_cell(cell, lines, bold=False, center=False):
    p = cell.paragraphs[0]
    for i, line in enumerate(lines):
        r = p.add_run()
        if i:
            r.add_break()
        r.add_text(line)
        r.bold = bold
        r.font.size = Pt(8)
        r.font.name = FONT
        r.element.get_or_add_rPr().get_or_add_rFonts().set(qn('w:eastAsia'), FONT)
    if center:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER if center else WD_CELL_VERTICAL_ALIGNMENT.TOP


def shade(cell, fill='A6A6A6'):
    tcPr = cell._tc.get_or_add_tcPr()
    sh = OxmlElement('w:shd')
    sh.set(qn('w:val'), 'clear'); sh.set(qn('w:color'), 'auto'); sh.set(qn('w:fill'), fill)
    tcPr.append(sh)


def main(out):
    d = docx.Document()
    st = d.styles['Normal']
    st.font.name = FONT; st.font.size = Pt(9)
    st.element.get_or_add_rPr().get_or_add_rFonts().set(qn('w:eastAsia'), FONT)
    pf = st.paragraph_format; pf.space_after = Pt(0); pf.line_spacing = 1.0
    for s in d.sections:  # 예시 표 문서와 같은 용지·여백
        s.page_width, s.page_height = Mm(215.9), Mm(279.4)
        s.left_margin = s.right_margin = Mm(31.75)
        s.top_margin = s.bottom_margin = Mm(25.4)

    t = d.add_paragraph(); r = t.add_run('학위논문 심사의견 반영표'); r.bold = True; r.font.size = Pt(13)
    t.alignment = WD_ALIGN_PARAGRAPH.CENTER
    d.add_paragraph('논문 제목: 반도체 하이브리드 본딩의 공정 단계 간 기술융합: 특허 기반 종단 분석')
    d.add_paragraph('심사일: 2026년 11월 13일    제출자: 이형규')
    d.add_paragraph('수정한 부분은 논문 수정본(별첨)에 빨간 글씨로 표시하였습니다. 쪽수는 수정본 기준입니다.')
    d.add_paragraph()

    tbl = d.add_table(rows=1, cols=6); tbl.style = 'Table Grid'
    for c, h in zip(tbl.rows[0].cells, HEAD):
        set_cell(c, [h], bold=True, center=True); shade(c)
    prev, first = None, None
    for sec, item, body, loc, status, prof in ROWS:
        cells = tbl.add_row().cells
        if sec != prev:
            set_cell(cells[0], [sec], bold=True, center=True); first = cells[0]; prev = sec
        else:
            first = first.merge(cells[0])  # 같은 장은 '목차' 칸 세로 병합
        set_cell(cells[1], [item])
        set_cell(cells[2], body)
        set_cell(cells[3], loc)
        set_cell(cells[4], [status], center=True)
        set_cell(cells[5], prof, center=True)
    tbl.autofit = False
    for gc, w in zip(tbl._tbl.tblGrid.findall(qn('w:gridCol')), WIDTHS):
        gc.set(qn('w:w'), str(w))
    tblPr = tbl._tbl.tblPr
    tw = tblPr.find(qn('w:tblW'))
    if tw is None:
        tw = OxmlElement('w:tblW'); tblPr.append(tw)
    tw.set(qn('w:w'), str(sum(WIDTHS))); tw.set(qn('w:type'), 'dxa')
    for tr in tbl._tbl.tr_lst:
        for tc, w in zip(tr.tc_lst, WIDTHS):
            tc.width = docx.shared.Twips(w)
    # 머리행 반복
    trPr = tbl.rows[0]._tr.get_or_add_trPr(); h = OxmlElement('w:tblHeader'); h.set(qn('w:val'), 'true'); trPr.append(h)
    d.save(out)


if __name__ == '__main__':
    main(sys.argv[1])
