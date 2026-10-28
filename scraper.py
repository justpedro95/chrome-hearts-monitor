"""Fetching and parsing for chromehearts.com (Salesforce Commerce Cloud storefront).

Design notes
------------
The storefront renders product tiles server-side, so plain HTML is enough - no
headless browser needed. Rather than depend on SFCC theme class names (which the
brand can rename at any time), the parser keys off two things that are far more
stable:

  1. The product URL shape:  /<category>/<slug>/<PRODUCT_ID>.html
     The product id is the primary key. A brand-new id == a new product.
  2. Open Graph / JSON-LD metadata on the product page, used to fill in the
     name, price and image when tile scraping comes up short.

Tile-level extraction is best-effort and heuristic; if it breaks, the monitor
still detects new products correctly and just posts a thinner embed.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import config

log = logging.getLogger("scraper")

# /<category>/<slug>/<PRODUCTID>.html  e.g. /scents/22-eau-de-parfum/162006CRYXXX271.html
# Product URLs appear in TWO shapes on this storefront:
#   /scents/22-eau-de-parfum/162006CRYXXX271.html   (section/slug/id)
#   /black-sweatpants/190372BLKXXX01W.html          (slug/id)
# The id is an uppercase alphanumeric SKU containing at least one digit - that
# is what separates a product from an editorial page like /magazine.html.
PRODUCT_PATH_RE = re.compile(
    r"^/(?P<path>[a-z0-9][a-z0-9\-_]*(?:/[a-z0-9][a-z0-9\-_]*)?)/(?P<pid>[A-Z0-9]{6,32})\.html$"
)
PRODUCT_HREF_RE = re.compile(
    r"\"(/[a-z0-9][a-z0-9\-_]*(?:/[a-z0-9][a-z0-9\-_]*)?/[A-Z0-9]{6,32}\.html)\""
)
# New sections are linked from the nav by their un-aliased storefront URL, e.g.
#   /on/demandware.store/Sites-ChromeHearts-Site/en_US/Search-Show?cgid=SWEATPANTS
# robots.txt disallows that form, so we derive the public alias (/sweatpants)
# and request that instead - the disallowed URL is never fetched.
CGID_RE = re.compile(r"[?&](?:amp;)?cgid=([A-Za-z0-9_\-]+)", re.I)
#: A plain internal link to a single-segment path, e.g. href="/sweatpants".
SECTION_HREF_RE = re.compile(r"href=[\"'](?:https?://(?:www\.)?chromehearts\.com)?/([a-z0-9][a-z0-9\-]*)/?[\"'#?]", re.I)


def is_product_id(pid):
    return bool(pid) and pid.isupper() and any(c.isdigit() for c in pid)


def match_product_path(path):
    match = PRODUCT_PATH_RE.match(path or "")
    return match if match and is_product_id(match.group("pid")) else None


def extract_sections(html):
    """Every candidate shoppable section linked from a page.

    Run against the homepage, the sitemap AND every category page we already
    fetch, so a one-time drop category is found wherever it happens to be
    linked - the nav, a hero banner, the sitemap, or another section's page.
    Chrome Hearts creates these for a single drop and removes them later, so
    "it was in the nav last cycle" is not a reliable way to find them.
    """
    sections = set()
    if not html:
        return sections
    for match in CGID_RE.finditer(html):
        slug = match.group(1).strip().lower().replace("_", "-")
        if slug and slug not in NON_CATEGORY:
            sections.add("/" + slug)
    for match in SECTION_HREF_RE.finditer(html):
        seg = match.group(1).lower()
        if seg.endswith(".html") or seg in NON_CATEGORY:
            continue
        sections.add("/" + seg)
    return sections


def category_from_cgid(href):
    found = CGID_RE.search(href or "")
    if not found:
        return None
    slug = found.group(1).strip().lower().replace("_", "-")
    return "/" + slug if slug else None
# Only a bare or US$ price counts. Runners occasionally geolocate to Canada or
# Mexico and the storefront answers in local currency, which was going into
# state as USD: CH PLUS TIGHTS was recorded at $435 (C$435) and $7,050
# (Mex$7,050). A letter immediately before the $ disqualifies it.
PRICE_RE = re.compile(r"(?<![A-Za-z])(?:US)?\$\s?([0-9][0-9,]*(?:\.[0-9]{2})?)")
LOCALISED_PRICE_RE = re.compile(r"(?<![A-Za-z])(?!US\$)([A-Za-z]{1,4})\$\s?[0-9]")


def page_is_localised(html: str) -> bool:
    """True when the storefront answered in a currency that is not USD."""
    return bool(LOCALISED_PRICE_RE.search(html or ""))
SOLD_OUT_RE = re.compile(r"sold\s*out|out\s*of\s*stock|unavailable", re.I)

# Paths that are content, not shoppable categories.
NON_CATEGORY = {
    "", "locations", "magazine", "login", "account", "cart", "checkout",
    "search", "stores", "customer-service", "terms", "privacy", "contact",
    "faq", "shipping", "returns", "careers", "press", "sitemap",
}


@dataclass
class Product:
    pid: str
    url: str
    category: str
    name: Optional[str] = None
    price: Optional[str] = None
    image: Optional[str] = None
    in_stock: bool = True
    extras: dict = field(default_factory=dict)


class Fetcher:
    """requests session with browser-ish headers, ETag caching and backoff."""

    def __init__(self, etag_store=None):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            # NB: only advertise what urllib3 can decode. Brotli needs the optional
            # "brotli" package (in requirements.txt); zstd is not supported at all.
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
        })
        if config.PROXY_URL:
            self.session.proxies.update({"http": config.PROXY_URL, "https": config.PROXY_URL})
        self.etag_store = etag_store or {}
        self._last_request = 0.0
        self.diagnostics = {}  # url -> what the response actually looked like

    def _throttle(self):
        gap = time.time() - self._last_request
        wait = config.REQUEST_DELAY_SECONDS - gap
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.4))
        self._last_request = time.time()

    def get(self, url: str, use_cache: bool = True, attempts: int = 3):
        """Return (status_code, text_or_None). 304 means 'unchanged, use last result'."""
        headers = {}
        cached = self.etag_store.get(url) if use_cache else None
        if cached:
            if cached.get("etag"):
                headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                headers["If-Modified-Since"] = cached["last_modified"]

        delay = 2.0
        for attempt in range(1, attempts + 1):
            self._throttle()
            try:
                resp = self.session.get(
                    url, headers=headers, timeout=config.REQUEST_TIMEOUT, allow_redirects=True
                )
            except requests.RequestException as exc:
                log.warning("request failed (%s/%s) %s: %s", attempt, attempts, url, exc)
                if attempt == attempts:
                    return 0, None
                time.sleep(delay)
                delay *= 2
                continue

            if resp.status_code == 304:
                return 304, None

            if resp.status_code == 200:
                body = resp.text
                self.diagnostics[url] = {
                    "status": resp.status_code,
                    "content_type": resp.headers.get("Content-Type", "?"),
                    "content_encoding": resp.headers.get("Content-Encoding", "(none)"),
                    "bytes": len(resp.content),
                    "chars": len(body),
                    "looks_like_html": "<html" in body[:4000].lower(),
                    "product_href_count": len(PRODUCT_HREF_RE.findall(body)),
                    "snippet": body[:300].replace("\n", " "),
                    "final_url": resp.url,
                }
                if use_cache:
                    self.etag_store[url] = {
                        "etag": resp.headers.get("ETag"),
                        "last_modified": resp.headers.get("Last-Modified"),
                    }
                return 200, body

            self.diagnostics.setdefault(url, {
                "status": resp.status_code,
                "content_type": resp.headers.get("Content-Type", "?"),
                "content_encoding": resp.headers.get("Content-Encoding", "(none)"),
                "final_url": resp.url,
            })

            if resp.status_code in (403, 429, 500, 502, 503, 504):
                retry_after = resp.headers.get("Retry-After")
                sleep_for = float(retry_after) if (retry_after or "").isdigit() else delay
                log.warning(
                    "HTTP %s from %s (%s/%s), backing off %.1fs",
                    resp.status_code, url, attempt, attempts, sleep_for,
                )
                if attempt == attempts:
                    return resp.status_code, None
                time.sleep(sleep_for)
                delay *= 2
                continue

            return resp.status_code, None
        return 0, None


# ---------------------------------------------------------------------------
# Category discovery
# ---------------------------------------------------------------------------

def _normalise_path(href: str) -> Optional[str]:
    if not href:
        return None
    href = href.strip()
    # Reject non-navigational hrefs. "javascript:void(0);" dropdown toggles were
    # being turned into a bogus /void(0); category that 404s on every cycle.
    if href.startswith("#") or ":" in href.split("/")[0] and not href.lower().startswith(("http://", "https://")):
        return None
    parsed = urlparse(urljoin(config.BASE_URL + "/", href))
    if parsed.netloc and "chromehearts.com" not in parsed.netloc:
        return None
    path = parsed.path.rstrip("/")
    return path or "/"


def sections_from_homepage(html):
    """(sections, untracked_paths) linked from the homepage."""
    found, other = set(), set()
    if not html:
        return found, other
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        # A cgid nav link carries the section in its QUERY STRING, which
        # _normalise_path discards - read it before normalising.
        aliased = category_from_cgid(a["href"])
        if aliased and aliased.lstrip("/") not in NON_CATEGORY:
            found.add(aliased)
            # Resolved - do not also report its raw storefront URL as an
            # untracked link, or the watchdog cries wolf every cycle.
            continue

        path = _normalise_path(a["href"])
        if not path or path == "/":
            continue
        parts = [p for p in path.split("/") if p]
        if len(parts) != 1:
            # Not a plain section path. Keep it as "something the site links
            # to that we do not understand" so the coverage watchdog can
            # surface it.
            if not path.endswith(".html") and not match_product_path(path):
                other.add(path)
            continue
        seg = parts[0]
        if seg.endswith(".html") or seg in NON_CATEGORY:
            continue
        found.add("/" + seg)
    return found, other


CANONICAL_RE = re.compile(
    r"<link[^>]+rel=[\"']canonical[\"'][^>]*href=[\"']([^\"']+)[\"']"
    r"|<link[^>]+href=[\"']([^\"']+)[\"'][^>]*rel=[\"']canonical[\"']", re.I)


def renders_homepage(html) -> bool:
    """True when a section URL just shows the homepage.

    That is how this storefront answers for a category it has taken down
    (/hat, /sports-mesh): a 200 with the homepage, not a 404.
    """
    match = CANONICAL_RE.search(html or "")
    if not match:
        return False
    href = (match.group(1) or match.group(2) or "").strip()
    return _normalise_path(href) == "/"


def cgid_sources(*bodies):
    """{public alias: URL exactly as the site linked it} for cgid sections.

    The alias can die while the site still lists the section (/sweatpants
    404s today). An alert must carry a link that works, so keep the original.
    """
    sources = {}
    for body in bodies:
        for raw in re.findall(r"(https?://[^\s\"'<>]*?[?&](?:amp;)?cgid=[A-Za-z0-9_\-]+)", body or ""):
            alias = category_from_cgid(raw)
            if alias:
                sources.setdefault(alias, raw.replace("&amp;", "&"))
    return sources


def sections_from_sitemap(xml):
    """Sections listed in the XML sitemap."""
    found = set()
    if not xml:
        return found
    for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml):
        # THE BUG THAT MISSED A DROP: a one-time category appears in the
        # sitemap as .../Search-Show?cgid=SWEATPANTS. The section is in the
        # query string, which _normalise_path discards, and the remaining path
        # is multi-segment so it was dropped. Read the cgid first.
        aliased = category_from_cgid(loc)
        if aliased and aliased.lstrip("/") not in NON_CATEGORY:
            found.add(aliased)
            continue
        path = _normalise_path(loc)
        if not path or path == "/":
            continue
        parts = [p for p in path.split("/") if p]
        if len(parts) == 1 and not parts[0].endswith(".html") and parts[0] not in NON_CATEGORY:
            found.add("/" + parts[0])
    return found


def discover_categories(fetcher: Fetcher) -> List[str]:
    """Pull candidate category paths from the homepage and the XML sitemap.

    When Chrome Hearts opens a brand-new section for a drop it appears in one
    of these before any product in it is known to us.
    """
    status, html = fetcher.get(config.BASE_URL + "/", use_cache=False)
    if status == 200 and html:
        found, other = sections_from_homepage(html)
    else:
        log.warning("category discovery: homepage returned %s", status)
        found, other = set(), set()

    status, xml = fetcher.get(config.BASE_URL + "/sitemap_0.xml", use_cache=False)
    if status == 200 and xml:
        found |= sections_from_sitemap(xml)

    fetcher.other_paths = other
    if not hasattr(fetcher, "found_sections"):
        fetcher.found_sections = set()
    fetcher.found_sections |= found
    return sorted(found)


def resolve_categories(fetcher: Fetcher) -> List[str]:
    if config.CATEGORIES:
        return sorted({c if c.startswith("/") else "/" + c for c in config.CATEGORIES})
    cats = set(config.SEED_CATEGORIES)
    if config.DISCOVER_CATEGORIES:
        cats.update(discover_categories(fetcher))
    return sorted(cats)


# ---------------------------------------------------------------------------
# Product parsing
# ---------------------------------------------------------------------------

def _tile_container(anchor):
    """Walk up from a product link to the smallest ancestor that looks like a tile."""
    node = anchor
    for _ in range(5):
        parent = node.parent
        if parent is None:
            break
        node = parent
        classes = " ".join(node.get("class", [])) if hasattr(node, "get") else ""
        if re.search(r"tile|product|card|grid-item", classes, re.I):
            return node
    return anchor.parent or anchor


def _first_image(container) -> Optional[str]:
    for img in container.find_all("img"):
        for attr in ("src", "data-src", "data-original", "data-lazy", "data-srcset", "srcset"):
            val = img.get(attr)
            if not val:
                continue
            candidate = val.split(",")[0].strip().split(" ")[0]
            if candidate and not candidate.startswith("data:"):
                return urljoin(config.BASE_URL, candidate)
    source = container.find("source")
    if source and source.get("srcset"):
        candidate = source["srcset"].split(",")[0].strip().split(" ")[0]
        if candidate:
            return urljoin(config.BASE_URL, candidate)
    return None


TRAILING_NOISE_RE = re.compile(
    r"(?:\s*(?:\$\s?[0-9][0-9,]*(?:\.[0-9]{2})?(?:\s*[-\u2013]\s*\$\s?[0-9][0-9,]*(?:\.[0-9]{2})?)?"
    r"|sold\s*out|out\s*of\s*stock|unavailable|new))+\s*$",
    re.I,
)


def _clean_name(text: Optional[str]) -> Optional[str]:
    """Strip price and availability wording that bleeds in from the tile text."""
    if not text:
        return text
    previous = None
    while previous != text:
        previous = text
        text = TRAILING_NOISE_RE.sub("", text).strip()
        text = re.sub(r"[\s\-\u2013,|]+$", "", text).strip()
    return text or None


def _tile_name(container, anchor) -> Optional[str]:
    for attr in ("aria-label", "title"):
        val = (anchor.get(attr) or "").strip()
        if val and len(val) > 2:
            return val
    for sel in ("[class*=name]", "[class*=title]", "h1", "h2", "h3", "h4"):
        node = container.select_one(sel)
        if node:
            text = node.get_text(" ", strip=True)
            if text and not PRICE_RE.fullmatch(text):
                return text[:240]
    text = anchor.get_text(" ", strip=True)
    return text[:240] or None


def parse_products(html: str, category: str) -> Dict[str, Product]:
    """Extract every product tile on a category page, keyed by product id."""
    soup = BeautifulSoup(html, "lxml")
    products: Dict[str, Product] = {}
    localised = page_is_localised(html)
    if localised:
        log.warning("%s rendered in a non-USD currency - ignoring its names and prices",
                    category)

    for anchor in soup.find_all("a", href=True):
        path = _normalise_path(anchor["href"])
        if not path:
            continue
        match = match_product_path(path)
        if not match:
            continue
        pid = match.group("pid")
        url = urljoin(config.BASE_URL, path)

        container = _tile_container(anchor)
        text = container.get_text(" ", strip=True)
        price_match = PRICE_RE.search(text)

        existing = products.get(pid)
        product = existing or Product(pid=pid, url=url, category=category)
        if not localised:
            product.name = product.name or _clean_name(_tile_name(container, anchor))
            if price_match and not product.price:
                product.price = "$" + price_match.group(1)
        product.image = product.image or _first_image(container)
        if SOLD_OUT_RE.search(text):
            product.in_stock = False
        products[pid] = product

    if not products:
        # Fallback: some renders put the link in JS/JSON rather than an <a href>.
        for path in set(PRODUCT_HREF_RE.findall(html)):
            match = match_product_path(path)
            if match:
                pid = match.group("pid")
                products.setdefault(
                    pid, Product(pid=pid, url=urljoin(config.BASE_URL, path), category=category)
                )

    return products


def _jsonld_objects(soup) -> Iterable[dict]:
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw.strip())
        except (ValueError, AttributeError):
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                yield item
                for value in item.values():
                    if isinstance(value, (dict, list)):
                        stack.append(value)


def enrich(product: Product, fetcher: Fetcher) -> Product:
    """Fill in name / price / image / stock from the product page itself."""
    status, html = fetcher.get(product.url, use_cache=False)
    if status != 200 or not html:
        log.info("enrich: %s returned %s", product.url, status)
        return product

    soup = BeautifulSoup(html, "lxml")

    for obj in _jsonld_objects(soup):
        if str(obj.get("@type", "")).lower() != "product":
            continue
        product.name = obj.get("name") or product.name
        image = obj.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        if isinstance(image, str):
            product.image = product.image or urljoin(config.BASE_URL, image)
        offers = obj.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if isinstance(offers, dict):
            price = offers.get("price") or offers.get("lowPrice")
            currency = offers.get("priceCurrency", "USD")
            if price and currency not in ("USD", ""):
                log.info("enrich: %s priced in %s - leaving price unset", product.url, currency)
                price = None
            if price:
                symbol = "$"
                try:
                    product.price = f"{symbol}{float(price):,.0f}" if float(price).is_integer() else f"{symbol}{float(price):,.2f}"
                except (TypeError, ValueError):
                    product.price = f"{symbol}{price}"
            availability = str(offers.get("availability", "")).lower()
            if availability:
                product.in_stock = "instock" in availability.replace("_", "")
        if obj.get("sku"):
            product.extras["sku"] = obj["sku"]
        break

    def meta(prop):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return (tag.get("content") or "").strip() if tag else None

    product.name = product.name or meta("og:title")
    og_image = meta("og:image")
    if og_image and not product.image:
        product.image = urljoin(config.BASE_URL, og_image)
    description = meta("og:description")
    if description:
        product.extras["description"] = description[:300]

    if not product.price:
        body = soup.get_text(" ", strip=True)
        match = PRICE_RE.search(body)
        if match:
            product.price = "$" + match.group(1)

    if SOLD_OUT_RE.search(soup.get_text(" ", strip=True)[:6000]):
        product.in_stock = False

    if product.name:
        product.name = re.sub(r"\s*\|\s*Chrome Hearts\s*$", "", product.name).strip()

    return product


#: 304 Not Modified - nothing changed there. NOT the same as an empty page.
UNCHANGED = object()
#: 404 - the path is not a section at all. NOT the same as a failed fetch.
GONE = object()


def scrape_category(fetcher: Fetcher, category: str):
    """Products, or UNCHANGED (304), or GONE (404), or None (fetch failed).

    These four MUST stay distinct. Collapsing any of them together is how a
    healthy cycle ends up indistinguishable from a broken one.
    """
    url = config.BASE_URL + category
    status, html = fetcher.get(url)
    if status == 304:
        return UNCHANGED
    if status == 404:
        log.info("category %s is 404 - not a section", category)
        return GONE
    if status != 200 or not html:
        log.warning("category %s returned %s", category, status)
        return None
    # Free crawl: this page may link to a section nothing else mentions.
    if not hasattr(fetcher, "found_sections"):
        fetcher.found_sections = set()
    fetcher.found_sections |= extract_sections(html)
    if renders_homepage(html):
        if not hasattr(fetcher, "homepage_renders"):
            fetcher.homepage_renders = set()
        fetcher.homepage_renders.add(category)
    return parse_products(html, category)
