# -*- coding: utf-8 -*-
"""Word documents for the thesis defense (10-15):
  defense_QA_vN.docx     expected questions and answers (qa_content.py), key numbers, glossary, references, checklist
  defense_script_vN.docx timed speaker script of the 20- and 15-minute decks (from build_defense.py notes)
Page references use only the page numbers printed in the thesis v84 table of contents.
Usage: python3 make_docs.py N   (reads ../defense_20min_vN_notes.json, ../defense_15min_vN_notes.json)"""
import json, re, sys
from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
import qa_content as QC

VER = sys.argv[1] if len(sys.argv) > 1 else '1'

# v2 (10-20): the decks were reordered after the reference deck; qa_content.py keeps v1 slide numbers, remapped here
if VER in ('2', '3', '4'):
    M20 = {1: 1, 2: 2, 3: 4, 4: 5, 5: 6, 6: 7, 7: 8, 8: 9, 9: 10, 10: 32, 11: 11, 12: 11, 13: 11, 14: 12, 15: 13, 16: 14, 17: 15, 18: 16,
           19: 17, 20: 18, 21: 35, 22: (19, 20, 21, 33), 23: 23, 24: 24, 25: 25, 26: 26, 27: 28, 28: 29, 29: 31, **{k: k + 8 for k in range(30, 37)}}
    M15 = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8, 9: 9, 10: 10, 11: 11, 12: 12, 13: 13, 14: 14, 15: 16, 16: 17, 17: 18, 18: 19,
           19: 21, 20: 22, 21: 23, 22: 24, 23: 25, 24: 26, 25: 31, 26: 32, 27: 34, **{k: k + 8 for k in range(28, 35)}}

    def _nums(part, app_base):
        out = []; app = False      # 'S15 부록 7, 14' = backup slides 7 and 14
        for tok in re.split(r',\s*', part.strip()):
            tok = tok.strip()
            if not tok: continue
            app = app or tok.startswith('부록')
            tok = tok.replace('부록', '').strip()
            a, _, z = tok.partition('–')
            rng = range(int(a), int(z or a) + 1)
            out += [int(n) + (app_base if app else 0) for n in rng]
        return out

    def _fmt(ns):
        ns = sorted(set(ns)); res = []; k = 0
        while k < len(ns):
            j = k
            while j + 1 < len(ns) and ns[j + 1] == ns[j] + 1: j += 1
            res.append(str(ns[k]) if j == k else f'{ns[k]}–{ns[j]}'); k = j + 1
        return ', '.join(res)

    def remap(s):
        if not s: return s
        out = []
        for seg in s.split('·'):
            seg = seg.strip()
            if seg.startswith('S20'):
                out.append('S20 ' + _fmt(m for n in _nums(seg[3:], 29) for m in (M20[n] if isinstance(M20[n], tuple) else (M20[n],))))
            elif seg.startswith('S15'):
                out.append('S15 ' + _fmt(M15[n] for n in _nums(seg[3:], 19)))
            else:
                out.append(seg)
        return ' · '.join(out)

    for q in QC.Q: q['slides'] = remap(q['slides'])
    QC.BACKUP = [
        ('반도체 분업 구조와 첨단 패키징', '본문 5', '부록 1 (22)'),
        ('수직 전문화와 지식 경계', '본문 9', '부록 2 (23)'),
        ('기술 순환 모형과 핵심–주변 구조', '본문 10', '부록 3 (24)'),
        ('이론 종합(Chapter 2 Summary)', '부록 1 (32)', '본문 7'),
        ('분석 대상: 하이브리드 본딩 기술과 산업(그림 2)', '본문 12', '부록 4 (25)'),
        ('측정: 실제 출원 세 건의 CPC 기호', '부록 5 (36)', '—'),
        ('핵심 기술의 식별(클러스터 · 네트워크)', '본문 15', '부록 5 (26)'),
        ('국면별 심층: 형성기 · 확산기 · 재편기의 역설', '본문 19–21', '부록 6–8 (27–29)'),
        ('RQ3 핵심 기술의 승산비(표 11 · 신뢰구간)', '부록 2 (33)', '본문 14'),
        ('RQ3 결합 중심(Jaccard 그래프 · 매개 중심성 표)', '부록 3 (34)', '부록 9 (30)'),
        ('RQ2 출원의 무게중심 이동(연도별 예측확률)', '부록 4 (35)', '부록 10 (31)'),
        ('강건성 검증 요약(표 13 · 14 종합)', '본문 24', '부록 11 (32)'),
        ('산업적 함의: 흔들리는 분업의 인터페이스', '본문 27', '부록 12 (33)'),
        ('실무적 시사점(세 가지)', '본문 28', '부록 13 (34)'),
        ('한계점과 향후 연구 방향', '본문 29', '부록 14 (35)'),
        ('솔더 범프와 하이브리드 본딩 비교', '부록 6 (37)', '—'),
        ('검색 전략과 자료의 특성(표 2)', '부록 7 (38)', '부록 15 (36)'),
        ('변수 정의와 기술통계(표 4)', '부록 8 (39)', '부록 16 (37)'),
        ('기술 클러스터와 네트워크(표 5 · 6)', '부록 9 (40)', '부록 17 (38)'),
        ('이항 · 다항 로짓 결과(표 8 · 9)', '부록 10 (41)', '부록 18 (39)'),
        ('핵심 기술 모형 세부(표 11)', '부록 11 (42)', '부록 19 (40)'),
        ('다항 로짓 재추정(표 13)', '부록 12 (43)', '부록 20 (41)'),
        ('핵심 기술 모형 재추정(표 14)', '부록 13 (44)', '부록 21 (42)')]
    QC.CHECKLIST = [t.replace('20분용은 18–19분, 15분용은 12–13분', '20분용은 19분, 15분용은 13분') for t in QC.CHECKLIST]
