# 에이전트/인프라 6종 검토 (2026-06)

> 대상: MiroFish-Ko · codegraph(+PR#274) · activegraph · claude-code-infrastructure-showcase ·
> hermes-agent #7816. 우리(한국어 논문 + 특허 RAG·KG + Claude Code 워크스페이스) 기준 적용성.

| 레포 | 정체 | 라이선스 | 우리 적합도 | 판정 |
|---|---|---|---|---|
| **hermes-agent #7816** | 에이전트 **스킬 수명/망각** 관리 제안(active/stale/archived, TTL) | — (이슈) | ★★ **아이디어 적용** | 우리 `tasks/lessons.md`에 **환경 의존 부정주장 TTL** 규약 도입 |
| claude-code-infra-showcase | Claude Code 인프라 패턴(스킬 **자동활성화** skill-rules.json+훅, dev-docs 3파일 컨텍스트 보존) — **한국어** | MIT | ★★ (중복) | 우리 session-start/checkpoint·memory와 **중복**. 자동활성화는 참고 |
| codegraph (+PR#274) | 코드베이스 **자동 인덱싱→SQLite 코드 KG**, MCP, 100% 로컬, 58% 적은 tool call | MIT | ★☆ | 코드 커지면 lat.md보다 우수(자동·MCP). 현재 우리 코드 작아 보류 |
| activegraph | **이벤트소싱 감사가능 에이전트 그래프 런타임**("graph=world, trace=proof") | Apache-2.0 | ★☆ | 감사성↔우리 무결성 철학 일치하나 **현 단계 과함**. 미래 인프라 후보 |
| MiroFish-Ko | **멀티에이전트 군집 시뮬레이션/예측** 엔진(GraphRAG, OASIS), 한국어 | **AGPL-3.0** | ☆ | 도메인 불일치(예측·시뮬 vs 회고적 특허분석) + 카피레프트. **부적용** |

## 상세 판정

### ① hermes-agent #7816 — **아이디어 적용 (유일한 실제 적용점)**
스킬 시스템이 "쌓기만 하고 안 잊는다"는 문제 → active/stale/archived 상태 + 마지막사용·사용횟수 메타,
그리고 **환경 의존 부정주장("도구 X 안 됨")엔 TTL** 부여해 환경 바뀌면 재검증. 
→ 우리 `tasks/lessons.md`는 이미 conf/seen/obs 메타가 있는데, 여기에 **`env-TTL`(환경 의존 부정주장 재검증)** 규약을 추가했다.
**시의성**: 방금 **VM이 서울→us-central1로 이전**(환경 변경!) → "외부 clone 프록시 403", "새 repo 불가" 같은
환경 의존 레슨은 **다음 세션에서 재검증 대상**으로 표시(망각 대신 재확인).

### ② claude-code-infra-showcase (한국어, MIT)
스킬 **자동활성화**(skill-rules.json + UserPromptSubmit 훅)와 **dev-docs 3파일**(plan/context/tasks)로
컨텍스트 리셋 대응. → **우리가 이미 session-start/checkpoint + `memory/`·`tasks/`로 같은 문제를 해결** 중이라
중복. 자동활성화 훅은 자동실행이라 신중(과거 settings.json 훅 자동도입은 분류기 차단). **참고 보관, 미도입.**

### ③ codegraph (MIT) / PR#274
코드 KG를 자동 인덱싱(SQLite)해 에이전트가 적은 tool call로 구조 파악. PR#274는 Hermes 설치 타깃 추가(MCP).
→ lat.md(수동)보다 **자동·MCP라 우수**하나, 우리 레포 코드(rag/·scripts/)가 작아 **현재 효용<비용**. 코드 커지면 재검토.

### ④ activegraph (Apache-2.0)
모든 변이가 추적·재개·포크·diff 가능한 그래프 런타임. **감사성**이 우리 무결성과 결이 같고, **Hyper-Extract와
결합하면 "감사가능한 특허-KG 파이프라인"**의 백본이 될 수 있음. 단 학위논문엔 **과한 인프라** → 미래 후보.

### ⑤ MiroFish-Ko (AGPL-3.0)
GraphRAG 기반 멀티에이전트 시뮬레이션으로 **미래 예측**(정책·여론·금융). 우리는 회고적 특허 분석이라 **도메인 불일치**.
AGPL 카피레프트도 도입 부담. → **부적용**(향후 '생태계 진화 시나리오 시뮬'을 한다면 그때 참고).

## 결론
- **새로 도입한 도구: 없음**(전부 에이전트/코드 인프라, 논문과 무관 또는 기존 채택과 중복).
- **실제 적용: hermes-agent #7816의 '스킬/레슨 망각' 아이디어** → `tasks/lessons.md`에 env-TTL 규약 + VM 이전 반영.
- 보류/미래: codegraph(코드 커지면)·activegraph(감사 KG 파이프라인)·자동활성화(훅 신중).
