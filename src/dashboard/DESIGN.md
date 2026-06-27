# 대시보드 DESIGN.md — 디자인 토큰 & 규약 (Linear 스타일)

대시보드(읽기 전용 정적 HTML)의 **단일 디자인 소스**. 색·여백·타이포를
즉흥으로 찍지 말고 여기 정의된 토큰을 쓴다. 새 강조색이 필요하면 **먼저
`:root`에 토큰을 추가**하고 `var(--…)`로 참조한다.

> **한 줄**: 거의 흰/near-black 배경 + 얇은 보더(그림자 최소) + 인디고 액센트
> `#5e6ad2` + Inter 폰트 + 좁은 헤딩 자간(-0.014em) + 8~10px 둥근 모서리 +
> 0.12~0.3s 미세 트랜지션. 그림자 대신 보더로 면을 나눔. (Linear 스타일)

> 정적 HTML은 `render` 모듈들이 생성하고 `server.py`가 서빙한다. 각 모듈은
> 자체 `:root` 블록을 갖는다. **적용 범위**: Q&A(`regenerate.py`)·노트
> (`notes_render.py`)·KG(`kg_render.py`) 3개가 Linear 룩. **위키
> (`wiki_render.py`)는 의도적으로 위키피디아풍 유지 — 제외**.

---

## 1. 색 팔레트 (light / dark) — Linear

`[data-theme]`로 토글(시간대 자동, 19:00~06:59 dark). Q&A·노트·KG 공통 토큰:

| 토큰 | 의미 | light | dark |
|---|---|---|---|
| `--bg` | 페이지 배경 | `#f7f8f9` | `#0b0c0e` |
| `--panel` | 카드/패널 면 | `#ffffff` | `#141518` |
| `--panel-alt` | 보조 면(범례/hover) | `#f0f1f3` | `#1c1d21` |
| `--border` | 면 테두리 | `#e8e8ea` | `#26272b` |
| `--border-input` | 입력/버튼 테두리 | `#e0e1e4` | `#2a2c31` |
| `--border-soft` | 옅은 구분선 | `#eef0f2` | `#1f2024` |
| `--text` | 본문 글자 | `#282a30` | `#e2e3e6` |
| `--heading` | 헤딩 글자 | `#16171a` | `#f7f8f8` |
| `--muted` | 보조/메타 글자 | `#8a8f98` | `#8a8f98` |
| `--accent` | 링크/active/포커스 | `#5e6ad2` | `#7c84e8` |
| `--accent-hover` | 액센트 hover | `#515dc4` | `#9aa2f0` |
| `--primary` | 주 버튼(저장/필터 active) | `#5e6ad2` | `#5e6ad2` |
| `--danger` | 위험/삭제 | `#e5484d` | `#f2555a` |
| `--shadow` | 그림자(거의 안 씀) | `0 1px 2px rgba(0,0,0,.03)` | `none` |

### 시맨틱 강조 토큰 (3개 surface 공통)

| 토큰 | 용도 | light | dark |
|---|---|---|---|
| `--important` | ★ 중요 표시·"중요만" 필터 | `#f5a623` (앰버) | `#f5a623` |
| `--memo` | 📝 메모·"메모만" 필터·메모 미리보기 | `#2faf6a` (그린) | `#3fbf7a` |

- 노트엔 `--due`(복습 임박, `--important`와 동값)도 있음 — 의미가 다르니 별도 유지.
- 메모/중요 카드의 옅은 배경·테두리는 알파 틴트(`rgba(245,166,35,…)`/
  `rgba(16,185,129,…)`)를 리터럴로 둠(알파는 hex 토큰으로 못 빼서). 토큰과
  같은 색 계열이라 의미는 일치.

### 타이포 / 형태 / 모션
- 폰트: `'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans KR",
  sans-serif` (한글은 Noto Sans KR fallback). 본문 15px/1.5, antialiased.
- 헤딩(`h1,h2,h3`): `--heading` 색 + `letter-spacing:-0.014em`.
- 둥근 모서리 8~10px, 그림자 대신 1px 보더. 트랜지션 hover 0.12~0.15s/테마 0.3s.

### 위키 전용 (위키피디아 테마 — Linear 미적용)
`--link`/`--link-visited`/`--toc-*`/`--bq-*`/`--code-bg`/`--highlight` 등 별도.
`--accent`가 `#3366cc`, 세리프(Noto Serif KR). **의도적으로 Linear 안 입힘.**

---

## 2. 타이포그래피
- 일반 surface: 시스템 산세리프 스택(기본).
- 위키 본문: `"Noto Serif KR", Georgia …` 세리프(문서 가독성).
- 메타/배지: 11~12px `--muted`. 제목: 굵게 `--text`.

## 3. 여백 / 모서리
- 카드 패딩 ~10–14px, 라운드 8–10px, 행 간격 6–10px.
- 필터/메모 버튼: padding `5px 12~14px`, radius 7px, font 12px/600.

## 4. 컴포넌트 색 규약
- **저장 버튼**: `--primary`(흰 글자). **삭제/해제 버튼**: `rgba(148,163,184,.25)` + `--muted`.
- **★ 토글·중요만 필터 활성**: `--important`.
- **📝 메모만 필터 활성·메모 미리보기 강조**: `--memo`.
- **알람 설정 버튼**: 인디고 `#6366f1`(위젯 전용, `widgets.py ALARM_CSS`).

## 5. Do / Don't
- ✅ 색은 토큰으로. 새 강조색은 `:root`(light+dark 둘 다)에 먼저 추가.
- ✅ 4개 surface에 같은 기능이면 같은 토큰·같은 클래스명(`memofilter`, `memo-preview`, `estar` 등).
- ⛔ 컴포넌트에 raw hex 직접 박기(특히 강조색). 기존 잔존 하드코딩은 점진적으로 토큰화.
- ⛔ 위키 토픽 페이지 CSS/HTML 변경 시 `_TPL_VERSION` 안 올리기(캐시가 옛 페이지 유지).

## 6. 마이그레이션 상태
- ✅ **솔리드 강조색 토큰화 완료**: ★ 버튼(`.star-btn`/`.nstar`/`.estar`/`.wstar`)
  색과 중요/메모 필터 활성 배경(`impfilter`/`memofilter`/`wstarfilter`/`wmemofilter`)을
  4개 surface 모두 `var(--important)`/`var(--memo)`로 매핑. 다크모드도 토큰으로 자동 적응.
- 🔸 **알파 틴트는 리터럴 유지**: `[data-important]` 카드 배경/테두리, `.memo-preview`
  배경 등 `rgba(245,158,11,…)`/`rgba(16,185,129,…)`은 알파를 hex 토큰으로 못 빼서 그대로.
  값은 `--important`/`--memo`와 같은 색 계열이라 의미는 일치.
- ⛔ `--primary`(저장 버튼)·`--due`(복습)·`--tool-*`·카테고리 배지는 의미가 달라
  토큰 통합 대상 아님(건드리지 말 것).
