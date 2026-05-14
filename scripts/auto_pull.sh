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

notify() {
    [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_OWNER_ID:-}" ] || return 0
    curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_OWNER_ID}" \
        --data-urlencode "text=$1" >/dev/null || true
}

git fetch origin "$BRANCH" >/dev/null 2>>"$LOG" || exit 0
LOCAL=$(git rev-parse HEAD 2>/dev/null || echo none)
REMOTE=$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo none)
# Simple invariant: only act when we actually need to pull. If LOCAL
# already matches REMOTE, another cron (legacy auto_deploy.sh) won
# the race and we go silent. To get BOTH 배포 시작 + 배포 완료 from
# this script reliably, remove the duplicate cron line:
#   crontab -e   → delete the '~/Thesis/scripts/auto_deploy.sh' row
# leaving only the 'scripts/auto_pull.sh' entry.
[ "$LOCAL" = "$REMOTE" ] && exit 0

SHORT_OLD=$(echo "$LOCAL" | cut -c1-7)
SHORT_NEW=$(echo "$REMOTE" | cut -c1-7)
TITLE=$(git log -1 --format=%s "$REMOTE" 2>/dev/null | tr -d '\r' | head -c 200)
# Real newline (printf -v with literal LF) so Telegram renders title
# on its own line instead of showing a literal backslash-n.
NL=$'\n'
SUBJECT="${SHORT_OLD} → ${SHORT_NEW}"
[ -n "$TITLE" ] && SUBJECT="${SUBJECT}${NL}${TITLE}"

{
    echo "===== $(date) — pulling ${SHORT_OLD} → ${SHORT_NEW} ====="
    [ -n "$TITLE" ] && echo "title: $TITLE"
} >>"$LOG"

notify "🚀 배포 시작: ${SUBJECT}"

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
            notify "❌ 배포 실패: ${SUBJECT}${NL}${TAIL:0:600}"
            exit 1
        }
    fi
    sleep 5
    STATUS=$(docker inspect -f '{{.State.Status}}' thesis-bot-1 2>/dev/null || echo missing)
    if [ "$STATUS" = "running" ]; then
        notify "✅ 배포 완료: ${SUBJECT}"
    else
        TAIL=$(docker compose logs --tail=10 bot 2>&1 | tail -10)
        notify "❌ 컨테이너 상태 ${STATUS}: ${SUBJECT}${NL}${TAIL:0:600}"
    fi
else
    TAIL=$(tail -15 "$LOG")
    notify "❌ 배포 실패: ${SUBJECT}${NL}${TAIL:0:600}"
fi
