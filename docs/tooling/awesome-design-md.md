# awesome-design-md 검토 — 현재 부적용 기록

> 대상: [VoltAgent/awesome-design-md](https://github.com/VoltAgent/awesome-design-md) (MIT) —
> AI 코딩 에이전트가 **일관된 UI**를 생성하도록 읽는 **DESIGN.md**(마크다운 디자인 시스템) 큐레이션(73+).
> 결론: **현재 우리 프로젝트엔 부적용.**

## 무엇인가
- `DESIGN.md`(프로젝트 루트에 두는 평문 디자인 문서)로 Figma export·JSON 대신 **LLM이 잘 읽는 마크다운**으로 UI 룩앤필을 전달.
- 9개 표준 섹션(테마·컬러·타이포·컴포넌트·레이아웃·깊이·가드레일·반응형·에이전트 프롬프트). 73+ 디자인 시스템(Claude·Cursor·Vercel·Figma 등) 수집.

## 우리 적용성
- 우리는 **UI/웹앱이 아니라 논문 + RAG 도구 레포** → DESIGN.md(앱 UI 일관성)는 **현재 해당 없음**.
- **조건부 미래 적용**: 만약 RAG/지식베이스용 **웹 UI**(예: kotaemon류)를 만들게 되면, 그때 DESIGN.md로 에이전트 생성 UI의 일관성을 잡는 참고자료로 유용.

## 결론
- **미채택**(현재). 웹 UI 프로젝트가 생기면 재검토. 라이선스 MIT라 그때 자유롭게 참고 가능.
