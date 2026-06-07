# LLM Wiki 층 (P1–P5) — 비용 · 리스크 · 원복 · 활용

> RAG는 질문마다 처음부터 검색·재조립한다(축적 없음). 이 층은 Karpathy의
> **LLM Wiki** 패턴 — 수집한 자료를 마크다운 위키 페이지로 **통합·누적**해
> 같은 주제 질문에 "어제 정리한 것 위에서" 답하게 한다. **RAG 대체가 아니라
> 그 위에 얹는 추가 층**이며, **기본 비활성(WIKI_ENABLED=0)** 이라 켜기
> 전까지 기존 동작은 byte-for-byte 그대로다.

전제: Obsidian vault(`OBSIDIAN_VAULT_PATH`)가 설정돼 있어야 동작한다.

---

## 0. ⛔ 일일 비용 상한 (가장 중요)

`WIKI_DAILY_BUDGET_KRW`(기본 **₩2000**, KST 기준). **오늘 위키 비용이 이
값에 도달하는 순간 그날 배치는 즉시 멈추고(블락) ack 버튼 알람이 온다.**

- 체크 지점: 배치 시작 시 + **머지 1건마다**. 도달하면 더 이상 머지 안 함.
- 블락돼도 **자료는 큐에 그대로 보존** → **다음날 KST 0시에 비용이 리셋**되며
  자동 재개(`cost.today_krw()`가 KST 일 단위라 자정에 0으로). 손실 없음.
- 오버슈트: 머지 도중엔 못 멈추므로 최대 머지 1건(~₩13)까지 초과 가능.
- 알람: `_send_actionable_alert`(notify_id `wiki_daily_budget`) — "오늘 ₩X ≥
  한도 ₩2000, 위키 중단" + 확인 버튼. `/wiki_status`에도 "오늘 ₩/한도·⛔초과"
  표시. 한도 조절은 `.env WIKI_DAILY_BUDGET_KRW`(0=무제한).

즉, **위키가 폭주해도 하루 비용은 ₩2000(±한 머지)에서 강제로 멈춘다.**

---

## 1. 아키텍처 (P1–P5 매핑)

| P | 기능 | 구현 |
|---|---|---|
| P1 | 개념(토픽) 위키 + 증분 통합 | `store/wiki.py: run_batch()` — 야간 배치가 큐를 토픽별로 묶어 flash 1회 머지 → `SecondBrain/Wiki/<토픽>.md` |
| P2 | 질의 위키-우선 → RAG 폴백 | `agent/tools.py: search_my_brain()` — `wiki.wiki_context()`가 매칭되면 합성 페이지를 top hit로 prepend, 미스면 그대로 RAG |
| P3 | provenance·모순 추적 + ack 알림 | 머지 프롬프트가 `## ⚠️ 검토 필요` 섹션 + 출처 표기 → `_wiki_batch_job`이 `_send_actionable_alert`로 알림 |
| P4 | git diff = "오늘 배운 것" 다이제스트 | 배치가 vault에 커밋(`obsidian.commit_subtree`) + 야간 텔레그램 다이제스트 |
| P5 | 텔레그램 표면 | `/wiki /wiki_today /wiki_status /wiki_run /wiki_off /wiki_on` |

**데이터 흐름**
```
수집(_ingest) ──enqueue()──▶ data/wiki_queue.json   (LLM 0, 비용 0)
                                  │
            매일 새벽 3시(KST) 야간 배치(run_batch)
              ├─ 예산 체크(머지마다): 오늘 ₩≥2000 → 블락+알람
              │  flash 머지(purpose="wiki")
                                  ▼
      SecondBrain/Wiki/<토픽>.md ──git commit──▶ vault repo
                                  │
            질의(WIKI_QUERY_FIRST=1) ──wiki_context()──▶ 답변 top hit
```
위키 층은 **Chroma/meta.db를 절대 건드리지 않는다** — vault 마크다운 +
`data/wiki_*.json` 상태 파일만 쓴다. 그래서 끄면 기존 RAG가 그대로다.

