---
version: alpha
name: Thesis Dashboard
description: >-
  higgack/thesis 개인 RAG 지식베이스의 웹 대시보드(아카이브·노트·위키·KG·
  Universe) 팔레트. Linear 계열 뉴트럴 + 단일 인디고 액센트.
omitted:
  - section: Elevation & Depth
    reason: 그림자는 --shadow 토큰 하나뿐(라이트 1단계, 다크 none). 단계 체계 없음.
  - section: Shapes
    reason: 반경은 6/8/10px을 상황에 맞게 인라인으로 씀. 스케일 토큰 미도입.
colors:
  # ── 표면 ─────────────────────────────────────────────
  bg: "#f7f8f9"
  panel: "#ffffff"
  panel-alt: "#f0f1f3"
  border: "#e8e8ea"
  border-input: "#e0e1e4"
  border-soft: "#eef0f2"
  # ── 글자 ─────────────────────────────────────────────
  text: "#282a30"
  heading: "#16171a"
  muted: "#8a8f98"
  warning-text: "#92400e"
  # ── 액센트 ───────────────────────────────────────────
  primary: "#5e6ad2"
  accent: "#5e6ad2"
  accent-hover: "#515dc4"
  # ── 상태 ─────────────────────────────────────────────
  important: "#f5a623"
  due: "#f5a623"
  memo: "#2faf6a"
  danger: "#e5484d"
  # ── 도구 배지 ────────────────────────────────────────
  tool-brain: "#ec4899"
  tool-paper: "#a855f7"
  tool-patent: "#5e6ad2"
  tool-report: "#14b8a6"
  tool-web: "#2faf6a"
  tool-ingest: "#f5a623"
colorsDark:
  bg: "#0b0c0e"
  panel: "#141518"
  panel-alt: "#1c1d21"
  border: "#26272b"
  border-input: "#2a2c31"
  border-soft: "#1f2024"
  text: "#e2e3e6"
  heading: "#f7f8f8"
  muted: "#8a8f98"
  warning-text: "#fcd34d"
  primary: "#5e6ad2"
  accent: "#7c84e8"
  accent-hover: "#9aa2f0"
  important: "#f5a623"
  due: "#f5a623"
  memo: "#3fbf7a"
  danger: "#f2555a"
  tool-brain: "#f472b6"
  tool-paper: "#c084fc"
  tool-patent: "#7c84e8"
  tool-report: "#2dd4bf"
  tool-web: "#3fbf7a"
  tool-ingest: "#f5a623"
typography:
  ui:
    fontFamily: -apple-system, BlinkMacSystemFont, sans-serif
    fontSize: 14px
  mono:
    fontFamily: monospace
---

## Overview

**비즈니스 목적**: 이 파일은 사람이 아니라 **이 저장소에서 일하는 AI
에이전트**(Claude Code · GitHub Copilot)가 읽으라고 있다. 대시보드 색을
고칠 때 파일마다 색을 새로 지어내지 말고 여기에 정의된 값만 쓰라는 뜻.

**런타임 소스는 이 파일이 아니다.** 실제로 브라우저에 나가는 CSS 변수는
`src/dashboard/widgets.py`의 `DESIGN_TOKENS_CSS`이고, 이 파일은 그 값의
읽을 수 있는 사본 + 의미 설명이다. 둘이 어긋나면 문서가 쓸모없어지므로
`scripts/preflight.sh` 섹션 7이 **두 파일의 색 값을 자동 대조**한다.
토큰을 바꿀 때는 **두 파일을 같은 커밋에서** 고쳐야 한다.

