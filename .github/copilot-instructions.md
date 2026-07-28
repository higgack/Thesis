# Repo instructions for AI coding agents (GitHub Copilot)

This repo is also actively developed with Claude Code, whose rules live in
`CLAUDE.md` (repo root, auto-loaded by Claude Code only — read it too, it's
the more detailed/authoritative source; this file is a Copilot-readable
distillation of the same invariants). If the two ever disagree, treat
`CLAUDE.md` as authoritative and flag the conflict instead of guessing.

## Branch strategy — read this first

- **`main` is stale.** It contains only the original "Initial commit" from
  2026-04-09 and has never been updated since — do NOT branch from `main`,
  it's missing essentially the entire codebase's history.
- **`claude/personal-rag-knowledge-base-sLSvV` is the real deploy branch.**
  A cron job on the production VM (`auto_pull.sh`, runs every minute) watches
  this branch and auto-deploys (git pull + docker compose rebuild) within
  ~60s of any push to it.
- Work on a feature branch, then fast-forward or PR into
  `claude/personal-rag-knowledge-base-sLSvV` when ready to ship. Don't push
  directly to it without the user's explicit go-ahead — a push there is a
  production deploy, not a routine commit.
- Other `claude/*` branches you may see are leftover one-off session
  branches from prior Claude Code sessions; ignore them unless the user
  points you at one specifically.

## What this project is

A personal Telegram bot + RAG knowledge base with four subsystems in one
codebase:

1. **Main RAG ingest** (`src/ingest/`) — URL / PDF / PPTX / DOCX / XLSX /
   image / audio / video / YouTube / plain text → summarize + embed →
   Chroma (vectors) + SQLite (`meta.db`) + Obsidian vault markdown.
2. **LLM wiki** (`src/store/wiki.py`) — ingested docs get auto-synthesized
   into accumulating per-topic wiki pages (append below a char threshold,
   LLM-rewrite/consolidate above it), capped by a daily ₩ budget.
3. **Knowledge graph** (`src/store/kg.py`) — entity/relation fact triples
   auto-extracted per doc (flash-lite, daily-budget capped).
4. **Study notes** (`src/notes/`) — a separate dedicated Telegram channel
   whose posts become structured "notes" (concept map + organized write-up,
   not a summary), tagged into one of 9 categories.

`src/dashboard/` renders all four as a web UI (static regen every ~15s from
a bot-container process + a small separate write-server for interactive
actions like delete/category-change).

**Stack**: Python 3, `python-telegram-bot`, Google Gemini via **Vertex AI**
(`google-genai` SDK, NOT the AI Studio api_key path), ChromaDB, several
SQLite databases (meta/kg/notes/pending/cost/...), Docker Compose, a single
GCE VM (`telegram-bot-usc`, us-central1-b, e2-highmem-2, 2 vCPU/16GB).

## Hard invariants — each of these caused a real production incident when violated

- **Always call Gemini through `config.make_genai_client()`.** Never inline
  a new `genai.Client(api_key=...)` call site — that's the AI Studio path,
  which 401s under this project's Vertex-only billing setup.
- **Never run blocking sync work directly on the event loop inside an
  `async def`.** Wrap it in `asyncio.to_thread(...)`. An unwrapped sync call
  (a 5000-row Python-level DB scan, a JSON read+rewrite under a lock, etc.)
  starves the loop badly enough to block `asyncio.wait_for`'s own
  cancellation timer — this caused three separate 15–35 minute ingest
  stalls in one week before being systematically fixed. See
  `src/ingest/pipeline.py`'s `_ingest()` for the pattern (every DB/file call
  wrapped, with a comment explaining why).
- **State files are written atomically**: write to a `.tmp` path, then
  `os.replace()` over the real path. A crash mid-write must never corrupt
  the only copy. Load with a `.bak` fallback where one exists.
