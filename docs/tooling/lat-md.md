# lat.md 검토 — 적용 여부 결정 기록

> 대상: [1st1/lat.md](https://github.com/1st1/lat.md) — "A knowledge graph for your codebase, written in markdown."
> 결론: **도구 자체는 미채택**(논문엔 부적합·Node 의존). 단, **핵심 아이디어(링크형 지식맵)는 무의존성으로 채택** → `docs/INDEX.md`.

## 무엇인가
- `lat.md/` 디렉터리에 마크다운들을 두고 **위키링크 `[[section-id]]`**, **코드심볼 링크 `[[src/auth.ts#fn]]`**,
  **코드 백링크 `// @lat: [[id]]`**, **`lat check` 일관성 검증**, **임베딩 시맨틱 검색**으로 코드베이스의
  지식그래프를 사람+에이전트가 함께 유지.
- 스택: **Node.js 22+ / npm**. 용도: **코드베이스 + AI 에이전트** 컨텍스트 유지(거대 단일 문서의 한계 해소).

## 우리 프로젝트 적용성
| 관점 | 평가 |
|---|---|
| 논문 본문(manuscript) | ❌ 부적합. lat.md는 **코드**용 링크그래프이지 학술 산문/문헌용이 아님. |
| 레포 메타-내비게이션(에이전트 길찾기) | △ 개념은 유용. 우리 레포가 커져(스킬 39에이전트·rag·triage·docs/tooling 7·manuscript 6버전) 링크형 지도가 도움. |
| 도구 도입(Node 22 + lat.md/ + lat check) | ❌ 비용 > 효용. 우리 코드 규모가 크지 않고 Node 의존·유지부담이 생김. README+docs/tooling로 이미 일부 커버. |

## 결정
- **lat.md 도구·Node 의존성: 도입하지 않음.**
- **아이디어만 채택**: lat.md의 "링크된 마크다운 지식맵" 발상을 **순수 마크다운**으로 구현한
  **`docs/INDEX.md`**(레포 지식맵)를 추가. 의존성 0, GitHub에서 클릭 네비게이션, 에이전트가 한 파일로
  레포 구조를 파악.
- 나중에 코드가 크게 늘고 에이전트 컨텍스트 관리가 진짜 병목이 되면 lat.md 재검토.

## (참고) 나중에 쓸 경우
```bash
npm install -g lat.md
lat init && lat check
lat search "rag pipeline"
```
