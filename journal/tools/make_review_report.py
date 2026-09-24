# -*- coding: utf-8 -*-
"""Write journal/review_v8_ko.md from the workflow's confirmed findings + editor dispositions."""
import json
S = '/tmp/claude-0/-home-user-Thesis/cfe49c05-3ef6-509c-a1ff-171488d6f028/scratchpad'
conf = json.load(open(S + '/confirmed.json', encoding='utf-8'))
unv = json.load(open(S + '/unverified.json', encoding='utf-8'))
CAT = {'cite': '인용 형식·목록', 'numbers': '수치 정합성', 'logic': '논리 정합성', 'format': '형식', 'sources': '출처 정확성', 'methods': '방법론', 'referee': '심사 예상(태클)'}
# dispositions: id -> note (default: 반영)
SKIP = {
 'format-r1-20': '보류(캡션·주 분리 체재는 저널 양식 변환 단계에서 적용)',
 'methods-r1-12': '대체 반영(Hausman–McFadden 검정 대신 축소 표본 이항 로짓 비교를 §5.4·S1.10에 보고)',
 'methods-r1-19': '변경 없음(제안문이 원문과 동일)',
 'cite-r1-3': '보류(PATSTAT 판본 Spring/Autumn·추출일은 저자 확인 필요)',
 'cite-r1-6': '보류(ITRS 백서 URL·접근일은 저자 확인 필요)',
 'format-r1-35': '부분 반영(영문 저자·소속 추가; 이메일은 저자 기입)',
 'referee-r1-2': '부분 반영(표본은 유지하고 §4.1 고지 + §5.4 여덟째 항목 + 보충자료 S2로 대응; 표본 시작 연도 변경은 원 분석과의 비교 가능성을 위해 채택하지 않음)',
 'referee-r1-13': '대체 반영(축소 표본 이항 로짓 비교)',
 'logic-r1-20': '반영',
}
order = {'필수': 0, '권장': 1, '선택': 2}
conf.sort(key=lambda f: (order[f['sev']], f['category'], f['id']))
lines = ['# v8_ko 심사 검토 보고서 (2026-09-24)', '',
         '> 7개 관점(인용·수치·논리·형식·출처·방법·적대적 심사)의 독립 검토 후, 지적마다 사실 검증자와 편집위원이 각각 독립 판정한 결과. 두 판정을 모두 통과한 항목만 수록한다(총 167건 중 151건 확정, 12건 기각, 4건 미검증). 세션 한도로 종합 단계는 편집자가 직접 수행하였다. 각 항목의 처리는 "처리" 열에 적었으며, 반영 결과는 `manuscript_v9_ko.md`다.', '',
         '## 요약', '',
         f"- 확정 지적 {len(conf)}건: 필수 {sum(f['sev']=='필수' for f in conf)}, 권장 {sum(f['sev']=='권장' for f in conf)}, 선택 {sum(f['sev']=='선택' for f in conf)}.",
         '- 가장 큰 변화: (1) H01L21에 조립 단계 메인그룹이 포함됨을 고지하고 엄격 정의 재추정을 추가(§4.2, §5.4 여섯째). (2) 2023–2024년 제외 시 중국 출원청의 공정 단독형 편중이 유의성을 잃음을 보고하고 H3a 판정에 예외를 명시. (3) 일본 출원청의 범위 우위는 로버스트 표준오차에서 비유의이고 2010년 이후 표본의 효과는 동일 패밀리 4건에 의존함을 밝혀 H3b 범위 성분을 판정 유보로 변경. (4) 패밀리 중복(473개 제목)과 군집 표준오차, 검색어 무관 용법 68건 제외 표본을 강건성에 추가. (5) 그림 6이 평균 예측확률임을 바로잡고, BP·LR·과산포 통계량, 델타법 표준오차, 표 7 판정 기준을 명시. (6) 키워드 필터 서술(AND→OR), BEOL 용어, Lee & Lim(2001)·Hu & Jefferson(2009)·Rosenberg(1963)·Sakakibara & Branstetter(2001) 귀속 수정.', '',
         '## 지적 목록', '']
cur = None
for f in conf:
    key = (f['sev'], f['category'])
    if key != cur:
        cur = key; lines += [f"### {f['sev']} · {CAT.get(f['category'], f['category'])}", '', '| id | 위치 | 문제 | 처리 |', '|---|---|---|---|']
    disp = SKIP.get(f['id'], '반영')
    prob = f['problem'].replace('|', '¦').replace('\n', ' ')
    lines.append(f"| {f['id']} | {f['location'].replace('|','¦')[:60]} | {prob[:260]} | {disp} |")
lines += ['', '### 미검증(검증 단계 미완료) 4건', '', '| id | 위치 | 문제 | 처리 |', '|---|---|---|---|']
for f in unv:
    lines.append(f"| {f['id']} | {f['location'].replace('|','¦')[:60]} | {f['problem'].replace('|','¦')[:260]} | {SKIP.get(f['id'], '반영')} |")
lines += ['', '## 기각된 지적(참고)', '', '- cite-r1-5(제목 대소문자), logic-r1-33(결정요인 용어), format-r1-21(표 1 주석 모순), format-r1-37(초록 분량), format-r1-38(단독 저자의 we), format-r1-41(출원청/관할권), format-r1-42(로버스트/강건), format-r1-43(Poisson/음이항 표기), sources-r1-20(Langlois & Steinmueller 귀속), sources-r1-21(Karvonen & Kässi 확인 불가) 등 12건은 사실 검증자 또는 편집위원이 실질적 문제가 아니라고 판정하였다.', '']
open('/home/user/Thesis/journal/review_v8_ko.md', 'w', encoding='utf-8').write('\n'.join(lines))
print('report written', len(conf))
