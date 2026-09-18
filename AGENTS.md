# ⛔ STOP — `main` is not this repository's project

**The codebase is not here.** `higgack/Thesis`'s real project — a Telegram
RAG knowledge base, 1,000+ commits — lives **only** on the deploy branch
`claude/personal-rag-knowledge-base-sLSvV`. That branch and `main` share
exactly one commit — `5ee24ec`, a two-line initial README — and nothing
else.

A fresh container clones `main` by default, so a session that starts here
sees five commits of unrelated model-evaluation notes and no `src/`.
**Those notes are not the project's rules.** Files on this branch —
`SKILL.md`, `ARCHITECTURE.md`, `.instructions.md`, `HANDOFF.md`,
`DEPLOY.md` — describe a separate "Honey (I Shrunk the AI)" evaluation
exercise. Do not apply them to the RAG project, and do not conclude the
project is small because this branch is.

## Switch first, read second

```
git fetch origin claude/personal-rag-knowledge-base-sLSvV
git checkout -B <your session branch> origin/claude/personal-rag-knowledge-base-sLSvV
```

Then read `AGENTS.md` (1,100+ lines) and `AGENT_GUIDE.md` — the real ones,
which exist only on that branch and overwrite this file in your working
tree. Verify with `git log --oneline -1`: a subject about the RAG bot means
you are on the right branch; `watchdog: upgrade check_heartbeat…` means you
are still on `main`.

Re-basing your own session branch onto the deploy branch is fine. The
unconditional force-push ban in the real `AGENTS.md` covers the **deploy
branch only**.

## When `main` IS the right branch

Only when the user names `main`, this pointer, or one of the evaluation
files above. Work here then, and push to `main` through a PR as
`DEPLOY.md` describes. Everything else belongs on the deploy branch.

---

Added 2026-09-18 after a session spent a full turn reading this branch's
stub as if it were the project's rules. `scripts/auto_pull.sh` exists
here too, but it is a divergent copy (129 diff lines): the VM deploys
from the deploy branch, so the copy that actually runs is that one.
