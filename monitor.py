#!/usr/bin/env python3
"""Chrome Hearts new-product monitor -> Discord webhook.

Usage:
    python monitor.py                 # run forever
    python monitor.py --once          # one cycle, then exit
    python monitor.py --selftest      # scrape and print, never notifies
    python monitor.py --test-webhook  # prove the Discord webhook works
    python monitor.py --reseed        # re-baseline: mark everything seen, no alerts
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import signal
import sys
import time
from typing import Dict

import requests

import config
import notifier
import hashlib

import scraper
from scraper import (GONE, UNCHANGED, Fetcher, Product, discover_categories,
                     enrich, resolve_categories, scrape_category)
from store import Store

log = logging.getLogger("monitor")
_running = True


def _setup_logging():
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-9s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def _handle_signal(signum, _frame):
    global _running
    log.info("received signal %s, shutting down after this cycle", signum)
    _running = False


def _product_from_record(pid: str, record) -> Product:
    return Product(
        pid=pid, url=record["url"], category=record["category"],
        name=record["name"], price=record["price"], image=record["image"],
        in_stock=bool(record["in_stock"]),
    )


def collect(fetcher: Fetcher, categories, store=None, between=None):
    """Scrape every category. Returns (products by pid, count of failed categories).

    A 304 replays that section's known products from state - nothing changed
    there, so what we saw last time is still what is on the page. Treating 304
    as empty made a quiet cycle look identical to a total parse failure.
    A 404 means the path is not a section, which is not a failure either.
    """
    seen: Dict[str, Product] = {}
    failures = 0
    per_category = {}
    gone = set()
    known_by_category = {}
    if store is not None:
        for pid, record in store.iter_products():
            known_by_category.setdefault(record["category"], {})[pid] = record

    for category in categories:
        result = scrape_category(fetcher, category)
        if between is not None:
            # Keep the fast probe on schedule while a sweep is in flight.
            between()
        if result is UNCHANGED:
            replayed = known_by_category.get(category, {})
            per_category[category] = len(replayed)
            for pid, record in replayed.items():
                seen.setdefault(pid, _product_from_record(pid, record))
            continue
        if result is GONE:
            gone.add(category)
            continue
        if result is None:
            failures += 1
            continue
        for pid, product in result.items():
            if pid in seen:
                # Same product listed in two categories - keep the richer record.
                existing = seen[pid]
                existing.name = existing.name or product.name
                existing.price = existing.price or product.price
                existing.image = existing.image or product.image
                existing.in_stock = existing.in_stock or product.in_stock
            else:
                seen[pid] = product
        per_category[category] = len(result)
        log.debug("%s -> %d products", category, len(result))
    return seen, failures, per_category, gone


def run_cycle(store: Store, fetcher: Fetcher, notify: bool = True, between=None) -> dict:
    categories, brand_new = resolve_tracked_sections(store, fetcher)
    if not categories:
        log.error("no categories resolved - site layout may have changed")
        return {"error": "no-categories"}

    new_categories = announce_sections(store, brand_new, notify=notify)
    for category in categories:
        store.add_category(category)

    products, failures, per_category, gone = collect(fetcher, categories, store, between)
    log.info(
        "cycle: %d categories (%d failed), %d products on site, %d known",
        len(categories), failures, len(products), store.product_count(),
    )

    # Every category failed => almost certainly network/WAF, not an empty store.
    if failures == len(categories):
        store.commit()
        return {"error": "all-categories-failed", "categories": len(categories),
                "detail": f"All {len(categories)} category pages failed to fetch - "
                          "the runner may be blocked by the site's CDN."}

    # Sections linked only from another section's page surface here; announce
    # them now so a new category is known within one cycle, not the next.
    new_section_watch(store, per_category, gone,
                      getattr(fetcher, "homepage_renders", set()), notify=notify)
    if hasattr(fetcher, "homepage_renders"):
        fetcher.homepage_renders = set()

    if not config.CATEGORIES:
        late = sorted(getattr(fetcher, "found_sections", set())
                      - set(_json_meta(store, "tracked_sections", []))
                      - set(_json_meta(store, "retired_sections", [])))
        new_categories = list(new_categories) + announce_sections(store, late, notify=notify)
        retire_dead_sections(store, gone, set(per_category))
        store.set_meta("tracked_sections",
                       json.dumps(sorted(set(_json_meta(store, "tracked_sections", []))
                                         | set(categories))))

    coverage_warnings = coverage_watch(
        store, per_category, getattr(fetcher, "other_paths", set()), notify=notify
    )

    first_run = store.get_meta("seeded") != "1"

    # A 200 response we simply failed to parse looks identical to "the store is
    # empty" unless we say otherwise. Refuse to seed an empty baseline, and
    # refuse to act on an empty sweep when we already know of a real catalog -
    # otherwise a markup change silently re-alerts the entire catalogue later.
    if not products:
        if first_run:
            log.error(
                "seed aborted: fetched %d categories but parsed 0 products - "
                "run --selftest, the page markup has probably changed",
                len(categories),
            )
            store.commit()
            return {"error": "seed-parsed-nothing", "categories": len(categories),
                    "immediate": True,
                    "detail": f"Fetched {len(categories)} category pages but parsed 0 products. "
                              "The site markup has probably changed, or the runner is being blocked."}
        if store.product_count() > 0:
            log.error(
                "parsed 0 products but %d are known - treating as a scrape failure",
                store.product_count(),
            )
            store.commit()
            return {"error": "parsed-zero-products", "known": store.product_count(),
                    "detail": f"Parsed 0 products but {store.product_count()} are known. "
                              "Nothing was forgotten and no alerts were sent."}

    new_products, restocks, price_changes = [], [], []

    for pid, product in products.items():
        previous = store.get_product(pid)
        if previous is None:
            if not first_run:
                new_products.append(product)
        else:
            was_in_stock = bool(previous["in_stock"])
            if config.ALERT_RESTOCKS and product.in_stock and not was_in_stock:
                restocks.append(product)
            if (
                config.ALERT_PRICE_CHANGES
                and product.price
                and previous["price"]
                and product.price != previous["price"]
            ):
                price_changes.append((product, previous["price"]))
            product.name = product.name or previous["name"]
            product.image = product.image or previous["image"]
            product.price = product.price or previous["price"]

    if first_run:
        for product in products.values():
            store.upsert(product)
        store.set_meta("seeded", "1")
        store.commit()
        store.save_etags(fetcher.etag_store)
        message = (
            f"Monitor online. Baseline captured: **{len(products)}** products across "
            f"{len(categories)} categories. You'll be pinged the moment anything new appears."
        )
        log.info(message)
        if notify and config.SEED_ON_FIRST_RUN:
            notifier.send_text(message)
        return {"seeded": len(products), "categories": len(categories)}

    # --- enrich + alert -----------------------------------------------------
    embeds = []

    if config.ALERT_NEW_PRODUCTS and new_products:
        capped = new_products[: config.MAX_ALERTS_PER_CYCLE]
        for product in capped:
            if config.ENRICH_PRODUCTS:
                enrich(product, fetcher)
            log.info("NEW %s | %s | %s", product.pid, product.name, product.url)
            embeds.append(notifier.build_embed(product, "new"))
        if len(new_products) > len(capped):
            log.warning("capped alerts at %d (%d new found)", len(capped), len(new_products))

    for product in restocks:
        log.info("RESTOCK %s | %s", product.pid, product.name)
        embeds.append(notifier.build_embed(product, "restock"))

    for product, old_price in price_changes:
        log.info("PRICE %s | %s -> %s", product.pid, old_price, product.price)
        embeds.append(notifier.build_embed(product, "price", old_price=old_price))

    if new_products:
        store.set_meta("last_new_product_at", time.time())

    if notify and embeds:
        notifier.send_embeds(embeds)

    for product in products.values():
        store.upsert(product)
    store.commit()
    store.save_etags(fetcher.etag_store)

    return {
        "coverage_warnings": len(coverage_warnings),
        "new": len(new_products),
        "restocks": len(restocks),
        "price_changes": len(price_changes),
        "new_categories": new_categories,
        "total": len(products),
        "failures": failures,
    }


def record_failure(store, kind: str, detail: str = "", immediate: bool = False) -> None:
    """Track consecutive failures in the store so the count survives process exit.

    The Actions deployment runs one cycle per process, so an in-memory counter
    would reset every 5 minutes and the alert would never fire.
    """
    count = int(store.get_meta("consecutive_failures", "0") or 0) + 1
    store.set_meta("consecutive_failures", count)
    already = store.get_meta("failure_alerted") == "1"
    threshold = config.FAILURE_ALERT_THRESHOLD
    should = immediate or (threshold and count >= threshold)
    if should and not already:
        notifier.send_text(
            f"**Monitor problem: `{kind}`**\n{detail}\n"
            f"Failed cycles in a row: {count}. Run the Selftest workflow to see what it's reading."
        )
        store.set_meta("failure_alerted", "1")
    store.commit()


def record_success(store) -> None:
    if store.get_meta("failure_alerted") == "1":
        notifier.send_text("Monitor recovered - reading the site normally again.")
    if store.get_meta("consecutive_failures", "0") != "0":
        store.set_meta("consecutive_failures", 0)
    store.set_meta("failure_alerted", "0")


def _ago(timestamp: float) -> str:
    if not timestamp:
        return "not since the monitor started"
    seconds = max(0, time.time() - float(timestamp))
    hours = seconds / 3600
    if hours < 1:
        return f"{int(seconds // 60)} minutes ago"
    if hours < 48:
        return f"{hours:.0f} hours ago"
    return f"{hours / 24:.0f} days ago"


def _probe_report(prober) -> str:
    stats = prober.stats() if prober else None
    if not stats:
        return ""
    verdict = "on target" if stats["worst"] <= 25 else "**SLOWER THAN TARGET**"
    return (f"\nNew-category probe: {stats['probes']} runs, median gap "
            f"{stats['median']:.0f}s, p99 {stats['p99']:.0f}s, worst "
            f"{stats['worst']:.0f}s - {verdict}.")


def maybe_heartbeat(store, prober=None) -> bool:
    """Post a periodic 'still working' message.

    The timestamp lives in the store, not in memory: under GitHub Actions every
    cycle is its own process, so an in-memory timer would reset every 5 minutes
    and never reach the threshold.
    """
    if not config.HEARTBEAT_HOURS:
        return False

    now = time.time()
    last = float(store.get_meta("last_heartbeat", 0) or 0)
    if not last:
        # First healthy cycle - start the clock rather than firing immediately.
        store.set_meta("last_heartbeat", now)
        store.commit()
        return False

    if now - last < config.HEARTBEAT_HOURS * 3600:
        return False

    last_new = float(store.get_meta("last_new_product_at", 0) or 0)
    categories = len(store.known_categories())
    notifier.send_text(
        f"**Monitor healthy.** Tracking {store.product_count()} products across "
        f"{categories} sections. Last new product: {_ago(last_new)}. "
        f"Next check in ~{config.POLL_INTERVAL_SECONDS // 60 or 5} minutes."
        + _probe_report(prober)
    )
    store.set_meta("last_heartbeat", now)
    store.commit()
    return True


#: A section must have held at least this many products before we will treat
#: it emptying as a coverage problem rather than ordinary churn.
COVERAGE_FLOOR = 3


def _json_meta(store, key, default):
    raw = store.get_meta(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


#: Consecutive 404s before a section is retired. At a 5-minute cadence this is
#: roughly a day, so a section is never dropped over a transient error.
RETIRE_AFTER_404S = 288


def resolve_tracked_sections(store, fetcher):
    """Every section to scrape this cycle, plus any never seen before.

    Sections are STICKY. Chrome Hearts spins up a category for one drop and
    removes it from the nav afterwards, so "it is in the nav today" is the
    wrong test - /hat and /scarf silently stopped being scraped that way.
    Once a section is known we keep polling it until it 404s for about a day.
    """
    if config.CATEGORIES:
        # Explicitly pinned: scrape exactly these and never expand the set.
        return sorted({c if c.startswith("/") else "/" + c for c in config.CATEGORIES}), []

    discovered = set(resolve_categories(fetcher))
    discovered |= getattr(fetcher, "found_sections", set())
    retired = set(_json_meta(store, "retired_sections", []))

    if store.get_meta("tracked_sections") is None:
        # Bootstrap. Without this, the first cycle after an upgrade announces
        # every section that already existed as if it had just appeared.
        tracked = set(store.known_categories()) | discovered
        store.set_meta("tracked_sections", json.dumps(sorted(tracked)))
        store.commit()
        log.info("bootstrapped section tracking with %d sections", len(tracked))
        return sorted(tracked - retired), []

    tracked = set(_json_meta(store, "tracked_sections", []))
    brand_new = sorted(discovered - tracked - retired)
    live = sorted((discovered | tracked) - retired)
    return live, brand_new


def announce_sections(store, sections, notify=True, via=None, sources=None):
    """Tell Discord the moment a section exists that we have never seen.

    This is the headline signal, not a footnote: a new category IS the drop.
    It fires before any product in it has been parsed, so it is useful even
    when the section's markup is something the parser has not met before.
    """
    if not sections:
        return []
    tracked = set(_json_meta(store, "tracked_sections", []))
    store.set_meta("tracked_sections", json.dumps(sorted(tracked | set(sections))))
    store.commit()
    now = time.time()
    seen_at = time.strftime("%H:%M:%S UTC", time.gmtime(now))
    detections = _json_meta(store, "section_detections", {})
    sources = sources or {}
    for section in sections:
        record = {"first_seen": now, "via": via or "sweep"}
        if sources.get(section):
            record["source_url"] = sources[section]
        detections.setdefault(section, record)
    store.set_meta("section_detections", json.dumps(detections, sort_keys=True))
    store.commit()

    def line(section):
        text = f"• **{section.lstrip('/')}** - {config.BASE_URL}{section}"
        if sources.get(section):
            text += f"\n   if that link is dead, the site's own link: {sources[section]}"
        return text

    listing = "\n".join(line(s) for s in sections)
    source = f" via the {via}" if via else ""
    log.warning("NEW SECTION(S)%s: %s", source, ", ".join(sections))
    if notify and config.ALERT_NEW_CATEGORIES:
        notifier.send_text(
            f"**New category on the site** ({len(sections)}):\n{listing}\n"
            f"First seen {seen_at}{source}. Scanning it for products now."
        )
    return sections


#: A section counts as "new" for this long after it was first announced.
NEW_SECTION_WINDOW_SECONDS = 6 * 3600


def new_section_watch(store, per_category, gone, homepage_renders, notify=True):
    """Warn once when a NEW section yields no products we can read.

    The coverage watchdog only fires when a section that used to hold
    products goes empty, so a brand-new section that is unreadable from its
    first sweep - which is precisely what a drop looks like when something
    goes wrong - was invisible. /sports-mesh was announced and then recorded
    zero products, and nothing said so.
    """
    detections = _json_meta(store, "section_detections", {})
    warned = set(_json_meta(store, "unreadable_new_sections", []))
    now = time.time()
    warnings = []
    for section, info in sorted(detections.items()):
        if section in warned or now - float(info.get("first_seen", 0)) > NEW_SECTION_WINDOW_SECONDS:
            continue
        if section in gone:
            why = "its link returns **404**"
        elif section in per_category and per_category[section] == 0:
            why = ("it now just shows the **homepage** - it was probably taken down, "
                   "or the link is wrong" if section in homepage_renders else
                   "the page loads but I can't read any products on it - they may "
                   "load with JavaScript, or use a link format I don't recognise")
        else:
            continue
        link = info.get("source_url") or f"{config.BASE_URL}{section}"
        warnings.append(f"**Check this one yourself:** new category "
                        f"`{section.lstrip('/')}` - {why}.\n{link}")
        warned.add(section)
    if warnings:
        store.set_meta("unreadable_new_sections", json.dumps(sorted(warned)))
        store.commit()
        for w in warnings:
            log.warning(w.replace("**", "").replace("`", ""))
        if notify:
            notifier.send_text("\n\n".join(warnings))
    return warnings


def retire_dead_sections(store, gone, scraped_ok):
    """Retire a section only after it has 404'd for about a day."""
    counts = _json_meta(store, "gone_counts", {})
    retired = set(_json_meta(store, "retired_sections", []))
    newly = []
    for section in sorted(gone):
        counts[section] = int(counts.get(section, 0)) + 1
        if counts[section] >= RETIRE_AFTER_404S:
            retired.add(section)
            newly.append(section)
    for section in scraped_ok:
        counts.pop(section, None)
    store.set_meta("gone_counts", json.dumps(counts, sort_keys=True))
    if newly:
        store.set_meta("retired_sections", json.dumps(sorted(retired)))
        log.info("retired sections after sustained 404s: %s", newly)
    store.commit()
    return newly


