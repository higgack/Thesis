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
| 심화 rev5 | [draft_expanded_rev5.md](../manuscript/draft_expanded_rev5.md) · [.docx](../manuscript/draft_expanded_rev5.docx) | 하이퍼그래프 관점(§4.5·7.3) |
| 심화 rev6 | [draft_expanded_rev6.md](../manuscript/draft_expanded_rev6.md) · [.docx](../manuscript/draft_expanded_rev6.docx) | 검증 문헌 반영: Petralia2020·Caviggioli2016·KnowMade(§2·§4·§6) |
| **최신 rev7** | [draft_expanded_rev7.md](../manuscript/draft_expanded_rev7.md) · [.docx](../manuscript/draft_expanded_rev7.docx) | **인용 보강만: 융합 측정 3전통(Cho&Kim2014·Hwang2020·Zhu&Motohashi2022) §2.3·§6.6** |

## 📰 저널 투고용 논문 (journal/)
| 버전 | 파일 | 성격 |
|---|---|---|
| **v1** | [manuscript_v1.md](../journal/manuscript_v1.md) · [.docx](../journal/manuscript_v1.docx) | 영문, Elsevier(Technovation/TFSC) 양식. RQ3·RQ4 중심, 융합 이론 + 반도체 산업구조 이론, 가설 H1–H3b, 참고문헌 61편 전수 검증 |
| **v1_ko** | [manuscript_v1_ko.md](../journal/manuscript_v1_ko.md) · [.docx](../journal/manuscript_v1_ko.docx) | v1의 국문 번역(구조·수치·참고문헌 동일) |
| **Rev1_ko** | [manuscript_rev1_ko.md](../journal/manuscript_rev1_ko.md) · [.docx](../journal/manuscript_rev1_ko.docx) | 국문 KCI 체재. 7단계 논리(무어의 법칙 종언→접합 후공정→산업 내 융합→특허 관점 공백→GPT 후보→RQ1 수렴·RQ2 결정요인·RQ3 국가·기업 전략). 김민구 외(2022) 참조 |
| v2_ko | [manuscript_v2_ko.md](../journal/manuscript_v2_ko.md) · [.docx](../journal/manuscript_v2_ko.docx) | v1_ko에서 §3.2 생태계 절·표 1 삭제, 부록 본문 편입, 그림·표 재번호 |
| **v7_ko(최신)** | [manuscript_v7_ko.md](../journal/manuscript_v7_ko.md) · [.docx](../journal/manuscript_v7_ko.docx) · [보충자료 S1](../journal/supplement_S1_v7_ko.md) | 최종 정합성·가독성 검토본(출원인 정보 없이 마무리) |
| v6_ko | [manuscript_v6_ko.md](../journal/manuscript_v6_ko.md) · [.docx](../journal/manuscript_v6_ko.docx) | HB.csv 원자료 재추정 반영: 변수 정의 정정, 강건성 §5.4(표 7·8, 그림 5), H2·H3b 판정 하향 | 
| v5_ko | [manuscript_v5_ko.md](../journal/manuscript_v5_ko.md) · [.docx](../journal/manuscript_v5_ko.docx) | 심사 의견 1–4 반영: 경계 넘기=통합형으로 정의 정합, 융합 vs 수직통합 구분, 관할권 해석 완화, CPC 재분류 대안 | 
| v4_ko | [manuscript_v4_ko.md](../journal/manuscript_v4_ko.md) · [.docx](../journal/manuscript_v4_ko.docx) | 유저 수정 반영(한양대·Yole Group) + 문장 다듬기 89곳 + 참고문헌 57편 재검증 |
| v3_ko | [manuscript_v3_ko.md](../journal/manuscript_v3_ko.md) · [.docx](../journal/manuscript_v3_ko.docx) | v2_ko에서 Elsevier 요소(하이라이트·선언문·각주) 제거, 그림 캡션 1회, 학술형 제목 |
| 강건성 | [robustness/results_2026-09-24.md](../journal/robustness/results_2026-09-24.md) | HB.csv 기준선 재현 + 재추정 결과 (원자료는 `journal/data/`, gitignore) |
| 심사·자료 | [review_v4_ko.md](../journal/review_v4_ko.md) · [DATA_REQUEST_ko.md](../journal/DATA_REQUEST_ko.md) | 내부 심사 의견 · 재분석용 원자료 명세 |
| 안내 | [README_ko.md](../journal/README_ko.md) | 저널 적합성 표, 가정(영어·사후가설), 남은 작업(S1·저자정보·그림 300dpi) |
| 그림 | `journal/figures/` | 자료①②에서 추출한 8개 그림 |

