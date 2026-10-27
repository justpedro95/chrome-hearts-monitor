# Running the monitor on an always-on Mac

GitHub Actions cannot keep time. Measured against this repo's own commit
history over 69 hours: a 5-minute cron produced a **median 14-minute** gap, a
worst gap of **185 minutes**, and delivered only about **36%** of its scheduled
runs. That is fine for a catalogue watcher and useless for a drop.

A launchd job is a real always-on process, so 60 seconds means 60 seconds.

## Install

```bash
bash install/macos/install.sh
```

It will ask for your Discord webhook once (not echoed, written to a
git-ignored `.env` with mode 600). Everything else is automatic: virtualenv,
dependencies, state seeding, a webhook smoke test, and the launchd job.

**It seeds local state from `state/products.json`**, so every product the
GitHub deployment already knew about stays known and nothing is re-alerted.

Re-running it is safe and is how you upgrade: it keeps your webhook and your
state, installs the new code, and restarts the job.

## Two things the script cannot do for you

**1. Disable the GitHub Actions schedule**, or you get every alert twice —
once fast from the Mac, once up to an hour later from Actions.
Open [the monitor workflow](https://github.com/justpedro95/chrome-hearts-monitor/actions/workflows/monitor.yml)
→ the `...` menu, top right → **Disable workflow**.

*Optional:* instead of disabling it, point it at a second Discord webhook as a
cold backstop for when the Mac is down — set a repository secret
`DISCORD_WEBHOOK_URL` for a different channel.

**2. Stop the Mac sleeping.** launchd does not run jobs while the machine is
asleep. System Settings → Displays → Advanced (older macOS: Energy Saver) →
turn on **"Prevent automatic sleeping when the display is off"**.

Verify with `pmset -g` — `sleep` should read `0`.

## Day to day

```bash
LOG=~/Library/Logs/chrome-hearts-monitor/monitor.log
tail -f "$LOG"                 # watch it work
grep -E "NEW|SECTION" "$LOG"   # everything it has found
```

| Task | Command |
|---|---|
| Stop | `launchctl bootout gui/$(id -u)/bet.jackpot.chromehearts-monitor` |
| Start | `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/bet.jackpot.chromehearts-monitor.plist` |
| Is it alive? | `launchctl print gui/$(id -u)/bet.jackpot.chromehearts-monitor \| head -5` |
| Check the parse | `.venv/bin/python monitor.py --selftest` |
| Re-baseline | `.venv/bin/python monitor.py --reseed` |
| Truncate the log | `: > "$LOG"` |
| Uninstall | `launchctl bootout gui/$(id -u)/bet.jackpot.chromehearts-monitor && rm ~/Library/LaunchAgents/bet.jackpot.chromehearts-monitor.plist` |

The log grows about 150 KB/day at `LOG_LEVEL=INFO`. Truncate it when you think of it.

## Going faster than 60s

Edit `POLL_INTERVAL_SECONDS` in `.env` and restart. 30 is reasonable. Below
that you are mostly raising the odds of their CDN blocking your shop's IP for
no real gain — the request budget is better spent on being punctual than on
being frequent, and 60s punctual beats 14 minutes ragged by 14x.