- **Model tiers are fixed, don't casually change them**: `SUMMARY_MODEL=
  gemini-2.5-flash-lite`, `ANSWER_MODEL=gemini-2.5-flash`, `DEEP_MODEL=
  gemini-2.5-pro`, `EMBED_MODEL=gemini-embedding-001`. The whole cost model
  is built on this tiering; changing a call site's model tier is a cost
  decision, not a refactor — ask first.
- **LLM rewrites that overwrite accumulated content need a length/loss
  guard.** A full-page LLM rewrite (e.g. wiki consolidation) that comes
  back under ~50% of the original length is almost certainly truncated or
  over-compressed — refuse the write and fall back to a safe append instead
  of silently losing history (see `src/store/wiki.py`
  `_consolidate_topic`/`reintegrate_contradictions` for the actual guard;
  this class of bug already caused a real data-loss incident once).
- **Resume-safety**: any live ingest path enqueues an in-flight marker
  before starting and clears it after — a mid-crash restart must recover
  cleanly (retry or resume), not silently drop or duplicate the item.

## Before pushing anything

Run `bash scripts/preflight.sh` (or `--all` for a full repo scan). It checks:
AST syntax on changed `.py` files, ruff F821 (undefined names), the
`_HELP_TEXT` 4000-char Telegram cap plus render checks on the guide-text
constants, and that every registered command handler appears somewhere in
`_HELP_TEXT`. A non-zero exit is blocking — don't push past it.

**Never skip hooks or verification (`--no-verify`, disabling a lint rule to
make a check pass, etc.)** — fix the underlying issue instead.

## Docs/help text that must move together with a feature change

Adding, renaming, or removing a command, changing a policy, or changing a
model id requires updating, in the same change:
- `_HELP_TEXT` (`src/bot.py`, ≤4000 chars — single Telegram message)
- `_LOOKUP_GUIDE_TEXT`, `_PATENTS_GUIDE_TEXT`, `_PAPERS_GUIDE_TEXT`,
  `_WIKI_GUIDE_TEXT` (`src/bot.py`) — whichever domain the change touches
- `_NOTES_GUIDE_TEXT` (`src/notes/telegram.py`) if it touches the study-notes
  channel

A new user-facing knowledge/lookup feature (something you *view or query* —
search, notes, wiki, knowledge graph) must land on both Telegram **and** the
dashboard in the same change, not Telegram-only. Pure ops/admin commands
don't need a dashboard view.

## Deployment — don't tell the user to do this manually

The VM auto-deploys from `claude/personal-rag-knowledge-base-sLSvV` every
minute via cron (`auto_pull.sh`): fetch → diff check → `git pull --ff-only`
+ `docker compose up -d --build` → verify the bot container is running →
Telegram "✅ 배포 완료 <sha>" (or "❌" + logs on failure). Because of this:

- Never suggest the user run `git pull`, `docker compose up`, or restart
  anything manually on the VM for a normal code change — it happens on its
  own within ~60 seconds of a push to the deploy branch.
- Never add a new cron entry — the existing ones (`auto_pull.sh` every
  minute, a watchdog every 5 minutes) already cover deployment and health.
- The one manual exception: after editing `.env` on the VM, the bot needs
  `docker compose up -d --force-recreate <service>` (a plain restart
  doesn't re-read `env_file`).

## Cost-sensitive defaults — don't change without asking

`SUMMARY_MODEL=gemini-2.5-flash-lite` · `ANSWER_MODEL=gemini-2.5-flash` ·
`DEEP_MODEL=gemini-2.5-pro` · `EMBED_MODEL=gemini-embedding-001` ·
`CHUNK_TOKENS=1000` / `CHUNK_OVERLAP=150` · `TOP_K=10`. Wiki batch has a
hard daily ₩ budget circuit breaker; knowledge-graph extraction has a
separate one. Both fail closed (skip, don't overspend) when the daily cap
is hit.

## Secrets

`.env` (on the VM, not in this repo) holds the Telegram bot token, Google
API/service-account config, a GitHub PAT, and dashboard credentials. Never
print/log/echo its contents. If you ever see it pasted into a prompt or
issue, flag it and recommend rotation rather than just proceeding.

## Working alongside Claude Code on this same repo

This repo is being developed by both Claude Code sessions and Copilot.
Prefer working on your own branch and merging into the deploy branch rather
than pushing straight to it, so the two agents' in-flight work doesn't
silently clobber each other. Don't force-push shared branches. If you find
uncommitted changes or an unfamiliar branch, assume it's the other agent's
in-progress work — investigate before overwriting or deleting it.
