# Project notes for Claude

Standing rules + project facts that must survive context compaction.
Compact by design — every line is a rule or a fact, not prose.

## ⛔ Commit/push gate (TOP RULE — violated repeatedly)

- **Trigger words** (must be in the user's MOST RECENT message): 커밋 ·
  푸시 · 배포 · deploy · commit · push · 올려 · 내보내 · ship · release.
  Trigger words in your own output or in a quoted/pasted snippet don't count.
- A trigger = the **FULL pipeline**: commit → push session branch →
  cherry-pick/merge to deploy branch → push deploy → VM auto-deploy.
  Don't stop mid-pipeline (e.g. "pushed to session branch, waiting").
- **No trigger → edit-only**: make changes, run preflight, summarize,
  leave UNCOMMITTED on disk, move to next request. Earlier authorization
  never carries to a new request.
- **Pre-commit self-check**: before any `git commit`/`push`, re-read the
  latest message verbatim and scan for a trigger. None → present the
  diff/summary, don't push.
- **NOT triggers**: "fix this bug" · "add X" · "thanks/looks good" ·
  test feedback or screenshots · bug reports · analytical questions
  ("왜 그래?") · clarifying questions · "이번건 그냥 넘어가". The
  stop-hook nag about uncommitted changes is NOT permission either.

## 📦 Batch/accumulate (default posture)

- Stack successive requests on disk; ONE ship at the end. Don't push or
  ask "푸시할까?" per task. Pause only on a real obstacle: a request
  contradicts an earlier one, needs a user-only decision, or would force
  reworking stacked edits.
- On a trigger word, ship EVERYTHING accumulated in one pipeline, then
  give a consolidated report.
- **Status footer every turn** (informational, not a push prompt):
  pending → `📦 누적: N개 파일` (N from `git status --short`; list names
  if short); clean → `📦 worktree 누적: 0개 (현재 깨끗)`.

## ✅ Pre-push verification (block on every push)

Run `bash scripts/preflight.sh` FIRST — automates AST syntax (changed
.py), ruff F821, `_HELP_TEXT` ≤4000 + guide-constant render, handler↔help
cross-check. Exit 1 = BLOCKING (syntax/help cap), don't push. F821 +
handler warnings = human glance (lazy-import / forward-ref `"ET.Element"`
strings are expected false positives). `--all` scans every tracked .py.
Preflight covers Python mechanics only — trace shell/cron/Telegram by hand:

- **Shell**: trace fresh-VM / idempotent re-run / race-loss. Every
  notification needs a HARD off-switch on no-op runs (every-minute script
  sends NOTHING when nothing changed). `\n` in double quotes is LITERAL —
  use `$'\n'`, printf, or a real newline. State files must be writable by
  cron user, not cleaned by git/docker, atomic write+read. `bash -n`.
- **Python**: `ast.parse` each modified .py; trace one happy + one error
  path; wrap optional-dep imports in try/except.
- **Cron**: imagine every-minute-for-24h — no spam (fix dedup first).
  List every cron entry touching the same resource; the loser sends nothing.
- **Telegram**: hand-write the message with substituted values (catch
  literal `\n`/`\t`); rate-limit anything that could fire >5×/hr.
- **Persistence**: atomic write; missing→default, corrupt→.bak; host vs
  container path differs (`/app/data` vs `~/Thesis/data`).
- **NEVER**: add a recurring cron entry (existing ones cover all cases;
  one-off pinned-date reminder OK on explicit request) · ship a script
  whose only off-switch is a `data/` state file · use `"\n"` in a bash
  double-quoted string · tell the user "run this yourself".
- When in doubt → STOP, ASK before pushing.

## VM ops — always step-by-step

Any command the user must run manually MUST include: (1) exact
copy-paste command, no placeholders; (2) for editors (nano/vim/less)
every keystroke — cursor move, delete, save, quit, intermediate prompts;
(3) expected output (say so if silent on success); (4) failure recovery
(bad output + how to back out); (5) verification command + its correct
output. Never "open editor and remove the line" / "save and exit" /
"should work now".

## Actionable alerts → ack-button pattern

Deliberate-action alerts (paddle release, ban lifted, cost over budget…)
route through `_send_actionable_alert` (`src/bot.py`): records in
`src/store/notify_acks.py` (atomic JSON), sends `[✅ 확인 / 알람 정지]`
button, hourly `_resend_unacked_alerts` re-fires every 24h until tapped,
`on_ack_callback` (`^ack:`) flips + edits to "→ ✅ 확인됨, 알람 중단".
Stable `notify_id` (e.g. `paddle_v3.4.0`, NO timestamps); dup ids refused
so recurring checks don't spam. Never raw `ctx.bot.send_message`; never
skip the hourly job.

## Automation-first

"from now on / 매번 / 항상 / 자동으로 / 알아서" → cron · docker compose
service · APScheduler hook · git hook/Action. Never "run it yourself".
Check existing `crontab -l` + `docker-compose.yml` first. If the user
must do anything to keep it running, that's a bug.

## Docker — service vs container names

`docker compose <verb> <SERVICE>` uses bare service; `docker
logs/rm/exec/inspect <CONTAINER>` uses full name:

| service | container |
|---|---|
| `bot` | `thesis-bot-1` |
| `forward-listener` | `thesis-forward-listener-1` |
| `dashboard` | `thesis-dashboard-1` |
| `mcp-server` | `thesis-mcp-server-1` |
| `telegram-bot-api` (profile local-api) | `thesis-telegram-bot-api-1` |

## MCP server (external AI clients — Claude Desktop/Copilot, 2026-07-29)

`src/mcp_server/server.py`, port 8083, Streamable HTTP. Exposes 7
read-only tools (`search_knowledge_base`, `list_wiki_topics`,
`get_wiki_page`, `search_notes`, `get_note`, `kg_query`,
`kg_top_entities`) over the RAG archive/wiki/notes/KG. Details + client
config example in `README.md`.

- **Deliberately no Chroma/semantic search** — `search_knowledge_base`/
  `search_notes` are FTS5/substring keyword-only. A second
  `chromadb.PersistentClient` in this process would ~double vector-index
  RAM (the bot already carries 4GB+ from it) on the shared VM — user
  chose keyword-only over that risk (2026-07-29). Don't silently add
  Chroma here without re-raising the RAM tradeoff first.
- **Auth**: single shared `MCP_TOKEN` (`.env`), checked as `Authorization:
  Bearer` by hand-rolled middleware — NOT FastMCP's built-in `auth=`/
  `token_verifier=` (that requires real OAuth resource-server metadata,
  `issuer_url`+`resource_server_url` mandatory; wrong tool for one shared
  secret). Empty `MCP_TOKEN` → server 503s every request, fail-closed.
- `mcp` pinned `>=1.29.0,<2.0.0` in `pyproject.toml` — 2.0.0 restructured
  the package (no `mcp.server.fastmcp.FastMCP` in the same shape) and is
  too new to trust yet. Don't bump past 2.0 without re-verifying the API.
- Read-only by design — never add a write/mutate tool here without
  explicit request (external agents calling this shouldn't be able to
  change the knowledge base, only query it).

## Auto-deploy is ACTIVE — never suggest manual git pull

VM crontab: `auto_pull.sh` every min · `@reboot` http.server 8080 ·
`scheduler_watchdog.sh` */5 · one pinned EPO reminder. `auto_pull.sh` is
the all-in-one deploy (legacy `auto_deploy.sh` consolidated in — do NOT
re-add; running both races and drops 배포 시작/완료 notices). Every min:
fetch branch → `LOCAL==REMOTE` silent exit → else send "🚀 배포 시작" →
`git pull --ff-only` + `docker compose --profile local-api up -d --build
--remove-orphans` (force-remove + retry once on failure) → inspect
`thesis-bot-1`: running → "✅ 배포 완료 <sha>", else "❌ …" + logs.

- **`scheduler_watchdog.sh` is NOT this project** — it monitors an
  unrelated legacy host-process system (`~/telegram-bot`'s
  `scheduler.py`/`bot_listener.py`, non-Docker). Confirmed by reading it
  2026-08-01. Don't touch it or assume it covers `thesis-bot-1`.
- `auto_pull.sh` doubles as this project's OWN watchdog on every no-deploy
  minute (`LOCAL==REMOTE` branch): `check_heartbeat()` — alert-only (by
  design, see its comment) on `data/bot_heartbeat` going stale >10min;
  `check_cpu_overload()` (added 2026-08-01) — **auto-restarts**
  (`docker compose up -d --force-recreate bot`) when `docker stats` CPU%
  stays ≥90% for ~5 consecutive minutes. Added after `thesis-bot-1` sat
  pegged 100%+ CPU for 3 days on `_wconn` lock contention (heavy
  concurrent `fts_upsert` from a forward-listener volume spike) while
  Telegram message-handling was starved via thread-pool exhaustion —
  `check_heartbeat` never caught it because the asyncio loop itself kept
  ticking (heartbeat job stayed fresh) even though user-facing
  responsiveness was dead. Both checks share the deploy-window mute
  (`DEPLOY_STAMP_FILE`) so a legitimate build/boot CPU spike never
  false-triggers a restart.
- After any push, VM redeploys in 60s + Telegram alert. Correct closing
  line: "푸시 완료 (sha). 1분 내 자동 배포 + 텔레그램 알림 갈 거야."
- Never tell the user to `git pull` / `compose up` / `restart`.
- Never add cron (except one-off pinned-date on explicit request).
- Manual exceptions: after a `.env` edit → `docker compose up -d
  --force-recreate <svc>` (restart doesn't re-read env_file). `docker
  logs` for diagnostics is read-only, fine to suggest.

## Branch / push policy

Deploy branch: `claude/personal-rag-knowledge-base-sLSvV`. Work commits
go here; never push to a different branch without explicit permission.

**Repo is SHARED with another AI agent (GitHub Copilot, user's work
account `Noah_Lee@amat.com`) on this SAME deploy branch** — confirmed
by user 2026-07-29 ("지켜봐, 나중엔 그쪽으로 넘어갈 것" — expect
ongoing/increasing Copilot commits here; don't intervene unless asked).
- Before pushing to the deploy branch: `git fetch` then try `git merge
  --ff-only`. If that fails the branch diverged (Copilot pushed) —
  never force-push or discard those commits. Inspect with `git log` /
  `git diff`, dry-run with `git merge-tree <base> <ours> <theirs>` to
  check for real conflicts, then `git merge --no-ff` (ask the user
  first if anything looks contentious). Already happened once cleanly
  (2026-07-29, commit 9821a7b) — same drill each time.
- `.github/copilot-instructions.md` (Copilot's version of this file)
  and `AGENT_GUIDE.md` (separate Copilot-authored doc: Korean,
  non-developer-audience answers, file-edit approval requirement) both
  exist in the repo now. User declined to reconcile them into one
  (2026-07-29) — leave all three docs as-is, don't merge/dedupe them.

## 🪶 Token-lean output (Ponytail lazy-first, 2026-07-10)

같은 아웃풋을 최소 토큰으로. 코드/응답 생성 전 사다리 순서로 자문 —
위 단계에서 해결되면 아래로 내려가지 않는다:
1. 안 만들어도 되나? (기존 동작·명령으로 이미 충분한지)
2. 코드베이스에 이미 있나? — grep 먼저, 재구현 금지
3. stdlib·기존 의존성으로 되나? (새 패키지 추가는 최후)
4. 한 줄/최소 diff로 되나?
5. 그제서야 최소 구현
- 부분 수정(Edit) > 통짜 파일 재생성. bot.py(~10.5k줄) 통독 금지 —
  Grep/Explore(quick)로 필요한 함수만.
- 설명은 결론 먼저, 필요한 만큼만; 안 갈 선택지 나열 금지.
- 절약 대상 아님(non-negotiable): 검증·에러처리·보안·preflight·
  단계별 VM 안내(위 VM ops 규칙) — 여기서 줄이면 버그로 더 비쌈.

## 🔍 완료 보고 전 검증 (verify-before-report)

"완료/배포됨/통과/확인됨" 같은 상태를 보고하기 전, 이번 세션의 실제 tool
결과와 대조한다. 근거 없이 좋게 말하지 않는다:
- 확인 못 한 항목은 "미검증"이라고 명시. 테스트/스크립트가 실패했으면
  실제 출력(에러 메시지)을 인용. 스킵한 단계는 스킵했다고 말할 것.
- 백그라운드 작업(예: `/wiki_fix_confirm`) 진행상황 보고도 동일 —
  progress_cb가 실제로 찍은 값만 보고, 추정치를 완료로 포장하지 않는다.
- 배포 완료 보고는 VM의 실제 "✅ 배포 완료 <sha>" 알림 기준 (VM ops
  섹션의 auto_pull.sh 동작과 일치해야 함), 푸시만 하고 완료라 부르지 않기.

## Standing rules

- Review first; commit only when asked (the gate above wins over any
  workflow assumption).
- **환경·비용·"X는 안 됨" 단정엔 날짜 태그** `(YYYY-MM-DD)` 필수; 관련
  작업을 다시 만질 때마다(최소 분기 1회) 의심·재검증. 쌓이기만 하는
  negative claim은 stale해진다 — 실제로 "₩10만/mo" 비용 가정이 4배 틀린
  채 남아 있었음. wiki는 `wiki.lint()`가 stale single-source >30d를 이미
  자동 플래그(중복 구현 금지).
- Every ingest-pipeline change applies to new ingest AND the retry queue
  — never partial.
- Update `_HELP_TEXT` (`src/bot.py`) on add/rename/remove command, policy
  change, or model-id change. Keep ≤4000 chars (single Telegram message).
- **Never drop a command from the `_HELP_TEXT` listing.** Tight on space →
  condense the prose sections first (핵심·트리거·모델·Retry·문제해결·백엔드;
  info-only prose was already purged 2026-07-02 — help is commands+URLs+운영
  info now); touch command listings only as a last resort + with explicit
  approval.
- `_HELP_TEXT` model ids must match `src/config.py`/`.env`; update them in
  the same commit as any model upgrade.
- **Help + ALL guide constants move together** on any command/feature/
  policy change: `_HELP_TEXT` (≤4000) · `_LOOKUP_GUIDE_TEXT`
  (`/guide_lookup`, all commands) · `_PATENTS_GUIDE_TEXT` · `_PAPERS_GUIDE_TEXT`
  · `_WIKI_GUIDE_TEXT`. Patent change→patents guide, paper→papers,
  wiki→wiki (all if it spans multiple).
- `.env` (VM) holds secrets (bot token, Google key, GitHub PAT, dashboard
  creds). Never echo it; if the user pastes it, warn + recommend rotation.
- **Dashboard ⇄ Telegram parity (user request, 2026-06-24):** a new
  *user-facing knowledge/lookup feature* (search, notes, wiki, KG — things
  you *view/query*) must be surfaced on BOTH Telegram and the dashboard,
  not Telegram-only. Wire a dashboard render + nav link in the same change.
  Pure ops/admin commands (`/failed`, `/queue`, ack flows, etc.) are
  Telegram-only — no dashboard view needed.

## /failed_clear & [🗑] = permanent delete (never re-queue)

`/failed_clear`, `[🗑 #N]` (`/failed`), `[🗑 영구 무시 #N]`
(`/recover_orphans`) all do the same: remove from `_INGEST_FAILED`; add
filename → `_IGNORED_FILENAMES` (`data/ignored_filenames.json`); add URL →
`_IGNORED_URLS` (`data/ignored_urls.json`); orphan scan also deletes the
`data/files/` copy. The item is GONE — not pending/retry/waiting.
Suppressed across orphan scan, URL ingest, forward relay, re-forward
dedup. Revive only by hand-editing those JSONs + restart. Never describe
it as "moved to pending".

## Cost defaults (don't change without asking)

`SUMMARY_MODEL=gemini-2.5-flash-lite` · `ANSWER_MODEL=gemini-2.5-flash` ·
`DEEP_MODEL=gemini-2.5-pro` · `EMBED_MODEL=gemini-embedding-001` ·
`EMBED_BACKEND=gemini` (BGE-M3 path disabled — CPU bottleneck caused
flood-bans) · `CHUNK_TOKENS=1000` `CHUNK_OVERLAP=150` · `TOP_K=10` ·
OCR `DPI=100` `AUTO_CAP=7` `SPARSE_THRESHOLD=800` `PROBE_PAGES=3`
`PROBE_MIN_TEXT=300`.

## Gemini backend = Vertex AI (since 2026-06-19, NOT AI Studio)

AI Studio's api_key path was excluded from the Free Trial credit (429'd
at ₩0); Vertex is Free-Trial-eligible (verified).

- All `genai.Client(...)` go through `config.make_genai_client()`.
  `GEMINI_BACKEND=vertex` → `Client(vertexai=True, project, location)`;
  `aistudio` = old key path. Don't regress the 7 call sites to inline
  `api_key=`.
- VM `.env`: `GEMINI_BACKEND=vertex`,
  `VERTEX_PROJECT=gen-lang-client-0325676393`,
  `VERTEX_LOCATION=us-central1`. `GOOGLE_API_KEY` now optional.
- Auth = ADC, no key file: SA
  `722358979517-compute@developer.gserviceaccount.com` has
  `roles/aiplatform.user`, OAuth scope `cloud-platform`. Containers need
  `extra_hosts: metadata.google.internal:169.254.169.254` (custom
  `dns: 8.8.8.8` can't resolve it) on bot + forward-listener + dashboard.
- Models unchanged (flash-lite/flash/pro + gemini-embedding-001 3072-dim).
  Vertex price == AI Studio (cost.db stays accurate). web_search
  grounding (`types.GoogleSearch()`) works (~30s latency is normal).

## Billing = new-account Free Trial (~mid-Sept 2026)

Account `01A847-50A403-149C08` ("결제계정-2"), $300 / 90 days. Projects
`gen-lang-client-0325676393` (VM/bot/stock) + `gen-lang-client-0957886559`
(Gemini API) link to it → VM + network + Vertex all draw from it.

- Hard limit: $300 OR 90 days, whichever first; at ~₩10만/mo the 90-day
  clock (~mid-Sept 2026) wins. Then pay-as-you-go or services stop.
- Balance/₩0-net proof is Console-only (billing → 보고서/크레딧). Budget
  alert recommended, not yet set.
- Don't chase another new account for fresh credits (dup card/phone
  detection, ToS risk; already did old→new once).

## VM right-sizing (2026-06-19)

**리전 이동(2026-06-30): 서울 → us-central1.** 새 VM `telegram-bot-usc`,
zone **`us-central1-b`** (a존 e2-standard-2 재고부족으로 b존), 새 고정 IP
**`136.115.27.77`**. Vertex와 같은 리전이라 egress↓ + 컴퓨트 ~25%↓.
gcloud 명령의 zone은 이제 `us-central1-b`.
**서울 잔재 정리 완료(2026-07-04):** 옛 VM `telegram-bot`·IP
`34.50.23.221`·서울발 스냅샷 7개·`weekly-regional`·`default-schedule-1`
정책 전부 삭제. 서울 리전 과금 0. 새 VM 백업 = `weekly-usc` 정책(주간)
+ 수동 베이스라인 `telegram-bot-usc-manual-20260704` (weekly-usc 첫
회차 확인 후 삭제 가능) + 매일 GCS 백업(backup.py).

`telegram-bot-usc` = **e2-highmem-2** (2 vCPU / 16 GB; 2026-07-05에
e2-standard-2/8GB에서 승급 — chroma HNSW가 임베딩을 전부 RAM에 들고
있어(354k청크≈4.4GB, 학습마다 증가) 8GB 박스가 93~95%에서 20~40분씩
스톨). Static IP `136.115.27.77` (구 `34.50.23.221`) survives stop/start.
bot `mem_limit: 11g` + `INGEST_SEM_CAPACITY=4` — **RAM은 늘었어도 2 vCPU
가 병렬 상한: 6으로 올렸다가 백로그 드레인 때 GIL 기아로 루프가 10분+
무음(2026-07-05). 코어 추가 전엔 4 고정** (.env도 4로 핀). Dashboard index rebuild = 15s. Host
SHARED with `~/stock` + `~/stock-trade` (separate host-venv bots, also
Vertex via `GOOGLE_GENAI_USE_VERTEXAI=true`) — don't eat all RAM. Those
aren't `higgack/thesis` (their `gemini_cache_manager.py:137` still passes
`api_key=` → 401, their own problem).

## Wiki system (don't regress)

- **Daily budget** ₩1,000 (`config.WIKI_DAILY_BUDGET_KRW`). Single source;
  no hardcoded fallbacks; no `.env` override.
- **Batch** hourly on the hour KST: `run_repeating(3600, first=next
  top-of-hour)`, recomputed each boot. Daily ₩ cap resets KST midnight;
  once hit, rest of day no-ops. `WIKI_BATCH_HOUR` DEPRECATED (back-compat).
- **Digest throttle**: "what it learned" digest once per KST day
  (`wiki.digest_sent_today`/`mark_digest_sent`, `data/wiki_last_digest.json`).
  Budget-block + contradiction alerts exempt (already deduped). Fails open.
- **Lint** (₩0, no LLM): `wiki.lint()` — index + one pass/page; flags
  stale single-source (1 doc, >30d → merge/delete cand), unresolved
  `## ⚠️ 검토 필요`, missing pages (index record whose `.md` is gone).
  Persists `data/wiki_lint.json`. Refreshed hourly by `_wiki_batch_job` +
  `/wiki_lint`. Dashboard health panel (`_render_lint_panel`) + Telegram
  cmd. NOT an actionable alert. (Orphan cross-link check dropped = noise;
  vault `log.md` changelog deliberately not added.)
- **Deleted topics permanent**: `data/wiki_deleted_topics.json`;
  `wiki.is_topic_deleted()` checked at `enqueue`/`backfill`/`run_batch`.
  Revive only by hand-edit + restart.
- **Drain temp budget clears immediately**: `drain_queue()` try/finally
  `clear_temp_budget()` (skipped only on `CancelledError`) → reverts to
  ₩1,000 instantly, not at midnight.
- **Query-first** (`WIKI_QUERY_FIRST=1` default): Q&A includes wiki
  knowledge before vector-only RAG.
- **Channel dup suppression**: an all-duplicate channel ingest sends no
  "♻️ 이미 있음" when `notify_chat_id != msg.chat_id`.
- **SQLite hardening**: all sites (cost/meta/qna/pending.db + dashboard)
  use `timeout=30` + `PRAGMA journal_mode=WAL`.
- **Merge = append-only + periodic consolidation** (`_merge_topic` by
  page size): new/empty → LLM; <30K chars → append dated section (₩0);
  ≥30K → LLM rewrite. `CONSOLIDATION_CHARS=30000`, `MAX_PAGE_CHARS=30000`,
  `MERGE_MAX_TOKENS=12000`. Consolidation ~₩55, fires every 1–2 wk/topic.
  Don't revert to full-rewrite-every-merge (lossy — 236-source "AI" page
  lost early info).