# v3 (10-20): 30-minute deck added; its slide numbers are mapped from the v2 20-minute deck (file numbers)
# v4 (10-20): 30-minute deck only; bump comparison folded into the dilemma, search strategy and real filings moved into the talk
N30 = 42 if VER == '4' else 41
if VER in ('3', '4'):
    M30 = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: (6, 7), 7: 8, 8: (9, 10), 9: 11, 10: 12, 11: (14, 15, 16, 17), 12: 18, 13: 19, 14: 20, 15: 21,
           16: 22, 17: (23, 24), 18: 25, 19: 27, 20: 28, 21: 29, 22: 31, 23: (32, 33), 24: 34, 25: 35, 26: 36, 27: 37, 28: 38, 29: 39, 30: 40,
           31: 41, 32: 13, 33: 30, 34: 33, 35: 26, 36: 42, 37: 6, **{k: k + 5 for k in range(38, 45)}}
    if VER == '4':   # v3 30-minute numbers -> v4
        M34 = {**{k: k for k in range(1, 6)}, 6: 6, 7: 6, **{k: k - 1 for k in range(8, 20)}, 20: 20, **{k: k + 1 for k in range(21, 42)},
               42: 21, 43: 19, **{k: k - 1 for k in range(44, 50)}}
        M30 = {k: tuple(sorted({M34[m] for m in (v if isinstance(v, tuple) else (v,))})) for k, v in M30.items()}

    def _m30(ns):
        return sorted({m for n in ns for m in (M30[n] if isinstance(M30[n], tuple) else (M30[n],))})

    for q in QC.Q:
        segs = [g.strip() for g in q['slides'].split('·')] if q['slides'] else []
        s20 = [g for g in segs if g.startswith('S20')]
        if s20:
            ns = [int(a) + k for t in re.split(r',\s*', s20[0][3:].strip()) for a, _, z in [t.partition('–')] for k in range(int(z or a) - int(a) + 1)]
            q['slides'] = ' · '.join(['S30 ' + _fmt(_m30(ns))] + segs)

    def _where30(cell20):
        ns = [int(x) for x in re.findall(r'\((\d+)\)', cell20)] or \
             [n for a, _, z in [re.sub(r'[^\d–]', '', cell20).partition('–')] for n in range(int(a), int(z or a) + 1)]
        ms = _m30(ns)
        main = [m for m in ms if m <= N30]; app = [m for m in ms if m > N30]
        out = []
        if main: out.append('본문 ' + _fmt(main))
        if app: out.append('부록 ' + _fmt([m - N30 for m in app]) + f' ({_fmt(app)})')
        return ' · '.join(out)

    QC.BACKUP = [(c, _where30(a), a, b) for c, a, b in QC.BACKUP]
    QC.CHECKLIST = [t.replace('발표 시간 연습: 20분용은 19분', '발표 시간 연습: 30분용은 29분, 20분용은 19분')
                     .replace('defense_20min_v1.pptx 또는 defense_15min_v1.pptx', f'defense_30min_v{VER}.pptx, defense_20min_v2.pptx 또는 defense_15min_v2.pptx')
                    for t in QC.CHECKLIST]
