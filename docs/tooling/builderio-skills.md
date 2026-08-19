# BuilderIO/skills 검토 — 선별 후보 (코딩 규율 스킬)

> 대상: [BuilderIO/skills](https://github.com/BuilderIO/skills) (MIT) — 코딩 에이전트용 조합형 스킬 10종
> (`/visual-plan` `/visual-recap` `/agent-watchdog` `/plan-arbiter` `/plow-ahead` `/efficient-fable`
> `/efficient-frontier` `/stay-within-limits` `/quick-recap` `/read-the-damn-docs`). npx/플러그인.

## 우리 적합성
- 대부분 **코딩/PR 오케스트레이션** 용 + 이미 도입한 sovereign-skills(세션) 및 academic-research-skills 규율과 중복.
- **범용으로 쓸 만한 cherry-pick 후보(미도입, 메모):**
  - `read-the-damn-docs` — 구현 전 공식 문서 웹검색 강제. 우리 **인용·사실 검증 철학과 일치**.
  - `stay-within-limits` — 사용량/예산 한도 모니터링. **장기 다세션** 작업에 유용.
  - `quick-recap` — green/yellow/red 상태 신호(정직 보고 보강).

## 결론
- **통째 도입 X.** 필요 시 위 2~3개만 개별 vendor 검토(MIT). 현재는 **후보 기록**만.