---

## 2. 플래그 (`src/config.py` / `.env`)

| 플래그 | 기본 | 의미 |
|---|---|---|
| `WIKI_ENABLED` | `0` | 마스터 스위치. 0이면 전부 inert |
| `WIKI_QUERY_FIRST` | `0` | 답변에 위키 페이지 우선(P2). WIKI_ENABLED=1일 때만 |
| `WIKI_DAILY_BUDGET_KRW` | `2000` | **일일(KST) 비용 상한 → 도달 시 블락+알람.** 0=무제한 |
| `WIKI_MERGE_MODEL` | `ANSWER_MODEL`(flash) | 머지 모델. pro 안 씀 |
| `WIKI_BATCH_HOUR` | `3` | 야간 배치 시각(KST) |
| `WIKI_MIN_SUMMARY_CHARS` | `600` | 중요도 게이트(이 미만 요약은 위키 제외) |
| `WIKI_MAX_TOPICS_PER_RUN` | `25` | run당 토픽 상한 |
| `WIKI_MAX_DOCS_PER_TOPIC` | `6` | 토픽당 문서 상한 |
| `WIKI_MAX_PAGE_CHARS` | `6000` | 기존 페이지 입력 컷 |
| `WIKI_DOC_SUMMARY_CHARS` | `1200` | 문서 요약 입력 컷 |
| `WIKI_MERGE_MAX_TOKENS` | `3000` | 머지 출력 상한 |
| `WIKI_BATCH_THROTTLE_SEC` | `1.0` | 토픽 간 간격 |

---

## 3. 비용 분석

**추가 임베딩 0** — 토픽 라우팅은 수집 때 이미 추출된 회사/태그 메타데이터를
재사용(LLM/임베딩 호출 없음). 비용은 **야간 머지(flash)** 에서만 발생하고,
**일일 ₩2000 상한에서 강제로 멈춘다.**

flash 단가(`store/cost.py`): in $0.30 / out $2.50 per 1M, ₩1,400/USD.

| 시나리오 | 입력 tok | 출력 tok | 토픽 1건 비용 |
|---|---|---|---|
| 일반 | ~3,000 | ~1,500 | **≈ ₩6.5** |
| 무거움(캡 한계) | ~5,500 | ~3,000 | **≈ ₩12.8** |

- **월 추정**: 하루 갱신 토픽 10~15건이면 **≈ ₩2,000~2,900/월(+2~3%)**. 폭주
  시에도 **일일 ₩2000 상한 → 월 최대 ~₩60,000**이지만, run당 25토픽 캡 +
  중요도 게이트로 보통 그 훨씬 아래.
- **측정 우선**: 모든 머지가 `purpose="wiki"` → `/wiki_status`(오늘/이번달
  위키 ₩) · `/usage`(purpose 분해)로 실비용 확인.
- P2(질의 위키-우선)는 **비용 중립~절감**(합성 1장 < top-K 청크).

---

## 4. 기존 시스템 리스크

| 영역 | OFF (기본) | ON |
|---|---|---|
| **수집 파이프라인** | 영향 0 — `enqueue()`가 `enabled()` False면 즉시 return | 로컬 JSON append 1회(LLM/네트워크 0), `try/except` → ingest를 깨뜨릴 수 없음 |
| **RAG 검색/답변** | 영향 0 | `WIKI_QUERY_FIRST=1`일 때만 위키 페이지를 top hit로 prepend. 미스/에러 → `hybrid()` 폴백 → recall 안 줄어듦 |
| **Chroma / meta.db** | 안 건드림 | **안 건드림** (위키는 vault 마크다운 + wiki_*.json만) |
| **비용** | 0 | 야간 배치만, **일일 ₩2000 상한** + run당 캡으로 이중 차단 |
| **이벤트 루프** | 0 | 야간 3시 배치, 토픽 간 throttle → 라이브 ingest와 경합 X |
| **알림 빈도** | 0 | 모순>0 또는 예산초과일 때만 ack 알림(콘텐츠/안정 id dedup) |
| **신규 cron** | 없음 | 없음 — PTB JobQueue(run_repeating) 사용, CLAUDE.md 준수 |

