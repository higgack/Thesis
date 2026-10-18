# Auto-deploy watchdogs and loop-blocking incidents

Moved VERBATIM out of `AGENTS.md` (`## Auto-deploy is ACTIVE, bullets 452-532`) on 2026-10-18 — text as of commit bf28456 — so the always-loaded rules file stays short. `AGENTS.md` keeps the rules and links here; nothing below was reworded. The deploy mechanics, closing line, and manual-exceptions list stayed in AGENTS.md verbatim.

---

- **`scheduler_watchdog.sh` is NOT this project** — it monitors an
  unrelated legacy host-process system (`~/telegram-bot`'s
  `scheduler.py`/`bot_listener.py`, non-Docker). Confirmed by reading it
  2026-08-01. Don't touch it or assume it covers `thesis-bot-1`.
- `auto_pull.sh` doubles as this project's OWN watchdog on every no-deploy
  minute (`LOCAL==REMOTE` branch): `check_heartbeat()` — **auto-restarts**
  on `data/bot_heartbeat` going stale >10min. Was alert-only through
  phase 1 (2026-06); Copilot promoted it to phase 2 auto-restart in
  `8c426cf` (2026-08-14) once phase 1 proved false-positive-free —
  alert → force-recreate → stamp `DEPLOY_STAMP_FILE` to mute the other
  watchdogs while it settles → report back, with a 30-min cooldown
  against restart thrash. (The 2026-05-27 removal of an earlier
  auto-restart was a BM25 warm-up restart loop; that path is gone.)
  So a "heartbeat stale → auto-restarting" Telegram pair is this check,
  and a "CPU ≥90% → auto-restarting" pair is the next one.
  `check_cpu_overload()` (added 2026-08-01) — **auto-restarts**
  (`docker compose up -d --force-recreate bot`) when `docker stats` CPU%
  stays ≥90% for ~5 consecutive minutes, guarded by the same
  container-uptime warm-up check as `check_heartbeat()` (added
  2026-08-09, so a fresh restart's own warm-up CPU spike can't trigger a
  restart loop). Added after `thesis-bot-1` sat pegged 100%+ CPU for 3
  days on `_wconn` lock contention (heavy concurrent `fts_upsert` from a
  forward-listener volume spike) while Telegram message-handling was
  starved via thread-pool exhaustion — `check_heartbeat` never caught it
  because the asyncio loop itself kept ticking (heartbeat job stayed
  fresh) even though user-facing responsiveness was dead. Both checks
  share the deploy-window mute (`DEPLOY_STAMP_FILE`) so a legitimate
  build/boot CPU spike never false-triggers a restart.
- **NEVER run `_run_memory_cleanup()` ungated** (`bot.py`). It is
  `gc.collect()` (stop-the-world, HOLDS THE GIL for the whole
  traversal) + `malloc_trim(0)` (walks the entire glibc arena). Its old
  docstring claimed "cheap (<50ms)" — true on a small heap, badly wrong
  at this process's 7GB+ RSS (Chroma HNSW keeps every embedding
  resident). Measured scaling: gc.collect() ≈ 100ms per million tracked
  objects. `_periodic_memory_cleanup` called it **unconditionally every
  3 min on the event loop**, which is what produced the 2026-08-09
  paired alerts — "CPU 100.53% 5분+" (gc is CPU-bound single-threaded)
  and "heartbeat 21min stale — event loop hung" (the loop couldn't tick,
  so `_write_heartbeat` stopped stamping). It worsened as the corpus
  grew. Fixed by gating it on `_MEM_CLEANUP_THRESHOLD` (0.90) like the
  4 other call sites already were — this was the lone ungated one — and
  offloading it. Offloading alone is NOT the fix: gc.collect() holds
  the GIL wherever it runs, so the point is to not run it needlessly.
- **`_write_heartbeat` runs ON the loop deliberately** — that's what
  makes a stale heartbeat mean "loop wedged" rather than "thread pool
  busy". So a stale-heartbeat alert always means something blocked the
  loop thread; look for sync work on the loop, not for a slow worker.
  Per-tick sync SQLite is the usual culprit: the dash query/ingest
  workers' `claim_pending` (2s/3s ticks, a WRITE, `timeout=30` on a DB
  the dashboard container also writes) ran inline and could freeze the
  loop up to 30s per call ~72k times/day even while idle — offloaded
  2026-08-09. Their `complete()`/`release()` calls are the same class
  but fire only on real work, so they were left inline.
- **`meta.py`'s `_W_LOCK` must never be taken on the loop** — and
  **`preflight.sh` section 6 now enforces it** (BLOCKING, exit 1). Every
  function that opens `_wconn()` (12 of them: `upsert_doc`, `fts_upsert`,
  `chunk_embed_remember`, `summary_cache_remember`, …) grabs that lock,
  and the lock's own comment states the contract: *"callers already run
  in asyncio.to_thread workers, so blocking here never touches the event
  loop."* Two call sites violated it and nothing caught them until
  `data/loop_stalls.log` printed the stack on 2026-08-26 — the loop sat
  431s inside `with _W_LOCK` while worker-thread `fts_upsert` held it
  during a bulk ingest. `summarize.py`'s `summary_cache_remember` was the
  one in the dump; the worse one was `vector.py`'s `chunk_embed_remember`,
  sitting in the ingest hot path directly below two carefully-offloaded
  `to_thread` calls (the Chroma write and the FTS upsert) — the third
  SQLite call in the same function was simply missed. Both offloaded, and
  their lock-free-but-still-disk-I/O read partners (`summary_cache_get`,
  `chunk_embed_lookup`) with them. The check reads the writer list out of
  `meta.py` at runtime, so a new `_wconn()` function is covered
  automatically; a reference passed to `to_thread(...)` is not a Call node
  and is correctly ignored.
- **Vision-OCR concurrency is capped process-wide** (`ocr_client.py`'s
  `_GLOBAL_OCR_SEM`, default 6, 2026-08-09) — the per-PDF
  `ThreadPoolExecutor(max_workers=7)` in `loaders.py` only bounds ONE
  PDF's page fan-out; several large PDFs OCR'ing at once (up to
  `INGEST_SEM_CAPACITY` concurrently) could otherwise pile up to
  7 × capacity threads — 28 at the 4 cap in force then, 21 at today's 3 —
  and reproduce the same GIL-starvation freeze pattern above
  (recurred 2026-08-04). Don't remove the semaphore without re-testing
  concurrent large-PDF ingest first.
