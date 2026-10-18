# Repo instructions for AI coding agents (GitHub Copilot)

Canonical rules + standing facts for this repo now live in **`AGENTS.md`**
(repo root) — consolidated 2026-08-09 so this file and `CLAUDE.md`
(Claude Code's auto-loaded rules file) stop drifting apart. Read
`AGENTS.md`; that's the one to keep updated going forward.

This file exists only because GitHub Copilot's custom-instructions
convention looks specifically at `.github/copilot-instructions.md`. If
your tooling doesn't automatically pull in a referenced file, open
`AGENTS.md` directly before making any change to this repo — don't rely
on memory of what used to be duplicated here.

Read `AGENT_GUIDE.md` as well (2026-09-06). `AGENTS.md` gives its §4
(답변 순서) binding authority, so skipping it means missing a rule you
are expected to follow. On anything else — 승인, 커밋, 완료 보고 —
`AGENTS.md` wins; see its `## Branch / push policy`.

## Core rules (summary — full text in AGENTS.md)

- **Branch:** deploy branch is `claude/personal-rag-knowledge-base-sLSvV`;
  a push there auto-deploys to the VM within ~1 min. `main` is an unrelated
  stub without `src/` — never base work on it. **Never force-push** the
  deploy branch; if it diverged, fetch and merge (`--no-ff`), keep others' commits.
- **Commit/push gate:** only when the user's latest message has a trigger
  word (커밋·푸시·배포·deploy·commit·push·올려·ship·release). Otherwise edit
  only and leave changes uncommitted.
- **Before pushing:** run `bash scripts/preflight.sh`; exit 1 blocks the push.
- **Ingest changes** apply to new ingest AND the retry queue, never one.
- **User-visible change** → update `_HELP_TEXT` in `src/bot.py` (<=4000 chars)
  and the matching guide constants in the same commit.
- **Event loop:** `meta.py` writers (`_W_LOCK`) must run via `asyncio.to_thread`.
- **Dashboard colours:** use `var(--token)` from `DESIGN.md`, never a new hex.
- **Irreversible actions** (delete data, `_ENT_STOP`, `_ENTITY_ALIASES`, wiki
  topic delete, `.env` values) need the owner's confirmation first.
- **Never** echo `.env` or secrets, tell the user to `git pull`/`compose up`/
  restart on the VM, or add a recurring cron entry.
- **Reports:** say 미검증 for anything not verified by a real tool result; do
  not call a push "deployed" before the VM's "✅ 배포 완료" notice.
- **Style:** answer in Korean; commit titles <=50 chars, imperative, no period.
