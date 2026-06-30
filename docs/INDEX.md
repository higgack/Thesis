# 레포 지식맵 (INDEX)

> lat.md의 "링크형 지식맵" 아이디어를 **무의존성 마크다운**으로 적용한 길찾기 지도.
> 사람·에이전트가 이 한 파일에서 레포 전체 구조를 파악·이동한다. 경로는 레포 루트 기준.

## 📄 논문 (manuscript/)
| 버전 | 파일 | 성격 |
|---|---|---|
| 압축판 | [draft.md](../manuscript/draft.md) | 핵심만 (~20p) |
| 확장판 | [draft_expanded.md](../manuscript/draft_expanded.md) | 표준 확장 |
| 심화 rev1 | [draft_expanded_rev1.md](../manuscript/draft_expanded_rev1.md) | 문헌·사례·논의 |
| 심화 rev2 | [draft_expanded_rev2.md](../manuscript/draft_expanded_rev2.md) | 방법·해석 |
| 심화 rev3 | [draft_expanded_rev3.md](../manuscript/draft_expanded_rev3.md) | 서론·비교·결론·표·전면부 |
| 심화 rev4 | [draft_expanded_rev4.md](../manuscript/draft_expanded_rev4.md) · [.docx](../manuscript/draft_expanded_rev4.docx) | §4.5 기술 네트워크 그림 삽입 |
| **최신 rev5** | [draft_expanded_rev5.md](../manuscript/draft_expanded_rev5.md) · [.docx](../manuscript/draft_expanded_rev5.docx) | **하이퍼그래프 관점 보강(§4.5·7.3)** |

- 파이프라인 산출물: [stage1 연구브리프](../manuscript/stage1_research_brief.md) · [outline](../manuscript/outline.md) · [stage2.5 무결성](../manuscript/stage2_5_integrity_report.md) · [stage3 리뷰](../manuscript/stage3_review.md) · [stage4 응답](../manuscript/stage4_response.md) · [stage3' 재검토](../manuscript/stage3prime_rereview.md) · [stage4.5 최종무결성](../manuscript/stage4_5_final_integrity.md) · [stage6 과정기록](../manuscript/stage6_process_record.md)
- 마감 안내: [FINALIZE_NOTES.md](../manuscript/FINALIZE_NOTES.md)
- 그림: [기술 네트워크 설명](../manuscript/figures/tech_network.md) · 렌더 `tech_network_rich.png`(영문)/`tech_network_rich_ko.png`(한글)
- 원자료: [manuscript/sources/](../manuscript/sources/) (① 영문 계량분석, ② 국문 특허분석 PDF+txt)

## 🔎 RAG 파이프라인 (rag/)
- [rag/README.md](../rag/README.md) — 설치(경량/GPU)·사용
- [triage.py](../rag/triage.py) — PDF 페이지 분류(SKIP/TEXT_ONLY/OCR_NEEDED/LLM_NEEDED), 비용 선별
- [ingest.py](../rag/ingest.py) (`--triage`) · [query.py](../rag/query.py) · [_common.py](../rag/_common.py)

## 🧩 스킬 suite (academic-research-skills, vendored)
- 스킬: `.claude/skills/` → deep-research · academic-paper · academic-paper-reviewer · academic-pipeline
- 세션관리(sovereign-skills, MIT): **session-start** · **session-checkpoint** (핸드오프 → `memory/`·`tasks/`)
- 커맨드/에이전트: `commands/` · `agents/` · 공유: `shared/` · 스크립트: `scripts/`
- 상위 문서: [ACADEMIC_RESEARCH_SKILLS.md](../ACADEMIC_RESEARCH_SKILLS.md) · [QUICKSTART.md](../QUICKSTART.md)

## 🛠 도구 검토 (docs/tooling/)
- [전체 평가서 (RAG/KB/KG 7종)](tooling/rag-kb-tools-eval-2026-06.md)
- 채택(opt-in): [Hyper-Extract](tooling/hyper-extract.md) (하이퍼그래프=CPC동시분류) · [kg-gen](tooling/kg-gen.md) · [OpenKB](tooling/openkb.md) · [rtk](tooling/rtk.md) · [notebooklm-mcp](tooling/notebooklm-mcp.md) · [ppt-master](tooling/ppt-master.md)
- 조건부: [Unlimited-OCR](tooling/unlimited-ocr.md) (GPU+스캔) · [Kami](tooling/kami.md) (부차 산출물) · [im-not-ai](tooling/im-not-ai.md) (한글 문체, 윤리주의)
- 후보/참고: [BuilderIO/skills](tooling/builderio-skills.md) · [기타(미채택)](tooling/_misc-reviewed.md) · [에이전트/인프라 6종](tooling/agent-infra-reviewed-2026-06.md)
- 세션관리 선별도입: [sovereign-skills](tooling/sovereign-skills.md) (session-start/checkpoint)
- 미채택(검토기록): [lat.md](tooling/lat-md.md) · [awesome-design-md](tooling/awesome-design-md.md) (UI용, 부적용)

## 🧠 세션 연속성 (memory/ · tasks/)
- [memory/session-handoff-LATEST.md](../memory/session-handoff-LATEST.md) — 다음 세션 우선순위·미결·블로커
- [tasks/lessons.md](../tasks/lessons.md) — 교정 규칙 + 졸업 게이트
- 사용: 세션 끝 "체크포인트" 저장 → 커밋 / 새 세션 "세션 시작"으로 복원

## 🗺 빠른 길잡이
- **논문 읽기/제출** → rev5 (`.docx`, 최신)
- **참고문헌 검색·색인** → `rag/` (+경량은 OpenKB)
- **기술 네트워크 그림** → `manuscript/figures/`
- **새 도구 검토 결과** → `docs/tooling/`
