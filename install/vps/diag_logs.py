"""Summarise the container log piped on stdin: sweep cadence, errors, warnings."""
import datetime, re, statistics, sys
sweeps, errors, warns = [], [], []
for line in sys.stdin:
    m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s+(\w+)", line)
    if not m:
        if "Traceback" in line:
            errors.append(line.strip())
        continue
    if "cycle:" in line:
        sweeps.append(datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
    if m.group(2) in ("ERROR", "CRITICAL"):
        errors.append(line.strip())
    elif m.group(2) == "WARNING":
        warns.append(line.strip())
gaps = [(b - a).total_seconds() for a, b in zip(sweeps, sweeps[1:])]
print(f"full sweeps, last 30 min : {len(sweeps)}")
if gaps:
    print(f"gap between sweeps      : median {statistics.median(gaps):.0f}s, worst {max(gaps):.0f}s (expect ~60s)")
print(f"errors / warnings       : {len(errors)} / {len(warns)}")
for e in errors[-5:]:
    print("  ERR ", e[:170])
for w in sorted({w[20:] for w in warns})[-5:]:
    print("  WARN", w[:170])
