#!/bin/bash
# Poll origin for new commits on the tracked branch and redeploy.
# Designed to be run every minute from cron. Notifies Telegram on result.

set -uo pipefail
cd "$(dirname "$0")/.."

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Single-instance guard. A docker build regularly takes >60s, so without
# this two cron minutes overlap: the loser's compose-failure retry path
# force-removes the containers the winner just started (duplicate 배포
# 시작/완료 + contradictory ❌ alerts). Loser exits SILENTLY (race-loss
# is a no-op by design). Lock fd auto-releases when the script exits.
exec 9>/tmp/thesis_auto_pull.lock
flock -n 9 || exit 0

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
WATCHDOG_COOLDOWN_SEC=1800     # alert at most once / 30 min (≤2/h)
# Deploy-window mute (2026-07-05). Every deploy produced a FALSE hang
# alert: the image build pegs both vCPUs (the old container starves and
# its heartbeat stalls), then the fresh container drains the ingest
# backlog for several more minutes. Those alerts trained the user to
# ignore the watchdog — the opposite of its job. The deploy path stamps
# this file before the build and again after success; check_heartbeat
# stays silent for DEPLOY_MUTE_SEC after the last stamp. An alert
# OUTSIDE a deploy window is therefore always real. Missing file →
# normal alerting (fresh VM safe).
DEPLOY_STAMP_FILE="/tmp/thesis_last_deploy_ts"
DEPLOY_MUTE_SEC=900            # 15 min after (re)stamp

notify() {
    [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_OWNER_ID:-}" ] || return 0
    curl -sS --max-time 15 \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_OWNER_ID}" \
        --data-urlencode "text=$1" >/dev/null || true
}

