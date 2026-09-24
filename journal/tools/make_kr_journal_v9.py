# -*- coding: utf-8 -*-
"""Reformat manuscript_v9_ko.md into the provisional 기술혁신연구 layout -> manuscript_v9_ko_JTI.md
(Roman-numeral chapters, <표 n>/<그림 n>, APA-style citations and reference list, 국문요약 + 주제어 first,
English abstract at the end, appendices A/B from the supplements)."""
import re, sys
src = open('manuscript_v9_ko.md', encoding='utf-8').read()
apa = open('refs_apa_v9.md', encoding='utf-8').read().split('\n', 1)[1].strip()
s1 = open('supplement_S1_v9_ko.md', encoding='utf-8').read()
s2 = open('supplement_S2_v9_ko.md', encoding='utf-8').read()

head, body_and_refs = src.split('## 1. 서론', 1)
body, refs = body_and_refs.split('## 참고문헌 (References)', 1)
body = '## 1. 서론' + body

# ---------- front matter ----------
title_ko = re.search(r'^# (.+)$', head, re.M).group(1).strip()
title_en = re.search(r'^\*\*(Front-end.+?)\*\*$', head, re.M).group(1).strip()
ko_abs = head.split('## 국문 초록')[1].split('**주제어:**')[0].strip()
keywords_ko = re.search(r'\*\*주제어:\*\* (.+)', head).group(1).strip()
keywords_en = re.search(r'\*\*Keywords:\*\* (.+)', head).group(1).strip()

KO_SUMMARY = ("기술융합 연구는 주로 서로 다른 산업 사이의 경계가 흐려지는 현상을 다루어 왔다. 본 연구는 한 산업 안에서 일어나는 융합, 곧 반도체 제조의 전공정과 후공정 사이의 융합을 하이브리드 본딩 특허로 검토한다. "
              "유럽특허청 PATSTAT에서 추출한 하이브리드 본딩 특허출원 928건을 이용하여, 접합·인터커넥트(H01L24)와 제조공정(H01L21)을 한 발명 안에서 함께 청구하는 통합형 특허를 경계를 넘는 특허로 정의하고 조립 단독형·공정 단독형과 구분하였다. "
              "다항·이항 로짓과 음이항 모형의 추정 결과, 첫째 기술범위가 경계 넘기의 가장 강한 상관요인이며(통합형 상대위험비 1.40, 조정 범위 1.26), 둘째 순수 전공정 청구는 일관되게 줄었으나 통합형 청구는 2015–2019년 정점 후 2020–2024년에 조립 수준 청구에 자리를 내주었고, "
              "셋째 중국 출원청 출원은 범위가 좁고 공정 단독형에 치우치되 공개 시차로 절단된 2023–2024년을 제외하면 공정 단독형 편중의 유의성이 약해지며, 일본 출원청 결과는 26건의 표본으로는 판정을 유보한다. "
              "본 연구는 융합 이론을 산업 내·공정단계 간 사례로 확장하고, 특허 수준의 메커니즘을 반도체 가치사슬의 아키텍처와 연결하며, 다른 공정기술에 적용할 수 있는 재현 가능한 공동분류 설계를 제공한다.")
EN_ABSTRACT = ("Research on technological convergence has concentrated on the blurring of boundaries between industries. This paper examines convergence within an industry, across the division between front-end fabrication and back-end assembly in semiconductor manufacturing, using 928 hybrid-bonding patent applications from EPO PATSTAT. "
               "We define a boundary-spanning patent as an integrated application that claims fabrication-process technology (CPC H01L21) together with bonding and interconnect (H01L24), as distinct from assembly-only and process-only applications, and model it with negative binomial, binary logit and multinomial logit estimators. "
               "Technological scope is the strongest correlate of boundary spanning (relative-risk ratio 1.40 for the integrated type, 1.26 with the mechanical floor removed). Pure front-end claiming declines consistently, whereas integrated claiming peaks in 2015–2019 and gives way to assembly-level claiming in 2020–2024. "
               "Applications at the Chinese office are narrower and tilted towards process-only claiming in most specifications, although the tilt loses significance once the publication-lag-truncated 2023–2024 filings are excluded; the Japanese-office results rest on 26 applications and are not robust. "
               "The results extend convergence theory to the intra-industry, cross-stage case, connect the patent-level mechanism to the architecture of the semiconductor value chain, and offer a reproducible co-classification design for other process technologies.")
assert len(KO_SUMMARY) <= 800, len(KO_SUMMARY)
assert len(EN_ABSTRACT.split()) <= 200, len(EN_ABSTRACT.split())

front = f"""※ 투고규정 확인 전 임시 서식(2026-09-24). 기술혁신연구(기술경영경제학회) 투고규정 원문을 확인한 뒤 초록 분량·참고문헌 표기·표/그림 체재를 최종 조정할 것.

# {title_ko}

이형규*

\\* 한양대학교 기술경영학과 석사과정, [e-mail]

**국문요약**

{KO_SUMMARY}

**주제어:** {keywords_ko}

---

"""