FONT = '맑은 고딕'
NAVY, MUTED, RED = RGBColor(0x13, 0x29, 0x4B), RGBColor(0x5B, 0x64, 0x75), RGBColor(0x9B, 0x2C, 0x1F)

SEC_PAGE = {'제1장': 1, '2.1': 4, '2.2': 6, '2.3': 8, '2.4': 11, '2.5': 14, '3.1': 16, '3.2': 17, '4.1': 19, '4.2': 20, '4.3': 21,
            '4.4': 23, '4.5': 26, '5.1': 30, '5.2': 31, '5.3': 33, '5.4': 36, '6.1': 40, '6.2': 43, '6.3': 44, '제7장': 46, '부록 A': 54, '부록 B': 55}
TAB_PAGE = {1: 14, 2: 19, 3: 21, 4: 22, 5: 24, 6: 25, 7: 27, 8: 30, 9: 31, 10: 32, 11: 34, 12: 36, 13: 37, 14: 39, 15: 39, 16: 42}
FIG_PAGE = {1: 17, 2: 18, 3: 23, 4: 26, 5: 31, 6: 32}


def src_fmt(src):
    """'5.3 (35쪽), 표 11' -> '제5장 제3절(33쪽~), 표 11(34쪽)': only table-of-contents page numbers are kept"""
    if not src: return ''
    s = re.sub(r'\s*\([^)]*쪽[^)]*\)', '', src)
    def sec(m):
        a, b = m.group(1), m.group(2)
        pg = SEC_PAGE.get(f'{a}.{b}')
        return f'제{a}장 제{b}절' + (f'({pg}쪽~)' if pg else '')
    s = re.sub(r'(?<![\d.])([2-6])\.([1-5])(?![\d])', sec, s)
    s = re.sub(r'제7장', '제7장(46쪽~)', s)
    def tabs(m):
        ns = [int(x) for x in m.group(1).split('·')]
        a, b = TAB_PAGE[ns[0]], TAB_PAGE[ns[-1]]
        return '표 ' + m.group(1) + (f'({a}쪽)' if a == b else f'({a}–{b}쪽)')
    s = re.sub(r'표 (\d+(?:·\d+)*)', tabs, s)
    s = s.replace('제1장', '제1장(1쪽~)').replace('제3장', '제3장(16쪽~)')
    s = re.sub(r'그림 (\d+)', lambda m: f'그림 {m.group(1)}({FIG_PAGE[int(m.group(1))]}쪽)', s)
    s = s.replace('부록 A', '부록 A(54쪽)').replace('부록 B', '부록 B(55쪽)')
    return s


