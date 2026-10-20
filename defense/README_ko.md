# 석사학위논문 심사(디펜스) 발표 자료

학위논문 최종본 **v84**(`journal/manuscript_v84_ko.docx`)를 바탕으로 만든 심사 발표 자료입니다. 저널 투고본(JIK)과는 별개입니다.
유저가 보낸 참고 발표(KAIST I&TM 디펜스 대본·캡처, 10-15)의 **구성**을 따랐고, 디자인과 색은 자유롭게(유저: "예시일 뿐") 만들었습니다.

| 파일 | 내용 |
|---|---|
| **`defense_20min_v1.pptx`** | 20분용: 본 발표 29장 + 부록(백업) 7장. 대본은 슬라이드 노트에 있음. 예상 18–19분 |
| **`defense_15min_v1.pptx`** | 15분용: 본 발표 19장 + 부록 15장(20분용에서 뺀 슬라이드 8장 + 공통 부록 7장). 예상 12–13분 |
| `defense_20min_v1_preview.pdf`, `defense_15min_v1_preview.pdf` | 미리보기용 PDF(이 환경에 맑은 고딕이 없어 Noto Sans CJK로 렌더링) |
| **`defense_QA_v1.docx`** | 디펜스 준비: 30초·1분 요약, 핵심 수치표, 답변 원칙, 예상 질문 71개(답변 요지·상세 답변·근거 위치·주의·꼬리 질문), 용어, 핵심 문헌, 질문별 슬라이드, 당일 체크리스트 |
| `defense_script_v1.docx` | 발표 대본(참고 대본처럼 슬라이드별 시작 시각 표시, 20분용·15분용) |

## v2 (10-20): 유저가 보낸 참고 PDF(15쪽, 이미지 슬라이드)의 흐름을 반영
- **`defense_20min_v2.pptx`**: 본 발표 31장 + 백업 13장, 대본 예상 약 19분 · **`defense_15min_v2.pptx`**: 본 발표 21장 + 백업 21장, 약 13분
- `defense_script_v2.docx`(시작 시각 포함 대본), `defense_QA_v2.docx`(예상 질문 71개, 슬라이드 번호를 v2 기준으로 변환), `*_v2_preview.pdf`
- 참고 PDF에서 가져온 흐름: 결론을 먼저 보여 주는 **핵심 요약**(표지 다음) → **딜레마**(후공정의 목표 vs 전공정의 필수 역량, 단면 개념도) → **두 CPC 클래스의 벤다이어그램**으로 측정 설명 → **국면별 심층**(형성기 · 확산기: 유형 구성 막대 + 특징 2개 + 큰 승산비 숫자) → **재편기의 역설**을 질문으로 제기 → **지배적 설계의 고착화**로 해석(인터페이스 불안정기 vs 고착화 이후) → **결합 중심 이동**을 세 시기 네트워크 그림으로(선 굵기 = Jaccard, 원 크기 = 매개 중심성) → 결과 종합 매트릭스 → **산업적 함의**(분업의 인터페이스) → **실무적 시사점 3가지** → **맺음말 한 문장** → Q&A
- 15분용은 시간에 맞춰 국면별 심층 3장 · 산업적 함의 · 실무 3카드는 백업으로 두고, 역설의 질문을 해석 슬라이드 위쪽 띠에 넣음
- 참고 PDF의 수치·표현 중 논문과 다른 것은 쓰지 않음: "통합형 45.8% = 표준/뉴노멀"(좁은 정의 37.2%를 함께 표기), "완벽히 이동", "특허 데이터로 증명된 내재화", "생존의 절대 조건", 임의 연도 축의 면적 그래프 등. 모든 수치는 논문 v84에 있는 값(audit_numbers.py: 장·절 번호만 남음)
- v1에서 본 발표에서 빠진 슬라이드(Chapter 2 Summary, RQ별 근거 3장, RQ3 승산비 표 · Jaccard 그래프, RQ2 무게중심, 실제 출원 세 건, 범프 비교)는 백업에 그대로 있음
- 다시 만들기: `python3 build_defense.py 20 ../defense_20min_v2.pptx`, `python3 build_defense.py 15 ../defense_15min_v2.pptx`, `python3 make_docs.py 2` (v1 슬라이드 번호 → v2 변환은 make_docs.py 안의 표)