# ---------- headings: chapters Ⅰ..Ⅶ, sections 1., subsections 1) ----------
ROMAN = {1: 'Ⅰ', 2: 'Ⅱ', 3: 'Ⅲ', 4: 'Ⅳ', 5: 'Ⅴ', 6: 'Ⅵ', 7: 'Ⅶ'}
def fix_headings(text):
    out = []
    for ln in text.split('\n'):
        m = re.match(r'^## (\d)\. (.+)$', ln)
        if m:
            out.append(f"## {ROMAN[int(m.group(1))]}. {m.group(2)}"); continue
        m = re.match(r'^### (\d)\.(\d) (.+)$', ln)
        if m:
            out.append(f"### {int(m.group(2))}. {m.group(3)}"); continue
        out.append(ln)
    return '\n'.join(out)
body = fix_headings(body)
# cross references 제X.Y절 -> 제Ⅹ장 Y절 ; 제X절 -> 제Ⅹ장
body = re.sub(r'제(\d)\.(\d)절', lambda m: f"제{ROMAN[int(m.group(1))]}장 {m.group(2)}절", body)
body = re.sub(r'제(\d)절', lambda m: f"제{ROMAN[int(m.group(1))]}장", body)

# ---------- tables / figures ----------
body = re.sub(r'^\*\*표 (\d+)\.\*\* (.+?)\.?$', lambda m: f"<표 {m.group(1)}> {m.group(2)}", body, flags=re.M)
body = re.sub(r'^\*\*그림 (\d+)\.\*\* (.+?)\.?$', lambda m: f"<그림 {m.group(1)}> {m.group(2)}", body, flags=re.M)
# in-text mentions: 표 8·9 -> <표 8>·<표 9>; 표 4–6 -> <표 4>–<표 6>; 표 n -> <표 n>
body = re.sub(r'(?<![<\[])표 (\d)·(\d)', r'<표 \1>·<표 \2>', body)
body = re.sub(r'(?<![<\[])표 (\d)–(\d)', r'<표 \1>–<표 \2>', body)
body = re.sub(r'(?<![<\[])표 (\d)(?!\d)', r'<표 \1>', body)
body = re.sub(r'(?<![<\[])그림 (\d)(?!\d)', r'<그림 \1>', body)
# restore captions that got double-bracketed
body = body.replace('<<표', '<표').replace('<<그림', '<그림')

# ---------- citations to APA style ----------
def apa_cites(text):
    # parenthetical "A and B, 2001" -> "A & B, 2001"
    text = re.sub(r'([A-Z][A-Za-zÀ-ÿ]+) and ([A-Z][A-Za-zÀ-ÿ]+), (\d{4})', r'\1 & \2, \3', text)
    # narrative "A와 B(2001)" / "A과 B(2001)" -> "A & B(2001)"
    text = re.sub(r'([A-Z][A-Za-zÀ-ÿ]+)[와과] ([A-Z][A-Za-zÀ-ÿ]+)\((\d{4})', r'\1 & \2(\3', text)
    # "Hu and Jefferson (2009)" narrative with space
    text = re.sub(r'([A-Z][A-Za-zÀ-ÿ]+) and ([A-Z][A-Za-zÀ-ÿ]+) \((\d{4})', r'\1 & \2(\3', text)
    return text
body = apa_cites(body)

# ---------- appendices from supplements ----------
def app(text, letter, title):
    text = text.split('\n', 1)[1]  # drop H1
    text = re.sub(r'^## S\d\.(\d+) ', lambda m: f"### {letter}.{m.group(1)} ", text, flags=re.M)
    text = re.sub(r'^## S\d\.0 ', f"### {letter}.0 ", text, flags=re.M)
    text = text.replace('본문 <표', '본문 <표')
    return f"## 부록 {letter}. {title}\n" + text
s1b = fix_headings(s1); s1b = re.sub(r'본문 제5\.4절\(표 8·9\)', '본문 제Ⅴ장 4절(<표 8>·<표 9>)', s1b); s1b = re.sub(r'표 (\d)–(\d)', r'<표 \1>–<표 \2>', s1b); s1b = re.sub(r'표 (\d)·(\d)', r'<표 \1>·<표 \2>', s1b); s1b = re.sub(r'그림 (\d)·(\d)', r'<그림 \1>·<그림 \2>', s1b); s1b = re.sub(r'(?<!<)표 (\d)(?!\d)', r'<표 \1>', s1b); s1b = re.sub(r'(?<!<)그림 (\d)(?!\d)', r'<그림 \1>', s1b)
s2b = s2.replace('본문 제4.1절·제5.4절 여덟째 항목', '본문 제Ⅳ장 1절·제Ⅴ장 4절 여덟째 항목')
appendix = app(s1b, 'A', '강건성 분석 전체 추정 결과') + '\n\n' + app(s2b, 'B', '검색어의 무관한 용법으로 제외한 출원 목록')
body = body.replace('보충자료 S1', '부록 A').replace('보충자료 S2', '부록 B')
appendix = appendix.replace('보충자료 S2', '부록 B')

# ---------- assemble ----------
refs_block = "## 참고문헌\n\n" + apa + "\n"
tail = f"""

---

# {title_en}

HyungKyu Lee*

\\* Master's student, Department of Management of Technology, Hanyang University, Seoul, Republic of Korea

**Abstract**

{EN_ABSTRACT}

**Key Words:** {keywords_en}
"""
out = front + body.rstrip() + '\n\n' + refs_block + '\n' + appendix.rstrip() + '\n' + tail
open('manuscript_v9_ko_JTI.md', 'w', encoding='utf-8').write(out)
print('KR journal version written; 국문요약', len(KO_SUMMARY), '자; Abstract', len(EN_ABSTRACT.split()), 'words')
