# SubstackNoah Second Brain (Telegram RAG)

개인용 RAG 지식 저장소. 텔레그램 채널 [t.me/substackNoahsummary](https://t.me/substackNoahsummary)에 올린
링크/PDF/유튜브/텍스트를 자동 수집·요약해 벡터DB와 Notion에 저장하고,
봇과 1:1 대화로 질의하면 **Gemini 기반 초저가 RAG**로 답변합니다.

## 아키텍처

```
[Telegram Channel]  --(channel_post)-->  [Bot]
                                          │
                       ┌──────────────────┼──────────────────┐
                       ▼                  ▼                  ▼
                 Ingest router    Notion (원문/요약)    Chroma + SQLite
                  (URL/PDF/YT/txt)
                       │
                       ▼
                ┌──hint 있나?(arXiv abstract / og:description)──┐
                │ yes → 요약 LLM 스킵 (무료)                       │
                │ no  → Gemini 2.5 Flash-Lite로 1회 요약           │
                └──────────────────────────────────────────────────┘
                       │
                       ▼
                 OpenAI text-embedding-3-small ($0.02/MTok)

[User DM] --(question)--> [Bot]
                            │
                            ▼
                Hybrid retrieve top-5 (Chroma dense + BM25)
                summary-first 컨텍스트
                            │
                ┌───────────┴───────────┐
                ▼                       ▼
         Gemini 2.5 Flash         /deep → Gemini 2.5 Pro
         (기본, 매우 저렴)          (어려운 질문에만)
```

## 비용 절감 핵심 6가지

1. **Gemini 전면 사용**: 요약·답변 모두 Gemini Flash 계열. Claude/GPT 동급 대비 5~10배 저렴.
2. **소스별 무료 요약**: arXiv는 Atom API의 abstract, 일반 웹은 og:description / meta description을 그대로 요약으로 사용 → **요약 LLM 호출 자체를 스킵**.
3. **수집 시점 1회 요약**: hint가 없을 때만 Flash-Lite로 1회 요약. 이후 모든 질의는 요약 우선.
4. **요약-우선 검색**: 벡터DB에 (요약 청크 + 원문 청크)를 함께 넣고 요약을 먼저 검색 → 답변 컨텍스트가 작음.
5. **단일 사용자라 라우터 제거**: 항상 RAG 직행. 분류용 LLM 호출 제거.
6. **2-tier 답변 모델**: 기본은 Flash, 어려운 질문만 `/deep`으로 Pro. 평소 답변 비용 ~90% 절감.

## 예상 비용 (참고)

월 100문서 수집 + 200질의 기준 **약 $1**, 월 500문서 헤비 사용 기준 **약 $5** (단가는 변동 가능).

## 실행 (로컬 개발)

```bash
cp .env.example .env       # 토큰 채우기
docker compose up --build
```

## 배포 (Fly.io 무료 티어)

```bash
fly launch --no-deploy
fly secrets set TELEGRAM_BOT_TOKEN=... GOOGLE_API_KEY=... \
  OPENAI_API_KEY=... NOTION_TOKEN=... NOTION_DATABASE_ID=... \
  TELEGRAM_OWNER_ID=...
fly volumes create rag_data --size 1
fly deploy
```

## 사용법

1. 봇을 채널에 admin으로 추가 (메시지 읽기 권한 필요)
2. 채널에 URL/PDF/유튜브/텍스트를 올리면 자동 수집
3. 봇과 DM으로 자연어 질문 → 답변 (Gemini Flash)
4. 명령어:
   - `/deep <질문>` - 어려운 질문은 Gemini Pro로 깊게
   - `/stats` - 저장된 문서 수, 청크 수
   - `/recent` - 최근 수집 10개
   - `/forget <id>` - 특정 문서 삭제

## Notion 데이터베이스 스키마

다음 속성을 가진 Notion 데이터베이스를 만들고 ID를 `NOTION_DATABASE_ID`에 입력:

| 속성 | 타입 |
|---|---|
| Title | Title |
| Source | URL |
| Type | Select (url, pdf, youtube, text) |
| Summary | Text |
| Doc ID | Text |
| Ingested | Date |
