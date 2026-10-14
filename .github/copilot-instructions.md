# 작업 지침 (GitHub Copilot용)

이 저장소는 이형규(한양대 기술경영전문대학원)의 석사논문과 저널 투고 원고 작업 공간이다.
Claude Code와 번갈아 작업하므로, 아래 규칙과 기록을 그대로 따른다.

## 작업 전에 읽을 것
1. `memory/session-handoff-LATEST.md` — 최신판과 규칙
2. `journal/README_ko.md` 맨 위 항목들 — 버전별 변경 내역과 빌드 명령
3. `docs/INDEX.md` — 파일 지도

## 브랜치
- 작업 기준 브랜치는 `claude/quirky-darwin-ph188p`. PR은 이 브랜치를 대상으로 연다(main 아님).
- 작업이 끝나면 위 세 기록 파일에도 무엇을 했는지 적는다.

## 원고 규칙
- 학위논문과 투고본(JIK)은 따로 관리한다. 요청에 적힌 쪽만 고친다.
  - 학위논문 최신: `journal/manuscript_v71_ko` (원본 `journal/sources/ko_v71/`)
  - 투고본 최신: `journal/manuscript_v69_ko_JIK`, `journal/manuscript_v69_en_JIK` (+ title_page, highlights)
- 본문을 고칠 때마다 새 버전 번호를 쓴다(다음은 v72). 이전 원본 폴더를 `sources/ko_vN/`으로 복사해서 고친다. 언어·저널 변형은 같은 번호(`_en`, `_JIK`).
- 수정·추가한 부분은 `[텍스트]{.mark}`로 노란색 표시. 새 참고문헌은 `refs_extra.md`에 넣고 `refs_mark.txt`에 "성 연도"로 표시.
- 문헌은 서지를 확인한 것만 인용하고, 확인하지 못한 내용은 쓰지 않는다.

## 빌드 (journal 폴더에서)
- 학위논문: `python3 tools/make_easy_appendix.py N && python3 tools/make_easy_ko.py N` → 결과가 `FAILS 0`이어야 한다.
- Word: `python3 -c "import pypandoc; pypandoc.convert_file('manuscript_vN_ko.md','docx',outputfile='manuscript_vN_ko.docx',extra_args=['--resource-path=.:figures','--dpi=300'])"`
  다음 `python3 tools/docx_polish.py manuscript_vN_ko.docx manuscript_vN_ko.docx --caption-pt 10 --front-black`
- 영문: `python3 tools/make_en.py N KO`, JIK: `python3 tools/make_jik.py N` / `python3 tools/make_jik.py N ko`
- 새 숫자가 수치 검사에 걸리면 `sources/ko_vN/numbers_vN.txt`에 근거와 함께 추가한다(통계 수치를 지어내지 말 것).

## 금지
- `journal/data/`(PATSTAT 원자료)는 절대 커밋하지 않는다. 이 저장소에는 없으므로 회귀분석 재실행은 하지 않는다.
- PATSTAT 판본·추출일을 새로 추가하지 않는다.
- 삭제된 강건성 분석(2023–2024년 제외, 시기 경계 이동)을 다시 넣지 않는다.
- 용어: "고착화", "지지", "연결/연결 기호"를 쓰고 "다리"는 쓰지 않는다. "예컨대"를 쓰지 않는다.
- 이론적 배경(2장)에 새로 추가하는 문장에는 RQ 표기를 넣지 않는다(원래 있던 RQ 표기는 그대로 둔다).
- 커밋 메시지·PR에 AI 모델명을 넣지 않는다.
