"""The 15-second fast tier: detect a change cheaply, then sweep."""
import json
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.com/api/webhooks/test/test"

import config  # noqa: E402
import notifier  # noqa: E402
from monitor import fast_probe  # noqa: E402
from store import Store  # noqa: E402
from test_monitor import check, PASS, FAIL  # noqa: E402

notifier._post = lambda payload, attempts=4: True

SITEMAP_A = "<urlset><url><loc>https://www.chromehearts.com/scents</loc></url></urlset>"
SITEMAP_B = SITEMAP_A.replace("</urlset>",
    "<url><loc>https://www.chromehearts.com/on/x/en_US/Search-Show?cgid=DROP</loc></url></urlset>")
HOME = "<html><body>home</body></html>"


class Probe:
    """Counts requests so we can prove the fast tier is actually cheap."""

    def __init__(self, sitemap=SITEMAP_A, home=HOME, not_modified=False):
        self.sitemap, self.home, self.not_modified = sitemap, home, not_modified
        self.requests = []
        self.etag_store = {}

    def get(self, url, use_cache=True, attempts=3):
        self.requests.append(url)
        if self.not_modified:
            return 304, None
        return (200, self.sitemap) if url.endswith(".xml") else (200, self.home)


if __name__ == "__main__":
    config.FAST_TIER = True
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(os.path.join(tmp, "s.db"))

        print("\n[1] first probe records a baseline and reports no change")
        f = Probe()
        check("no change on first sighting", fast_probe(store, f, tick=1) == [], None)
        check("only the sitemap is fetched on an odd tick",
              f.requests == ["https://www.chromehearts.com/sitemap_0.xml"], f.requests)

        print("\n[2] an unchanged sitemap is silent")
        f = Probe()
        check("still no change", fast_probe(store, f, tick=3) == [], None)

        print("\n[3] a 304 costs nothing and reports no change")
        f = Probe(not_modified=True)
        check("304 is not a change", fast_probe(store, f, tick=5) == [], None)

        print("\n[4] a new drop category in the sitemap IS a change")
        f = Probe(sitemap=SITEMAP_B)
        moved = fast_probe(store, f, tick=7)
        check("the sitemap change is detected", moved == ["/sitemap_0.xml"], moved)
        check("and it is only reported once",
              fast_probe(store, Probe(sitemap=SITEMAP_B), tick=9) == [], None)

        print("\n[5] the homepage is probed on alternate ticks only")
        f = Probe(sitemap=SITEMAP_B)
        fast_probe(store, f, tick=10)
        check("even tick fetches sitemap + homepage", len(f.requests) == 2, f.requests)
        f = Probe(sitemap=SITEMAP_B)
        fast_probe(store, f, tick=11)
        check("odd tick fetches sitemap only", len(f.requests) == 1, f.requests)

        print("\n[6] a homepage change also triggers a sweep")
        f = Probe(sitemap=SITEMAP_B, home="<html><body>NEW BANNER</body></html>")
        moved = fast_probe(store, f, tick=12)
        check("homepage change detected", moved == ["/"], moved)

        print("\n[7] fingerprints survive a restart")
        prints = json.loads(store.get_meta("fingerprints", "{}"))
        check("both endpoints are remembered in state",
              set(prints) == {"/sitemap_0.xml", "/"}, sorted(prints))
        store.close()
        store2 = Store(os.path.join(tmp, "s.db"))
        check("a restarted process does not re-report a change",
              fast_probe(store2, Probe(sitemap=SITEMAP_B,
                                       home="<html><body>NEW BANNER</body></html>"),
                         tick=14) == [], None)
        store2.close()

    print(f"\n{'='*60}\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1 if FAIL else 0)
