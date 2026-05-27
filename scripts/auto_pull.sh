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
# Defined up-front (not only in the deploy path) so check_heartbeat,
# which runs on the no-deploy branch, can use it for multi-line alerts.
NL=$'\n'

# Liveness watchdog tuning. bot.py stamps data/bot_heartbeat with the
# current epoch every 60s on the asyncio loop; a wedged loop freezes it.
HEARTBEAT_FILE="data/bot_heartbeat"
HEARTBEAT_STALE_SEC=600        # 10 min with no stamp = hung loop
WATCHDOG_COOLDOWN_FILE="/tmp/thesis_watchdog_cooldown"
WATCHDOG_COOLDOWN_SEC=600      # restart at most once / 10 min

notify() {
    [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_OWNER_ID:-}" ] || return 0
    curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_OWNER_ID}" \
        --data-urlencode "text=$1" >/dev/null || true
}

# Restart the bot container iff it claims to be running but its
# heartbeat has gone stale (loop wedged). Crashes are already handled by
# compose 'restart: unless-stopped'; this only covers the hung-but-alive
# case. No-op (and SILENT) on every healthy minute — only the actual
# restart sends Telegram.
check_heartbeat() {
    local status
    status=$(docker inspect -f '{{.State.Status}}' thesis-bot-1 2>/dev/null || echo missing)
    [ "$status" = "running" ] || return 0

    # Container-uptime guard. A freshly (re)started/deployed container
    # inherits the PREVIOUS run's heartbeat file (bind mount) and may not
    # have written a fresh stamp yet — esp. during the cold BM25 warm-up
    # that delays the heartbeat job for the first minute. Without this we
    # restart-loop every healthy deploy (saw "무응답 638s" one minute
    # after "배포 완료"). Only judge a container hung once it has been up
    # at least as long as the stale window.
    local started_at started_epoch uptime
    started_at=$(docker inspect -f '{{.State.StartedAt}}' thesis-bot-1 2>/dev/null)
    if [ -n "$started_at" ]; then
        started_epoch=$(date -d "$started_at" +%s 2>/dev/null || echo 0)
        if [ "$started_epoch" -gt 0 ]; then
            uptime=$(( $(date +%s) - started_epoch ))
            [ "$uptime" -ge "$HEARTBEAT_STALE_SEC" ] || return 0
        fi
    fi

    # Missing file → bot hasn't stamped yet (fresh boot / pre-watchdog
    # image). Don't act, so a healthy-but-young container is never
    # restart-looped.
    [ -f "$HEARTBEAT_FILE" ] || return 0
    local hb now age
    hb=$(tr -dc '0-9' < "$HEARTBEAT_FILE" 2>/dev/null)
    [ -n "$hb" ] || return 0
    now=$(date +%s)
    age=$(( now - hb ))
    [ "$age" -ge "$HEARTBEAT_STALE_SEC" ] || return 0

    # Cooldown: a just-restarted container needs time to boot and write
    # its first stamp. Without this we'd fire every minute during boot.
    if [ -f "$WATCHDOG_COOLDOWN_FILE" ]; then
        local last
        last=$(tr -dc '0-9' < "$WATCHDOG_COOLDOWN_FILE" 2>/dev/null)
        if [ -n "$last" ] && [ "$(( now - last ))" -lt "$WATCHDOG_COOLDOWN_SEC" ]; then
            return 0
        fi
    fi
    echo "$now" > "$WATCHDOG_COOLDOWN_FILE"

    echo "===== $(date) — heartbeat stale ${age}s, force-recreating bot =====" >>"$LOG"
    notify "⚠️ 봇 무응답 ${age}s (하트비트 정지) → 자동 재시작 중"
    docker compose --profile local-api up -d --force-recreate bot >>"$LOG" 2>&1
    sleep 5
    local st2
    st2=$(docker inspect -f '{{.State.Status}}' thesis-bot-1 2>/dev/null || echo missing)
    if [ "$st2" = "running" ]; then
        notify "✅ 봇 자동 재시작 완료 (무응답 복구)"
    else
        local tail2
        tail2=$(docker compose logs --tail=10 bot 2>&1 | tail -10)
        notify "❌ 봇 재시작 후 상태 ${st2}${NL}${tail2:0:600}"
    fi
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
# No new commits → steady state. Run the liveness watchdog (silent
# unless it actually restarts a hung bot) and exit.
if [ "$LOCAL" = "$REMOTE" ]; then
    check_heartbeat
    exit 0
fi

SHORT_OLD=$(echo "$LOCAL" | cut -c1-7)
SHORT_NEW=$(echo "$REMOTE" | cut -c1-7)
TITLE=$(git log -1 --format=%s "$REMOTE" 2>/dev/null | tr -d '\r' | head -c 200)
# NL ($'\n', a real newline) is defined near the top so both the deploy
# and watchdog paths render multi-line Telegram alerts correctly.
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
