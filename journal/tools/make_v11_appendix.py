"""Generate sources/ko_v11/07_appendix.md (부록 A–C) from robustness/results_v9.json.

부록 A: OLS / Poisson / 음이항 비교 (values from manuscript_v9_ko.md Table 4, verified)
부록 B: full re-estimation results (MNL both equations, NB) across the specifications used in 5.4
부록 C: identification of irrelevant uses of the search terms (criteria and counts)
"""
import json, pathlib
r = json.load(open('robustness/results_v9.json'))
def stars(p): return '\\*\\*\\*' if p < 0.01 else ('\\*\\*' if p < 0.05 else ('\\*' if p < 0.10 else ''))
def cell(d, k='rrr'):
    if d is None: return '—'
    return f"{d[k]:.3f}{stars(d['p'])} ({d['se_log']:.3f})"
rows = [('yc','출원연도(yc)'),('breadth','기술범위'),('ccode_CN','중국(CN)'),('ccode_KR','한국(KR)'),('ccode_TW','대만(TW)'),
        ('ccode_JP','일본(JP)'),('ccode_EP','EPO(EP)'),('ccode_WO','PCT(WO)'),('ccode_Other','기타')]
specs = [('mnl_base','기준'),('mnl_adj','조정 범위'),('mnl_dedup','패밀리 중복 제거'),('mnl_strict','좁은 전공정 정의'),
         ('mnl_clean','무관 용법 제외'),('mnl_le2022','2023–2024년 제외'),('mnl_2010','2010년 이후')]
out = []
out.append('## 부록\n')
out.append('### 부록 A. 기술범위 모형의 비교\n')
out.append('표 A1은 본문 표 4의 음이항 모형을 선형 회귀(OLS) 및 Poisson 모형과 나란히 보인 것이다. 부호와 유의성은 세 모형에서 같다. 다만 기술범위는 분산이 평균보다 훨씬 큰 카운트 변수여서(음이항의 과산포 모수 α = 0.268, 표준오차 0.020; Poisson 대비 우도비 검정 χ² = 596.3, p < 0.001), Poisson의 표준오차는 실제보다 작게 추정된다. 예컨대 일본 계수의 표준오차는 Poisson에서 0.080, 음이항에서 0.133이다. 이 때문에 본문에서는 음이항을 쓴다.\n')
out.append('**표 A1.** 기술범위의 결정요인: 모형 비교(N = 928; 출원청 기준 = 미국; 괄호 안은 표준오차; Poisson·음이항 계수의 지수가 IRR; \\* p < 0.10, \\*\\* p < 0.05, \\*\\*\\* p < 0.01).\n')
out.append('''| | OLS | Poisson | 음이항 |
|---|:---:|:---:|:---:|
| 출원연도(yc) | 0.070\\*\\*\\* (0.019) | 0.013\\*\\*\\* (0.002) | 0.014\\*\\*\\* (0.003) |
| 중국(CN) | −1.321\\*\\*\\* (0.370) | −0.241\\*\\*\\* (0.040) | −0.242\\*\\*\\* (0.062) |
| 한국(KR) | 0.089 (0.570) | 0.017 (0.059) | 0.017 (0.094) |
| 대만(TW) | 0.102 (0.510) | 0.016 (0.051) | 0.021 (0.083) |
| 일본(JP) | 1.737\\*\\* (0.845) | 0.280\\*\\*\\* (0.080) | 0.239\\* (0.133) |
| EPO(EP) | −0.032 (0.487) | −0.005 (0.050) | −0.007 (0.080) |
| PCT(WO) | −1.071\\*\\* (0.432) | −0.193\\*\\*\\* (0.047) | −0.191\\*\\*\\* (0.073) |
| 기타 | 0.124 (1.029) | 0.024 (0.106) | 0.056 (0.171) |
| 상수 | 2.506\\*\\*\\* (0.944) | 1.143\\*\\*\\* (0.107) | 1.069\\*\\*\\* (0.175) |
| R² | 0.033 | — | — |
| AIC | 5,224.9 | 5,477.2 | 4,883.0 |
''')
out.append('### 부록 B. 재추정 결과 전체\n')
out.append('표 B1과 B2는 본문 표 9에 요약한 재추정의 전체 계수다. 각 열은 표본 또는 변수 정의를 하나씩 바꾼 다항 로짓이며, 기준 열은 본문 표 6과 같다. 조정 범위는 각 클래스의 첫 기호를 뺀 기술범위를, 패밀리 중복 제거는 같은 제목의 출원 가운데 가장 이른 1건만 남긴 표본을, 좁은 전공정 정의는 H01L21의 조립 단계 공정 기호를 제조공정으로 세지 않은 변수를, 무관 용법 제외는 부록 C의 68건을 뺀 표본을, 2023–2024년 제외는 공개 시차의 영향을 받는 두 해를 뺀 표본을 뜻한다. 2010년 이후 표본은 본문 제5.1절과 제5.4절(일본)에서 언급한 것이다.\n')
def mnl_table(eq, label, tno):
    hdr = '| | ' + ' | '.join(s[1] for s in specs) + ' |\n|---|' + ':---:|'*len(specs) + '\n'
    body = ''
    for k, name in rows:
        cells = []
        for key, _ in specs:
            d = r[key][eq].get(k)
            if key == 'mnl_2010' and eq == 'Process' and k == 'ccode_Other':
                d = None   # no process-only 'other' filings after 2010: not identified
            cells.append(cell(d))
        body += f'| {name} | ' + ' | '.join(cells) + ' |\n'
    body += '| N | ' + ' | '.join(str(r[key]['n']) for key,_ in specs) + ' |\n'
    body += '| 로그우도 | ' + ' | '.join(f"{r[key]['llf']:,.1f}".replace('-', '−') for key,_ in specs) + ' |\n'
    return (f'**표 {tno}.** 다항 로짓의 {label} 방정식: 표본·변수별 상대위험비(기준 범주 = 조립 단독형; 출원청 기준 = 미국; 괄호 안은 로그 계수의 표준오차; \\* p < 0.10, \\*\\* p < 0.05, \\*\\*\\* p < 0.01).\n\n' + hdr + body)
