"""Offline tests: parsing, state diffing, and Discord payload construction.

Run:  python tests/test_monitor.py
No network access required - everything is stubbed against saved fixtures.
"""
import json
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test/test")

import config  # noqa: E402
import notifier  # noqa: E402
import scraper  # noqa: E402
from monitor import run_cycle  # noqa: E402
from store import Store  # noqa: E402

PASS, FAIL = [], []


def check(label, condition, detail=""):
    (PASS if condition else FAIL).append(label)
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{(' -> ' + str(detail)) if detail and not condition else ''}")


class FakeFetcher:
    """Serves fixtures instead of hitting the network."""

    def __init__(self, category_html, pdp_html=None):
        self.category_html = category_html
        self.pdp_html = pdp_html
        self.etag_store = {}
        self.requested = []

    def get(self, url, use_cache=True, attempts=3):
        self.requested.append(url)
        if url.endswith(".xml"):
            return 200, "<urlset><loc>https://www.chromehearts.com/scents</loc></urlset>"
        if url.rstrip("/") == config.BASE_URL:
            return 200, self.category_html
        if url.endswith(".html") and self.pdp_html:
            return 200, self.pdp_html
        return 200, self.category_html


def test_parsing():
    print("\n[1] category page parsing")
    html = (HERE / "fixture_category.html").read_text()
    products = scraper.parse_products(html, "/scents")

    check("finds exactly the 3 product tiles", len(products) == 3, sorted(products))
    check("ignores nav / footer / external links",
          all(p.url.startswith("https://www.chromehearts.com/scents/") for p in products.values()))

    edp = products.get("162006CRYXXX271")
    check("extracts product id from URL", edp is not None)
    check("extracts name", edp and edp.name == "+22+ Eau de Parfum", edp and edp.name)
    check("extracts price", edp and edp.price == "$500", edp and edp.price)
    check("extracts absolute image URL", edp and edp.image and edp.image.startswith("https://"), edp and edp.image)
    check("in-stock item reads as in stock", edp and edp.in_stock is True)

    rollon = products.get("162008CRYXXX271")
    check("detects 'Sold Out'", rollon and rollon.in_stock is False)
    matchstick = products.get("148310SLVXXX186")
    check("detects 'Out of Stock'", matchstick and matchstick.in_stock is False)
    check("parses comma-separated price", matchstick and matchstick.price == "$1,500", matchstick and matchstick.price)
    check("handles absolute hrefs", matchstick and matchstick.url.endswith("/148310SLVXXX186.html"))


def test_url_filtering():
    print("\n[2] URL shape filtering")
    cases = {
        "/scents/22-eau-de-parfum/162006CRYXXX271.html": True,
        "/locations.html": False,
        "/magazine.html": False,
        "/scents": False,
        "/scents/22-eau-de-parfum": False,
        "/boxers-leggings/mens-boxer_brief/1234ABCD5678.html": True,
    }
    for path, expected in cases.items():
        matched = scraper.PRODUCT_PATH_RE.match(path) is not None
        check(f"{path} -> {'product' if expected else 'not a product'}", matched == expected)


def test_name_and_href_cleaning():
    print("\n[2b] tile-text cleaning (real strings from the live site)")
    from scraper import _clean_name

    for raw, want in [
        ("BOXER BRIEF - SHORTS $85 - $110", "BOXER BRIEF - SHORTS"),
        ("STENCIL SOCKS $255 OUT OF STOCK", "STENCIL SOCKS"),
        ("LEGGINGS $245", "LEGGINGS"),
        ("+22+ Eau de Parfum", "+22+ Eau de Parfum"),
        ("Matchstick Holder", "Matchstick Holder"),
    ]:
        check(f"{raw!r} -> {want!r}", _clean_name(raw) == want, _clean_name(raw))

    for href, want in [
        ("javascript:void(0);", None),
        ("mailto:a@b.com", None),
        ("/scents", "/scents"),
        ("https://instagram.com/chromehearts", None),
    ]:
        check(f"href {href!r} rejected/kept correctly", scraper._normalise_path(href) == want)


