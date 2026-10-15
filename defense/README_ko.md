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

## 구성 (참고 발표의 흐름)
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
- 규칙: 발표 자료나 문서를 고칠 때마다 새 번호(다음은 v2). 논문·저널 번호와는 별도.
