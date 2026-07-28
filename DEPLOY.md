# Deployment Policy

## Definition: "배포" (Deploy)

**배포** means: **Commit → Push to deploy branch → Telegram ✅/❌ (automatic)**.

### Telegram Notifications

Auto-deployed to `claude/personal-rag-knowledge-base-sLSvV` by VM cron every 60s:

```
🚀 배포 시작: <git_sha_before> → <git_sha_after>
   <commit_message_first_line>
   
✅ 배포 완료: <git_sha_before> → <git_sha_after>
   <commit_message_first_line>
   
❌ 배포 실패: <git_sha>
   Error logs...
```

**You will see this** — it's automatic. Don't make it manual.

### Deploy Branch

- **Main deploy branch**: `claude/personal-rag-knowledge-base-sLSvV`
- **VM cron** (`auto_pull.sh`) watches this branch every 60 seconds
- **On push**: VM auto-deploys (git pull + docker compose rebuild) within ~60s
- **Telegram notification**: ✅ or ❌ (automatic via `auto_pull.sh`)

### Command Interpretation

```
User: "배포" (deploy)
↓
Agent Action:
  1. git commit -m "..."
  2. git push -u origin claude/personal-rag-knowledge-base-sLSvV
  3. Telegram sends: 🚀 배포 시작: <before> → <after>
  4. ~60s later → Telegram sends: ✅ 배포 완료 (or ❌ 배포 실패 + logs)
  5. Report: "✅ 배포 완료"
```

### Non-Negotiable Rules

- **Always push to `claude/personal-rag-knowledge-base-sLSvV`** — this is the production deploy branch
- **Never push to `main`** — it's stale (only Initial commit from 2026-04-09); use for archive PRs only
- **Always wait for Telegram ✅** — that's the confirmation cron ran and deployed
- **Never manually run git pull / docker compose on VM** — cron handles it within 60s
- **Never force-push** — cron uses `git pull --ff-only`, force-push breaks it

## Related Commands

| Command | Target | Action | Notification |
|---------|--------|--------|--------------|
| **배포** | `claude/personal-rag-knowledge-base-sLSvV` | Push to deploy branch | 🚀 + ✅ Telegram |
| **커밋** | Current branch | `git commit` only | None |
| **푸시** | Current branch | `git push` | None (unless deploy branch → Telegram) |
| **PR** | `main` (archive) | Create PR | None |
| **머지** | `main` (archive) | Merge PR to main | None (doesn't deploy) |

## Workflow

**Ship code (common case)**:
```bash
git checkout -b feature/my-change
git add .
git commit -m "feat: ..."

# Ready to ship?
git push -u origin claude/personal-rag-knowledge-base-sLSvV

# Telegram: 🚀 배포 시작: abc123 → def456
#           feat: ...
# [60s delay for VM cron]
# Telegram: ✅ 배포 완료: abc123 → def456
#           feat: ...

# You: "✅ Deployed!"
```

**Optional: Archive to main for review** (does NOT deploy):
```bash
# First: push to deploy branch (above)
# After Telegram ✅: create PR to main for record-keeping
git push -u origin feature/my-change
github-create_pull_request(base=main, head=feature/my-change)
# User reviews, then merge to main
# ⚠️ This is archive only — real deployment already happened via cron above
```

## Auto-Deploy Details

From `copilot-instructions.md`:

```
auto_pull.sh (runs on VM every 60 seconds):
  1. fetch latest from origin
  2. Check if claude/personal-rag-knowledge-base-sLSvV differs from local
  3. If yes: git pull --ff-only + docker compose up -d --build
  4. Verify bot container running
  5. If success: Telegram "✅ 배포 완료 <sha>"
     If fail: Telegram "❌ <error_logs>"
```

**Key points**:
- ✅ ~60s max time from push to live
- ✅ Telegram is the source of truth
- ✅ Retry is automatic (cron runs every 60s)
- ❌ Never manually restart/pull (breaks idempotency)
- ❌ Never force-push (breaks --ff-only guard)

---

*Push to `claude/personal-rag-knowledge-base-sLSvV` = instant production deploy + Telegram notification (🚀 → ✅)*


