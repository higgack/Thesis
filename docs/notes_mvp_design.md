# 체화 노트 (Study Notes) — Phase 0 MVP 설계도

검색형 위키와 분리된 **개인 학습 노트 + 되새김질(체화) 엔진**.
별도 학습 TG 채널 → 봇이 풍부한 노트로 재구성 → Notion형 대시보드 +
복습(spaced repetition / active recall) 레이어.

> 핵심 차별: 위키 = "찾아보는 백과사전". 노트 = "내가 공부한 걸
> 오래 내재화하는 되새김질 도구". 저장소가 아니라 **복습 엔진**.

제약: 같은 8GB CPU VM(GPU 없음, 봇 5.5GB), 비용 민감(Free Trial).
주 문서 = 디지털 PDF > 웹 > YouTube/PPT/워드 > (드물게) 엑셀.

---

## 1. 재사용 맵 (thesis에서 그대로 가져옴 — 새로 안 만듦)

| 필요 | 재사용 대상 | 비고 |
|---|---|---|
| 인입 로더 (PDF·웹·YT·PPT·워드·엑셀) | `src/ingest/loaders.py` | 문서 유형 거의 전부 커버됨 |
| 채널 수신 | `on_channel_post` / forward-listener 패턴 | 학습 채널 id만 분기 |
| 영속성 (atomic JSON / SQLite WAL) | `_atomic_write_json`, `timeout=30`+WAL | CLAUDE.md 불변식 준수 |
| 마크다운 vault 패턴 | `src/store/wiki.py` | 더 풍부/개인용으로 변형 |
| 정적 대시보드 재생성 | `src/dashboard/{server,regenerate}.py` | 노트 페이지 추가 |
| LLM 호출 | `config.make_genai_client()` (Vertex) | 노트 합성 = flash |

→ **신규 작업은 파싱이 아니라**: 노트 합성 프롬프트 + 체화(SRS) +
대시보드 노트/복습 페이지. 파싱 고도화(opendataloader)는 Phase 1로 보류.

## 2. 모듈 구조 (thesis 레포 내 신규)

```
src/notes/
  __init__.py
  channel.py     # 학습 채널 수신 → 로더 재사용 → 합성 파이프라인
  synth.py       # LLM 노트 합성 (파싱 결과 → 풍부한 노트 + 복습질문)
  store.py       # 노트 vault(md) + notes.db 메타 (atomic + WAL)
  srs.py         # spaced repetition 스케줄 (SM-2 lite) + 복습 상태
  recall.py      # 복습질문 생성/자가평가 → 주기 갱신
data/notes/<slug>.md          # 노트 마크다운 vault (위키와 분리)
data/notes.db                 # SQLite (WAL): notes / note_srs / questions / reviews
src/dashboard/ (확장)          # 노트 인덱스 + 노트 페이지 + "오늘 복습" 큐
```

## 3. 데이터 모델 (`data/notes.db`, SQLite WAL)

```sql
notes(
  id TEXT PRIMARY KEY,         -- slug
  title TEXT, source_type TEXT,-- pdf|web|youtube|pptx|docx|xlsx
  source_ref TEXT,             -- url / 파일명 / video_id
  md_path TEXT,                -- data/notes/<slug>.md
  created TEXT, updated TEXT
)
note_srs(                      -- 노트당 1행, 현재 복습 상태 (SM-2 lite)
  note_id TEXT PRIMARY KEY,
  ease REAL DEFAULT 2.5,
  interval_days INTEGER DEFAULT 0,
  reps INTEGER DEFAULT 0, lapses INTEGER DEFAULT 0,
  last_reviewed TEXT, next_due TEXT
)
questions(                     -- active recall 자가테스트
  id INTEGER PRIMARY KEY, note_id TEXT,
  question TEXT, answer TEXT, q_type TEXT  -- recall|concept|application
)
reviews(                       -- 복습 이력 (통계/주기조정용)
  id INTEGER PRIMARY KEY, note_id TEXT,
  reviewed_at TEXT, grade INTEGER          -- 0 까먹음 / 1 가물가물 / 2 기억함
)
```

## 4. 노트 합성 (`synth.py`) — 요약이 아니라 "노트"

모델 = flash (품질 우선; 노트는 자주 다시 읽으므로). 파싱 결과를 받아
**구조 보존 + 능동 복습 장치 포함**한 마크다운 노트 생성.

출력 템플릿:
```markdown
# {제목}
> 출처: {source_ref} · 학습일: {YYYY-MM-DD} · 유형: {source_type}

## 🎯 한 줄 요지
{한 문장 핵심}

## 🧠 개념 지도
- {핵심개념1} — {다른 개념과의 관계}
- ...

## 📖 정리 (구조화 본문)
### {섹션}
{설명. 원본 표는 마크다운 표로, 수식은 $...$ 로 보존}

## 🔑 핵심 용어
- **{용어}**: {정의}

## 🔗 관련 노트
- [[{기존 노트 제목}]]   ← vault 인덱스와 매칭된 것만

## ❓ 복습 질문   (← DB questions 로 분리 저장, 대시보드에서 답 가림)
1. Q: {recall 질문} / A: {답}
2. Q: {concept 질문} / A: {답}
3. Q: {application 질문} / A: {답}
```

