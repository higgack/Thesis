# -*- coding: utf-8 -*-
"""Generate supplement_S1_v9_ko.md (full robustness estimates) and supplement_S2_v9_ko.md (excluded titles)
from robustness/results_v9.json."""
import json
r = json.load(open('robustness/results_v9.json', encoding='utf-8'))
OFF = [('ccode_CN', '중국(CN)'), ('ccode_KR', '한국(KR)'), ('ccode_TW', '대만(TW)'), ('ccode_JP', '일본(JP)'), ('ccode_EP', 'EPO(EP)'), ('ccode_WO', 'PCT(WO)'), ('ccode_Other', '기타')]
def star(p): return '\\*\\*\\*' if p < 0.01 else ('\\*\\*' if p < 0.05 else ('\\*' if p < 0.10 else ''))
def f3(x): return f"{x:.3f}"

def mnl_table(key, title, note=''):
    m = r[key]; rows = [f"**{title}** (N = {m['n']}; 로그우도 = {m['llf']:.1f}; AIC = {m['aic']:.1f})", '',
                        '| | 통합형 RRR | 로그 계수 표준오차 | p | 공정 단독형 RRR | 로그 계수 표준오차 | p |', '|---|:---:|:---:|:---:|:---:|:---:|:---:|']
    for v, lab in [('yc', '출원연도(yc)'), ('breadth', '기술범위')] + OFF:
        if v == 'yc' and v not in m['Integrated']: continue
        a, b = m['Integrated'].get(v), m['Process'].get(v)
        if a is None: continue
        rows.append(f"| {lab} | {f3(a['rrr'])}{star(a['p'])} | ({f3(a['se_log'])}) | {a['p']:.3f} | {f3(b['rrr'])}{star(b['p'])} | ({f3(b['se_log'])}) | {b['p']:.3f} |")
    if note: rows += ['', '*주:* ' + note]
    return '\n'.join(rows)

def period_table(key, title):
    m = r[key]; rows = [f"**{title}** (N = {m['n']}; 로그우도 = {m['llf']:.1f})", '', '| | 통합형 RRR | p | 공정 단독형 RRR | p |', '|---|:---:|:---:|:---:|:---:|']
    for v, lab in [('breadth', '기술범위'), ('per_p1', '2015–2019년(2014년 이전 대비)'), ('per_p2', '2020–2024년(2014년 이전 대비)')] + OFF:
        a, b = m['Integrated'][v], m['Process'][v]
        rows.append(f"| {lab} | {f3(a['rrr'])}{star(a['p'])} | {a['p']:.3f} | {f3(b['rrr'])}{star(b['p'])} | {b['p']:.3f} |")
    w = m['wald_int_p1_vs_p2']; rows += ['', f"*주:* 통합형 방정식의 두 기간 계수 차이에 대한 Wald 검정: χ²(1) = {w['chi2']:.1f}, p = {w['p']:.4f}."]
    return '\n'.join(rows)

def logit_table(key, title, dep='has_process(전공정 지향)'):
    m = r[key]; rows = [f"**{title}** (종속변수 {dep}; N = {m['n']}; 로그우도 = {m['llf']:.1f}; AIC = {m['aic']:.1f}; AUC = {m['auc']:.3f})", '', '| | 승산비 | 로그 계수 표준오차 | p |', '|---|:---:|:---:|:---:|']
    for v, lab in [('yc', '출원연도(yc)'), ('breadth', '기술범위')] + OFF:
        a = m[v]; rows.append(f"| {lab} | {f3(a['or_'])}{star(a['p'])} | ({f3(a['se_log'])}) | {a['p']:.3f} |")
    return '\n'.join(rows)

def nb_table(key, title):
    m = r[key]; rows = [f"**{title}** (N = {m['n']}; α = {m['alpha']:.3f} (표준오차 {m['alpha_se']:.3f}); 로그우도 = {m['llf']:.1f}; AIC = {m['aic']:.1f})", '', '| | IRR | 로그 계수 표준오차 | p |', '|---|:---:|:---:|:---:|']
    for v, lab in [('yc', '출원연도(yc)')] + OFF:
        a = m[v]; rows.append(f"| {lab} | {f3(a['irr'])}{star(a['p'])} | ({f3(a['se_log'])}) | {a['p']:.3f} |")
    return '\n'.join(rows)