def test_enrichment():
    print("\n[3] product page enrichment (JSON-LD + Open Graph)")
    pdp = (HERE / "fixture_pdp.html").read_text()
    product = scraper.Product(pid="999123ABCXXX01Z",
                              url="https://www.chromehearts.com/scents/dagger-incense-holder/999123ABCXXX01Z.html",
                              category="/scents")
    fetcher = FakeFetcher("", pdp_html=pdp)
    scraper.enrich(product, fetcher)
    check("name from JSON-LD", product.name == "Dagger Incense Holder", product.name)
    check("price formatted from JSON-LD", product.price == "$850", product.price)
    check("image absolutised", product.image and product.image.startswith("https://www.chromehearts.com/"), product.image)
    check("availability parsed", product.in_stock is True)
    check("og:description captured", "Sterling silver" in product.extras.get("description", ""))

    print("\n[3b] enrichment with no JSON-LD (og fallback only)")
    bare = """<html><head><meta property="og:title" content="Mystery Item | Chrome Hearts">
    <meta property="og:image" content="/img/x.jpg"></head><body>Price $1,250 Sold Out</body></html>"""
    product2 = scraper.Product(pid="X", url="https://www.chromehearts.com/scents/x/X.html", category="/scents")
    scraper.enrich(product2, FakeFetcher("", pdp_html=bare))
    check("falls back to og:title and strips brand suffix", product2.name == "Mystery Item", product2.name)
    check("falls back to a price regex", product2.price == "$1,250", product2.price)
    check("detects sold out in body text", product2.in_stock is False)


def test_cycle_and_alerts():
    print("\n[4] full cycle: seed, then detect the new product")
    before = (HERE / "fixture_category.html").read_text()
    after = (HERE / "fixture_category_after.html").read_text()
    pdp = (HERE / "fixture_pdp.html").read_text()

    sent = []
    notifier._post = lambda payload, attempts=4: (sent.append(payload), True)[1]

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(os.path.join(tmp, "state.db"))

        config.CATEGORIES = ["/scents"]
        config.DISCOVER_CATEGORIES = False
        config.ENRICH_PRODUCTS = True

        result = run_cycle(store, FakeFetcher(before, pdp_html=pdp))
        check("first run seeds instead of alerting", result.get("seeded") == 3, result)
        check("seed posts exactly one 'monitor online' message", len(sent) == 1, len(sent))
        check("seed message has no embeds", "embeds" not in sent[0])

        sent.clear()
        result = run_cycle(store, FakeFetcher(before, pdp_html=pdp))
        check("unchanged page produces zero alerts", result.get("new") == 0 and not sent, result)

        sent.clear()
        result = run_cycle(store, FakeFetcher(after, pdp_html=pdp))
        check("detects exactly 1 new product", result.get("new") == 1, result)
        check("sends exactly one webhook message", len(sent) == 1, len(sent))
        embeds = sent[0].get("embeds", []) if sent else []
        check("message carries 1 embed", len(embeds) == 1, len(embeds))
        if embeds:
            embed = embeds[0]
            check("embed title is the enriched product name", embed["title"] == "Dagger Incense Holder", embed["title"])
            check("embed links to the product", embed["url"].endswith("999123ABCXXX01Z.html"))
            check("embed carries an image", "image" in embed)
            field_names = [f["name"] for f in embed["fields"]]
            check("embed shows Price + Availability + Product ID",
                  {"Price", "Availability", "Product ID"} <= set(field_names), field_names)

        sent.clear()
        result = run_cycle(store, FakeFetcher(after, pdp_html=pdp))
        check("new product is not re-alerted on the next cycle", result.get("new") == 0 and not sent, result)

        print("\n[5] restock + price-change detection")
        config.ALERT_RESTOCKS = True
        config.ALERT_PRICE_CHANGES = True
        sent.clear()
        result = run_cycle(store, FakeFetcher(after, pdp_html=pdp))
        check("matchstick still sold out -> no restock", result.get("restocks") == 0, result)

        # fixture_category_after has the roll-on back in stock and the EDP at $550
        store.conn.execute("UPDATE products SET in_stock = 0, price='$500' WHERE pid='162006CRYXXX271'")
        store.conn.commit()
        sent.clear()
        result = run_cycle(store, FakeFetcher(after, pdp_html=pdp))
        check("detects restock", result.get("restocks") == 1, result)
        check("detects price change $500 -> $550", result.get("price_changes") == 1, result)
        config.ALERT_RESTOCKS = False
        config.ALERT_PRICE_CHANGES = False

        print("\n[6a] a 200 that parses to nothing is a failure, not an empty store")

        class BlankFetcher(FakeFetcher):
            def get(self, url, use_cache=True, attempts=3):
                return 200, "<html><body>maintenance</body></html>"

        count_before = store.product_count()
        result = run_cycle(store, BlankFetcher(""))
        check("empty parse on a known catalog is an error",
              result.get("error") == "parsed-zero-products", result)
        check("empty parse forgets nothing", store.product_count() == count_before)
        sent.clear()
        check("empty parse sends no alerts", not sent)

        print("\n[6b] failure handling")
        class DeadFetcher(FakeFetcher):
            def get(self, url, use_cache=True, attempts=3):
                if url.endswith(".xml"):
                    return 0, None
                return 403, None

        result = run_cycle(store, DeadFetcher(""))
        check("a fully blocked sweep is an error, not an empty catalog",
              result.get("error") == "all-categories-failed", result)
        check("blocked sweep wipes nothing from state", store.product_count() == 4, store.product_count())
        store.close()


