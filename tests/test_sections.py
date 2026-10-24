"""New-category detection: the signal that matters most on this storefront.

Chrome Hearts creates a category for a single drop, links it from one place,
and removes it afterwards. Two real misses came from this: /sweatpants (linked
only by its cgid storefront URL) and a one-time category that appeared in the
SITEMAP as a cgid URL, which the sitemap parser threw away.
"""
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
import scraper  # noqa: E402
from monitor import run_cycle  # noqa: E402
from store import Store  # noqa: E402
from test_monitor import FakeFetcher, check, PASS, FAIL  # noqa: E402

GOOD = (HERE / "fixture_category.html").read_text()
PDP = (HERE / "fixture_pdp.html").read_text()

sent = []
notifier._post = lambda payload, attempts=4: (sent.append(payload), True)[1]

SITEMAP_WITH_CGID = """<urlset>
<url><loc>https://www.chromehearts.com/scents</loc></url>
<url><loc>https://www.chromehearts.com/on/demandware.store/Sites-ChromeHearts-Site/en_US/Search-Show?cgid=HOLIDAY_DROP</loc></url>
</urlset>"""


class SiteFetcher(FakeFetcher):
    """Homepage has no new section; the SITEMAP carries the cgid one."""

    def get(self, url, use_cache=True, attempts=3):
        if url.endswith(".xml"):
            return 200, SITEMAP_WITH_CGID
        return 200, GOOD


def test_sitemap_cgid():
    print("\n[1] a one-time category that only appears in the sitemap")
    found = scraper.discover_categories(SiteFetcher(""))
    check("the cgid section is discovered from the sitemap",
          "/holiday-drop" in found, found)
    check("ordinary sitemap sections still work", "/scents" in found, found)


def test_new_category_alerts():
    print("\n[2] a new category is announced to Discord immediately")
    config.CATEGORIES = []
    config.DISCOVER_CATEGORIES = True
    config.ALERT_NEW_CATEGORIES = True
    config.ENRICH_PRODUCTS = False

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(os.path.join(tmp, "s.db"))
        run_cycle(store, SiteFetcher(""))          # seed run
        sent.clear()
        run_cycle(store, SiteFetcher(""))
        check("a known category is not re-announced", not sent, len(sent))

        class PlusNew(SiteFetcher):
            def get(self, url, use_cache=True, attempts=3):
                if url.endswith(".xml"):
                    return 200, SITEMAP_WITH_CGID.replace(
                        "cgid=HOLIDAY_DROP", "cgid=HOLIDAY_DROP</loc></url>"
                        "<url><loc>https://www.chromehearts.com/on/x/en_US/Search-Show?cgid=FW26_JACKETS")
                return 200, GOOD

        sent.clear()
        run_cycle(store, PlusNew(""))
        texts = [m.get("content", "") for m in sent]
        joined = " ".join(texts)
        check("the new category is announced", "fw26-jackets" in joined.lower(), joined[:200])
        check("the message carries a clickable URL",
              "chromehearts.com/fw26-jackets" in joined, joined[:200])

        sent.clear()
        run_cycle(store, PlusNew(""))
        check("it is announced once, not every cycle",
              not any("fw26" in (m.get("content") or "").lower() for m in sent), sent)
        store.close()


def test_bootstrap_is_silent():
    print("\n[2b] upgrading an existing monitor does not announce old sections")
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(os.path.join(tmp, "s.db"))
        for c in ("/scents", "/baccarat", "/socks"):
            store.add_category(c)
        store.commit()
        sent.clear()
        run_cycle(store, SiteFetcher(""))
        joined = " ".join(m.get("content", "") for m in sent)
        check("no 'new category' blast on the first upgraded cycle",
              "New category" not in joined, joined[:200])
        tracked = set(json.loads(store.get_meta("tracked_sections", "[]")))
        check("but everything is now tracked", "/holiday-drop" in tracked, sorted(tracked))

        class PlusNew(SiteFetcher):
            def get(self, url, use_cache=True, attempts=3):
                if url.endswith(".xml"):
                    return 200, SITEMAP_WITH_CGID.replace(
                        "cgid=HOLIDAY_DROP", "cgid=HOLIDAY_DROP</loc></url>"
                        "<url><loc>https://www.chromehearts.com/on/x/en_US/Search-Show?cgid=NEW_AFTER_BOOT")
                return 200, GOOD

        sent.clear()
        run_cycle(store, PlusNew(""))
        joined = " ".join(m.get("content", "") for m in sent)
        check("a genuinely new one after bootstrap IS announced",
              "new-after-boot" in joined.lower(), joined[:200])
        store.close()


def test_sections_are_sticky():
    print("\n[3] a section that leaves the nav keeps being scraped")
    config.CATEGORIES = []
    config.DISCOVER_CATEGORIES = True

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(os.path.join(tmp, "s.db"))
        run_cycle(store, SiteFetcher(""))
        tracked = set(json.loads(store.get_meta("tracked_sections", "[]")))
        check("the drop section is tracked", "/holiday-drop" in tracked, sorted(tracked))

        class NavDropped(FakeFetcher):
            """The section is gone from the nav AND the sitemap."""
            def get(self, url, use_cache=True, attempts=3):
                if url.endswith(".xml"):
                    return 200, "<urlset><url><loc>https://www.chromehearts.com/scents</loc></url></urlset>"
                return 200, GOOD

        sent.clear()
        result = run_cycle(store, NavDropped(""))
        still = set(json.loads(store.get_meta("tracked_sections", "[]")))
        check("it is STILL tracked after vanishing from the site's links",
              "/holiday-drop" in still, sorted(still))
        check("no spurious error", not result.get("error"), result)
        store.close()


def test_retire_only_after_sustained_404():
    print("\n[4] a section is retired only after sustained 404s")
    from monitor import retire_dead_sections, RETIRE_AFTER_404S
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(os.path.join(tmp, "s.db"))
        for _ in range(RETIRE_AFTER_404S - 1):
            retire_dead_sections(store, {"/dead"}, set())
        check("not retired one short of the threshold",
              not json.loads(store.get_meta("retired_sections", "[]")))
        retire_dead_sections(store, {"/dead"}, set())
        check("retired at the threshold",
              json.loads(store.get_meta("retired_sections", "[]")) == ["/dead"])

        retire_dead_sections(store, {"/flaky"}, set())
        retire_dead_sections(store, set(), {"/flaky"})
        counts = json.loads(store.get_meta("gone_counts", "{}"))
        check("a recovered section's 404 count resets", "/flaky" not in counts, counts)
        store.close()


if __name__ == "__main__":
    test_sitemap_cgid()
    test_new_category_alerts()
    test_bootstrap_is_silent()
    test_sections_are_sticky()
    test_retire_only_after_sustained_404()
    print(f"\n{'='*60}\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1 if FAIL else 0)