d = r['diag']
S1 = f"""# 보충자료 S1. 강건성 분석 전체 추정 결과

> 본문 제5.4절(표 8·9)의 근거. 원자료: EPO PATSTAT 추출 출원–CPC 레코드 5,277건(출원 928건). 추정: Python statsmodels(NegativeBinomial, Logit, MNLogit; Stata `nbreg`/`logit`/`mlogit`과 동일 명세). 기준선(본문 표 4–6)은 원 Stata 분석과 소수점 셋째 자리까지 일치한다. 아래 표의 표준오차는 로그 척도 계수의 표준오차이며(본문 표 5·6의 괄호 값은 델타법으로 계산한 비율 척도의 표준오차), 유의성은 로그 척도의 z 검정이다. \\* p < 0.10, \\*\\* p < 0.05, \\*\\*\\* p < 0.01. 출원청 기준은 미국이며, 다항 로짓의 결과 기준 범주는 조립 단독형이다.

## S1.0 자료 구조와 진단 통계

- 추출 파일의 CPC 기호는 H01L21/\\*와 H01L24/\\* 두 클래스뿐이다(고유 262개). 다른 클래스(H01L2224 인덱싱 코드 등)는 추출 단계에서 보존되지 않았으므로 기술범위는 두 클래스 내부의 고유 서브그룹 수다.
- H01L21 레코드 {r['h21']['records']:,}건의 메인그룹 구성: 웨이퍼 제조공정 그룹 {r['h21']['wafer']}건, 장치·핸들링 그룹(21/67–21/68) {r['h21']['equipment']}건, 조립 단계 그룹(21/48, 21/50–21/58, 21/60, 21/78) {r['h21']['assembly_stage']}건.
- OLS 잔차 이분산: Breusch–Pagan(설명변수 전체) χ²(8) = {d['bp_orig_chi2']:.1f}, p < 0.001; Koenker형 LM = {d['bp_koenker_lm']:.1f}, p < 0.001. 적합값만 넣은 보조회귀(Stata `estat hettest` 기본형)는 기각하지 못한다(χ²(1) = 0.90, p = 0.34).
- Poisson 대 음이항: 경계값 우도비 검정 χ̄²(01) = {d['lr_pois_nb']:.1f}, p < 0.001; Poisson Pearson χ²/자유도 = {d['poisson_pearson_df']:.2f}.
- 이분산 로버스트(HC1) 표준오차: OLS 일본 계수 p = {d['ols_jp_p']:.3f} → {d['ols_jp_p_hc1']:.3f}; Poisson 로버스트 표준오차 중국 {d['poisson_hc1_se_cn']:.3f}, 일본 {d['poisson_hc1_se_jp']:.3f}.

## S1.1 기준 모형(본문 표 4–6과 동일)

{nb_table('nb_base', '음이항(기술범위)')}

{logit_table('logit_base', '이항 로짓')}

{mnl_table('mnl_base', '다항 로짓')}

## S1.2 이분산 로버스트 및 패밀리(제목) 군집 표준오차

{nb_table('nb_hc1', '음이항, HC1 로버스트 표준오차')}

{nb_table('nb_cluster', '음이항, 제목 군집 표준오차')}

{logit_table('logit_cluster', '이항 로짓, 제목 군집 표준오차')}

{mnl_table('mnl_cluster', '다항 로짓, 제목 군집 표준오차', '점추정치는 기준 모형과 같다. 제목 군집 수 = 473.')}

## S1.3 조정 범위(breadth_adj = breadth − has_process − has_assembly)

{logit_table('logit_adj', '이항 로짓, 조정 범위')}

{mnl_table('mnl_adj', '다항 로짓, 조정 범위')}

## S1.4 2010년 이후 하위표본

{nb_table('nb_2010', '음이항, 2010년 이후')}

{nb_table('nb_2010_nojp21', '음이항, 2010년 이후에서 기술범위 21의 일본 출원 4건 제외')}

{logit_table('logit_2010', '이항 로짓, 2010년 이후')}

{mnl_table('mnl_2010', '다항 로짓, 2010년 이후', '2010년 이후 일본 출원청 15건은 조립 단독형 1건·통합형 13건·공정 단독형 1건이며, 기타 출원청 10건에는 공정 단독형이 없어 기타 → 공정 단독형 계수는 완전분리로 식별되지 않는다(표시된 값은 무의미).')}

## S1.5 기간 더미 명세와 경계 민감도

{period_table('mnl_period', '다항 로짓, 기간 더미(–2014 / 2015–2019 / 2020–2024)')}

{period_table('mnl_period_b1', '경계 1년 앞당김(–2013 / 2014–2018 / 2019–2024)')}

{period_table('mnl_period_b2', '경계 1년 늦춤(–2015 / 2016–2020 / 2021–2024)')}

## S1.6 2023–2024년(공개 시차 절단) 제외

{nb_table('nb_le2022', '음이항, 2022년 이전')}

{logit_table('logit_le2022', '이항 로짓, 2022년 이전')}

{mnl_table('mnl_le2022', '다항 로짓, 2022년 이전')}

2023–2024년 출원 가운데 중국 출원청 {r['recent']['cn_n']}건 중 공정 단독형 {r['recent']['cn_process']}건, 미국 출원청 {r['recent']['us_n']}건 중 {r['recent']['us_process']}건. 출원연도별 평균 기술범위: {', '.join(f"{k}년 {v:.2f}" for k, v in r['recent']['mean_breadth_by_year'].items())}.

## S1.7 패밀리 중복 제거(정규화한 제목당 최초 출원 1건, N = {r['mnl_dedup']['n']})

유형 분포: 조립 단독형 {r['dedup_counts']['Assembly']}건({r['dedup_shares']['Assembly']*100:.1f}%), 통합형 {r['dedup_counts']['Integrated']}건({r['dedup_shares']['Integrated']*100:.1f}%), 공정 단독형 {r['dedup_counts']['Process']}건({r['dedup_shares']['Process']*100:.1f}%). 같은 제목의 미국–중국 쌍 {r['family']['us_cn_pairs']}개 중 기술범위 동일 {r['family']['same_breadth']}쌍, 전략 유형 동일 {r['family']['same_type']}쌍, 중국 쪽이 좁은 쌍 {r['family']['cn_narrower']}개.

{nb_table('nb_dedup', '음이항, 중복 제거')}

{logit_table('logit_dedup', '이항 로짓, 중복 제거')}

{mnl_table('mnl_dedup', '다항 로짓, 중복 제거')}

## S1.8 엄격한 전공정 정의(조립 단계 메인그룹 제외, N = {r['mnl_strict']['n']})

유형 분포: 조립 단독형 {r['strict_counts']['Assembly']}건, 통합형 {r['strict_counts']['Integrated']}건, 공정 단독형 {r['strict_counts']['Process']}건(두 클래스 모두 기호가 남지 않는 {r['strict_counts']['None']}건 제외). 전공정 지향 비율 {r['strict_hps_share']*100:.1f}%. 기간별 비중(조립/통합/공정): –2014 {r['strict_period_shares']['Assembly']['–2014']*100:.1f}/{r['strict_period_shares']['Integrated']['–2014']*100:.1f}/{r['strict_period_shares']['Process']['–2014']*100:.1f}; 2015–2019 {r['strict_period_shares']['Assembly']['2015–2019']*100:.1f}/{r['strict_period_shares']['Integrated']['2015–2019']*100:.1f}/{r['strict_period_shares']['Process']['2015–2019']*100:.1f}; 2020–2024 {r['strict_period_shares']['Assembly']['2020–2024']*100:.1f}/{r['strict_period_shares']['Integrated']['2020–2024']*100:.1f}/{r['strict_period_shares']['Process']['2020–2024']*100:.1f}.

{logit_table('logit_strict', '이항 로짓, 엄격 정의', 'has_process(엄격 정의)')}

{mnl_table('mnl_strict', '다항 로짓, 엄격 정의')}

## S1.9 검색어의 무관한 용법 제외(N = {r['mnl_clean']['n']}; 제외 목록은 보충자료 S2)

{nb_table('nb_clean', '음이항, 무관 용법 제외')}

{logit_table('logit_clean', '이항 로짓, 무관 용법 제외')}

{mnl_table('mnl_clean', '다항 로짓, 무관 용법 제외')}

## S1.10 무관 대안의 독립성(IIA) 간접 점검

공정 단독형을 제외한 표본(통합형 대 조립 단독형, N = {r['logit_int_vs_asm']['n']})의 이항 로짓: 기술범위 OR {r['logit_int_vs_asm']['breadth']['or_']:.3f}, 출원연도 {r['logit_int_vs_asm']['yc']['or_']:.3f}, 중국 {r['logit_int_vs_asm']['ccode_CN']['or_']:.3f}, 한국 {r['logit_int_vs_asm']['ccode_KR']['or_']:.3f}, 대만 {r['logit_int_vs_asm']['ccode_TW']['or_']:.3f}, 일본 {r['logit_int_vs_asm']['ccode_JP']['or_']:.3f}, EPO {r['logit_int_vs_asm']['ccode_EP']['or_']:.3f}, PCT {r['logit_int_vs_asm']['ccode_WO']['or_']:.3f}, 기타 {r['logit_int_vs_asm']['ccode_Other']['or_']:.3f}. 본문 표 6 통합형 방정식(1.403, 0.993, 1.205, 1.166, 1.312, 1.046, 1.307, 1.126, 2.326)과 큰 차이가 없다.

## S1.11 분류 관행 점검에 쓴 집계

- H01L24 계열 서브그룹 {r['classif']['h24_subgroups']}개 중 2009년 이전 출원에도 부여된 기호 {r['classif']['h24_pre2010']}개. 2015년 이후 접합 기호 보유 출원 {r['classif']['post2015_apps_with_h24']}건 중 기존 기호 보유 비율 {r['classif']['share_with_old_sym']*100:.1f}%; 같은 기간 H01L24 레코드 중 기존 기호 비율 {r['classif']['share_records_old_sym']*100:.1f}%.
- 출원당 H01L24 기호 수: {', '.join(f"{k} {v:.2f}" for k, v in r['symbols_by_period']['n24'].items())}; H01L21 기호 수: {', '.join(f"{k} {v:.2f}" for k, v in r['symbols_by_period']['n21'].items())}; 접합 기호 보유 비율: {', '.join(f"{k} {v*100:.1f}%" for k, v in r['symbols_by_period']['has24'].items())}.

## S1.12 그림 4·6의 예측값

- 그림 4(음이항, 출원연도를 표본 평균 yc = {r['yc_mean']:.1f}에 고정): {', '.join(f"{k} {v['pred']:.2f} [{v['lo']:.2f}, {v['hi']:.2f}]" for k, v in r['fig4_pred'].items())}.
- 그림 6(다항 로짓, 평균 예측확률): {', '.join(f"{k} {v['mean']:.3f} [{v['lo']:.3f}, {v['hi']:.3f}]" for k, v in r['fig6_margins'].items())}. 공변량을 평균에 고정한 예측(참고): {', '.join(f"{k} {v['atmeans']:.3f}" for k, v in r['fig6_margins'].items())}.
"""
open('supplement_S1_v9_ko.md', 'w', encoding='utf-8').write(S1)

