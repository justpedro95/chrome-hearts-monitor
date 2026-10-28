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
DIR="${CHM_DIR:-/opt/chrome-hearts-monitor}"
# The container runs as this uid (see Dockerfile). Its bind-mounted data dir
# must be writable by it; the installer runs as root and would otherwise
# create that dir root-owned, and the monitor would crash on its first save.
APP_UID=10001

RESET_WEBHOOK=0
[ "${1:-}" = "--reset-webhook" ] && RESET_WEBHOOK=1

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
if [ "$RESET_WEBHOOK" = 1 ] && [ -f .env ]; then
  sed -i '/^DISCORD_WEBHOOK_URL=/d' .env
  echo "==> Cleared the stored webhook; you will be asked for a new one"
fi
if [ -f .env ] && grep -q '^DISCORD_WEBHOOK_URL=https' .env; then
  echo "==> Reusing the webhook already in .env"
else
  echo
  echo "Paste your Discord webhook URL (not echoed):"
  read -r -s WEBHOOK < "${CHM_TTY:-/dev/tty}"
  echo
  # Browser consoles can carry stray spaces or a CR through a paste.
  WEBHOOK="$(printf '%s' "$WEBHOOK" | tr -d '[:space:]')"
  if [[ "$WEBHOOK" != https://discord.com/api/webhooks/* ]]; then
    echo "ERROR: that does not look like a Discord webhook URL."; exit 1
  fi
  # The input is hidden, so show enough to confirm the paste landed whole.
  echo "==> Got a ${#WEBHOOK}-character webhook ending in ...${WEBHOOK: -6}"
  echo
  echo "Optional: paste a healthchecks.io ping URL so you are told if the"
  echo "monitor itself ever goes silent. Press Enter to skip."
  read -r HEALTH < "${CHM_TTY:-/dev/tty}" || HEALTH=""
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

# ---- test the webhook from the host, before building anything --------------
# Testing it inside the container conflated "the webhook is wrong" with "the
# container is not running", and reported both as a webhook failure.
HOOK="$(grep '^DISCORD_WEBHOOK_URL=' .env | cut -d= -f2-)"
echo "==> Testing the Discord webhook"
CODE="$(curl -sS -o /dev/null -w '%{http_code}' -m 15 -H 'Content-Type: application/json' \
        -d '{"content":"Chrome Hearts monitor: server install test - webhook works."}' \
        "$HOOK" || echo 000)"
case "$CODE" in
  200|204) echo "    OK - check your Discord channel for a test message" ;;
  401|403|404)
    sed -i '/^DISCORD_WEBHOOK_URL=/d' .env
    echo "ERROR: Discord rejected that webhook (HTTP $CODE): it is mistyped, cut"
    echo "       short, or was deleted. Copy it again from Discord and re-run:"
    echo "         bash $DIR/install/vps/bootstrap.sh"
    exit 1 ;;
  000)
    echo "ERROR: could not reach discord.com from this server (network/DNS)."
    exit 1 ;;
  *)
    echo "ERROR: Discord answered HTTP $CODE. Wait a minute and re-run."
    exit 1 ;;
esac

# ---- seed state so nothing known is re-alerted ----------------------------
if [ ! -f data/state.json ] && [ -f state/products.json ]; then
  cp state/products.json data/state.json
  echo "==> Seeded state from the repo ($(python3 -c "import json;print(len(json.load(open('data/state.json'))['products']))" 2>/dev/null || echo '?') products)"
fi

chown -R "$APP_UID:$APP_UID" data
echo "==> data/ owned by uid $APP_UID so the container can write its state"

echo "==> Building and starting"
docker compose up -d --build --force-recreate

echo "==> Waiting for the monitor to settle"
# "Stable" = running, and the restart count is not climbing. Do not require
# it to be zero: a container that crash-looped before this fix keeps its
# historical count, and that must not read as a failure on a re-run.
STABLE=0
LAST_RC=""
for _ in $(seq 1 30); do
  STATE="$(docker inspect -f '{{.State.Status}} {{.RestartCount}}' chrome-hearts-monitor 2>/dev/null || echo 'missing -1')"
  set -- $STATE
  if [ "$1" = "running" ] && [ "$2" = "$LAST_RC" ]; then STABLE=$((STABLE+1)); else STABLE=0; fi
  LAST_RC="$2"
  [ "$STABLE" -ge 5 ] && break
  sleep 2
done
if [ "$STABLE" -lt 5 ]; then
  echo
  echo "ERROR: the monitor container is not staying up ($STATE)."
  echo "       This is NOT a webhook problem - the webhook already tested OK."
  echo "       Last log lines:"
  docker compose logs --tail 25 chrome-hearts-monitor
  exit 1
fi

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