**환각/오통합 리스크**: 합성 페이지에 머지 오류 가능 → (1) 원문 청크 + RAG
**그대로 유지**(P2는 폴백, Chroma 불변), (2) 모든 주장에 출처 표기 → 검증
가능, (3) 페이지는 git → diff·revert 가능, (4) `WIKI_QUERY_FIRST=0`이면 핵심
Q&A는 불변.

---

## 5. 원복 런북

1. **즉시(재배포 X)** — **`/wiki_off`**. `data/wiki_disabled` 킬스위치 →
   엔진 전체(큐 적재·야간 배치·질의우선) 즉시 no-op. 복구는 `/wiki_on`.
2. **설정** — `.env WIKI_ENABLED=0` (+ `WIKI_QUERY_FIRST=0`) →
   `docker compose up -d --force-recreate bot`. 기본이 0이라 평소 할 일 없음.
3. **데이터** — 위키 페이지는 vault git → `git rm -r SecondBrain/Wiki && commit`.
   엔진 상태 초기화: `rm data/wiki_queue.json data/wiki_index.json
   data/wiki_last_run.json`. **코퍼스(Chroma/meta.db)는 무관.**
4. **코드** — 전부 신규 모듈 + 플래그 게이트 hook. 최후엔 feature 커밋 revert.

---

## 6. 활용 가이드

**점진 도입 — 한 번에 다 켜지 말 것**
1. **1주차**: `.env WIKI_ENABLED=1` → 재배포. `WIKI_QUERY_FIRST`는 0 유지.
   매일 **`/wiki_status`**(오늘 ₩·한도·큐·페이지)·**`/wiki_today`**(어젯밤
   결과) 확인. 즉시 테스트는 **`/wiki_run`**.
2. 페이지가 쓸 만해지면 **`WIKI_QUERY_FIRST=1`** → Q&A가 합성 페이지 우선.

**매일 루틴**
- 아침에 야간 **"📚 위키 업데이트"** 다이제스트 + ⛔예산/⚠️모순 알람 확인.
- **Obsidian** `SecondBrain/Wiki/` 폴더에서 누적·교차참조 페이지 열람.
- **`/wiki <토픽>`** 으로 특정 주제 합성 페이지 열람.
- **⚠️ 모순 알람** 오면 해당 `/wiki <토픽>`의 `## ⚠️ 검토 필요` 확인.
- **⛔ 예산 알람** 오면: 한도를 올릴지(`WIKI_DAILY_BUDGET_KRW`) 또는 캡/게이트로
  비용을 줄일지 판단. 그날은 자동으로 멈춰 있으니 급할 것 없음.

**튜닝**: 비용↑ → `WIKI_MAX_TOPICS_PER_RUN`↓ / `WIKI_MIN_SUMMARY_CHARS`↑ /
`WIKI_DAILY_BUDGET_KRW`↓.

---

## 7. 한계 / 향후

- **토픽 라우팅이 메타데이터 기반**(회사/태그)이라 거칠 수 있다 → 임베딩
  클러스터링은 향후.
- **기존 자료 백필**: 기본은 신규 ingest만 위키화. 기존 meta.db 문서까지
  올리려면 **`/wiki_backfill [개월|all]`** (예: `/wiki_backfill 6` = 최근 6개월).
  적재는 ₩0, 실제 머지는 야간 배치가 일일 ₩2000 캡 내에서 며칠~몇 주에 걸쳐
  처리(문서당 ~₩1, 자동 분산). 범위 제한(최근 N개월)부터 권장 — 오래된 문서는
  메타데이터가 없어 '기타' 페이지로 몰릴 수 있으므로.
- **위키 페이지는 Chroma 미색인** — P2 매칭은 토픽명 기반. 의미검색 색인은 향후.
- **머지 환각** 가능 → 출처 백링크로 검증, 원문 RAG 유지(replacement 아님).
