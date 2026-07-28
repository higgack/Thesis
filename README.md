# SubstackNoah Second Brain (Telegram RAG → Obsidian)

개인용 RAG 지식 저장소. 텔레그램 채널 [t.me/substackNoahsummary](https://t.me/substackNoahsummary)에 올린
링크/PDF/유튜브/텍스트(논문 포함)를 자동 수집·요약해 Chroma + **Obsidian vault**에 저장하고,
봇과 1:1 대화로 질의하면 **Gemini 기반 초저가 RAG**로 답변합니다.

## 두 가지 논문 워크플로우

1. **검색 후 저장** — `/find <검색어>` → Semantic Scholar에서 상위 5개 후보 표시 → 마음에 드는 링크를 그대로 봇에 보내면 ingest.
2. **수집·기억** — PDF 파일을 봇에 첨부 → 본문 추출 → arXiv ID 감지되면 **arXiv abstract를 무료 요약으로 사용**, 아니면 Flash-Lite로 요약 → Obsidian의 `Papers/`에 `.md` 저장.

## 아키텍처

```
[Telegram Channel/DM] --(post or upload)--> [Bot]
                                               │
                       ┌───────────────────────┼─────────────────────┐
                       ▼                       ▼                     ▼
                 Ingest router      Obsidian vault (Markdown)    Chroma + SQLite
              (URL/PDF/YT/text)     SecondBrain/{Papers,Web,
                       │             YouTube,Notes,Misc}/
                       ▼             + YAML frontmatter + git push
        ┌── hint(arXiv abstract / og:description)? ──┐
        │ yes → 요약 LLM 스킵 (무료)                 │
        │ no  → Gemini 2.5 Flash-Lite 1회 요약        │
        └─────────────────────────────────────────────┘
                       │
                       ▼
              Gemini gemini-embedding-001

[User DM "질문"] → Hybrid retrieve (Chroma + BM25) → Gemini Flash 답변
[/find <q>]      → Semantic Scholar 검색 → 후보 5개 표시
[/deep <q>]      → 같은 RAG에 Gemini Pro 호출
```

## 비용 절감 (Gemini + 무료 요약 + 라우터 제거)

| 사용량 | 월 비용 (예상) |
|---|---|
| 100문서 + 200질의 | **~$1** |
| 500문서 + 1000질의 | **~$5** |

핵심: Gemini Flash 단가, arXiv abstract / og:description 우선 사용으로 요약 LLM 호출 자체를 스킵, 단일 사용자라 라우터 제거.

## Obsidian 연동 (Git remote 권장)

봇은 클라우드에서 돌아가므로 Obsidian vault에 직접 쓰지 못합니다. **별도 GitHub repo**를 두고 양쪽이 git으로 동기화합니다.

### 1) vault용 GitHub repo 만들기

비공개 repo 하나 생성 (예: `<you>/second-brain-vault`).

### 2) GitHub Personal Access Token 발급

`repo` 권한을 가진 Fine-grained PAT 생성. URL 형태로 사용:
```
https://x-access-token:<TOKEN>@github.com/<you>/second-brain-vault.git
```
이걸 `.env`의 `OBSIDIAN_GIT_REMOTE`에 입력.

### 3) Obsidian 쪽

내 Obsidian vault 폴더 안에 같은 repo를 클론하고, **Obsidian Git** 플러그인 설치 후 자동 pull 간격(예: 5분)을 설정. 봇이 push → 잠시 후 내 노트북·폰에 자동으로 새 파일이 나타남.

### 파일 구조

```
SecondBrain/
├── Papers/            # PDF, arXiv 논문
├── Web/               # 일반 웹 링크
├── YouTube/           # 유튜브 자막
├── Notes/             # 채널/DM 텍스트 메모
└── Misc/
```

각 파일은 YAML frontmatter (`id`, `title`, `source`, `type`, `ingested`, `tags`) + Summary + Source + Original 섹션.

## 실행 (로컬 개발)

```bash
cp .env.example .env       # 토큰 채우기
docker compose up --build
```

### push 전 점검 (필수)

```bash
# Linux/macOS
bash scripts/preflight.sh

# Windows PowerShell
scripts\\preflight.cmd
```

전체 파일 점검은 `--all` 옵션을 사용합니다.

## 배포 (Fly.io 무료 티어)

### 1) AI Studio 방식 (기본)

```bash
fly launch --no-deploy
fly secrets set TELEGRAM_BOT_TOKEN=... GOOGLE_API_KEY=... \
  TELEGRAM_OWNER_ID=... \
  OBSIDIAN_GIT_REMOTE='https://x-access-token:PAT@github.com/<you>/second-brain-vault.git'
fly volumes create rag_data --size 1
fly deploy
```

### 2) Vertex 방식 (권장 운영)

`src/config.py` 기준으로 `GEMINI_BACKEND=vertex`이면 `GOOGLE_API_KEY` 대신
`VERTEX_PROJECT`, `VERTEX_LOCATION`(예: us-central1) 설정을 사용합니다.

```bash
fly launch --no-deploy
fly secrets set TELEGRAM_BOT_TOKEN=... \
  TELEGRAM_OWNER_ID=... \
  GEMINI_BACKEND=vertex \
  VERTEX_PROJECT=<gcp-project-id> \
  VERTEX_LOCATION=us-central1 \
  OBSIDIAN_GIT_REMOTE='https://x-access-token:PAT@github.com/<you>/second-brain-vault.git'
fly volumes create rag_data --size 1
fly deploy
```

`OBSIDIAN_VAULT_PATH=/data/vault`는 `fly.toml`에 이미 설정되어 있습니다.

## 명령어

| 명령 | 설명 |
|---|---|
| (DM 텍스트) | 자연어 질문 → Gemini Flash로 RAG 답변 |
| (DM URL/PDF) | 즉시 ingest |
| `/find <검색어>` | Semantic Scholar로 논문 5개 검색 |
| `/deep <질문>` | Gemini Pro로 깊게 답변 |
| `/recent` | 최근 수집 10개 |
| `/stats` | 문서·청크 수 |
| `/forget <id>` | 특정 문서 삭제 (vault 파일은 수동 정리) |