## v3: 30분용 추가 (10-20)
- 심사 전체 1시간에 맞춘 30분용 `defense_30min_v3.pptx`: 본 발표 41장(대본 기준 약 29분) + 백업 8장. 20분용·15분용은 v2 그대로
- 20분용 v2의 흐름에 v1에서 합치거나 백업으로 옮긴 슬라이드를 본 발표로 되돌림: 범프 비교(1장), 기술융합 개념과 단계 모형을 두 장으로, Chapter 2 Summary, RQ별 근거 3장, RQ1 수준·형태 두 장, RQ2 무게중심, RQ3 승산비 표(재편기 역설 다음), RQ3 결합 강도와 매개 중심성(네트워크 다음)
- 슬라이드가 새로 이어지는 곳의 대본은 `S30`(딜레마, 연구 질문 정리, RQ2 무게중심, 재편기, RQ3 승산비, 네트워크, 결합 강도)
- `defense_script_v3.docx`(30·20·15분 대본), `defense_QA_v3.docx`(질문별 슬라이드 번호와 백업 표에 30분용 추가)
- 다시 만들기: `python3 build_defense.py 30 ../defense_30min_v3.pptx`, `python3 make_docs.py 3`

## v4: 30분용 수정 (10-20)
- `defense_30min_v4.pptx`: 본 발표 42장(대본 기준 약 29.5분) + 백업 6장. 20분용·15분용은 v2 그대로
- v3의 6·7번(범프 비교와 딜레마)을 딜레마 한 장으로 통합: 가운데 그림을 솔더 범프 대 하이브리드 본딩 비교로 바꾸고 대본도 하나로 합침
- v3 백업의 측정 예시(실제 출원 세 건)는 3.3 공동분류 다음, 검색 전략과 자료의 특성은 3.2 분석 데이터 다음의 본 발표로 옮김. 벤 그림에서는 겹치는 예시 카드를 뺌
- 검색 전략 슬라이드의 재추정 부록 번호는 덱마다 자동 계산
- `defense_script_v4.docx`, `defense_QA_v4.docx`(v3 번호 → v4 번호 변환은 make_docs.py의 `M34`)
- 다시 만들기: `python3 build_defense.py 30 ../defense_30min_v4.pptx`, `python3 make_docs.py 4`

## 구성 (v1, 참고 발표의 흐름)
- 5개 장: 01 서론 · 02 이론적 배경 · 03 연구 질문 및 연구 방법 · 04 연구 결과 · 05 결론
- 질문으로 여는 도입(생성형 AI · AI 가속기 · HBM의 공통 과제), 비유(접착제 vs. 거울처럼 평평한 면, 도로의 규격), 장 요약(Chapter 2 · 4 Summary), 근거 슬라이드 뒤에 연구 질문을 밝힘, 슬라이드마다 아래쪽 핵심 한 줄, Q&A 마무리
- 머리글 "| 3.1 제목"과 가는 선, 흰 바탕 + 남색, 유형 색(공정 단독형 파랑 · 통합형 보라 · 접합 단독형 주황 — 포스터와 같음), 표지·Q&A는 웨이퍼 이미지(직접 생성)
- 차트는 PowerPoint 기본 차트(직접 수정 가능): 연도별 출원, 유형 도넛, 예측확률(기술범위·연도), 시기별 구성비, Jaccard. 승산비 그림은 도형

## 다시 만들기 (`defense/build`에서)
```
python3 make_images.py                         # 배경 이미지(웨이퍼, 회로) — 한 번만
NODE_PATH=<react-icons 설치 경로>/node_modules node icons.js   # 아이콘 PNG — 한 번만
python3 build_defense.py 20 ../defense_20min_v1.pptx
python3 build_defense.py 15 ../defense_15min_v1.pptx
python3 check_fit.py ../defense_20min_v1.pptx 1.2   # 맑은 고딕 폭 기준 글 넘침 검사(0건이어야 함)
python3 audit_numbers.py ../defense_20min_v1.pptx ../../journal/manuscript_v84_ko.docx   # 모든 수치가 논문 v84에 있는지(장·절 번호만 남아야 함)
python3 make_docs.py 1                         # Q&A·대본 Word (노트 JSON을 읽음)
./render.sh ../defense_20min_v1.pptx <폴더>    # 미리보기 PDF·PNG (LibreOffice)
```
- 슬라이드 구성·대본: `build_defense.py`(20분용 다듬은 대본 `S20`, 15분용 `S15`), 공통 함수 `kit.py`, Q&A 내용 `qa_content.py`.
- 차트 자료: `assets/data_v84.json`(논문 그림 3·5의 값: 연도별 출원 수, 예측확률; `journal/data/app.pkl`·`robustness/results_v16.json`에서 추출).
- 규칙: 발표 자료나 문서를 고칠 때마다 새 번호(다음은 v5). 논문·저널 번호와는 별도.