def coverage_watch(store, per_category, other_paths, notify=True):
    """Catch the monitor going blind in a way that otherwise looks like silence.

    Two checks, each aimed at a real failure we hit:

    1. A section that used to hold products now parses to zero. That is how a
       markup change in one section hides forever - the sweep still succeeds,
       so nothing else notices.
    2. The site links somewhere we are not watching. A new section arriving in
       a link shape we do not handle is invisible otherwise; this names it.
    """
    warnings = []

    peaks = _json_meta(store, "category_peaks", {})
    emptied = set(_json_meta(store, "emptied_sections", []))

    for category, count in sorted(per_category.items()):
        peak = int(peaks.get(category, 0))
        if count > peak:
            peaks[category] = count
        if peak >= COVERAGE_FLOOR and count == 0:
            if category not in emptied:
                warnings.append(
                    f"**Coverage warning:** `{category}` returned **0 products** but has "
                    f"held up to **{peak}**. The page loaded fine, so its markup has "
                    f"probably changed and that section is now invisible."
                )
                emptied.add(category)
        elif count > 0:
            emptied.discard(category)

    store.set_meta("category_peaks", json.dumps(peaks, sort_keys=True))
    store.set_meta("emptied_sections", json.dumps(sorted(emptied)))

    if other_paths:
        reported = set(_json_meta(store, "reported_unknown_paths", []))
        monitored = set(per_category)
        fresh = sorted(p for p in other_paths if p not in reported and p not in monitored)
        if fresh:
            listing = "\n".join(f"• `{p}`" for p in fresh[:10])
            warnings.append(
                "**Untracked link:** the site points at something the monitor is not "
                f"watching:\n{listing}\nIf that is a new section, products in it will "
                "be missed until it is added."
            )
            reported.update(fresh)
            store.set_meta("reported_unknown_paths", json.dumps(sorted(reported)))

    if warnings and notify:
        notifier.send_text("\n\n".join(warnings))
    for warning in warnings:
        log.warning(warning.replace("**", "").replace("`", ""))

    store.commit()
    return warnings


