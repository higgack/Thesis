# SubstackNoah Second Brain (Telegram RAG)

개인용 RAG 지식 저장소. 텔레그램 채널 [t.me/substackNoahsummary](https://t.me/substackNoahsummary)에 올린
링크/PDF/유튜브/텍스트를 자동 수집·요약해 벡터DB와 Notion에 저장하고,
봇과 1:1 대화로 질의하면 토큰 최소 RAG로 답변합니다.

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
                 chunk + summarize (Haiku, 1회성)
                       │
                       ▼
                 OpenAI text-embedding-3-small

[User DM] --(question)--> [Bot]
                            │
                ┌───────────┴───────────┐
                ▼                       ▼
         Hermes router (Haiku)   prompt cache hit
         답할 수 있나? / RAG필요?         │
                │                       ▼
                ▼               Sonnet final answer
         Hybrid retrieve top-5
         + summary-first 컨텍스트
```

## 토큰 절약 핵심 4가지

1. **수집 시점 1회 요약**: 긴 문서를 저장할 때만 Haiku로 요약. 이후 모든 질의는 요약을 우선 컨텍스트로 사용.
2. **요약-우선 검색**: 벡터DB에 (요약 청크 + 원문 청크)를 함께 넣고, 요약을 먼저 top-K로 가져와 사용. 부족할 때만 원문 청크.
3. **Hermes 라우터**: 저렴한 Haiku가 "메모리만으로 답 가능 / RAG 필요 / 잡담" 분류 → 불필요한 RAG 차단.
4. **Anthropic prompt caching**: 시스템 프롬프트와 라우터 프롬프트를 캐시해 반복 호출 시 입력 비용 90% 절감.

## 실행 (로컬 개발)

```bash
cp .env.example .env       # 토큰 채우기
docker compose up --build
```

## 배포 (Fly.io 무료 티어)

```bash
fly launch --no-deploy
fly secrets set TELEGRAM_BOT_TOKEN=... ANTHROPIC_API_KEY=... \
  OPENAI_API_KEY=... NOTION_TOKEN=... NOTION_DATABASE_ID=... \
  TELEGRAM_OWNER_ID=...
fly volumes create rag_data --size 1
fly deploy
```

## 사용법

1. 봇을 채널에 admin으로 추가 (메시지 읽기 권한 필요)
2. 채널에 URL/PDF/유튜브/텍스트를 올리면 자동 수집
3. 봇과 DM으로 자연어 질문 → 답변
4. 명령어:
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
