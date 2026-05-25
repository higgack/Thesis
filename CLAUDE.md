# Project notes for Claude

Standing instructions / project-specific facts that need to survive
context compaction. Anything Claude should ALWAYS remember while
working on this repo.

## Commit / push gate (CRITICAL — top priority rule, VIOLATED REPEATEDLY)

Do NOT run `git commit`, `git push`, or anything that triggers a
deploy (touching `pyproject.toml`, `.env`, Dockerfile, anything the
auto-deploy cron will rebuild) UNLESS the user has used an explicit
trigger word in their MOST RECENT message:
  • "커밋", "푸시", "배포", "deploy", "commit", "push", "올려",
    "내보내", "ship", "release"

Earlier authorisation does NOT carry over to a new request. Every
new task starts in "edit-only" mode:

1. Make the file changes.
2. Run pre-push verification (next section).
3. Show the user a brief summary of what changed.
4. STOP and wait for the explicit trigger word.

### MANDATORY pre-commit self-check (paste-the-message rule)

Before running ANY `git commit` or `git push` Bash call, do this
explicit mental step (no shortcuts):

  a. Re-read the user's most recent message verbatim.
  b. Scan it for ONE of the trigger words above (Korean or English,
     exact match, case-insensitive). Trigger words appearing in your
     OWN earlier output don't count. Trigger words inside a quoted
     code snippet or pasted log the user shared don't count either.
  c. If no trigger word: STOP. Present the diff/summary instead.
     Ask "푸시할까?" only if you genuinely need confirmation —
     otherwise just end the turn cleanly.

