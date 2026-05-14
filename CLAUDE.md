# Project notes for Claude

Standing instructions / project-specific facts that need to survive
context compaction. Anything Claude should ALWAYS remember while
working on this repo.

## Docker — service vs container names

`docker-compose.yml` services and their auto-generated container names:

| Service name (use with `docker compose ...`) | Container name (use with raw `docker ...`) |
|---|---|
| `bot` | `thesis-bot-1` |
| `forward-listener` | `thesis-forward-listener-1` |
| `dashboard` | `thesis-dashboard-1` |
| `telegram-bot-api` (profile: local-api) | `thesis-telegram-bot-api-1` |

Rules:
- `docker compose <verb> <SERVICE>` — always the bare service name (`bot`,
  not `bot-1` and not `thesis-bot-1`).
- `docker logs / rm / exec / inspect <CONTAINER>` — full container name
  (`thesis-bot-1`).

Common commands the user runs after a push:
```bash
cd ~/Thesis && git pull && docker compose up -d --force-recreate bot
docker logs --tail 20 thesis-bot-1
docker logs --tail 20 thesis-forward-listener-1
```

`docker compose restart` does NOT re-read `.env` env_file values — it
keeps the existing container's env. After editing `.env`, always use
`docker compose up -d --force-recreate <service>` (or `down`+`up -d`).

## Branch / push policy

Development branch: `claude/personal-rag-knowledge-base-sLSvV`.
All work commits go here. Never push to a different branch without
explicit permission. PR #1 already exists for this branch.

## Standing behavioural rules

- Review code first; only commit when the user explicitly asks. ("리뷰
  먼저하고 내가 요청하면 커밋")
- Every ingest-pipeline change must apply equally to new ingest AND
  retry queue — this is the default, never partial.
- Always update `_HELP_TEXT` in `src/bot.py` when adding / renaming /
  removing a command or changing user-visible policy. Help must stay
  under the 4000-char soft-split limit so it renders as a single
  Telegram message.
- `.env` on the VM contains secrets (Telegram bot token, Google API
  key, GitHub PAT, dashboard creds). Never echo its contents back in
  chat. If the user pastes them, warn and recommend rotation
  immediately.

## Cost-sensitive defaults (do not change without asking)

- `SUMMARY_MODEL=gemini-2.5-flash-lite`
- `ANSWER_MODEL=gemini-2.5-flash` (Q&A)
- `DEEP_MODEL=gemini-2.5-pro` (/deep, large compare)
- `EMBED_MODEL=gemini-embedding-001`
- `EMBED_BACKEND=gemini` (BGE-M3 path exists but disabled — earlier
  CPU bottleneck caused flood-bans, do not re-enable casually)
- `CHUNK_TOKENS=1000`, `CHUNK_OVERLAP=150`
- `TOP_K=10`
- OCR: `OCR_DPI=100`, `OCR_AUTO_CAP=7`, `OCR_SPARSE_THRESHOLD=800`,
  `OCR_PROBE_PAGES=3`, `OCR_PROBE_MIN_TEXT=300`

## Resume-safety invariants

- All persisted state files use `_atomic_write_json` (tmp → fsync →
  rename) with `.bak` fallback on load.
- All live ingest entry points call `_enqueue_with_inflight` BEFORE
  the pipeline call and `_finish_inflight(done/retry)` after, so a
  mid-process kill is recoverable.
- `docker-compose.yml` `stop_grace_period: 120s` gives short
  ingests time to finish gracefully on deploy.
