"""A NEW section that yields nothing readable must be reported, once.

Real incident: /sports-mesh was announced, then recorded zero products, and
nothing said so. A section's public alias can also die while the site still
lists it (/sweatpants 404s), so alerts must carry a link that works.
"""
import json
import os
import pathlib
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.com/api/webhooks/test/test"

import config  # noqa: E402
import monitor  # noqa: E402
import notifier  # noqa: E402
import scraper  # noqa: E402
from store import Store  # noqa: E402
from test_monitor import FakeFetcher, check, PASS, FAIL  # noqa: E402

GOOD = (HERE / "fixture_category.html").read_text()
HOME_RENDER = ('<html><head><link rel="canonical" href="https://www.chromehearts.com/">'
               '</head><body><a href="/scents">S</a></body></html>')
EMPTY_GRID = ('<html><head><link rel="canonical" href="https://www.chromehearts.com/new-drop">'
              '</head><body><div class="product-grid"></div></body></html>')
RAW = "https://www.chromehearts.com/on/demandware.store/Sites-ChromeHearts-Site/en_US/Search-Show?cgid=NEW_DROP"

sent = []
notifier._post = lambda payload, attempts=4: (sent.append(payload), True)[1]


def texts():
    return " ".join(p.get("content") or "" for p in sent)


def detected(store, section, ago=0, source=None):
    det = json.loads(store.get_meta("section_detections", "{}"))
    rec = {"first_seen": time.time() - ago, "via": "sitemap"}
    if source:
        rec["source_url"] = source
    det[section] = rec
    store.set_meta("section_detections", json.dumps(det))
    store.commit()


if __name__ == "__main__":
    print("\n[1] spotting a taken-down section")
    check("canonical = homepage -> renders homepage", scraper.renders_homepage(HOME_RENDER))
    check("a real category page does not", not scraper.renders_homepage(EMPTY_GRID))
    check("no canonical tag -> not assumed", not scraper.renders_homepage(GOOD))

    print("\n[2] the site's own link is kept for each cgid section")
    xml = f"<urlset><url><loc>{RAW}</loc></url></urlset>"
    src = scraper.cgid_sources(xml)
    check("alias -> original URL", src.get("/new-drop") == RAW, src)
    check("&amp; decoded from HTML", scraper.cgid_sources(
        '<a href="https://www.chromehearts.com/x/Search-Show?a=1&amp;cgid=FOO">').get("/foo")
        == "https://www.chromehearts.com/x/Search-Show?a=1&cgid=FOO")

    with tempfile.TemporaryDirectory() as tmp:
        print("\n[3] the announcement carries a fallback link")
        store = Store(os.path.join(tmp, "a.db"))
        store.set_meta("tracked_sections", "[]"); store.commit()
        config.ALERT_NEW_CATEGORIES = True
        sent.clear()
        monitor.announce_sections(store, ["/new-drop"], via="sitemap", sources={"/new-drop": RAW})
        check("alias link present", "chromehearts.com/new-drop" in texts(), texts()[:200])
        check("fallback link present", RAW in texts(), texts()[:300])
        det = json.loads(store.get_meta("section_detections"))
        check("source stored for later warnings", det["/new-drop"].get("source_url") == RAW)
        store.close()

        print("\n[4] new section, renders the homepage -> one warning")
        store = Store(os.path.join(tmp, "b.db"))
        detected(store, "/new-drop", source=RAW)
        sent.clear()
        w = monitor.new_section_watch(store, {"/new-drop": 0}, set(), {"/new-drop"})
        check("warned", len(w) == 1 and len(sent) == 1, len(sent))
        check("says it shows the homepage", "homepage" in texts(), texts()[:200])
        check("gives the site's own link", RAW in texts(), texts()[:300])
        sent.clear()
        monitor.new_section_watch(store, {"/new-drop": 0}, set(), {"/new-drop"})
        check("only once", not sent, len(sent))
        store.close()

        print("\n[5] new section whose link 404s")
        store = Store(os.path.join(tmp, "c.db"))
        detected(store, "/new-drop")
        sent.clear()
        monitor.new_section_watch(store, {}, {"/new-drop"}, set())
        check("warned about the 404", "404" in texts(), texts()[:200])
        store.close()

        print("\n[6] new section, real page, no readable products")
        store = Store(os.path.join(tmp, "d.db"))
        detected(store, "/new-drop")
        sent.clear()
        monitor.new_section_watch(store, {"/new-drop": 0}, set(), set())
        check("warned it can't read products", "can't read any products" in texts(), texts()[:200])
        store.close()

        print("\n[7] cases that must stay QUIET")
        store = Store(os.path.join(tmp, "e.db"))
        detected(store, "/new-drop")
        sent.clear()
        monitor.new_section_watch(store, {"/new-drop": 4}, set(), set())
        check("new section WITH products: quiet", not sent, len(sent))
        monitor.new_section_watch(store, {"/hat": 0, "/shop": 0}, set(), {"/hat", "/shop"})
        check("old taken-down sections (/hat, /shop): quiet", not sent, len(sent))
        detected(store, "/old-drop", ago=7 * 3600)
        monitor.new_section_watch(store, {"/old-drop": 0}, set(), {"/old-drop"})
        check("a section first seen >6h ago: quiet", not sent, len(sent))
        store.close()

        print("\n[8] end to end: announced by the probe, empty on the sweep")
        config.CATEGORIES = []
        config.DISCOVER_CATEGORIES = True
        config.ENRICH_PRODUCTS = False
        store = Store(os.path.join(tmp, "f.db"))
        store.set_meta("seeded", "1")
        store.set_meta("tracked_sections", json.dumps(["/scents"]))
        store.commit()

        class Site(FakeFetcher):
            def get(self, url, use_cache=True, attempts=3):
                if url.endswith(".xml"):
                    return 200, ("<urlset><url><loc>https://www.chromehearts.com/scents</loc></url>"
                                 f"<url><loc>{RAW}</loc></url></urlset>")
                if url.endswith("/new-drop"):
                    return 200, HOME_RENDER
                return 200, GOOD

        site, prober = Site(""), monitor.Prober()
        sent.clear()
        prober.run(store, site)
        check("probe announced it", "new-drop" in texts(), texts()[:160])
        sent.clear()
        monitor.run_cycle(store, site)
        check("the sweep then warned it is unreadable",
              "Check this one yourself" in texts() and "homepage" in texts(), texts()[:240])
        store.close()

    print(f"\n{'='*60}\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1 if FAIL else 0)