def _digest(items) -> str:
    return hashlib.sha256("\n".join(sorted(items)).encode()).hexdigest()[:32]


class Prober:
    """The fast tier: detect a new category within ~FAST_INTERVAL seconds.

    Runs on its own clock, including BETWEEN section fetches of a full sweep,
    so a sweep in flight never delays it. Fingerprints the MEANING of each
    page - the sections and product links on it - not its bytes: homepages
    carry rotating tokens, and a byte hash would read as "changed" on every
    probe and trigger a full sweep every few seconds, which is the fastest way
    to get the IP blocked.
    """

    def __init__(self):
        self.last = 0.0
        self.home = None
        self.sitemap = None
        self.sweep_requested = False
        self.gaps = []               # seconds between probe starts
        self.runs = 0

    def due(self) -> bool:
        return time.time() - self.last >= config.FAST_INTERVAL_SECONDS

    def next_due(self) -> float:
        return self.last + config.FAST_INTERVAL_SECONDS

    def _fetch(self, fetcher, path, cached):
        # Unconditional until we hold a body to fall back on after a 304.
        status, body = fetcher.get(config.BASE_URL + path, use_cache=cached is not None)
        if status == 200 and body:
            return body, True
        return cached, False

    def run(self, store, fetcher, notify=True):
        started = time.time()
        if self.last:
            self.gaps.append(started - self.last)
            del self.gaps[:-2000]
        self.last = started
        self.runs += 1

        self.sitemap, _ = self._fetch(fetcher, "/sitemap_0.xml", self.sitemap)
        self.home, _ = self._fetch(fetcher, "/", self.home)

        from_sitemap = scraper.sections_from_sitemap(self.sitemap)
        from_home, other = scraper.sections_from_homepage(self.home)
        home_products = set(scraper.parse_products(self.home, "/")) if self.home else set()

        prints = _json_meta(store, "fingerprints", {})
        current = {
            "sitemap": _digest(from_sitemap),
            "home": _digest(from_home | other | {f"p:{pid}" for pid in home_products}),
        }
        changed = [k for k, v in current.items() if k in prints and prints[k] != v]
        if prints != current:
            store.set_meta("fingerprints", json.dumps(current, sort_keys=True))
            store.commit()

        # Announce new sections right now - this is the 30-second path.
        new = []
        if store.get_meta("tracked_sections") is not None:
            tracked = set(_json_meta(store, "tracked_sections", []))
            retired = set(_json_meta(store, "retired_sections", []))
            sources = scraper.cgid_sources(self.sitemap, self.home)
            for via, found in (("sitemap", from_sitemap), ("homepage", from_home)):
                fresh = sorted(found - tracked - retired - set(new))
                if fresh:
                    new += announce_sections(store, fresh, notify=notify, via=via,
                                             sources=sources)

        if changed or new:
            self.sweep_requested = True
            log.info("fast tier: %s changed%s - sweeping",
                     "+".join(changed) or "nothing",
                     f", new sections {new}" if new else "")
        return changed, new

    def stats(self):
        if not self.gaps:
            return None
        ordered = sorted(self.gaps)
        return {
            "probes": self.runs,
            "median": ordered[len(ordered) // 2],
            "p99": ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))],
            "worst": ordered[-1],
        }