fp = r['fp']
S2 = ["# 보충자료 S2. 검색어의 무관한 용법으로 제외한 출원 목록", "",
      f"> 본문 제4.1절·제5.4절 여덟째 항목의 근거. 제목에 다음 용어가 포함된 출원을 직접 접합(direct bonding)의 무관한 용법으로 판정하였다: 세라믹·DBC(direct bonded copper)·알루미나·지르코니아·질화알루미늄 기판, 구리 합금(copper alloy having … direct bonding property), 다이아몬드–몰리브덴, 레이저 전구체(precursor), 정전 척, 전력 모듈·전력소자 기판, 히트 스프레더, 직접 접합 금속 기판(direct bonded metal/copper substrate), 직접 접합 회로 조립체(direct bond circuit assembly), sp3–sp2 하이브리드 결합(화학 결합의 뜻). 총 {fp['n']}건(2014년 이전 {fp['pre2015']}건). 출원청별: " + ', '.join(f"{k} {v}" for k, v in fp['by_office'].items()) + ". 유형별: " + ', '.join(f"{k} {v}" for k, v in fp['by_type'].items()) + ".", "",
      "| 출원연도 | 출원청 | 기술범위 | 전략 유형 | 제목 |", "|---|---|---:|---|---|"]
for x in fp['titles']:
    S2.append(f"| {x['year']} | {x['office']} | {x['breadth']} | {x['type']} | {x['title']} |")
open('supplement_S2_v9_ko.md', 'w', encoding='utf-8').write('\n'.join(S2) + '\n')
print('written S1/S2')
