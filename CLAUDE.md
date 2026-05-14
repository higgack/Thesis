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

## Auto-deploy is ALREADY active — do NOT suggest manual git pull

**VM has these cron entries running every minute** (verified via `crontab -l`):

```
* * * * * cd /home/higgack/Thesis && bash scripts/auto_pull.sh
* * * * * ~/Thesis/scripts/auto_deploy.sh
*/5 * * * * bash /home/higgack/scheduler_watchdog.sh >> ~/deploy.log 2>&1
```

What this means for the agent:
- After ANY `git push`, the VM auto-pulls the branch within 60 seconds
  and recreates the bot/forward-listener/dashboard containers.
- The bot then sends a "🚀 배포 완료 <sha> <title>" notification to the
  owner's Telegram on every successful redeploy.
- **NEVER** tell the user to run `git pull` / `docker compose up -d` /
  `docker compose restart` themselves. They've heard that 10× already
  and it wastes their time.
- **NEVER** add new cron entries. The existing ones do the job — adding
  duplicates causes race conditions.
- After pushing, the correct closing line is: "푸시 완료 (sha). 1분 내
  자동 배포 + 텔레그램 알림 갈 거야." That's it.

The only exceptions where a manual command IS needed:
- After editing `.env` on the VM, because `docker compose restart` doesn't
  re-read env_file. Use `docker compose up -d --force-recreate <service>`.
  But auto-deploy doesn't touch `.env`, so this only applies to manual
  edits the user makes.
- `docker logs` for diagnostics — read-only, fine to suggest.

Common commands the user might run on their own (read-only / observational):
```bash
docker logs --tail 20 thesis-bot-1
docker logs --tail 20 thesis-forward-listener-1
tail ~/deploy.log
crontab -l
```

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

## OCR backend — dormant local worker is pre-built but disabled

`src/ingest/ocr_client.py` routes Vision OCR through `OCR_BACKEND`
(default `gemini` = current behaviour). Two other backends exist but
are dormant:

- `OCR_BACKEND=local`  — every page goes through the PaddleOCR
  `ocr-worker` container. Zero per-page API cost, but quality drops
  ~10-20% on chart-heavy pages.
- `OCR_BACKEND=hybrid` — PaddleOCR first; if confidence < 0.8 or
  text < 50 chars, falls back to Gemini Vision for that one page.

To activate:
```bash
docker compose --profile ocr-local up -d ocr-worker
# Edit .env: OCR_BACKEND=hybrid
docker compose up -d --force-recreate bot
```

To deactivate:
```bash
# Edit .env: OCR_BACKEND=gemini  (or remove the line)
docker compose stop ocr-worker
docker compose up -d --force-recreate bot
```

Worker uses a file queue (`data/ocr_queue/` ↔ `data/ocr_results/`)
with atomic-write + heartbeat (`data/ocr_worker_heartbeat`). PaddleOCR
pin: 2.7.3 (proven API). v3.x rewrite exists (3.5.0 current) — see
`ocr-worker/worker.py` comment for upgrade notes.

## Resume-safety invariants

- All persisted state files use `_atomic_write_json` (tmp → fsync →
  rename) with `.bak` fallback on load.
- All live ingest entry points call `_enqueue_with_inflight` BEFORE
  the pipeline call and `_finish_inflight(done/retry)` after, so a
  mid-process kill is recoverable.
- `docker-compose.yml` `stop_grace_period: 120s` gives short
  ingests time to finish gracefully on deploy.