def set_font(run, size=None, bold=None, color=None, italic=None):
    run.font.name = FONT
    rPr = run._element.get_or_add_rPr()
    rf = rPr.find(qn('w:rFonts'))
    if rf is None:
        rf = OxmlElement('w:rFonts'); rPr.insert(0, rf)
    for a in ('w:ascii', 'w:hAnsi', 'w:eastAsia', 'w:cs'): rf.set(qn(a), FONT)
    if size: run.font.size = Pt(size)
    if bold is not None: run.font.bold = bold
    if italic is not None: run.font.italic = italic
    if color is not None: run.font.color.rgb = color


def base_doc(title, subtitle):
    d = Document()
    sec = d.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2.2); sec.top_margin = sec.bottom_margin = Cm(2.0)
    st = d.styles['Normal']; st.font.name = FONT; st.font.size = Pt(10.5)
    st.element.rPr.rFonts.set(qn('w:eastAsia'), FONT)
    pf = st.paragraph_format; pf.space_after = Pt(4); pf.line_spacing = 1.25
    for name, size, col in (('Heading 1', 15, NAVY), ('Heading 2', 12.5, NAVY), ('Heading 3', 11, NAVY), ('Title', 20, NAVY)):
        s = d.styles[name]; s.font.name = FONT; s.font.size = Pt(size); s.font.color.rgb = col; s.font.bold = True
        rpr = s.element.get_or_add_rPr(); rf = rpr.find(qn('w:rFonts'))
        if rf is None: rf = OxmlElement('w:rFonts'); rpr.append(rf)
        for a in ('w:ascii', 'w:hAnsi', 'w:eastAsia', 'w:cs'): rf.set(qn(a), FONT)
        for a in ('w:asciiTheme', 'w:hAnsiTheme', 'w:eastAsiaTheme', 'w:cstheme'):
            if rf.get(qn(a)) is not None: del rf.attrib[qn(a)]
        s.paragraph_format.space_before = Pt(12 if name == 'Heading 1' else 8); s.paragraph_format.space_after = Pt(4)
        s.paragraph_format.keep_with_next = True
    p = d.add_paragraph(); r = p.add_run(title); set_font(r, 18, True, NAVY)
    p = d.add_paragraph(); r = p.add_run(subtitle); set_font(r, 11, False, MUTED)
    # page numbers in footer
    fp = sec.footer.paragraphs[0]; fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = fp.add_run(); f1 = OxmlElement('w:fldChar'); f1.set(qn('w:fldCharType'), 'begin'); r._r.append(f1)
    it = OxmlElement('w:instrText'); it.set(qn('xml:space'), 'preserve'); it.text = 'PAGE'; r._r.append(it)
    f2 = OxmlElement('w:fldChar'); f2.set(qn('w:fldCharType'), 'end'); r._r.append(f2)
    set_font(r, 9, False, MUTED)
    return d


def para(d, text='', size=10.5, bold=False, color=None, after=4, before=0, indent=0.0, runs=None, align=None, keep=False):
    p = d.add_paragraph()
    p.paragraph_format.space_after = Pt(after); p.paragraph_format.space_before = Pt(before)
    if indent: p.paragraph_format.left_indent = Cm(indent)
    if keep: p.paragraph_format.keep_with_next = True
    if align: p.alignment = align
    for t, kw in (runs or [(text, {})]):
        r = p.add_run(t); set_font(r, kw.get('size', size), kw.get('bold', bold), kw.get('color', color), kw.get('italic'))
    return p


def bullet(d, text, size=10.5, color=None, bold_prefix=None):
    p = d.add_paragraph(style='List Bullet')
    p.paragraph_format.space_after = Pt(2)
    if bold_prefix:
        r = p.add_run(bold_prefix); set_font(r, size, True, NAVY)
    r = p.add_run(text); set_font(r, size, False, color)
    return p


def shade(cell, hexcol):
    tcPr = cell._tc.get_or_add_tcPr()
    sh = OxmlElement('w:shd'); sh.set(qn('w:val'), 'clear'); sh.set(qn('w:color'), 'auto'); sh.set(qn('w:fill'), hexcol)
    tcPr.append(sh)


