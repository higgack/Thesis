# 2026 한양MOT 리서치데이 포스터 (A1 세로)

- 행사: 2026-10-31(토) 12:00~, 융합교육관 602·608호 및 6층 복도. 포스터 제출 마감 10-27(화) 17:00 (곽상숙, kwagss72@hanyang.ac.kr) — 기한 내 제출 시 행정팀 일괄 출력.
- 모든 판은 학과 A1 템플릿(`build/template_A1.pptx`) 위에 만들었고, 글꼴은 맑은 고딕, 차트는 PowerPoint 기본 차트(직접 수정 가능)입니다. 내용과 수치는 학위논문 최종본 v59와 같습니다.
- PDF는 미리보기용입니다(이 환경에 맑은 고딕이 없어 Noto Sans CJK로 렌더링). 줄 간격은 글자 크기의 1.2배 고정값이라 PowerPoint와 미리보기의 줄 높이가 같습니다.

| 판 | 파일 | 성격 |
|---|---|---|
| **v2 (기본형)** | `poster_v2.pptx/.pdf` | v1과 같은 디자인, 연락처 메일만 higgack@naver.com으로 변경 |
| v2-B 문제·해결·평가형 | `poster_v2_B_pse.pptx/.pdf` | 예시 포스터(KYC·Robot 등)처럼 Problem / Solution / Evaluation 3단, 남색 + 금색, KPI 숫자 칸, 분석 모형 표 |
| v2-C 학술 클래식형 | `poster_v2_C_classic.pptx/.pdf` | 예시(공유 ESS)처럼 하늘색 그라데이션 머리, 교표, 알약 모양 절 제목, ■ 글머리, <그림>·<표> 번호 |
| v2-D 다크 임팩트형 | `poster_v2_D_dark.pptx/.pdf` | 짙은 남색 바탕, 큰 영문 절 제목(PROBLEM / SOLUTION / EVALUATION / TAKEAWAY), 큰 숫자 3개 |
| v1 | `poster_v1.pptx/.pdf` | 첫 초안(연락처 Gmail) |

- 한눈에 비교: `poster_v2_overview.png`.
- 다시 만들기(`poster/build`에서):
  - 기본형: `python3 build_poster.py template_A1.pptx assets icons ../poster_vN.pptx`
  - 변형: `python3 build_B.py|build_C.py|build_D.py template_A1.pptx assets icons ../poster_vN_X.pptx` (공통 함수·논문 문구는 `common.py`)
  - 미리보기 PDF: `./render.sh ../poster_vN.pptx ..` (LibreOffice Impress 필요, 한글–영문 자동 띄어쓰기를 끄고 변환)
  - python-pptx 필요. 벤 다이어그램은 `make_venn.py`·`make_venn_dark.py`, 네트워크 그림은 `journal/figures/fig_core_network_v58_ko.png`에서 여백·범례를 잘라 냄, 그림 1(C형)은 논문 그림 2(`fig_framework_psp.png`).
- 규칙: 포스터 문구를 고칠 때마다 새 버전 번호(v3 …).