- 파이프라인 산출물: [stage1 연구브리프](../manuscript/stage1_research_brief.md) · [outline](../manuscript/outline.md) · [stage2.5 무결성](../manuscript/stage2_5_integrity_report.md) · [stage3 리뷰](../manuscript/stage3_review.md) · [stage4 응답](../manuscript/stage4_response.md) · [stage3' 재검토](../manuscript/stage3prime_rereview.md) · [stage4.5 최종무결성](../manuscript/stage4_5_final_integrity.md) · [stage6 과정기록](../manuscript/stage6_process_record.md)
- 마감 안내: [FINALIZE_NOTES.md](../manuscript/FINALIZE_NOTES.md)
- §2 문헌 보강 후보: [lit_candidates.md](../manuscript/lit_candidates.md) — **A·B·C 전량 rev6/rev7에 반영 완료**. 추가 보강은 새 후보 탐색부터 필요.
- 그림: [기술 네트워크 설명](../manuscript/figures/tech_network.md) · 렌더 `tech_network_rich.png`(영문)/`tech_network_rich_ko.png`(한글)
- 원자료: [manuscript/sources/](../manuscript/sources/) (① 영문 계량분석, ② 국문 특허분석 PDF+txt)

## 🔎 RAG 파이프라인 (rag/)
- [rag/README.md](../rag/README.md) — 설치(경량/GPU)·사용
- [triage.py](../rag/triage.py) — PDF 페이지 분류(SKIP/TEXT_ONLY/OCR_NEEDED/LLM_NEEDED), 비용 선별
- [ingest.py](../rag/ingest.py) (`--triage`) · [query.py](../rag/query.py) · [_common.py](../rag/_common.py)
- [paper_lookup.py](../rag/paper_lookup.py) — arXiv·Semantic Scholar 실재 문헌 검색(무키, paper-lookup 경량판)

## 🧩 스킬 suite (academic-research-skills, vendored)
- 스킬: `.claude/skills/` → deep-research · academic-paper · academic-paper-reviewer · academic-pipeline
- 세션관리(sovereign-skills, MIT): **session-start** · **session-checkpoint** (핸드오프 → `memory/`·`tasks/`)
- 커맨드/에이전트: `commands/` · `agents/` · 공유: `shared/` · 스크립트: `scripts/`
- 상위 문서: [ACADEMIC_RESEARCH_SKILLS.md](../ACADEMIC_RESEARCH_SKILLS.md) · [QUICKSTART.md](../QUICKSTART.md)

## 🛠 도구 검토 (docs/tooling/)
- [전체 평가서 (RAG/KB/KG 7종)](tooling/rag-kb-tools-eval-2026-06.md)
- 채택(opt-in): [Hyper-Extract](tooling/hyper-extract.md) (하이퍼그래프=CPC동시분류) · [kg-gen](tooling/kg-gen.md) · [OpenKB](tooling/openkb.md) · [rtk](tooling/rtk.md) · [notebooklm-mcp](tooling/notebooklm-mcp.md) · [ppt-master](tooling/ppt-master.md)
- 조건부: [Unlimited-OCR](tooling/unlimited-ocr.md) (GPU+스캔) · [Kami](tooling/kami.md) (부차 산출물) · [im-not-ai](tooling/im-not-ai.md) (한글 문체, 윤리주의)
- 후보/참고: [BuilderIO/skills](tooling/builderio-skills.md) · [기타(미채택)](tooling/_misc-reviewed.md) · [에이전트/인프라 6종](tooling/agent-infra-reviewed-2026-06.md) · [과학·roboco 6종](tooling/scientific-roboco-reviewed-2026-06.md)
- 세션관리 선별도입: [sovereign-skills](tooling/sovereign-skills.md) (session-start/checkpoint)
- 미채택(검토기록): [lat.md](tooling/lat-md.md) · [awesome-design-md](tooling/awesome-design-md.md) (UI용, 부적용)

## 🧠 세션 연속성 (memory/ · tasks/)
- [memory/session-handoff-LATEST.md](../memory/session-handoff-LATEST.md) — 다음 세션 우선순위·미결·블로커
- [tasks/lessons.md](../tasks/lessons.md) — 교정 규칙 + 졸업 게이트
- 사용: 세션 끝 "체크포인트" 저장 → 커밋 / 새 세션 "세션 시작"으로 복원

## 🗺 빠른 길잡이
- **논문 읽기/제출** → rev7 (`.docx`, 최신)
- **참고문헌 검색·색인** → `rag/` (+경량은 OpenKB)
- **기술 네트워크 그림** → `manuscript/figures/`
- **새 도구 검토 결과** → `docs/tooling/`
