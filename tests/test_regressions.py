"""Regressions for failures found in production. Each name is a real incident."""
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
import notifier  # noqa: E402
import scraper  # noqa: E402
from monitor import run_cycle  # noqa: E402
from store import Store  # noqa: E402
from test_monitor import FakeFetcher, check, PASS, FAIL  # noqa: E402

GOOD = (HERE / "fixture_category.html").read_text()
AFTER = (HERE / "fixture_category_after.html").read_text()
PDP = (HERE / "fixture_pdp.html").read_text()
SWEATS = (HERE / "fixture_sweatpants.html").read_text()

sent = []
notifier._post = lambda payload, attempts=4: (sent.append(payload), True)[1]


def test_two_segment_products():
    print("\n[A] a product at /slug/ID.html is found (the missed sweatpants drop)")
    products = scraper.parse_products(SWEATS, "/sweatpants")
    check("finds the 2-segment product", "190372BLKXXX01W" in products, sorted(products))
    item = products.get("190372BLKXXX01W")
    check("name cleaned of the price", item and item.name == "BLACK SWEATPANTS", item and item.name)
    check("price parsed", item and item.price == "$730", item and item.price)
    check("only the product, no nav junk", len(products) == 1, sorted(products))


def test_cgid_discovery():
    print("\n[B] a section linked only by its cgid storefront URL is discovered")

    class NavFetcher(FakeFetcher):
        def get(self, url, use_cache=True, attempts=3):
            if url.endswith(".xml"):
                return 200, "<urlset></urlset>"
            return 200, SWEATS

    found = scraper.discover_categories(NavFetcher(""))
    check("/sweatpants discovered from the cgid link", "/sweatpants" in found, found)
    check("/scents still discovered normally", "/scents" in found, found)
    check("javascript:void(0) produces no bogus section",
          not any("void" in c for c in found), found)
    check("magazine.html is not treated as a section",
          "/magazine" not in found and "/magazine.html" not in found, found)


def test_304_is_healthy():
    print("\n[C] 304 Not Modified is a healthy cycle, not a failure")
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(os.path.join(tmp, "s.db"))
        config.CATEGORIES = ["/scents"]
        config.DISCOVER_CATEGORIES = False
        run_cycle(store, FakeFetcher(GOOD, pdp_html=PDP))
        sent.clear()

        class NotModified(FakeFetcher):
            def get(self, url, use_cache=True, attempts=3):
                return (200, "<urlset></urlset>") if url.endswith(".xml") else (304, None)

        result = run_cycle(store, NotModified(""))
        check("not an error", not result.get("error"), result)
        check("zero new products", result.get("new") == 0, result)
        check("known products replayed, not lost", result.get("total") == 3, result)
        check("nothing sent to Discord", not sent, len(sent))
        store.close()


def test_404_is_not_a_failure():
    print("\n[D] a 404 path is 'not a section', not a scrape failure")
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(os.path.join(tmp, "s.db"))
        config.CATEGORIES = ["/scents", "/not-a-real-section"]
        config.DISCOVER_CATEGORIES = False

        class MixedFetcher(FakeFetcher):
            def get(self, url, use_cache=True, attempts=3):
                if url.endswith(".xml"):
                    return 200, "<urlset></urlset>"
                if "not-a-real-section" in url:
                    return 404, None
                return 200, GOOD

        run_cycle(store, MixedFetcher(""))
        result = run_cycle(store, MixedFetcher(""))
        check("404 is not counted as a failure", result.get("failures") == 0, result)
        check("the real section still parsed", result.get("total") == 3, result)
        store.close()


def test_state_churn():
    print("\n[E] state only changes when the catalogue changes")
    from jsonstore import JsonStore
    config.STATE_BACKEND = "json"
    config.CATEGORIES = ["/scents"]
    config.DISCOVER_CATEGORIES = False
    config.HEARTBEAT_HOURS = 0

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state", "products.json")

        def run(html):
            config.STATE_JSON = path
            store = JsonStore(path)
            out = run_cycle(store, FakeFetcher(html, pdp_html=PDP))
            store.close()
            return out

        run(GOOD)
        first = pathlib.Path(path).read_bytes()
        run(GOOD)
        check("a quiet cycle leaves state byte-identical",
              pathlib.Path(path).read_bytes() == first)
        run(AFTER)
        changed = pathlib.Path(path).read_bytes()
        check("a real change does rewrite state", changed != first)
        run(AFTER)
        check("then goes quiet again", pathlib.Path(path).read_bytes() == changed)
        check("all 4 products retained",
              len(json.loads(changed)["products"]) == 4)


if __name__ == "__main__":
    test_two_segment_products()
    test_cgid_discovery()
    test_304_is_healthy()
    test_404_is_not_a_failure()
    test_state_churn()
    print(f"\n{'='*60}\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1 if FAIL else 0)