# ALERT-ONLY hang watchdog (phase 1, 2026-06). Crashes are recovered by
# compose 'restart: unless-stopped'; this covers the hung-but-alive case
# (process up, event loop wedged, heartbeat frozen) — previously the bot
# just went silently dead until the user noticed. Sends a Telegram alert
# at most once per WATCHDOG_COOLDOWN_SEC; deliberately does NOT restart:
# the 2026-05-27 auto-restart version caused a restart loop when the
# cold BM25 warm-up starved the loop past the stale window. That root
# cause is gone (heartbeat stamped at boot; warm-up no-ops above
# BM25_MAX_CHUNKS), but restarts stay manual until this alert proves
# itself false-positive-free for a few weeks (phase 2: re-add restart).
# No-op and SILENT on every healthy minute.
check_heartbeat() {
    local status
    status=$(docker inspect -f '{{.State.Status}}' thesis-bot-1 2>/dev/null || echo missing)
    [ "$status" = "running" ] || return 0

    # Deploy-window mute: a recent deploy (build CPU starvation + post-boot
    # backlog drain) makes a stale heartbeat EXPECTED, not a hang.
    if [ -f "$DEPLOY_STAMP_FILE" ]; then
        local dts
        dts=$(tr -dc '0-9' < "$DEPLOY_STAMP_FILE" 2>/dev/null)
        if [ -n "$dts" ] && [ "$(( $(date +%s) - dts ))" -lt "$DEPLOY_MUTE_SEC" ]; then
            return 0
        fi
    fi

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

    # Cooldown: re-alert at most every 30 min while the hang persists —
    # enough to know it's STILL hung without flooding Telegram.
    if [ -f "$WATCHDOG_COOLDOWN_FILE" ]; then
        local last
        last=$(tr -dc '0-9' < "$WATCHDOG_COOLDOWN_FILE" 2>/dev/null)
        if [ -n "$last" ] && [ "$(( now - last ))" -lt "$WATCHDOG_COOLDOWN_SEC" ]; then
            return 0
        fi
    fi
    echo "$now" > "$WATCHDOG_COOLDOWN_FILE"

    local mins=$(( age / 60 ))
    echo "===== $(date) — heartbeat stale ${age}s, ALERT (no restart) =====" >>"$LOG"
    notify "⚠️ 봇 하트비트 ${mins}분 정지 — 컨테이너는 running이지만 이벤트 루프가 멈춘 듯.${NL}복구: docker compose --profile local-api up -d --force-recreate bot${NL}(30분마다 재알림, 정상화되면 자동 중지)"
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
# No new commits → steady state: run the alert-only hang check, then
# exit silently. (History: the auto-RESTART watchdog was disabled
# 2026-05-27 after a BM25-warm-up restart loop; revived 2026-06 as
# alert-only — see check_heartbeat() comment for the full rationale.)
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

# Failure latch: if THIS exact remote sha already failed to pull, keep
# retrying every minute but SILENTLY — without this, a stuck pull
# (force-pushed branch, diverged HEAD, dirty tracked file) fired
# 배포 시작 + 배포 실패 twice a minute forever. A new push (different
# sha) or a successful pull clears the latch and restores loud mode.
FAIL_LATCH="/tmp/thesis_deploy_failed_sha"
QUIET=0
if [ -f "$FAIL_LATCH" ] && [ "$(tr -dc 'a-f0-9' < "$FAIL_LATCH" 2>/dev/null)" = "$REMOTE" ]; then
    QUIET=1
fi

{
    echo "===== $(date) — pulling ${SHORT_OLD} → ${SHORT_NEW} (quiet=${QUIET}) ====="
    [ -n "$TITLE" ] && echo "title: $TITLE"
} >>"$LOG"

# Mute the hang watchdog for the whole build + warm-up window (quiet
# retries rebuild too, so stamp regardless of QUIET).
date +%s > "$DEPLOY_STAMP_FILE"

[ "$QUIET" -eq 1 ] || notify "🚀 배포 시작: ${SUBJECT}"

# Disk guard. The torch layer (line 30 of Dockerfile) cache-busts on every
# code push, so each deploy re-downloads a multi-GB wheel + new image
# layers + build cache. Left unchecked this fills the 58G boot disk and the
# build dies mid-`pip install` with `[Errno 28] No space left on device`
# (happened 2026-06-24 — three back-to-back rebuilds → 100%). When the root
# fs is tight, reclaim build cache + dangling images BEFORE building so the
# build has room. Running containers' images are protected; data/ bind mount
# + named volumes are never touched by builder/image prune. No-op when there's
# plenty of headroom (keeps the warm build cache for fast rebuilds).
free_disk_if_needed() {
    local use
    use=$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')
    [ -n "$use" ] || return 0
    if [ "$use" -ge 85 ]; then
        echo "disk ${use}% ≥85% — pruning build cache + dangling images" >>"$LOG"
        docker builder prune -af >>"$LOG" 2>&1 || true
        docker image prune -f >>"$LOG" 2>&1 || true
        local after
        after=$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')
        echo "disk after prune: ${after:-?}%" >>"$LOG"
    fi
}

compose_up() {
    docker compose --profile local-api up -d --build --remove-orphans \
        "$@" >>"$LOG" 2>&1
}

if git pull --ff-only origin "$BRANCH" >>"$LOG" 2>&1; then
    rm -f "$FAIL_LATCH"
    free_disk_if_needed
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
    # Each --build retags the image and leaves the PREVIOUS one dangling
    # (untagged). Reap it now so old bot/dashboard images don't accumulate
    # and refill the disk over successive deploys. Dangling-only (-f, no -a)
    # → never removes an image a running container still references.
    docker image prune -f >>"$LOG" 2>&1 || true
    # Re-stamp AFTER the build finished so the mute covers build time +
    # a full DEPLOY_MUTE_SEC of post-boot backlog drain.
    date +%s > "$DEPLOY_STAMP_FILE"
    sleep 5
    STATUS=$(docker inspect -f '{{.State.Status}}' thesis-bot-1 2>/dev/null || echo missing)
    if [ "$STATUS" = "running" ]; then
        notify "✅ 배포 완료: ${SUBJECT}"
    else
        TAIL=$(docker compose logs --tail=10 bot 2>&1 | tail -10)
        notify "❌ 컨테이너 상태 ${STATUS}: ${SUBJECT}${NL}${TAIL:0:600}"
    fi
else
    printf '%s' "$REMOTE" > "${FAIL_LATCH}.tmp" && mv "${FAIL_LATCH}.tmp" "$FAIL_LATCH"
    if [ "$QUIET" -eq 0 ]; then
        TAIL=$(tail -15 "$LOG")
        notify "❌ 배포 실패(pull): ${SUBJECT}${NL}${TAIL:0:600}${NL}(같은 SHA는 무음 재시도, 새 푸시가 오면 다시 알림)"
    fi
fi
