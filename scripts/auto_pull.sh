#!/bin/bash
# Poll origin for new commits on the tracked branch and redeploy.
# Designed to be run every minute from cron. Notifies Telegram on result.

set -uo pipefail
cd "$(dirname "$0")/.."

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Load TELEGRAM_BOT_TOKEN / TELEGRAM_OWNER_ID etc.
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

BRANCH="${AUTO_DEPLOY_BRANCH:-claude/personal-rag-knowledge-base-sLSvV}"
LOG="/tmp/thesis_auto_deploy.log"
# State file so we always notify on a NEW remote SHA, even when a
# parallel cron entry (legacy ~/Thesis/scripts/auto_deploy.sh) wins
# the race and pulls before us. Without this the loser of the race
# silently exits, the user only sees one of the two notifications,
# and "배포 시작" never fires for some commits.
SEEN_PATH="${REPO_DIR:-$(pwd)}/data/.auto_pull_last_seen"
mkdir -p "$(dirname "$SEEN_PATH")" 2>/dev/null || true

notify() {
    [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_OWNER_ID:-}" ] || return 0
    curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_OWNER_ID}" \
        --data-urlencode "text=$1" >/dev/null || true
}

git fetch origin "$BRANCH" >/dev/null 2>>"$LOG" || exit 0
LOCAL=$(git rev-parse HEAD 2>/dev/null || echo none)
REMOTE=$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo none)

# Nothing remote OR already on remote AND already notified → exit
# silent.
LAST_SEEN=""
[ -f "$SEEN_PATH" ] && LAST_SEEN=$(cat "$SEEN_PATH" 2>/dev/null || echo "")
if [ "$LOCAL" = "$REMOTE" ] && [ "$LAST_SEEN" = "$REMOTE" ]; then
    exit 0
fi
# Record BEFORE doing any work so a duplicate cron tick can't
# double-notify the same SHA. Atomic via mv from temp file.
echo "$REMOTE" > "${SEEN_PATH}.tmp" && mv "${SEEN_PATH}.tmp" "$SEEN_PATH"

SHORT_OLD=$(echo "$LOCAL" | cut -c1-7)
SHORT_NEW=$(echo "$REMOTE" | cut -c1-7)
# Rich title (first line of the new commit message). Newlines and
# Telegram parse-mode special chars stripped so the notification
# can't accidentally trip parse_mode HTML.
TITLE=$(git log -1 --format=%s "$REMOTE" 2>/dev/null | tr -d '\r' | head -c 200)
SUBJECT="${SHORT_OLD} → ${SHORT_NEW}"
[ -n "$TITLE" ] && SUBJECT="${SUBJECT}\n${TITLE}"

{
    echo "===== $(date) — pulling ${SHORT_OLD} → ${SHORT_NEW} ====="
    [ -n "$TITLE" ] && echo "title: $TITLE"
} >>"$LOG"

# Always fire "deploy starting" notification, even if a concurrent
# cron (legacy auto_deploy.sh) already pulled — the state file
# above guarantees we only do this once per remote SHA.
notify "🚀 배포 시작: ${SUBJECT}"

# Lost the race: another cron already pulled to REMOTE. Wait for
# its compose up to finish, then send 배포 완료 + exit. No double-
# pull, no duplicate deploy.
if [ "$LOCAL" = "$REMOTE" ]; then
    sleep 10
    STATUS=$(docker inspect -f '{{.State.Status}}' thesis-bot-1 2>/dev/null || echo missing)
    if [ "$STATUS" = "running" ]; then
        notify "✅ 배포 완료: ${SUBJECT}"
    else
        TAIL=$(docker compose logs --tail=10 bot 2>&1 | tail -10)
        notify "❌ 컨테이너 상태 ${STATUS}: ${SUBJECT}
${TAIL:0:600}"
    fi
    exit 0
fi

# Self-healing compose up: --remove-orphans handles renamed services,
# and if a stale container is squatting on the canonical name (a known
# recurring failure when manual + auto deploys race), nuke by name and
# retry once.
compose_up() {
    docker compose --profile local-api up -d --build --remove-orphans \
        "$@" >>"$LOG" 2>&1
}

if git pull --ff-only origin "$BRANCH" >>"$LOG" 2>&1; then
    if ! compose_up; then
        echo "compose up failed, removing stale containers and retrying" >>"$LOG"
        docker rm -f thesis-bot-1 thesis-forward-listener-1 \
            thesis-telegram-bot-api-1 thesis-dashboard-1 \
            >>"$LOG" 2>&1 || true
        compose_up || {
            TAIL=$(tail -15 "$LOG")
            notify "❌ 배포 실패: ${SUBJECT}
${TAIL:0:600}"
            exit 1
        }
    fi
    sleep 5
    STATUS=$(docker inspect -f '{{.State.Status}}' thesis-bot-1 2>/dev/null || echo missing)
    if [ "$STATUS" = "running" ]; then
        notify "✅ 배포 완료: ${SUBJECT}"
    else
        TAIL=$(docker compose logs --tail=10 bot 2>&1 | tail -10)
        notify "❌ 컨테이너 상태 ${STATUS}: ${SUBJECT}
${TAIL:0:600}"
    fi
else
    TAIL=$(tail -15 "$LOG")
    notify "❌ 배포 실패: ${SUBJECT}
${TAIL:0:600}"
fi
