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
[ "$LOCAL" = "$REMOTE" ] && exit 0

SHORT=$(echo "$REMOTE" | cut -c1-7)
{
    echo "===== $(date) — pulling $SHORT ====="
} >>"$LOG"

notify "🔄 자동 배포 시작 ${SHORT}"

if git pull --ff-only origin "$BRANCH" >>"$LOG" 2>&1 \
    && docker compose --profile local-api up -d --build >>"$LOG" 2>&1; then
    sleep 5
    STATUS=$(docker inspect -f '{{.State.Status}}' thesis-bot-1 2>/dev/null || echo missing)
    if [ "$STATUS" = "running" ]; then
        notify "✅ 배포 완료 ${SHORT}"
    else
        TAIL=$(docker compose logs --tail=10 bot 2>&1 | tail -10)
        notify "❌ 컨테이너 상태 ${STATUS} (${SHORT})
${TAIL:0:600}"
    fi
else
    TAIL=$(tail -15 "$LOG")
    notify "❌ 배포 실패 ${SHORT}
${TAIL:0:600}"
fi