규칙: 원문 표/수식/숫자 **임의 변형 금지**(체화는 정확성이 생명).
근거 없는 내용 생성 금지 — 파싱된 본문에 있는 것만.

## 5. 체화 = SRS (`srs.py`) — SM-2 lite

복습 3버튼 자가평가 → 다음 복습일 자동 계산:
- **까먹음(0)**: interval→1일, reps→0, lapses+1, ease−0.2
- **가물가물(1)**: interval→max(1, round(interval×1.2)), ease 유지
- **기억함(2)**: reps==0→1일 / reps==1→4일 / 이후→round(interval×ease),
  ease+0.1 (상한 2.7, 하한 1.3)
- `next_due = today + interval_days`

신규 노트: 합성 직후 `next_due = 다음날` 1행 자동 생성 → 바로 복습 큐 진입.

## 6. 대시보드 (Notion형)

- **상단 "오늘 복습할 노트" 큐**: `next_due <= today` 정렬. 개수 배지.
- **노트 인덱스**: 제목 · 유형 · 마지막 복습일 · 다음 복습일 · due 배지.
- **노트 페이지**: TOC + 렌더(표, **KaTeX 수식**) + 접힌 복습 질문
  (클릭→답 펼침) + 자가평가 3버튼 → `reviews` 기록 + `note_srs` 갱신.
- 재생성은 기존 `regenerate.py` 패턴(off-thread, lock, 증분) 재사용.
  자가평가만 경량 동적 엔드포인트(또는 TG 콜백)로 처리.

## 7. 비용 (Free Trial 영향 ~0)

- 파싱 = 로컬 무료. 노트 합성 = flash 1콜/문서 ≈ 20K in + 3K out
  ≈ **₩18/노트** (복습질문 동일 콜에 포함). 개인 학습 저빈도 →
  하루 ₩수십~수백. 인프라 정액 대비 무시 가능.
- pro로 올리면 ~₩100/노트 — 중요한 자료만 선택적으로.

## 8. 리스크 / 가드레일

- **RAM**: 노트 합성 = API 콜(저RAM), 대시보드 재생성 = 경량.
  MinerU/opendataloader(Java/모델) 미도입 → 8GB VM 안전.
- **위키와 격리**: 채널·vault(`data/notes/`)·db(`notes.db`)·대시보드
  페이지 전부 분리. 위키 파이프라인 무간섭.
- **정확성**: 합성 시 표/수식/숫자 보존 + 근거 없는 생성 금지(체화 신뢰).
- CLAUDE.md 불변식: atomic write + .bak, SQLite WAL+timeout=30 적용.

## 9. 빌드 단계

- **Phase 0 (MVP, 체화 포함)**: 채널 수신 → 로더 재사용 → flash 노트
  합성 + 복습질문 → vault + notes.db(SRS) → 대시보드 노트/복습 큐.
- **Phase 1 (파싱 고도화, 필요 시만)**: 디지털 PDF 구조가 부족하면
  `opendataloader-pdf` A/B 투입(회사리포트 A/B와 동일 방식).
- **Phase 2 (고급 체화 — 지식그래프)**: 핵심은 **노트 간 지식그래프**.
  내재화 = 연결 만들기이므로, "🔗 관련 노트"를 단순 링크에서 그래프로
  키운다. 그 외 인터리빙·약점 토픽 집중·복습 통계·수식 OCR(스캔 자료
  생기면 클라우드 Vision fallback).

### Phase 2 채택안: LightRAG + Gemini 지식그래프 (확정)

RAG-Anything(HKUDS) 검토 결과 **통째 도입은 회피**한다: 주 파서 MinerU =
GPU 성향(8GB CPU VM 부적합), 기본 LLM = OpenAI(우리는 Vertex/Gemini),
LibreOffice 등 무거운 의존성 → 리스크·비용 과다. **대신 그 베이스인
LightRAG만 채택**한다.

설계:
1. **파싱은 우리 로더 재사용**(+필요시 opendataloader, CPU). MinerU/GPU
   안 씀 → 새 인프라 0.
2. LightRAG의 `llm_model_func`·`embedding_func`를 **`config.make_genai_client()`
   (Vertex/Gemini)에 배선** → 비용은 cost.db에 그대로 집계, OpenAI 미사용.
3. 합성된 노트 텍스트 → LightRAG **엔티티·관계 추출 → 노트 간 그래프**.
   그래프 저장은 notes.db 옆에 분리 보관(위키 무간섭).
4. 대시보드: "🔗 관련 노트"를 **그래프 뷰**로 — 한 개념에서 연결된
   노트로 타고 들어가며 되새김질.

비용: 노트당 합성 ₩18 + KG 추출 ₩10~30 ≈ **₩30~50/노트**. 개인 학습
저빈도라 월 ₩수백, 정액 인프라 대비 무시 가능. **GPU/OpenAI/MinerU =
새 비용·리스크라 도입 금지**(어제 egress 비용 교훈 반영: 새 인프라를
얹지 않는다).

착수 조건: Phase 0가 실제로 체화에 먹히는 게 검증된 뒤. 그 전엔
프레임워크 도입 = 과설계.