형식은 [google-labs-code/design.md](https://github.com/google-labs-code/design.md)
(alpha)를 따랐다. 단, 그쪽 npm CLI(`@google/design.md`)는 **쓰지 않는다** —
이 저장소는 파이썬이고, 필요한 검사 두 가지(참조 무결성·명암비)는
preflight가 표준 라이브러리로 한다. 알파 규격이라 형식만 빌리고 도구에
묶이지 않는다는 판단 (2026-09-12).

## Colors

라이트/다크 두 벌. 다크 값은 front matter의 `colorsDark`에 있다
(design.md alpha 규격에 테마 분리가 없어서 쓴 확장 키 — 그쪽 린터는
확장 키를 조용히 통과시킨다).

| 토큰 | 의미 | 쓰는 곳 |
|---|---|---|
| `--bg` `--panel` `--panel-alt` | 바탕·카드·카드 대체면 | 전 대시보드 |
| `--border` `--border-input` `--border-soft` | 테두리 3단계 | 카드·입력·구분선 |
| `--text` `--heading` `--muted` | 본문·제목·보조 | 전 대시보드 |
| `--primary` `--accent` `--accent-hover` | 링크·활성 상태 | 인디고 단일 액센트 |
| `--important` | ★ 중요 표시 | 노트·위키·KG·Q&A 공통 |
| `--due` | SRS 복습 기한 도래 | 노트 |
| `--memo` | 📝 메모 · ✓ 읽음 표시 | 노트·위키·KG·Q&A 공통 |
| `--danger` | 삭제·실패·초과 | 전 대시보드 |
| `--tool-*` | 도구 배지 6종 | 아카이브 답변 출처 배지 |

**주의**: `--important`와 `--due`는 현재 같은 값(`#f5a623`)이다. 의미가
다르므로 한쪽만 바꿀 수 있게 토큰은 둘로 유지한다. `--memo`와
`--tool-web`도 마찬가지(라이트에서 둘 다 `#2faf6a`).

### 팔레트 밖 색 (알려진 부채)

대시보드 파이썬 파일에는 토큰에 없는 색 리터럴이 **90종/217회** 남아
있다 (2026-09-12 실측). 대부분 Tailwind 팔레트에서 즉흥적으로 꺼내 쓴
것이다. 가장 많이 반복되는 두 색(`#10b981`·`#f59e0b`)이 차지한 24줄을
`git blame`으로 확인하면 작성자가 전부 Claude고 Copilot은 0건이다.
대표적 중복:

| 값 | 정체 | 팔레트의 정답 |
|---|---|---|
| `#10b981` ×16 | Tailwind emerald-500 | `--memo` `#2faf6a` (두 번째 초록) |
| `#f59e0b` ×16 | Tailwind amber-500 | `--important` `#f5a623` (두 번째 주황) |
| `#2da44e` ×1 | GitHub green | `--memo` (세 번째 초록) |

**한 번에 정리하지 않는다** — 색이 실제로 바뀌는 작업이라 사용자가 눈으로
보고 판단할 일이다. 대신 preflight 섹션 7이 **개수를 래칫으로 고정**해서
새 색이 추가되는 것만 막는다. 부채를 줄이면 preflight가 기준선을 낮추라고
알려준다.

## Typography

시스템 폰트 한 벌만 쓴다 — `-apple-system, BlinkMacSystemFont, sans-serif`.
웹폰트 없음(로딩 비용·오프라인). 코드/해시/ID는 `monospace`.
크기는 11~13px(보조) · 14px(본문) · 15~28px(제목)을 인라인으로 쓴다.

## Layout

카드 격자 + 세로 목록. 간격은 4/6/8/10/12/16px을 인라인으로 쓰고 스케일
토큰은 없다. 대시보드는 정적 HTML을 파이썬 f-string으로 찍어내는 구조라
(`regenerate.py`·`notes_render.py`·`wiki_render.py`·`kg_render.py`·
`universe_render.py`) CSS는 각 페이지 `<style>`에 인라인된다.

`wiki_render`와 `universe_render`는 **일부러 자기 팔레트를 따로 쓴다**
(위키피디아 느낌 `--link`/`--toc-bg`, 그래프 캔버스 `--ink`/`--node`).
preflight 섹션 7의 허용 목록에 있다.

## Components

노트 목록 행의 상태 색. **토큰이 아니라 리터럴**이라 여기에 적어둔다
(런타임 위치: `notes_render.py`).

| 상태 | 테두리 | 배경(라이트/다크) |
|---|---|---|
| ★ 중요만 | `rgba(59,130,246,.60)` | `.07` / `.13` |
| ✓ 읽음만 | `rgba(47,175,106,.55)` | `.06` / `.10` |
| ★ + ✓ | `rgba(245,158,11,.55)` | `.06` / `.10` |

2026-09-12에 사용자 요청으로 ★만(주황)과 ★+✓(파랑)의 색을 맞바꿨다.

**CSS 규칙 순서를 건드리지 말 것.** 다크 모드의 한-플래그 규칙과
두-플래그 규칙은 특이도가 (0,3,1)로 같아서 **작성 순서만이** 승부를
가른다. 두-플래그 규칙이 마지막에 와야 배경을 지킨다.

## Do's and Don'ts

**하라**
- 색은 `var(--토큰)`으로 쓴다. 토큰에 이미 있는 값을 숫자로 다시 적지 않는다.
- 새 색이 정말 필요하면 `widgets.py`와 **이 파일을 같은 커밋에서** 고친다.
- 배경+글자를 둘 다 지정하는 규칙은 명암비 4.5:1(작은 글씨 기준)을 넘긴다.
- 라이트/다크 두 테마 모두에서 확인한다. 다크 값을 빠뜨리면 토큰이 라이트
  값을 그대로 물려받아 대비가 깨진다.

**하지 마라**
- Tailwind·GitHub·Material 팔레트에서 색을 즉흥적으로 꺼내 오지 말 것.
  초록이 3종, 주황이 2종이 된 원인이 정확히 이것이다.
- `wiki_render`/`universe_render`의 자체 팔레트를 공용 토큰으로 접지 말 것.
  중복 제거가 아니라 **디자인 변경**이다.
- 명암비 미달 부채(현재 21개 규칙)를 리뷰 없이 일괄 수정하지 말 것.
  사용자가 화면에서 보고 판단할 일이다.
