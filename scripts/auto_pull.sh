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

# Snapshot the evidence BEFORE a watchdog restart destroys it.
# `docker compose up --force-recreate` replaces the container, so its
# logs go with it — including the loop-stall stack dump bot.py writes
# after 120s of silence, which exists precisely to explain these hangs.
# Every auto-restart so far has therefore reported the symptom and
# deleted the cause. Bounded with `timeout` so a wedged docker daemon
# can't stall the cron minute, and pruned to the newest 10 files.
HANG_DIAG_DIR="/tmp/thesis_hang_diag"
capture_hang_diag() {
    local reason="$1" out
    mkdir -p "$HANG_DIAG_DIR" 2>/dev/null || return 0
    out="$HANG_DIAG_DIR/$(date +%Y%m%d-%H%M%S)-${reason}.log"
    {
        echo "=== $(date -Is) reason=${reason} ==="
        echo
        echo "--- docker inspect ---"
        timeout 15 docker inspect -f 'status={{.State.Status}} started={{.State.StartedAt}} oom={{.State.OOMKilled}} exit={{.State.ExitCode}}' thesis-bot-1
        echo
        echo "--- docker stats ---"
        timeout 20 docker stats --no-stream thesis-bot-1
        echo
        echo "--- threads (docker top) ---"
        timeout 15 docker top thesis-bot-1
        echo
        echo "--- bot log tail (stall stack dump lands here) ---"
        timeout 30 docker logs thesis-bot-1 --tail 800
    } >"$out" 2>&1
    # Retention: newest 10 only, so this can't grow unbounded in /tmp.
    ls -1t "$HANG_DIAG_DIR"/*.log 2>/dev/null | tail -n +11 | while read -r f; do
        rm -f "$f"
    done
    echo "hang diag saved: $out" >>"$LOG"
}

# Auto-restart hang watchdog (phase 2, 2026-08). Phase 1 (alert-only,
# 2026-06) proved false-positive-free: deploy-window mute, container-uptime
# guard, and 30-min cooldown all worked correctly. Phase 2 re-adds the
# auto-restart removed 2026-05-27 (BM25 warm-up restart loop) — that root
# cause is long gone (heartbeat stamped at boot; BM25 warm-up no-ops above
# BM25_MAX_CHUNKS). All false-positive guards unchanged.
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
    echo "===== $(date) -- heartbeat stale ${age}s, AUTO-RESTART (phase 2) =====" >>"$LOG"
    notify "WARNING: bot heartbeat ${mins}min stale -- event loop hung, auto-restarting"
    capture_hang_diag "heartbeat-${mins}min"
    docker compose --profile local-api up -d --force-recreate bot >>"$LOG" 2>&1
    date +%s > "$DEPLOY_STAMP_FILE"
    sleep 5
    local new_status
    new_status=$(docker inspect -f '{{.State.Status}}' thesis-bot-1 2>/dev/null || echo missing)
    if [ "$new_status" = "running" ]; then
        notify "OK: bot auto-restart complete (was hung ${mins}min)"
    else
        notify "ERROR: bot auto-restart failed -- status: ${new_status}"
    fi
}


# CPU-sustained-overload watchdog (added after the 2026-08-01 incident:
# thesis-bot-1 pegged 100%+ CPU for 3 days on _wconn lock contention from
# a forward-listener volume spike — Telegram message-handling was starved
# via thread-pool exhaustion even though the asyncio loop itself kept
# ticking, so check_heartbeat() above never caught it (heartbeat stayed
# fresh throughout). This checks docker-reported CPU% directly instead,
# and — unlike check_heartbeat, which stays alert-only — actually
# restarts: a restart here is cheap and safe (resume-safety invariants
# mean nothing in flight is lost), and the alternative demonstrated is
# silent multi-day unresponsiveness with no other reliable signal.
CPU_STATE_FILE="/tmp/thesis_cpu_high_count"
CPU_THRESHOLD_PCT=90
CPU_CONSECUTIVE_NEEDED=5   # auto_pull.sh runs every ~60s → ~5분 연속 과부하
check_cpu_overload() {
    local status
    status=$(docker inspect -f '{{.State.Status}}' thesis-bot-1 2>/dev/null || echo missing)
    [ "$status" = "running" ] || return 0

    # Deploy-window mute — a build/boot legitimately pegs CPU, not a hang.
    if [ -f "$DEPLOY_STAMP_FILE" ]; then
        local dts
        dts=$(tr -dc '0-9' < "$DEPLOY_STAMP_FILE" 2>/dev/null)
        if [ -n "$dts" ] && [ "$(( $(date +%s) - dts ))" -lt "$DEPLOY_MUTE_SEC" ]; then
            echo 0 > "$CPU_STATE_FILE"
            return 0
        fi
    fi

    # Container-uptime guard (same rationale as check_heartbeat): chroma
    # HNSW/BM25 warm-up right after a fresh start/restart legitimately
    # pegs CPU for a bit. Without this, a manual .env-edit restart (which
    # never stamps DEPLOY_STAMP_FILE, so the mute above doesn't cover it)
    # could count consecutive high-CPU minutes during its own warm-up and
    # trigger a self-inflicted restart loop.
    local started_at started_epoch uptime
    started_at=$(docker inspect -f '{{.State.StartedAt}}' thesis-bot-1 2>/dev/null)
    if [ -n "$started_at" ]; then
        started_epoch=$(date -d "$started_at" +%s 2>/dev/null || echo 0)
        if [ "$started_epoch" -gt 0 ]; then
            uptime=$(( $(date +%s) - started_epoch ))
            if [ "$uptime" -lt "$HEARTBEAT_STALE_SEC" ]; then
                echo 0 > "$CPU_STATE_FILE"
                return 0
            fi
        fi
    fi

    local cpu cpu_int count
    cpu=$(docker stats --no-stream --format "{{.CPUPerc}}" thesis-bot-1 2>/dev/null | tr -d '%')
    [ -n "$cpu" ] || return 0
    cpu_int=${cpu%.*}
    case "$cpu_int" in ''|*[!0-9]*) return 0 ;; esac
    count=$(cat "$CPU_STATE_FILE" 2>/dev/null || echo 0)

    if [ "$cpu_int" -ge "$CPU_THRESHOLD_PCT" ]; then
        count=$((count + 1))
    else
        count=0
    fi
    echo "$count" > "$CPU_STATE_FILE"

    if [ "$count" -ge "$CPU_CONSECUTIVE_NEEDED" ]; then
        echo "===== $(date) — CPU ${cpu}% sustained ${count}회 연속 → 강제 재시작 =====" >>"$LOG"
        notify "🚨 thesis-bot CPU ${cpu}% ${CPU_CONSECUTIVE_NEEDED}분+ 지속 — 텔레그램 응답 불가 상태로 판단, 자동 재시작 진행"
        capture_hang_diag "cpu-${cpu_int}pct"
        docker compose --profile local-api up -d --force-recreate bot >>"$LOG" 2>&1
        echo 0 > "$CPU_STATE_FILE"
        date +%s > "$DEPLOY_STAMP_FILE"   # mute other checks while it settles
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
# No new commits → steady state: run the alert-only hang check, then
# exit silently. (History: the auto-RESTART watchdog was disabled
# 2026-05-27 after a BM25-warm-up restart loop; revived 2026-06 as
# alert-only — see check_heartbeat() comment for the full rationale.)
if [ "$LOCAL" = "$REMOTE" ]; then
    check_heartbeat
    check_cpu_overload
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
        # NOT -af: a full prune deletes the multi-GB torch/deps layers, so
        # the very next deploy is a 10-20 min cold build (2026-07-08 — a
        # burst of deploys crossed 85%, -af fired, the following build
        # re-downloaded torch from scratch). --keep-storage retains the
        # most-recently-used ~6GB — the hot torch + deps layers are used
        # by every build, so they survive; stale cache still gets freed.
        docker builder prune -f --keep-storage 6g >>"$LOG" 2>&1 || true
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