def test_empty_seed():
    print("\n[6c] refusing to seed an empty baseline")
    notifier._post = lambda payload, attempts=4: True
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(os.path.join(tmp, "state.db"))
        config.CATEGORIES = ["/scents"]
        config.DISCOVER_CATEGORIES = False

        class BlankFetcher(FakeFetcher):
            def get(self, url, use_cache=True, attempts=3):
                return 200, "<html><body>maintenance</body></html>"

        result = run_cycle(store, BlankFetcher(""))
        check("does not seed a zero-product baseline",
              result.get("error") == "seed-parsed-nothing", result)
        check("stays unseeded so a later good run baselines properly",
              store.get_meta("seeded") != "1")

        good = (HERE / "fixture_category.html").read_text()
        result = run_cycle(store, FakeFetcher(good, pdp_html=(HERE / "fixture_pdp.html").read_text()))
        check("the next healthy run seeds correctly", result.get("seeded") == 3, result)
        store.close()


def test_discord_chunking():
    print("\n[7] Discord payload limits")
    sent = []
    notifier._post = lambda payload, attempts=4: (sent.append(payload), True)[1]
    products = [scraper.Product(pid=f"P{i}", url=f"https://www.chromehearts.com/scents/a/P{i}.html",
                                category="/scents", name=f"Item {i}", price="$100",
                                image="https://x/y.jpg") for i in range(23)]
    embeds = [notifier.build_embed(p, "new") for p in products]
    notifier.send_embeds(embeds)
    check("23 embeds split into 3 messages", len(sent) == 3, len(sent))
    check("no message exceeds Discord's 10-embed cap", all(len(m["embeds"]) <= 10 for m in sent))
    check("each embed payload is valid JSON", all(json.dumps(m) for m in sent))


if __name__ == "__main__":
    test_parsing()
    test_url_filtering()
    test_name_and_href_cleaning()
    test_enrichment()
    test_cycle_and_alerts()
    test_empty_seed()
    test_discord_chunking()
    print(f"\n{'='*60}\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
    sys.exit(1 if FAIL else 0)