def cell_text(cell, text, size=9.5, bold=False, color=None):
    cell.text = ''
    p = cell.paragraphs[0]; p.paragraph_format.space_after = Pt(0)
    r = p.add_run(text); set_font(r, size, bold, color)


def table(d, rows, widths, head=True, size=9.5, head_fill='13294B', zebra='F4F7FB'):
    t = d.add_table(rows=len(rows), cols=len(rows[0]))
    t.style = 'Table Grid'; t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, row in enumerate(rows):
        for j, v in enumerate(row):
            c = t.cell(i, j); c.width = Cm(widths[j])
            if head and i == 0:
                cell_text(c, v, size, True, RGBColor(0xFF, 0xFF, 0xFF)); shade(c, head_fill)
            else:
                cell_text(c, v, size, j == 0 and len(row) > 2)
                if zebra and i % 2 == 0: shade(c, zebra)
    fix_widths(t, widths)
    d.add_paragraph().paragraph_format.space_after = Pt(2)
    return t


def fix_widths(t, widths):
    t.autofit = False
    tblPr = t._tbl.tblPr
    lay = OxmlElement('w:tblLayout'); lay.set(qn('w:type'), 'fixed'); tblPr.append(lay)
    grid = t._tbl.find(qn('w:tblGrid'))
    for gc, w in zip(grid.findall(qn('w:gridCol')), widths):
        gc.set(qn('w:w'), str(int(Cm(w).twips)))
    for row in t.rows:
        for c, w in zip(row.cells, widths):
            c.width = Cm(w)


def boxed(d, label, text, fill='FFF0C2', size=10.5, bold=True):
    t = d.add_table(rows=1, cols=1); t.style = 'Table Grid'; t.alignment = WD_TABLE_ALIGNMENT.CENTER
    c = t.cell(0, 0); shade(c, fill)
    fix_widths(t, [16.6])
    p = c.paragraphs[0]; p.paragraph_format.space_after = Pt(0)
    r = p.add_run(label); set_font(r, size, True, NAVY)
    r = p.add_run(text); set_font(r, size, bold)
    d.add_paragraph().paragraph_format.space_after = Pt(0)


