# 과학에이전트·roboco 6종 검토 (2026-06)

> 대상: scientific-agent-skills(K-Dense-AI) · roboco-io(plugins · roboco-cli · serverless-autoresearch · awesome-vibecoding) · serithemage/awesome-student-developer-resources.
> 우리(한국어 특허분석 석사논문 + RAG·KG + Claude Code) 기준 적용성.

| 레포 | 정체 | 라이선스 | 적합도 | 판정 |
|---|---|---|---|---|
| **scientific-agent-skills** | **148개 과학 에이전트 스킬**(문헌탐색·인용·특허DB·NetworkX·피어리뷰…) | MIT | ★★★ | **선별 채택 후보** (paper-lookup·citation·특허DB) |
| awesome-student-developer-resources | 학생 개발자용 **무료/할인 리소스**(Student Pack·클라우드 크레딧·AI툴), 한국어 | CC0 | ★★ (실용 포인터) | 도구 아님 — **GPU/API 크레딧으로 우리 보류 도구 해금** 가능 |
| roboco-io/plugins | Claude Code 플러그인 모음(코드리뷰·보안·**llm-wiki**·**한국어 기술문서**·**ralph-mem 메모리**) | MIT | ★☆ | 대부분 개발/보안. 일부(한국어 글쓰기·llm-wiki)만 관련, 기존 채택과 중복 |
| awesome-vibecoding | 바이브코딩 도구/MCP/RAG **큐레이션**(다국어) | CC0 | ☆ | 도구 아님 — **새 도구 발굴 참고 인덱스**만 |
| roboco-cli | 바이브코딩 **프로젝트 스캐폴딩**(init/audit/sync, Node24) | MIT | ☆ | 신규 코딩 프로젝트 셋업용. 우리는 이미 구성됨 → 부적용 |
| serverless-autoresearch | **AWS SageMaker GPU 병렬 ML 실험** 진화(Karpathy류) | MIT | ☆ | 도메인 불일치(ML 모델탐색, 문헌연구 아님) → 부적용 |

## 상세

### ① scientific-agent-skills — **유일한 실질 채택 후보**
"어떤 AI 에이전트든 AI Scientist로" — 148 스킬. 대부분 바이오/유전체/케모인포라 우리(기술경영 특허분석)와 무관하지만,
**우리에게 진짜 더해지는 것**이 분명히 있다:
- **Paper Lookup** (arXiv·Semantic Scholar·PubMed) — **실재 문헌 자동 탐색**. 우리 §2 문헌보강·인용 검증을 강화(우리가 줄곧 "실재 출처만" 강조한 것과 정합). arXiv/Semantic Scholar는 **무료 API라 이 환경에서도 동작 가능성** 있음(LLM 키 불요).
- **Citation Management** (pyzotero) — 참고문헌 정리.
- **과학 데이터베이스(특허 포함)** — 우리 PATENT 논문에 직접 관련.
- **NetworkX 시각화** — 우리 기술 네트워크 그림.
- 단, 피어리뷰·과학글쓰기·문서처리는 이미 vendored **academic-research-skills**와 중복.
→ **선별(cherry-pick) 채택 권장**: `paper-lookup`(+ citation) 1~2개만 opt-in. 무결성 게이트로 결과 검증 전제.

### ② awesome-student-developer-resources — 실용 포인터 (도구 아님)
학생(석사)용 무료/할인: **GitHub Student Pack·AWS/Azure/GCP 크레딧·Zotero·AI툴 액세스** 등.
→ 우리가 줄곧 보류한 도구들(RAG-Anything/Unlimited-OCR=**GPU**, kg-gen/Hyper-Extract=**API 키**)을 **학생 크레딧으로 해금** 가능.
즉 "키/GPU 없어서 보류"를 사용자가 학생 혜택으로 풀 수 있다는 **현실적 길**. (도구 채택 아님 — 안내.)

### ③ roboco-io/plugins (MIT)
관련 슬라이스: **llm-wiki**(KB/RAG·Obsidian·하이브리드검색 — OpenKB와 중복), **한국어 기술문서 작성**(우리 한글 글쓰기에 잠재 유용),
**ralph-mem**(세션 메모리 — 우리 session-start/checkpoint와 중복). 나머지는 개발/보안/AWS. → **대부분 중복**, 한국어 글쓰기 플러그인만 참고.

### ④⑤⑥ 부적용/참고
- awesome-vibecoding: 도구 발굴 **참고 인덱스**(CC0).
- roboco-cli: 신규 프로젝트 스캐폴딩 → 우리는 셋업 완료, 부적용.
- serverless-autoresearch: AWS ML 실험 진화 → 도메인 불일치, 부적용.

## 결론 (적용할 부분)
1. **scientific-agent-skills의 `paper-lookup`(+citation)** 선별 채택 → 문헌 탐색·검증 강화(arXiv/Semantic Scholar는 키 없이 동작 가능성). **opt-in 권장.**
2. **학생 개발자 혜택**으로 GPU/API 크레딧 확보 → 보류했던 RAG/KG 도구(Hyper-Extract·kg-gen·Unlimited-OCR) **실사용 해금**.
3. 나머지(roboco 일체·serverless-autoresearch)는 중복/부적용 → **미채택**.
