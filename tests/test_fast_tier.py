"""The fast tier: a new category must reach Discord within ~FAST_INTERVAL.

Includes an end-to-end run of the REAL main loop with a new category appearing
while a full sweep is in flight, timed from the moment it appears to the
moment the Discord message is sent.
"""
import json
import os
import pathlib
import sys
import tempfile
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.com/api/webhooks/test/test"

import config  # noqa: E402
import monitor  # noqa: E402
import notifier  # noqa: E402
from store import Store  # noqa: E402
from test_monitor import check, PASS, FAIL  # noqa: E402

GOOD = (HERE / "fixture_category.html").read_text()
SECTIONS = ["scents", "baccarat", "socks", "intimates", "boxers-leggings",
            "underwear", "scarf", "hat"]


def sitemap(extra=()):
    locs = [f"https://www.chromehearts.com/{s}" for s in SECTIONS]
    locs += [f"https://www.chromehearts.com/on/x/en_US/Search-Show?cgid={c}" for c in extra]
    return "<urlset>" + "".join(f"<url><loc>{l}</loc></url>" for l in locs) + "</urlset>"


def home(token="a"):
    # A rotating token, like a real CSRF value: must NOT read as a change.
    return f'<html><body><input name="csrf" value="{token}"><a href="/scents">S</a></body></html>'


class Site:
    def __init__(self, sweep_fetch_seconds=0.0):
        self.extra = []
        self.token = 0
        self.not_modified = False
        self.delay = sweep_fetch_seconds
        self.etag_store = {}
        self.diagnostics = {}

    def get(self, url, use_cache=True, attempts=3):
        if self.not_modified and use_cache:
            return 304, None
        if url.endswith(".xml"):
            return 200, sitemap(self.extra)
        if url.rstrip("/").endswith(".com"):
            self.token += 1
            return 200, home(str(self.token))
        if self.delay:
            time.sleep(self.delay)
        return 200, GOOD


def seeded_store(path):
    store = Store(path)
    store.set_meta("seeded", "1")
    store.set_meta("tracked_sections", json.dumps(["/" + s for s in SECTIONS]))
    store.commit()
    return store


sent = []
notifier._post = lambda payload, attempts=4: (sent.append((time.time(), payload)), True)[1]


def unit_tests():
    config.FAST_INTERVAL_SECONDS = 10
    config.ALERT_NEW_CATEGORIES = True
    with tempfile.TemporaryDirectory() as tmp:
        print("\n[1] baseline probe: records, announces nothing")
        store = seeded_store(os.path.join(tmp, "a.db"))
        site, prober = Site(), monitor.Prober()
        sent.clear()
        changed, new = prober.run(store, site)
        check("no change on first probe", changed == [] and new == [], (changed, new))
        check("nothing sent", not sent, len(sent))

        print("\n[2] a rotating homepage token is NOT a change")
        changed, new = prober.run(store, site)
        check("token churn ignored", changed == [], changed)
        check("no sweep requested", prober.sweep_requested is False)

        print("\n[3] a 304 reuses the cached body")
        site.not_modified = True
        changed, new = prober.run(store, site)
        check("304 is not a change", changed == [] and new == [], (changed, new))
        site.not_modified = False

        print("\n[4] a new category in the sitemap is announced immediately")
        site.extra = ["WINTER_DROP"]
        changed, new = prober.run(store, site)
        check("announced on THIS probe", new == ["/winter-drop"], new)
        check("sitemap marked as the source", "sitemap" in changed, changed)
        check("a sweep is requested", prober.sweep_requested is True)
        body = sent[-1][1]["content"] if sent else ""
        check("message has the URL", "chromehearts.com/winter-drop" in body, body[:160])
        check("message says how it was seen", "via the sitemap" in body, body[:200])
        det = json.loads(store.get_meta("section_detections", "{}"))
        check("detection is recorded with its source",
              det.get("/winter-drop", {}).get("via") == "sitemap", det)

        print("\n[5] never announced twice")
        sent.clear()
        prober.sweep_requested = False
        changed, new = prober.run(store, site)
        check("no repeat", new == [] and not sent, (new, len(sent)))
        store.close()

        print("\n[6] no announcements before section tracking is bootstrapped")
        bare = Store(os.path.join(tmp, "b.db"))
        site2, p2 = Site(), monitor.Prober()
        site2.extra = ["SOMETHING"]
        sent.clear()
        _, new = p2.run(bare, site2)
        check("silent on an un-bootstrapped store", new == [] and not sent, (new, len(sent)))
        bare.close()


def end_to_end():
    print("\n[7] END TO END: new category appears MID-SWEEP; time to Discord")
    fast = 0.4
    per_fetch = 0.3                                  # 8 sections => ~2.4s sweeps
    config.FAST_TIER = True
    config.FAST_INTERVAL_SECONDS = fast
    config.POLL_INTERVAL_SECONDS = 0.1               # sweeps back to back
    config.REQUEST_DELAY_SECONDS = 0
    config.CATEGORIES = []
    config.DISCOVER_CATEGORIES = True
    config.ENRICH_PRODUCTS = False
    config.HEARTBEAT_HOURS = 0
    config.HEALTHCHECK_URL = ""
    config.ALERT_NEW_CATEGORIES = True

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "e2e.db")
        seeded_store(path).close()
        site = Site(sweep_fetch_seconds=per_fetch)
        monitor.open_store = lambda: Store(path)
        monitor.Fetcher = lambda etag_store=None: site
        sent.clear()
        appeared = {}

        def drive():
            time.sleep(1.5)                          # well inside a sweep
            appeared["t"] = time.time()
            site.extra = ["SURPRISE_DROP"]
            deadline = time.time() + 10
            while time.time() < deadline:
                if any("surprise-drop" in (p.get("content") or "") for _, p in sent):
                    break
                time.sleep(0.02)
            monitor._running = False

        threading.Thread(target=drive, daemon=True).start()
        sys.argv = ["monitor.py"]
        monitor._running = True
        monitor.main()

        hits = [t for t, p in sent if "surprise-drop" in (p.get("content") or "")]
        sweep_len = per_fetch * len(SECTIONS)
        if hits:
            latency = hits[0] - appeared["t"]
            print(f"      appeared -> Discord: {latency:.2f}s "
                  f"(probe interval {fast}s, one sweep takes ~{sweep_len:.1f}s)")
            check("announced within one probe interval + one fetch",
                  latency <= fast + per_fetch + 0.5, f"{latency:.2f}s")
            check("faster than waiting for the sweep to finish",
                  latency < sweep_len, f"{latency:.2f}s vs {sweep_len:.1f}s")
        else:
            check("the new category was announced at all", False, "never announced")
        check("announced exactly once", len(hits) == 1, len(hits))


if __name__ == "__main__":
    unit_tests()
    end_to_end()
    print(f"\n{'='*60}\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1 if FAIL else 0)