# ================================================================ Q&A document
def make_qa(out):
    d = base_doc(QC.TITLE, QC.SUBTITLE)
    d.add_heading('이 문서의 사용법', 1)
    for t in QC.HOWTO: bullet(d, t)
    d.add_heading('1. 연구를 짧게 설명하기', 1)
    para(d, '심사 시작이나 질의응답 중 "한마디로 무슨 연구인가"라는 질문을 받으면 아래 문장으로 답합니다.', color=MUTED)
    d.add_heading('30초 버전', 3); boxed(d, '', QC.PITCH_30, fill='EAF0FA', bold=False)
    d.add_heading('1분 버전', 3); boxed(d, '', QC.PITCH_60, fill='EAF0FA', bold=False)
    d.add_heading('2. 반드시 외울 핵심 수치', 1)
    para(d, '수치를 정확히 말하면 답변의 신뢰가 높아집니다. 출처는 학위논문 v84의 장·절·표입니다.', color=MUTED)
    rows = [['구분', '항목', '값', '논문 위치']] + [[g, i, v, src_fmt(w)] for g, i, v, w in QC.NUMBERS]
    table(d, rows, [1.4, 3.2, 8.8, 3.2], size=8.5)
    d.add_heading('3. 답변 원칙', 1)
    for h, t in QC.TIPS: bullet(d, t, bold_prefix=h + ': ')
    d.add_heading('바꿔 말해야 할 표현', 3)
    rows = [['피할 표현', '이렇게 말하기']] + [[a, b] for a, b in QC.AVOID]
    table(d, rows, [6.6, 10.0], size=9.5)
    d.add_heading('4. 예상 질문과 답변', 1)
    para(d, f'모두 {len(QC.Q)}개 질문입니다. ★★★ {sum(1 for q in QC.Q if q["stars"] == 3)}개, ★★ {sum(1 for q in QC.Q if q["stars"] == 2)}개, '
            f'★ {sum(1 for q in QC.Q if q["stars"] == 1)}개.', color=MUTED)
    # index table of ★★★ questions
    d.add_heading('먼저 준비할 ★★★ 질문', 3)
    n = 0; index = []
    for cat, cname in QC.CATS:
        for q in [q for q in QC.Q if q['cat'] == cat]:
            n += 1; q['no'] = f'{cat}{sum(1 for x in QC.Q[:QC.Q.index(q) + 1] if x["cat"] == cat)}'
            if q['stars'] == 3: index.append([q['no'], q['q']])
    table(d, [['번호', '질문']] + index, [1.6, 15.0], size=9.5)
    for cat, cname in QC.CATS:
        d.add_heading(f'{cat}. {cname}', 2)
        for q in [q for q in QC.Q if q['cat'] == cat]:
            h = d.add_heading('', 3)
            r = h.add_run(f'{q["no"]}  {"★" * q["stars"]}{"☆" * (3 - q["stars"])}  '); set_font(r, 11, True, RGBColor(0xC9, 0xA2, 0x27))
            r = h.add_run(q['q']); set_font(r, 11, True, NAVY)
            boxed(d, '답변 요지  ', q['a'])
            if q['d']:
                para(d, '상세 답변', size=10, bold=True, color=NAVY, after=1, keep=True)
                for t in q['d']: para(d, t, size=10.5, after=3, indent=0.3)
            meta = []
            if q['src']: meta.append(('근거: ', src_fmt(q['src'])))
            if q['slides']: meta.append(('슬라이드: ', q['slides']))
            for lab, val in meta:
                para(d, runs=[(lab, {'bold': True, 'color': MUTED, 'size': 9.5}), (val, {'color': MUTED, 'size': 9.5})], after=1, indent=0.3)
            if q['warn']:
                para(d, runs=[('주의: ', {'bold': True, 'color': RED, 'size': 10}), (q['warn'], {'color': RED, 'size': 10})], after=2, indent=0.3)
            for fq, fa in q['follow']:
                para(d, runs=[('꼬리 질문: ', {'bold': True, 'color': NAVY, 'size': 10}), (fq, {'bold': True, 'size': 10})], after=1, indent=0.3)
                para(d, runs=[('→ ', {'bold': True, 'color': NAVY, 'size': 10}), (fa, {'size': 10})], after=3, indent=0.6)
    d.add_heading('5. 용어 정리', 1)
    table(d, [['용어', '뜻']] + [[a, b] for a, b in QC.GLOSSARY], [4.2, 12.4], size=9)
    d.add_heading('6. 핵심 문헌 한 줄 요약', 1)
    para(d, '심사위원이 특정 문헌을 짚어 물을 때를 대비한 요약입니다. 논문에서 그 문헌을 어떻게 썼는지를 중심으로 정리했습니다.', color=MUTED)
    table(d, [['문헌', '논문에서의 역할']] + [[a, b] for a, b in QC.REFS], [4.6, 12.0], size=9)
    d.add_heading('7. 질문별로 넘길 슬라이드', 1)
    para(d, '질문에 답하면서 보여 줄 슬라이드 번호입니다. 괄호 안은 파일의 슬라이드 번호이며, 발표 모드에서 번호를 입력하고 Enter를 누르면 바로 이동합니다.', color=MUTED)
    if VER in ('3', '4'):
        table(d, [['내용', '30분용', '20분용', '15분용']] + [list(r) for r in QC.BACKUP], [7.4, 3.6, 2.9, 2.9], size=9)
    else:
        table(d, [['내용', '20분용', '15분용']] + [list(r) for r in QC.BACKUP], [9.6, 3.5, 3.5], size=9.5)
    d.add_heading('8. 심사 당일 체크리스트', 1)
    for t in QC.CHECKLIST: bullet(d, t)
    d.save(out); print('saved', out, len(QC.Q), 'questions')


# ================================================================ script document
RATE = 5.3   # non-space characters per second, calibrated on the reference talk (about 5.5-6)

