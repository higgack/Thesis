# Session Handoff — LATEST

> 작성: session-checkpoint (첫 핸드오프) · 브랜치: `claude/quirky-darwin-ph188p` (PR #8, main 미병합)
> 프로젝트: higgack/Thesis = AI 보조 학술 논문 작성 워크스페이스 (원격 컨테이너=휘발성)

## Priority 1 (다음에 가장 먼저)
**학교 학위논문 양식(template)이 나오면** → `academic-paper` format-convert로 **rev4를 그 양식으로 변환** +
**부록의 그림 11개·세부 표 7개(원본 ①② 이미지)를 §해당 위치에 삽입**. (현재 양식 미정 → 사용자 대기 중)

## Priority 2 (선택)
- 원자료(PATSTAT)로 핵심 수치 **재현 검증**(재현성 한계 해소) — 데이터·코드·환경 필요.
- `kg-gen`으로 의미 기반 기술 KG 추출(현 그림은 결정론 KG) — LLM 키 필요.
- `rag/` 실제 인제스트/쿼리 — OPENAI_API_KEY + 디스크 여유 머신 필요(telegram-bot은 보류).

## 미결 의사결정 (유저 대기)
- 학교 양식 미정 → 확정 시 형식 마감.
- 논문 본문 추가 확장은 "더 안 함"으로 종료(텍스트). 분량은 그림/표 삽입으로 채움.

## 잔존 이슈 / 블로커
- PDF: 이 컨테이너에 LaTeX 없음 → 공식 PDF는 로컬(pandoc+xelatex) 또는 학교양식 후. DOCX는 pypandoc로 생성 가능(확인됨).
- RAG-Anything 설치 무거움(torch/CUDA·디스크) → telegram-bot(6GB)에선 실패. 경량 대안 OpenKB 문서화됨.
- 새 GitHub repo 생성/푸시 불가(환경 권한: higgack/Thesis만). 산출물은 tarball/PR로 전달.

## 컨텍스트 메모 (반복 방지)
- 최신 논문본: `manuscript/draft_expanded_rev4.md` (+ `.docx`) — §4.5에 **한글 기술네트워크 그림**(`figures/tech_network_rich_ko.png`) 임베드. 버전: draft→expanded→rev1~rev4 모두 보존.
- 그림 라벨 규칙(유저 지정): 한국기업=한글(삼성전자), 외국 장비사/기업=영어, 공정/구조 일부 영어(Chiplet/Dicing/Surface Prep), 관할권=한글.
- 도구 검토 누적: `docs/tooling/` (rtk·notebooklm-mcp·ppt-master·kg-gen·OpenKB·unlimited-ocr·lat.md·sovereign-skills·kami·awesome-design-md) + 평가서. 레포 지식맵: `docs/INDEX.md`.
- 세션관리 스킬(session-start/checkpoint) 도입됨 → 다음 세션은 "세션 시작"으로 이 핸드오프 로드.
