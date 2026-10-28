#!/bin/bash
# One-command deploy on a fresh Ubuntu/Debian cloud server.
#
#   curl -fsSL https://raw.githubusercontent.com/justpedro95/chrome-hearts-monitor/main/install/vps/bootstrap.sh | bash
#
# Installs Docker, fetches the monitor, asks for your Discord webhook once,
# seeds state so nothing already known is re-alerted, and starts it under
# restart:unless-stopped so it survives reboots.
# Re-run it any time to upgrade; the webhook and state are preserved.
set -euo pipefail

REPO_URL="https://github.com/justpedro95/chrome-hearts-monitor.git"
DIR="/opt/chrome-hearts-monitor"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root (or: sudo bash $0)"; exit 1
fi

echo "==> Installing Docker if needed"
if ! command -v docker >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl git
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
docker --version

echo "==> Fetching the monitor into $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q origin main && git -C "$DIR" reset -q --hard origin/main
else
  git clone -q "$REPO_URL" "$DIR"
fi
cd "$DIR"
mkdir -p data

# ---- webhook (never written anywhere git tracks) --------------------------
if [ -f .env ] && grep -q '^DISCORD_WEBHOOK_URL=https' .env; then
  echo "==> Reusing the webhook already in .env"
else
  echo
  echo "Paste your Discord webhook URL (not echoed):"
  read -r -s WEBHOOK < /dev/tty
  echo
  if [[ "$WEBHOOK" != https://discord.com/api/webhooks/* ]]; then
    echo "ERROR: that does not look like a Discord webhook URL."; exit 1
  fi
  echo
  echo "Optional: paste a healthchecks.io ping URL so you are told if the"
  echo "monitor itself ever goes silent. Press Enter to skip."
  read -r HEALTH < /dev/tty || HEALTH=""
  umask 077
  cat > .env <<ENV
DISCORD_WEBHOOK_URL=$WEBHOOK
HEALTHCHECK_URL=$HEALTH
ENV
  chmod 600 .env
  echo "==> Wrote .env (chmod 600)"
fi

# Settings this version expects. Added only if absent, so an upgrade never
# overwrites a value you changed by hand.
ensure() { grep -q "^$1=" .env || echo "$1=$2" >> .env; }
ensure STATE_BACKEND          json
ensure STATE_JSON             /data/state.json
ensure FAST_TIER              true
ensure FAST_INTERVAL_SECONDS  10
ensure POLL_INTERVAL_SECONDS  60
ensure REQUEST_DELAY_SECONDS  0.75
ensure ALERT_NEW_PRODUCTS     true
ensure ALERT_NEW_CATEGORIES   true
ensure ALERT_RESTOCKS         false
ensure ALERT_PRICE_CHANGES    false
ensure ENRICH_PRODUCTS        true
ensure HEARTBEAT_HOURS        12
ensure LOG_LEVEL              INFO
ensure HEALTHCHECK_URL        ""

# ---- seed state so nothing known is re-alerted ----------------------------
if [ ! -f data/state.json ] && [ -f state/products.json ]; then
  cp state/products.json data/state.json
  echo "==> Seeded state from the repo ($(python3 -c "import json;print(len(json.load(open('data/state.json'))['products']))" 2>/dev/null || echo '?') products)"
fi

echo "==> Building and starting"
docker compose up -d --build

sleep 8
echo "==> Webhook test"
docker compose exec -T chrome-hearts-monitor python monitor.py --test-webhook || {
  echo "ERROR: webhook test failed. Fix DISCORD_WEBHOOK_URL in $DIR/.env then re-run."; exit 1; }

cat <<EOF

RUNNING. New-category probe every 10s, full sweep every 60s. Restarts on reboot.

  Logs      : docker compose -f $DIR/docker-compose.yml logs -f
  Status    : docker compose -f $DIR/docker-compose.yml ps
  Restart   : docker compose -f $DIR/docker-compose.yml restart
  Upgrade   : bash $DIR/install/vps/bootstrap.sh
  Check parse: docker compose -f $DIR/docker-compose.yml exec -T chrome-hearts-monitor python monitor.py --selftest

NOW TURN OFF THE OTHER TWO, or every alert arrives three times:

  1. GitHub Actions: open the monitor workflow -> "..." -> Disable workflow
     https://github.com/justpedro95/chrome-hearts-monitor/actions/workflows/monitor.yml
  2. On the Mac (if you installed it there):
     launchctl bootout gui/\$(id -u)/bet.jackpot.chromehearts-monitor
EOF