out.append(mnl_table('Integrated', '통합형', 'B1'))
out.append(mnl_table('Process', '공정 단독형', 'B2'))
jp = r['jp_2010_types']; ot = r['other_2010_types']
out.append(f"*주:* 2010년 이후 표본의 일본 출원은 {sum(jp.values())}건(조립 단독형 {jp['Assembly']}, 통합형 {jp['Integrated']}, 공정 단독형 {jp['Process']})뿐이어서 해당 계수는 신뢰할 수 없고, 기타 출원청은 공정 단독형이 0건이어서 공정 단독형 방정식의 계수가 식별되지 않는다(표시 생략).\n")
nbs = [('nb_base','기준'),('nb_dedup','패밀리 중복 제거'),('nb_clean','무관 용법 제외'),('nb_le2022','2023–2024년 제외'),('nb_2010','2010년 이후')]
hdr = '| | ' + ' | '.join(s[1] for s in nbs) + ' |\n|---|' + ':---:|'*len(nbs) + '\n'
body = ''
for k, name in rows:
    if k == 'breadth': continue
    body += f'| {name} | ' + ' | '.join(cell(r[key].get(k), 'irr') for key,_ in nbs) + ' |\n'
body += '| N | ' + ' | '.join(str(r[key]['n']) for key,_ in nbs) + ' |\n'
out.append('**표 B3.** 기술범위의 음이항 모형: 표본별 발생률비(출원청 기준 = 미국; 괄호 안은 로그 계수의 표준오차; \\* p < 0.10, \\*\\* p < 0.05, \\*\\*\\* p < 0.01).\n\n' + hdr + body)
out.append('2010년 이후 표본에서 출원연도의 IRR이 1보다 작아지는 것은 본문 제5.1절에서 언급한 대로다. 2010년 이후 표본의 일본 계수(IRR 1.577)는 기술범위가 21인 같은 패밀리 출원 4건에 좌우되며, 이 4건을 빼면 1.057(p = 0.75)이 된다.\n')
fp = r['fp']
out.append('### 부록 C. 검색어의 무관한 용법\n')
out.append('키워드 필터의 *direct bonding*은 하이브리드 본딩과 무관한 뜻으로도 쓰인다. 제목에 다음 용어가 들어 있는 출원을 무관한 용법으로 판정하였다: 세라믹·DBC(direct bonded copper)·알루미나·지르코니아·질화알루미늄 기판, 구리 합금, 다이아몬드–몰리브덴 접합, 레이저 전구체, 정전 척, 전력 모듈·전력소자 기판, 히트 스프레더, 직접 접합 금속 기판, 직접 접합 회로 조립체, sp3–sp2 하이브리드 결합(화학 결합의 뜻). 해당 출원은 68건이며 표 C1에 분포를 보인다. 2010년 이전 출원 가운데 무관 용법을 뺀 뒤 남는 출원의 상당수는 실리콘 웨이퍼 직접 접합(SOI 웨이퍼 제조 등) 출원으로, 하이브리드 본딩의 유전체 접합을 이루는 전구 기술이므로 표본에 남겼다. 68건의 제목 목록은 저자에게 요청하면 제공한다.\n')
bo = fp['by_office']; bt = fp['by_type']
off_order = [('US','미국'),('CN','중국'),('JP','일본'),('EP','EPO'),('WO','PCT'),('KR','한국'),('TW','대만'),('Other','기타')]
out.append('**표 C1.** 무관 용법으로 판정한 출원 68건의 분포.\n')
out.append('| 구분 | 건수 |\n|---|---:|\n' + ''.join(f'| 출원청: {nm} | {bo.get(c,0)} |\n' for c,nm in off_order) +
           f"| 유형: 조립 단독형 | {bt.get('Assembly',0)} |\n| 유형: 통합형 | {bt.get('Integrated',0)} |\n| 유형: 공정 단독형 | {bt.get('Process',0)} |\n| 출원 시기: 2014년 이전 | {fp['pre2015']} |\n| 출원 시기: 2015년 이후 | {fp['n']-fp['pre2015']} |\n")
pathlib.Path('sources/ko_v11/07_appendix.md').write_text('\n'.join(out), encoding='utf-8')
print('appendix written')
