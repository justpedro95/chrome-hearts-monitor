"""Read-only health report, run INSIDE the container. Sends nothing to Discord."""
import json, os, sys, tempfile, time
sys.path.insert(0, "/app")
import config, monitor, scraper
from store import Store

problems, notes = [], []

def ago(v):
    try:
        f = float(v)
        return time.strftime("%m-%d %H:%M UTC", time.gmtime(f)) + f" ({(time.time()-f)/3600:.1f}h ago)"
    except (TypeError, ValueError):
        return "never"

print("-- saved state --")
d = json.load(open(config.STATE_JSON))
m = d.get("meta", {})
tracked = json.loads(m.get("tracked_sections") or "[]")
print("products tracked  :", len(d.get("products", {})))
print("sections tracked  :", len(tracked), " ".join(tracked))
print("sections retired  :", " ".join(json.loads(m.get("retired_sections") or "[]")) or "none")
print("failures in a row :", m.get("consecutive_failures", "0"))
print("last heartbeat    :", ago(m.get("last_heartbeat")))
print("last new product  :", ago(m.get("last_new_product_at")))
fps = json.loads(m.get("fingerprints") or "{}")
print("probe fingerprints:", " ".join(sorted(fps)) or "NONE")
det = json.loads(m.get("section_detections") or "{}")
for s, info in sorted(det.items()):
    print(f"detected          : {s} at {ago(info.get('first_seen'))} via {info.get('via')}"
          + (f"  (site link {info['source_url']})" if info.get("source_url") else ""))
unreadable = json.loads(m.get("unreadable_new_sections") or "[]")
if unreadable:
    notes.append(f"new sections flagged unreadable: {' '.join(unreadable)}")
if int(m.get("consecutive_failures", "0") or 0) > 0:
    problems.append(f"{m.get('consecutive_failures')} failed cycles in a row")
try:
    hb_age = (time.time() - float(m.get("last_heartbeat"))) / 3600
except (TypeError, ValueError):
    hb_age = None
if config.HEARTBEAT_HOURS and (hb_age is None or hb_age > config.HEARTBEAT_HOURS + 1):
    problems.append("no heartbeat for "
                    + (f"{hb_age:.0f}h" if hb_age is not None else "ever")
                    + f" (expected every {config.HEARTBEAT_HOURS:g}h)")
if not fps:
    problems.append("the fast probe has never saved a fingerprint - it is not running")

print("\n-- live probe from this server (throwaway state; sends nothing) --")
st = Store(os.path.join(tempfile.mkdtemp(), "probe.db"))
p, f = monitor.Prober(), scraper.Fetcher()
t = time.time(); p.run(st, f, notify=False); cold = time.time() - t
t = time.time(); p.run(st, f, notify=False); warm = time.time() - t
print(f"probe duration    : {cold:.1f}s first, {warm:.1f}s repeat")
print(f"sitemap readable  : {bool(p.sitemap)}   homepage readable: {bool(p.home)}")
if not (p.sitemap and p.home):
    problems.append("this server cannot read the sitemap/homepage - possibly blocked")
live = scraper.sections_from_sitemap(p.sitemap) | scraper.sections_from_homepage(p.home)[0]
print("sections live now :", " ".join(sorted(live)))
missing = sorted(live - set(tracked))
print("live, NOT tracked :", " ".join(missing) or "none")
if missing:
    notes.append(f"live sections not yet tracked (picked up on the next sweep): {' '.join(missing)}")
localised = scraper.page_is_localised(p.home or "")
print("non-USD render    :", localised)
if localised:
    problems.append("the site is answering this server in a non-USD currency")
worst = config.FAST_INTERVAL_SECONDS + max(cold, warm) + 2
print(f"new-category detection, worst case: ~{worst:.0f}s (target 30s)")
if worst > 30:
    problems.append(f"worst-case detection ~{worst:.0f}s exceeds 30s")

print("\n-- verdict --")
for n in notes:
    print("NOTE   ", n)
for q in problems:
    print("PROBLEM", q)
print(f"STATE PROBLEMS: {len(problems)}")
