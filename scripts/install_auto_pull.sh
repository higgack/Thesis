#!/bin/bash
# One-time install: adds a cron entry that polls for new commits and redeploys.
# After running this, no SSH key or GitHub Actions secret is needed; the VM
# notices new pushes within 1 minute and rebuilds itself.

set -euo pipefail
cd "$(dirname "$0")/.."
REPO_DIR="$(pwd)"

chmod +x scripts/auto_pull.sh

CRON_LINE="* * * * * cd ${REPO_DIR} && bash scripts/auto_pull.sh"

if crontab -l 2>/dev/null | grep -F "scripts/auto_pull.sh" >/dev/null; then
    echo "[skip] cron already installed:"
    crontab -l | grep "auto_pull"
    exit 0
fi

(crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -

echo "[ok] cron installed:"
echo "  $CRON_LINE"
echo ""
echo "Polls origin every minute. Logs: /tmp/thesis_auto_deploy.log"
echo "Telegram start/success/failure notifications use TELEGRAM_BOT_TOKEN + TELEGRAM_OWNER_ID from .env."
echo ""
echo "Manual trigger: bash scripts/auto_pull.sh"
echo "Disable: crontab -e  (delete the auto_pull line)"