This step exists because the agent has violated the gate multiple
times in single sessions, always with the same self-justification
pattern ("the user clearly wants this fixed, so they'd want it
pushed"). They wouldn't. They want it presented first.

### Forbidden inferences (none of these are trigger words)

  • "fix this bug" ≠ permission to push the fix
  • "please add X" ≠ permission to deploy X
  • "thanks, looks good" ≠ permission to push something pending
  • A prior session's `/ultrareview` or similar ≠ blanket push licence
  • Test result feedback ("이렇게 나왔어", "여기 결과", screenshot)
    ≠ permission to push the next fix
  • Bug report or unexpected output ≠ permission to push patch
  • Analytical question ("왜 그래?", "이건 데이터 부족인가?")
    ≠ permission to push
  • Asking a clarifying question ("Q: should X work?") ≠ permission
    to push your answer
  • "이번건은 그냥 넘어가" (let this one slide) about a past unauthorized
    push ≠ blanket permission for future pushes

### Common violation pattern (the one that keeps happening)

After a multi-step implementation: edits → syntax check → diff
preview → it feels "done" → the agent reflexively reaches for
`git commit && git push`. THIS IS WRONG. "Done with edits" ≠
"ready to push". The default end-of-task state is uncommitted
changes on disk + a summary message to the user. Pushing without
trigger overrides their explicit workflow.

If the auto-stop-hook nags about uncommitted changes, that is NOT
permission to push. It's just a reminder; the gate still requires a
user trigger word.

The user has lost work and time multiple times because the agent
pushed unprompted. This rule overrides any in-task assumption about
workflow speed, "completion feel", or hook nagging.

## Pre-push verification (MANDATORY — block on every push)

User has been burned multiple times by shipped-then-immediately-
broken code: shell scripts with `\n` literal in double-quoted
strings, state files that don't persist and trigger infinite
notification loops, scheduler scripts that fire every minute
forever, etc. Each incident costs the user real time and trust.

Before EVERY git push, walk this checklist:

### 1. Shell scripts (cron / deploy / scheduler)
- [ ] Trace the script with three concrete value sets:
      (a) first run on a fresh VM, (b) idempotent re-run, (c) the
      race-loss case (another script just did the work).
- [ ] For every notification/side-effect, confirm the path that
      fires it has a HARD off-switch on no-op runs. If the script
      runs every minute, what does it send on minute N when
      nothing changed? Must be NOTHING.
- [ ] `\n` in double-quoted strings is LITERAL. Use `$'\n'`
      (ANSI-C quoting), printf `\n`, or actual newline in the
      source. NEVER ship `"...\n..."` to Telegram / curl / log.
- [ ] State files (anything written to disk to survive between
      runs) MUST be in a path that's:
        • writable from the cron user's context (test with `ls -la`),
        • NOT cleaned by `git reset --hard`, `git clean -fdx`,
          docker volume re-create, or a parallel cron job,
        • atomically written (tmp + rename), atomically read.
- [ ] `bash -n script.sh` to lint syntax before commit.

### 2. Python
- [ ] `python3 -c "import ast; ast.parse(open('path').read())"`
      on every modified .py.
- [ ] For new code paths, mentally trace one happy path + one
      error path with concrete values.
- [ ] Imports for new optional deps wrapped in try/except so a
      missing module doesn't break the rest of the bot.

### 3. Cron-scheduled code
- [ ] Pretend the script runs every minute for 24 hours. Does the
      user get spammed? If yes, that's a bug — fix the dedup
      condition BEFORE pushing.
- [ ] Multi-script races: list every cron entry that touches the
      same resource. Spell out who wins, what the loser does.
      Loser must NOT send notifications about work it didn't do.

### 4. Telegram notifications
- [ ] Test the exact message string with substituted values
      (write it out by hand, look for literal `\n`, `\t`, broken
      escape sequences).
- [ ] No new high-frequency notification source. If it could fire
      more than ~5×/hour under any condition, add a rate limit
      BEFORE pushing.

### 5. Persistence (JSON / SQLite / state files)
- [ ] Atomic write (`_atomic_write_json` or tmp + os.replace).
- [ ] Recovery on read: missing file → empty default, corrupt
      file → log + .bak fallback (per existing pattern in bot.py).
- [ ] Path consistency: same path from host cron AND from inside
      container? Bind-mount root differs (/app/data vs ~/Thesis/
      data) — make sure your code uses the right one for its
      runtime context.

### 6. Forbidden patterns (lessons learned)
- [ ] NEVER add a recurring cron entry. The existing ones (auto_pull.sh
      every minute, scheduler_watchdog.sh every 5 minutes, @reboot
      http.server) cover all current use cases. One-off pinned-date
      reminders (e.g. `0 0 18 5 * curl ... EPO reminder`) are OK when
      the user explicitly asks for a future-date alert.
- [ ] NEVER ship a script whose only off-switch is a state file
      in `./data/` — that dir gets touched by docker, git, etc.
- [ ] NEVER use `"\n"` in a bash double-quoted string and assume
      it's a newline.
- [ ] NEVER tell the user "run this command yourself". See the
      Automation-first principle below.

### When in doubt → STOP, ASK before pushing.
A 10-second confirmation costs nothing. A botched push that spams
the user's Telegram every minute costs trust and forces an
emergency revert. Always pick the confirmation.

## VM operation instructions — always step-by-step

When the user has to run something on the VM that can't be
automated yet (interactive editor, one-time setup, recovery
after a manual config change), the response MUST include:

1. **Exact command** — copy-pasteable, no placeholders the user
   has to substitute.
2. **For interactive editors (nano, vim, less, etc.)** — every
   keystroke spelled out:
     • how to move the cursor to the right line
     • the exact shortcut to delete / save / quit
     • what prompt appears between steps
3. **Expected output** — the literal text that confirms success.
   If a step is silent on success, say so explicitly.
4. **Failure recovery** — what does it look like when it goes
   wrong? Show the bad output and how to back out (e.g. `Ctrl+C`
   to abort nano without saving).
5. **Verification step** — a follow-up command (`crontab -l`,
   `docker logs`, `git status`, etc.) plus what its output
   should look like when correct.

User's exact request: '앞으로도 꼭 다른것도 이렇게 알려줘.
이것도 박아넣어.' Translation: 'always give VM steps in this
much detail going forward. Lock this in too.'

Anti-patterns to avoid:
- 'Open the file in an editor and remove the line.' (Which
  editor? Which keys?)
- 'Save and exit.' (How? `:wq`? `Ctrl+O`+`Ctrl+X`? `:x`?)
- 'It should work now.' (How do I verify? What if it didn't?)

## Actionable alerts — always use the ack-button pattern

When the bot needs the user to take a deliberate action (paddle
release out, ban lifted, queue full, monthly cost over budget,
etc.), DO NOT use a one-shot Telegram send_message. The user can
miss it while away from chat, and a single notification has no
state — you can't tell whether the user saw it.

Always route through `_send_actionable_alert` (defined in
`src/bot.py`) which:
  1. Records the alert in `src/store/notify_acks.py` (atomic
     JSON, persisted across bot restarts).
  2. Sends the message with an inline `[✅ 확인 / 알람 정지]`
     button.
  3. The hourly `_resend_unacked_alerts` job re-fires the same
     message every 24 hours until the user taps the button.
  4. `on_ack_callback` (registered on pattern `^ack:`) flips
     the record and edits the message to "→ ✅ 확인됨, 알람 중단".

Each alert needs a stable `notify_id` (e.g. `paddle_v3.4.0`).
Duplicate ids are refused so a recurring check (weekly paddle scan,
hourly cost watchdog, etc.) can call `_send_actionable_alert`
freely without spamming — the ack store dedups.

DO NOT:
  - Send actionable alerts via raw `ctx.bot.send_message` — the
    user will miss it the day they're not on Telegram.
  - Use a notify_id that includes timestamps (e.g.
    `paddle_2026-05-15`); the resend pattern depends on the id
    being identifiable across days.
  - Skip the hourly job registration; without it the alert never
    re-fires after the first send.

## Automation-first principle (always default to schedulers)

When the user wants something to happen "from now on" / "every time"
/ "automatically", the answer is ALWAYS one of:
  - cron job (system-level, every minute / hour / day)
  - docker compose service (restart: unless-stopped)
  - APScheduler hook inside the bot (already used for retry tick,
    memory cleanup, pending promotion)
  - git hook / GitHub Action

NEVER answer with "run this command yourself when needed". The user
has explicitly rejected that posture multiple times. If a task
involves a recurring action, the default deliverable is the
scheduler config + the script it runs, not a manual recipe.

Verification checklist before responding:
  1. Did the user say "from now on / 매번 / 항상 / 자동으로 / 알아서"?
     → YES means: automation only, no manual fallback offered.
  2. Is there an existing scheduler that should handle this?
     → Check `crontab -l` and `docker-compose.yml` mentally first.
  3. After the work, does the user have to do anything to keep
     it running?
     → If yes, that's a bug — either wire it into cron / docker /
     APScheduler, or explain why automation isn't possible.

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

**VM has these cron entries** (verified via `crontab -l`):

```
* * * * * cd /home/higgack/Thesis && bash scripts/auto_pull.sh
@reboot cd ~/.summary_archive && nohup python3 -m http.server 8080 --bind 0.0.0.0 > /dev/null 2>&1 &
*/5 * * * * bash /home/higgack/scheduler_watchdog.sh >> ~/deploy.log 2>&1
0 0 18 5 * source /home/higgack/Thesis/.env && curl -s ".../sendMessage" -d "text=📬 EPO OPS Phase B 시작..."
```

`scripts/auto_pull.sh` is the **all-in-one deploy script**. The legacy
`scripts/auto_deploy.sh` row (from an earlier setup) has been
consolidated into auto_pull.sh — do NOT re-add it. The script itself
has an inline comment ("legacy auto_deploy.sh won the race") warning
that running both creates a race that drops 배포 시작 + 배포 완료
notifications. Stick to auto_pull.sh alone.

`scripts/auto_pull.sh` (matching this branch's HEAD) does, every minute:
  1. `git fetch origin <BRANCH>` — checks for new commits.
  2. If `LOCAL == REMOTE` → silent exit (no spam).
  3. If new commits → send "🚀 배포 시작: <old> → <new>\n<title>" to
     TELEGRAM_OWNER_ID via curl.
  4. `git pull --ff-only` + `docker compose --profile local-api up -d
     --build --remove-orphans`. On compose failure, force-remove stale
     containers and retry once.
  5. `docker inspect thesis-bot-1` → if `running` send "✅ 배포 완료
     <sha> <title>"; else send "❌ 컨테이너 상태 …" with `docker
     compose logs --tail=10`.

What this means for the agent:
- After ANY `git push`, the VM auto-pulls the branch within 60 seconds
  and recreates the bot/forward-listener/dashboard containers.
- The user sees "🚀 배포 시작" → "✅ 배포 완료 <sha>" on Telegram for
  every successful redeploy.
- **NEVER** tell the user to run `git pull` / `docker compose up -d` /
  `docker compose restart` themselves. They've heard that 10× already
  and it wastes their time.
- **NEVER** add new cron entries (only exception: a one-off reminder
  pinned to a specific date+month like the EPO line above is OK if
  explicitly requested). The repeating ones cover all current cases —
  adding duplicates causes race conditions.
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
  먼저하고 내가 요청하면 커밋"). See the **Commit / push gate** section
  at the top — it has authority over every other workflow assumption.
- Every ingest-pipeline change must apply equally to new ingest AND
  retry queue — this is the default, never partial.
- Always update `_HELP_TEXT` in `src/bot.py` when adding / renaming /
  removing a command or changing user-visible policy. Help must stay
  under the 4000-char soft-split limit so it renders as a single
  Telegram message.
- **Help AND every guide constant must move together on any command /
  feature / policy change.** Four guide surfaces today:
    • `_HELP_TEXT`            — one-line summary, 4000-char cap
    • `_LOOKUP_GUIDE_TEXT`    — `/guide_lookup`, all commands detail,
                                no cap (auto-splits)
    • `_PATENTS_GUIDE_TEXT`   — `/patents_guide`, patent features only
    • `_PAPERS_GUIDE_TEXT`    — `/papers_guide`, paper features only
  Workflow:
    1. Change behaviour / add a command.
    2. Update `_HELP_TEXT` (one-line entry under right category).
    3. Update `_LOOKUP_GUIDE_TEXT` (full prose section).
    4. If it's a patent change → also `_PATENTS_GUIDE_TEXT`.
       If it's a paper change → also `_PAPERS_GUIDE_TEXT`.
       (Both if it spans both, e.g. a new shared filter.)
  Skipping any of these is a regression — users discover commands
  through these surfaces. CI gate: the pre-push checklist's syntax
  pass already runs `len(_HELP_TEXT) ≤ 4000`; add the same render
  check for the guide constants when extending them substantially.
- `.env` on the VM contains secrets (Telegram bot token, Google API
  key, GitHub PAT, dashboard creds). Never echo its contents back in
  chat. If the user pastes them, warn and recommend rotation
  immediately.

## /failed_clear and [🗑] semantics — permanent delete, never re-queue

Both the bulk `/failed_clear` command and the per-item `[🗑 #N]`
button (in `/failed`) and the per-item `[🗑 영구 무시 #N]` button
(in `/recover_orphans`) do the SAME thing:

1. Remove the row(s) from `_INGEST_FAILED`.
2. Add the filename to `_IGNORED_FILENAMES` (persisted in
   `data/ignored_filenames.json`).
3. Add the URL to `_IGNORED_URLS` (persisted in
   `data/ignored_urls.json`).
4. For orphan scan: also delete the file from `data/files/` so the
   next scan doesn't see it at all.

After any of these actions the item is GONE — it does NOT move to
pending, retry queue, or any other waiting list. It is permanently
suppressed across:
  • orphan scan
  • URL ingest pipeline
  • forward-listener relay
  • re-forwarded telegram messages (text/file dedup)

Only way to revive: edit `data/ignored_filenames.json` /
`data/ignored_urls.json` by hand, restart the bot. There is no
"undo /failed_clear" command.

DO NOT explain this as "moved to pending" or "queued for review"
or any other intermediate state. The user has explicitly confirmed
this is the intended semantics ("그냥 없어지는거야 아무것으로도
pending 이나 대기로 남지 않고 꼭 명심해").

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

## Backlog / future considerations (not active work)

Low-priority items parked here so they survive context compaction.
None of these are authorised for work until the user explicitly asks.

- **CodeGraph 시범 적용** — when `src/bot.py` grows to ~15k lines
  (currently ~10.5k), trial `npx @colbymchenry/codegraph` (local
  semantic knowledge graph, zero config, no cloud/API key). Goal:
  cut agent exploration token cost on the single large file (handler
  lookup, callback patterns, command registration) without reading
  the whole file each session. Not impactful now — this project's
  context cost is dominated by `bot.py` size + conversation length,
  not multi-file exploration, so don't expect the marketed ~59%
  token reduction. After indexing, compare agent-session token use
  before/after to decide whether to keep it.
