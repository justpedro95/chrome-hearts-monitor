#!/bin/bash
# Read-only health check for the cloud monitor. Changes nothing, sends nothing.
#   bash /opt/chrome-hearts-monitor/install/vps/check.sh
DIR="${CHM_DIR:-/opt/chrome-hearts-monitor}"
cd "$DIR" || { echo "Not installed at $DIR"; exit 1; }
PROBS=0
problem() { echo "PROBLEM: $*"; PROBS=$((PROBS+1)); }

echo "===== CONTAINER ====="
docker inspect -f 'state={{.State.Status}}  restarts={{.RestartCount}}  up since={{.State.StartedAt}}  health={{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}' chrome-hearts-monitor 2>/dev/null \
  || problem "no chrome-hearts-monitor container"

echo; echo "===== CODE ====="
git log --oneline -1
grep -q "new_section_watch" monitor.py && echo "v15 (unreadable-section watchdog): present" || problem "v15 code missing - run the installer again"

echo; echo "===== PERMISSIONS ====="
stat -c '%n owned by uid %u' data data/state.json 2>/dev/null
if [ "$(stat -c %u data 2>/dev/null)" != "10001" ]; then
  problem "data/ is not owned by uid 10001 - the monitor cannot save. Re-run the installer."
fi

echo; echo "===== SETTINGS (secrets masked) ====="
sed -E 's#^(DISCORD_WEBHOOK_URL=).*(.{6})$#\1...\2#; s#^(HEALTHCHECK_URL=).+(.{6})$#\1...\2#' .env

echo; echo "===== SERVER RESOURCES ====="
free -m | awk 'NR==2{printf "memory: %s MB used of %s MB\n", $3, $2}'
df -h / | awk 'NR==2{printf "disk  : %s used of %s (%s)\n", $3, $2, $5}'

echo; echo "===== LOGS (last 30 min) ====="
docker compose logs --no-log-prefix --since 30m chrome-hearts-monitor 2>&1 \
  | docker compose exec -T chrome-hearts-monitor python -c "$(cat install/vps/diag_logs.py)"

echo; echo "===== STATE + LIVE PROBE ====="
OUT="$(docker compose exec -T chrome-hearts-monitor python - < install/vps/diag_state.py 2>&1)"
printf '%s\n' "$OUT" | grep -vE "^20[0-9]{2}-[0-9]{2}-[0-9]{2} .*(INFO|DEBUG)|^STATE PROBLEMS:"
STATE_PROBS="$(printf '%s\n' "$OUT" | sed -n 's/^STATE PROBLEMS: //p')"
if [ -z "$STATE_PROBS" ]; then
  problem "the in-container check did not finish (see output above)"
else
  PROBS=$((PROBS + STATE_PROBS))
fi

echo; echo "===== OVERALL ====="
if [ "$PROBS" -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "$PROBS PROBLEM(S) FOUND"; fi
