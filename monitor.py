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
import logging
import random
import signal
import sys
import time
from typing import Dict, Tuple

import config
import notifier
from scraper import Fetcher, Product, enrich, resolve_categories, scrape_category
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


def collect(fetcher: Fetcher, categories) -> Tuple[Dict[str, Product], int]:
    """Scrape every category. Returns (products by pid, count of failed categories)."""
    seen: Dict[str, Product] = {}
    failures = 0
    for category in categories:
        result = scrape_category(fetcher, category)
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
        log.debug("%s -> %d products", category, len(result))
    return seen, failures


def run_cycle(store: Store, fetcher: Fetcher, notify: bool = True) -> dict:
    categories = resolve_categories(fetcher)
    if not categories:
        log.error("no categories resolved - site layout may have changed")
        return {"error": "no-categories"}

    known_categories = store.known_categories()
    new_categories = [c for c in categories if c not in known_categories]
    for category in categories:
        store.add_category(category)

    products, failures = collect(fetcher, categories)
    log.info(
        "cycle: %d categories (%d failed), %d products on site, %d known",
        len(categories), failures, len(products), store.product_count(),
    )

    # Every category failed => almost certainly network/WAF, not an empty store.
    if failures == len(categories):
        store.commit()
        return {"error": "all-categories-failed", "categories": len(categories)}

    known_pids = store.known_pids()
    first_run = store.get_meta("seeded") != "1"

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

    if notify and embeds:
        notifier.send_embeds(embeds)

    if notify and config.ALERT_NEW_CATEGORIES and new_categories and not first_run:
        listing = "\n".join(f"• {config.BASE_URL}{c}" for c in new_categories)
        notifier.send_text(f"New section(s) appeared on the site:\n{listing}")

    for product in products.values():
        store.upsert(product)
    store.commit()
    store.save_etags(fetcher.etag_store)

    return {
        "new": len(new_products),
        "restocks": len(restocks),
        "price_changes": len(price_changes),
        "new_categories": new_categories,
        "total": len(products),
        "failures": failures,
    }


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
        categories = resolve_categories(fetcher)
        print(f"\nResolved {len(categories)} categories: {', '.join(categories)}\n")
        products, failures = collect(fetcher, categories)
        print(f"Parsed {len(products)} products ({failures} categories failed)\n")
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
        "starting: interval=%ss jitter=%ss enrich=%s restocks=%s price=%s db=%s",
        config.POLL_INTERVAL_SECONDS, config.JITTER_SECONDS, config.ENRICH_PRODUCTS,
        config.ALERT_RESTOCKS, config.ALERT_PRICE_CHANGES, config.STATE_DB,
    )

    consecutive_failures = 0
    failure_alerted = False
    last_heartbeat = time.time()

    while _running:
        started = time.time()
        try:
            result = run_cycle(store, fetcher)
            if result.get("error"):
                consecutive_failures += 1
                log.error("cycle error: %s (%d in a row)", result["error"], consecutive_failures)
            else:
                if consecutive_failures and failure_alerted:
                    notifier.send_text("Monitor recovered - scraping the site normally again.")
                consecutive_failures = 0
                failure_alerted = False
        except Exception:
            consecutive_failures += 1
            log.exception("unhandled error in cycle (%d in a row)", consecutive_failures)

        if (
            config.FAILURE_ALERT_THRESHOLD
            and consecutive_failures >= config.FAILURE_ALERT_THRESHOLD
            and not failure_alerted
        ):
            notifier.send_text(
                f"Monitor has failed {consecutive_failures} cycles in a row - the site may be "
                "blocking requests or the page layout changed. Check the container logs."
            )
            failure_alerted = True

        if config.HEARTBEAT_HOURS and time.time() - last_heartbeat >= config.HEARTBEAT_HOURS * 3600:
            notifier.send_text(f"Heartbeat: monitor alive, tracking {store.product_count()} products.")
            last_heartbeat = time.time()

        if args.once:
            break

        elapsed = time.time() - started
        sleep_for = max(5.0, config.POLL_INTERVAL_SECONDS - elapsed + random.uniform(0, config.JITTER_SECONDS))
        log.debug("cycle took %.1fs, sleeping %.1fs", elapsed, sleep_for)
        deadline = time.time() + sleep_for
        while _running and time.time() < deadline:
            time.sleep(min(1.0, deadline - time.time()))

    store.close()
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
