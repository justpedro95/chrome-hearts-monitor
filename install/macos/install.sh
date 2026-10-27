#!/bin/bash
# Install the Chrome Hearts monitor as an always-on launchd job on macOS.
#
#   bash install/macos/install.sh
#
# Idempotent: re-running it upgrades the code in place and keeps your webhook
# and your existing state, so no product is ever re-alerted.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LABEL="bet.jackpot.chromehearts-monitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs/chrome-hearts-monitor"
VENV="$REPO/.venv"
STATE="$REPO/state/local-state.json"

echo "==> Repo:  $REPO"
mkdir -p "$LOGDIR" "$HOME/Library/LaunchAgents"

# ---- 1. python -------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Run 'git --version' first and install the"
  echo "       Command Line Tools when macOS offers, then re-run this."
  exit 1
fi
echo "==> Python: $(python3 --version)"

# ---- 2. venv + deps --------------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
  echo "==> Creating virtualenv"
  python3 -m venv "$VENV"
fi
echo "==> Installing dependencies"
"$VENV/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
"$VENV/bin/pip" install -q -r "$REPO/requirements.txt"

# ---- 3. webhook ------------------------------------------------------------
# Never written anywhere git tracks: .env is gitignored.
if [ -f "$REPO/.env" ] && grep -q '^DISCORD_WEBHOOK_URL=https' "$REPO/.env"; then
  echo "==> Reusing the webhook already in .env"
else
  echo
  echo "Paste your Discord webhook URL (it will not be echoed):"
  read -r -s WEBHOOK
  echo
  if [[ "$WEBHOOK" != https://discord.com/api/webhooks/* ]]; then
    echo "ERROR: that does not look like a Discord webhook URL."
    exit 1
  fi
  cat > "$REPO/.env" <<ENV
DISCORD_WEBHOOK_URL=$WEBHOOK
STATE_BACKEND=json
STATE_JSON=$STATE
POLL_INTERVAL_SECONDS=60
JITTER_SECONDS=10
REQUEST_DELAY_SECONDS=1.5
ALERT_NEW_PRODUCTS=true
ALERT_NEW_CATEGORIES=true
ALERT_RESTOCKS=false
ALERT_PRICE_CHANGES=false
ENRICH_PRODUCTS=true
HEARTBEAT_HOURS=12
LOG_LEVEL=INFO
ENV
  chmod 600 "$REPO/.env"
  echo "==> Wrote .env (chmod 600, git-ignored)"
fi

# ---- 4. seed state so nothing is re-alerted --------------------------------
if [ ! -f "$STATE" ]; then
  if [ -f "$REPO/state/products.json" ]; then
    cp "$REPO/state/products.json" "$STATE"
    echo "==> Seeded local state from the repo's state ($(python3 -c "import json,sys;print(len(json.load(open('$STATE'))['products']))") products)"
    echo "    Nothing already known will be alerted again."
  else
    echo "==> No existing state found; the first cycle will baseline silently."
  fi
else
  echo "==> Keeping existing local state"
fi

# ---- 5. webhook smoke test -------------------------------------------------
echo "==> Testing the webhook"
if ! "$VENV/bin/python" "$REPO/monitor.py" --test-webhook; then
  echo "ERROR: the webhook test failed. Fix DISCORD_WEBHOOK_URL in $REPO/.env and re-run."
  exit 1
fi

# ---- 6. launchd ------------------------------------------------------------
echo "==> Installing the launchd job"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV/bin/python</string>
    <string>$REPO/monitor.py</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOGDIR/monitor.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/monitor.log</string>
</dict>
</plist>
PL

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 3

echo
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  echo "==> RUNNING. It starts automatically on login/boot and restarts if it crashes."
else
  echo "==> WARNING: the job did not come up. Check $LOGDIR/monitor.log"
  exit 1
fi
cat <<EOF

Done. Polling every 60 seconds.

  Watch it live : tail -f $LOGDIR/monitor.log
  Stop          : launchctl bootout gui/$(id -u)/$LABEL
  Start         : launchctl bootstrap gui/$(id -u) $PLIST
  Upgrade        : git pull && bash install/macos/install.sh
  Uninstall     : launchctl bootout gui/$(id -u)/$LABEL && rm $PLIST

IMPORTANT, two things left:

1. Stop GitHub Actions double-alerting you. Open
   https://github.com/justpedro95/chrome-hearts-monitor/actions/workflows/monitor.yml
   -> the "..." menu, top right -> Disable workflow.

2. Keep this Mac awake. System Settings -> Displays -> Advanced (or Energy
   Saver on older macOS) -> turn ON "Prevent automatic sleeping when the
   display is off". launchd cannot run the monitor while the Mac is asleep.
EOF