def mmss(sec):
    sec = int(round(sec)); return f"{sec // 60}'{sec % 60:02d}"


def make_script(out):
    d = base_doc('석사학위논문 심사 발표 대본', '반도체 하이브리드 본딩의 공정 단계 간 기술융합: 특허 기반 종단 분석 (학위논문 최종본 v84 기준)')
    d.add_heading('사용법', 1)
    for t in ['참고하신 발표 대본처럼 슬라이드마다 시작 시각(분\'초)을 붙였습니다. 시각은 대본 길이로 추정한 값(초당 약 5.3자, 화면 전환 1.5초)이며, 실제 연습에서 맞춰 보십시오.',
              '같은 대본이 발표 파일의 슬라이드 노트에도 들어 있습니다. PowerPoint 발표자 보기에서 노트를 보면서 연습할 수 있습니다.',
              '굵은 글씨는 강조할 핵심 수치입니다. 수치를 말할 때는 반 박자 쉬고 천천히 말하십시오.',
              ('30분용은 약 29분, 20분용은 약 19분, 15분용은 약 13분으로 맞추었습니다.' if VER in ('3', '4') else
               '20분용은 약 19분, 15분용은 약 13분으로 맞추었습니다.' if VER == '2' else '20분용은 약 18–19분, 15분용은 약 12–13분으로 맞추어 질문 전에 여유를 두었습니다.')]:
        bullet(d, t)
    for v in (('30', '20', '15') if VER in ('3', '4') else ('20', '15')):
        nd = json.load(open(f'../defense_{v}min_v{VER if v == "30" or VER not in ("3", "4") else "2"}_notes.json'))
        n_main = nd['n_main']; notes = nd['notes']
        d.add_page_break() if v != ('30' if VER in ('3', '4') else '20') else None
        d.add_heading(f'{v}분 발표 대본 (본 발표 {n_main}장)', 1)
        t = 0.0; rows = [['슬라이드', '제목', '시작', '길이']]; plan = []
        for (no, sec, hdr, sc) in notes[:n_main]:
            dur = len(re.sub(r'\s', '', sc)) / RATE + 1.5
            plan.append((no, sec, hdr, sc, t, dur)); rows.append([str(no), hdr, mmss(t), f'{int(round(dur))}초']); t += dur
        para(d, f'예상 총 소요 시간: {mmss(t)} (질의응답 제외)', bold=True, color=NAVY)
        table(d, rows, [1.6, 10.6, 2.2, 2.2], size=9)
        cur = None
        for no, sec, hdr, sc, start, dur in plan:
            if sec != cur:
                d.add_heading(sec, 2); cur = sec
            para(d, runs=[(f'[{no}] ', {'bold': True, 'color': MUTED, 'size': 10}), (hdr, {'bold': True, 'color': NAVY, 'size': 10.5}),
                          (f'   {mmss(start)}', {'bold': True, 'color': RGBColor(0xC9, 0xA2, 0x27), 'size': 10})], after=1, keep=True)
            # bold the numbers in the script
            parts = re.split(r'(\d[\d.,]*\s?%?(?:배|건|마이크로미터)?)', sc)
            p = d.add_paragraph(); p.paragraph_format.space_after = Pt(6); p.paragraph_format.left_indent = Cm(0.3)
            for k, part in enumerate(parts):
                if not part: continue
                r = p.add_run(part); set_font(r, 10.5, bool(k % 2))
        d.add_heading('백업 슬라이드', 2)
        para(d, '질문을 받았을 때만 보여 주는 슬라이드입니다. 답변은 디펜스 준비 문서(예상 질문과 답변)를 참고하십시오.', color=MUTED)
        for (no, sec, hdr, sc) in notes[n_main:]:
            bullet(d, f'[{no}] {hdr}', size=10)
    d.save(out); print('saved', out)


make_qa(f'../defense_QA_v{VER}.docx')
make_script(f'../defense_script_v{VER}.docx')