- **Topic split gate**: `_split_multi_topic` splits single spaces only
  when ALL fragments are known topics (index/alias); explicit delimiters
  `,;/·` always split.

## OCR backend (local worker pre-built, dormant)

`src/ingest/ocr_client.py` routes via `OCR_BACKEND` (default `gemini`).
`local` = every page through PaddleOCR `ocr-worker` (₩0/page, −10-20% on
charts); `hybrid` = PaddleOCR then Gemini Vision fallback if confidence
<0.8 or text <50 chars.
- Activate: `docker compose --profile ocr-local up -d ocr-worker` → set
  `.env OCR_BACKEND=hybrid` → `docker compose up -d --force-recreate bot`.
- Deactivate: `.env OCR_BACKEND=gemini` → `docker compose stop ocr-worker`
  → force-recreate bot.
- File queue `data/ocr_queue/` ↔ `data/ocr_results/` (atomic + heartbeat).
  PaddleOCR pin 2.7.3 (v3.x rewrite notes in `ocr-worker/worker.py`).

## Resume-safety invariants

All state files use `_atomic_write_json` (tmp→fsync→rename) + `.bak` load
fallback. Live ingest entry points call `_enqueue_with_inflight` before
the pipeline and `_finish_inflight(done/retry)` after (mid-kill
recoverable). `docker-compose.yml` `stop_grace_period: 120s`.

## Backlog (not active until the user asks)

- **Wiki 팩트 테이블 (Phase 2)**: extract atomic facts to a `wiki_facts`
  SQLite table `(topic, claim_text, source_doc_id, date, confidence)`;
  page becomes a rendered view → no info loss + DB-query contradiction
  detection. ~₩2/doc (Flash-Lite). Start only if append proves
  insufficient for information preservation.
- **CodeGraph trial**: when `src/bot.py` hits ~15k lines (now ~10.5k),
  trial `npx @colbymchenry/codegraph` to cut exploration token cost. Low
  impact (cost is dominated by bot.py size + conversation length, not
  multi-file exploration). Compare token use before/after to decide.