def ping_healthcheck(ok: bool = True) -> None:
    """Dead-man's switch. Never allowed to break the monitor."""
    if not config.HEALTHCHECK_URL:
        return
    url = config.HEALTHCHECK_URL.rstrip("/") + ("" if ok else "/fail")
    try:
        requests.get(url, timeout=5)
    except requests.RequestException as exc:
        log.debug("healthcheck ping failed: %s", exc)


def open_store():
    """Pick the state backend: SQLite for long-running hosts, JSON for CI runs."""
    if config.STATE_BACKEND == "json":
        from jsonstore import JsonStore

        return JsonStore()
    return Store()


def main() -> int:
    parser = argparse.ArgumentParser(description="Chrome Hearts -> Discord product monitor")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--selftest", action="store_true", help="scrape and print; never notifies")
    parser.add_argument("--test-webhook", action="store_true", help="send a test message to Discord")
    parser.add_argument("--reseed", action="store_true", help="re-baseline without alerting")
    args = parser.parse_args()

    _setup_logging()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    if args.test_webhook:
        ok = notifier.send_text("Chrome Hearts monitor: webhook test. If you can read this, you're wired up.")
        print("webhook OK" if ok else "webhook FAILED - check DISCORD_WEBHOOK_URL")
        return 0 if ok else 1

    if args.selftest:
        fetcher = Fetcher()
        discovered = discover_categories(fetcher)
        print(f"\nDiscovered from nav + sitemap: {discovered or '(nothing - suspicious)'}")
        categories = resolve_categories(fetcher)
        print(f"Resolved {len(categories)} categories: {', '.join(categories)}\n")
        # No store here: --selftest returns before open_store() and must report
        # only what the live site gave us, never products replayed from state.
        products, failures, per_category, _gone = collect(fetcher, categories, None)
        print(f"Parsed {len(products)} products ({failures} categories failed)\n")

        if not products or not discovered:
            print("=" * 72)
            print("DIAGNOSTICS - what the server actually returned")
            print("=" * 72)
            for url, info in fetcher.diagnostics.items():
                print(f"\n  {url}")
                for key in ("status", "final_url", "content_type", "content_encoding",
                            "bytes", "chars", "looks_like_html", "product_href_count"):
                    if key in info:
                        print(f"      {key:<20} {info[key]}")
                if info.get("snippet"):
                    print(f"      snippet              {info['snippet'][:200]!r}")
            print()
            if any(not d.get("looks_like_html", True) for d in fetcher.diagnostics.values()):
                print("  -> Responses are not HTML. Almost certainly a content-encoding the")
                print("     client cannot decode, or an interstitial/bot page.")
            elif all(d.get("product_href_count", 0) == 0 for d in fetcher.diagnostics.values()
                     if "product_href_count" in d):
                print("  -> HTML decoded fine but contains no product-shaped URLs at all.")
                print("     The grid is probably rendered client-side, or the URL shape changed.")
            else:
                print("  -> Product URLs ARE present in the HTML but the parser missed them.")
                print("     PRODUCT_PATH_RE in scraper.py needs updating.")
            print()
        for pid, product in sorted(products.items(), key=lambda kv: kv[1].category):
            stock = "in stock" if product.in_stock else "SOLD OUT"
            print(f"  [{product.category:<18}] {pid:<18} {(product.name or '?')[:44]:<46} "
                  f"{(product.price or '?'):>9}  {stock}")
        if products:
            sample = next(iter(products.values()))
            print("\nEnriching one product to verify product-page parsing...")
            enrich(sample, fetcher)
            print(f"  name  : {sample.name}\n  price : {sample.price}\n  image : {sample.image}\n"
                  f"  stock : {'in stock' if sample.in_stock else 'sold out'}")
        return 0 if products else 2

    if not config.DISCORD_WEBHOOK_URL:
        log.error("DISCORD_WEBHOOK_URL is required. Set it in your .env file.")
        return 1

    store = open_store()
    fetcher = Fetcher(etag_store=store.load_etags())

    if args.reseed:
        store.set_meta("seeded", "0")
        log.info("state reset - next cycle re-baselines without alerting")

    log.info(
        "starting: fast=%ss full=%ss fast_tier=%s enrich=%s restocks=%s price=%s",
        config.FAST_INTERVAL_SECONDS, config.POLL_INTERVAL_SECONDS, config.FAST_TIER,
        config.ENRICH_PRODUCTS, config.ALERT_RESTOCKS, config.ALERT_PRICE_CHANGES,
    )

    prober = Prober() if (config.FAST_TIER and not args.once) else None
    last_full = 0.0

    def between():
        if prober is not None and prober.due():
            prober.run(store, fetcher)

    while _running:
        if prober is not None and prober.due():
            try:
                prober.run(store, fetcher)
            except Exception:
                log.exception("fast probe failed")

        now = time.time()
        sweep_due = (args.once
                     or now - last_full >= config.POLL_INTERVAL_SECONDS
                     or (prober is not None and prober.sweep_requested))
        if not sweep_due:
            wake = last_full + config.POLL_INTERVAL_SECONDS
            if prober is not None:
                wake = min(wake, prober.next_due())
            while _running and time.time() < wake:
                time.sleep(min(0.5, max(0.05, wake - time.time())))
            continue

        if prober is not None:
            prober.sweep_requested = False
        started = time.time()
        last_full = started
        result = {}
        try:
            result = run_cycle(store, fetcher, between=between if prober else None)
            if result.get("error"):
                log.error("cycle error: %s", result["error"])
                record_failure(store, result["error"], result.get("detail", ""),
                               immediate=bool(result.get("immediate")))
            else:
                record_success(store)
                maybe_heartbeat(store, prober)
                ping_healthcheck(ok=True)
        except Exception as exc:
            log.exception("unhandled error in cycle")
            result = {"error": "unhandled-exception"}
            record_failure(store, "unhandled-exception", str(exc)[:300])

        if args.once:
            if result.get("error"):
                # Persist FIRST. A non-zero exit skips the workflow's state
                # step, so failing immediately reset the failure counter every
                # run - the Discord warning could never reach its threshold
                # and the only signal was a GitHub email.
                streak = int(store.get_meta("consecutive_failures", "0") or 0)
                store.close()
                threshold = config.FAILURE_ALERT_THRESHOLD
                if threshold and streak >= threshold:
                    log.error("cycle reported %s - %d in a row, failing the job",
                              result["error"], streak)
                    return 1
                log.warning("cycle reported %s - %d in a row; not failing the job yet",
                            result["error"], streak)
                return 0
            break

        log.debug("sweep took %.1fs", time.time() - started)
        if prober is None and not args.once:
            # No fast tier: plain jittered interval between sweeps.
            pause = max(5.0, config.POLL_INTERVAL_SECONDS - (time.time() - started)
                        + random.uniform(0, config.JITTER_SECONDS))
            deadline = time.time() + pause
            while _running and time.time() < deadline:
                time.sleep(min(1.0, deadline - time.time()))

    store.close()
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
