# sovereign-skills 검토 — 선별 도입 기록

> 대상: [AlexZio00/sovereign-skills](https://github.com/AlexZio00/sovereign-skills) (MIT) —
> AI 코딩 에이전트(Claude Code/Codex/Cursor)용 **12개 마크다운 스킬**(프로젝트 셋업~코드리뷰~세션관리).
> 결론: **전체 도입 X**(코딩 편향·기존 스킬과 중복). **세션관리 2개만 선별 vendor.**

## 12개 스킬 적합도 요약
| 스킬 | 우리 적합도 | 비고 |
|---|---|---|
| **session-start / session-checkpoint** | ★★★ 도입 | 세션 간 컨텍스트 연속성 — 다세션·휘발성 원격 환경인 우리에 최적 |
| goal-lock / scope / freeze | ★★☆ 보류 | 스코프·목표 잠금. 유용하나 우선 2개만 |
| pre-push | ★☆☆ | rag/ 파이썬엔 약간 유용(빌드/린트 거의 없음) |
| code-autopsy / project-check / project-init / setup | ☆ | 소프트웨어 프로젝트용, 논문 레포 부적합 |
| collab-audit | ☆ | academic-pipeline의 collaboration_depth_agent와 중복 |

## 도입한 것 (선별 vendor)
- `.claude/skills/session-start/SKILL.md` — 세션 시작 시 핸드오프·레슨 로드, 준비 신호 출력(읽기전용).
- `.claude/skills/session-checkpoint/SKILL.md` — 세션 종료/컴팩트 전 컨텍스트 추출 → 핸드오프·레슨 저장.
- 라이선스: `.claude/skills/SOVEREIGN-SKILLS-LICENSE` (MIT © 2026 AlexZio00).
- **짝(pair) 설계**: 둘 다 설치하거나 둘 다 미설치. `/session-start` → 작업 → `/session-checkpoint`.

## 동작 구조 (의존 경로)
- `memory/session-handoff-LATEST.md` — checkpoint가 생성/갱신, start가 로드(다음 할 일·미결·블로커).
- `tasks/lessons.md` — 교정 규칙(conf/seen/obs 메타·졸업 게이트). checkpoint 갱신, start 리뷰.
- 글로벌(있으면 읽음, 없으면 조용히 스킵): `~/.claude/settings.json`·`STATE.md`·`MEMORY.md` 등.
- 첫 실행 시 파일이 없으면 start는 "새로 시작"으로 안전 fallback, checkpoint가 구조를 부트스트랩.

## 우리 환경에서의 의미
원격 컨테이너가 **휘발성**이라, `memory/`·`tasks/` 를 **커밋 대상으로 유지**하면 핸드오프가 레포에 남아
**세션·컨테이너를 넘어 연속성**이 생긴다(그래서 .gitignore하지 않음). 사용법:
- 세션 끝/컴팩트 전: **"체크포인트"** 또는 `/session-checkpoint` → 핸드오프 저장 후 커밋·푸시.
- 다음 세션 시작: **"세션 시작"** 또는 `/session-start` → 핸드오프 로드, 우선순위 복원.

## 미도입 사유
- 코드 스캐폴딩/리뷰 계열은 *소프트웨어 개발* 전제 → 연구·집필 레포엔 과함.
- 정직보고·검증·anti-masquerading 철학은 이미 vendored **academic-research-skills**(IRON RULE·무결성 게이트)와 겹침.
- 필요해지면 goal-lock·scope를 추가 vendor 검토.
