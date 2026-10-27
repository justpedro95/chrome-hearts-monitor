# Chrome Hearts Monitor

Polls chromehearts.com every 60 seconds and fires a Discord webhook the moment a
product appears that it has never seen before.

---

## How it works

Chrome Hearts runs on Salesforce Commerce Cloud. There is no `products.json` and
no public API, but the category pages are rendered server-side — names, prices,
images and stock status are all in the HTML. So:

1. **Discover categories.** Each cycle it reads the homepage nav *and*
   `sitemap_0.xml` and merges the shoppable sections it finds. This matters:
   when Chrome Hearts opens a brand-new section for a drop, it shows up in the
   nav before any product inside it is known — the monitor picks it up on its
   own with no config change from you.
2. **Scrape each category.** Every product link matching
   `/<category>/<slug>/<PRODUCT_ID>.html` is captured. The **product ID** is the
   primary key — a brand-new ID is a brand-new product.
3. **Diff against SQLite.** State lives in `data/state.db`, so restarts,
   redeploys and reboots never cause duplicate alerts.
4. **Enrich and alert.** For each genuinely new product it opens the product
   page and pulls the name, price, image and availability from JSON-LD (with
   Open Graph tags as a fallback), then posts a rich Discord embed.

**Deliberately resilient:** the parser keys off URL shape and standard metadata,
not theme class names, so a storefront redesign degrades the embed detail rather
than breaking detection. Conditional requests (ETag / If-Modified-Since) mean an
unchanged category page costs a 304 instead of a full download.

---

## Deployment A — a cloud server (fastest, survives your power going out)

On a fresh Ubuntu box, as root:

```bash
curl -fsSL https://raw.githubusercontent.com/justpedro95/chrome-hearts-monitor/main/install/vps/bootstrap.sh | bash
```

Installs Docker, asks for your webhook once, seeds state so nothing known is
re-alerted, and runs under `restart: unless-stopped` so it survives reboots.
Re-run to upgrade.

### Why 15 seconds does not mean "sweep everything every 15 seconds"

Ten sections at a 15-second sweep is **~57,600 requests and ~11 GB a day from
one IP**. That gets the IP blocked, and a blocked monitor sees nothing. So
polling is tiered:

| Tier | Every | What it fetches | Catches |
|---|---|---|---|
| Fast | **15s** | the ~1 KB sitemap (plus the homepage every other tick), with conditional requests so an unchanged one costs a 304 | a **new drop category**, within 15s |
| Full | 60s | every tracked section | a new product inside an existing section |

Any change on the fast tier triggers a full sweep immediately. Total load is
~14 requests/min and ~2.75 GB/day — a seventh of the naive approach, with
15-second detection of the thing that actually signals a drop here.

Raise `FAST_INTERVAL_SECONDS` / `POLL_INTERVAL_SECONDS` in `.env` to go gentler;
lower them only if you are willing to risk the block.

---

## Deployment B — an always-on Mac (fast, but dies with your power)

`bash install/macos/install.sh`, then read
[install/macos/README.md](install/macos/README.md).

A launchd job polls every 60 seconds and actually keeps time. Use this when
you need to catch a drop. GitHub Actions cannot: measured on this repo's own
history, a 5-minute cron ran at a **median 14-minute** interval, with a worst
gap of **185 minutes** and only ~**36%** of scheduled runs delivered.

---

## Deployment C — GitHub Actions (no server, free, far too slow for a drop)

This is the zero-infrastructure path. A scheduled workflow wakes up every 5
minutes, checks the site, pings Discord, and commits any catalog change back to
the repo. State lives in `state/products.json` — plain JSON, so your commit
history *is* the audit log of everything the monitor has ever seen.

**What you need:** a GitHub account, plus the Discord webhook stored as a repo
secret named `DISCORD_WEBHOOK_URL`.

**Public vs private repo.** Actions minutes are unlimited on public repos but
capped at 2,000/month on private ones. A 5-minute cadence burns roughly
8,600 minutes/month, so a private repo would have to drop to a ~30-minute
cadence to stay inside the free tier. Nothing sensitive lives in the repo —
the webhook is an encrypted Actions secret, not a file — so public at 5 minutes
is the recommended combination.

**Workflows included:**

| Workflow | Trigger | What it does |
|---|---|---|
| `monitor.yml` | every 5 min | The monitor. One cycle per run. |
| `selftest.yml` | manual | Runs the offline tests, then live-scrapes and prints every product found. Sends nothing to Discord. Use this to check the parser. |
| `keepalive.yml` | weekly | One tiny commit so GitHub doesn't disable the schedule after 60 quiet days. |

**Manual controls** (Actions tab → pick the workflow → *Run workflow*):

- **Chrome Hearts Monitor** → tick `reseed` to forget everything and re-baseline
  without alerting. Do this if you ever get a burst of false alerts.
- **Selftest** → see exactly what the scraper is reading right now.

**Tuning without touching code.** Set repository *variables*
(Settings → Secrets and variables → Actions → Variables):
`ALERT_RESTOCKS`, `ALERT_PRICE_CHANGES`, `ALERT_NEW_CATEGORIES` (`true`/`false`),
and `ROLE_MENTION` (e.g. `<@&123456789012345678>`).

**It says it's alive every 12 hours.** A heartbeat message reports the tracked
product count and how long since the last new product, so a long quiet stretch
is distinguishable from a monitor that quietly died. Change the cadence with a
`HEARTBEAT_HOURS` repository variable, or set it to `0` to turn it off.

**A new category is the headline alert.** This storefront announces a drop by
creating a category for it, linking it from one place, and removing it later.
So sections are discovered from the nav, the sitemap (including the un-aliased
`Search-Show?cgid=` form) **and** every category page the monitor already
fetches - and the moment a section appears that has never been seen, you get a
Discord message with its URL, before any product in it has been parsed. Once a
section is known it is **sticky**: it keeps being polled even after it leaves
the nav, and is only retired after roughly a day of 404s.

**Non-USD renders are ignored.** Runners occasionally geolocate outside the US
and the storefront answers in local currency. Those renders are detected and
their names and prices discarded, so pesos can never overwrite a good USD
price. Products on them are still detected, so a drop is never missed because
of where the runner happened to be.

**A failed cycle still saves state**, and only a streak past
`FAILURE_ALERT_THRESHOLD` fails the job - otherwise the failure counter resets
every run and the Discord warning can never fire.

**It watches its own coverage.** Two checks run every cycle, because the worst
failure mode here is going blind in a way that looks exactly like a quiet week:

- A section that has previously held products parses to **zero** while the page
  still loads fine → you get a warning naming the section and its peak count.
  That is what a markup change in one section looks like.
- The site links somewhere the monitor is **not watching** → you get the path.
  A new section arriving in a link shape the code does not handle is otherwise
  invisible; this names it so it can be added.

Each fires once, not every cycle, and re-arms after recovery.

**It reports its own health to Discord.** You never need to open the Actions
logs to know whether it's working. If it fetches pages but parses nothing, gets
blocked by the CDN, or fails repeatedly, it says so in the same channel as the
product alerts — and says so again when it recovers. Silence means healthy.

**Expect some drift.** GitHub runs scheduled workflows on a best-effort basis and
defers them under load — a nominal 5 minutes is often 5–15 in practice, and
occasionally a run is skipped entirely. It is reliable in aggregate, not punctual.

---

## Deployment D — Docker by hand (60-second checks)

Use this if you want tighter timing than GitHub's scheduler allows.

### Setup (about 5 minutes)

#### 1. Create the Discord webhook

In Discord: **Server Settings → Integrations → Webhooks → New Webhook**. Pick the
channel you want alerts in, then **Copy Webhook URL**.

#### 2. Configure

```bash
cp .env.example .env
nano .env          # paste your DISCORD_WEBHOOK_URL
```

#### 3. Deploy

```bash
docker compose up -d --build
docker compose logs -f
```

The **first run posts no product alerts** — it silently records everything
currently on the site as the baseline and posts a single "Monitor online,
baseline captured: N products" message. Everything new from that moment on gets
an alert.

---

## Verifying it works

```bash
# Prove the Discord webhook is wired up
docker compose run --rm chrome-hearts-monitor python monitor.py --test-webhook

# Scrape the live site once and print what it found — never notifies.
# This is the command to run if you ever suspect the parser has drifted.
docker compose run --rm chrome-hearts-monitor python monitor.py --selftest

# Run one cycle and exit
docker compose run --rm chrome-hearts-monitor python monitor.py --once

# Offline test suite (45 assertions, no network needed)
docker compose run --rm chrome-hearts-monitor python tests/test_monitor.py
```

`--selftest` output looks like:

```
Resolved 6 categories: /baccarat, /boxers-leggings, /intimates, /scarf, /scents, /socks

Parsed 21 products (0 categories failed)

  [/scents           ] 162006CRYXXX271    +22+ Eau de Parfum                     $500  in stock
  [/scents           ] 162008CRYXXX271    +22+ Roll-On Perfume                   $220  SOLD OUT
  ...
```

If that prints products, the monitor works. If it prints zero, see Troubleshooting.

---

## Configuration

Everything is set in `.env`.

| Variable | Default | What it does |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | — | **Required.** Where alerts go. |
| `POLL_INTERVAL_SECONDS` | `60` | Seconds between full sweeps. |
| `JITTER_SECONDS` | `15` | Random extra delay so requests aren't perfectly periodic. |
| `REQUEST_DELAY_SECONDS` | `1.5` | Minimum gap between individual page requests. |
| `ALERT_NEW_PRODUCTS` | `true` | The core alert. |
| `ALERT_RESTOCKS` | `false` | Also ping on sold out → in stock. Flip to `true` if you want it. |
| `ALERT_PRICE_CHANGES` | `false` | Ping when a known product's price moves. |
| `ALERT_NEW_CATEGORIES` | `true` | Ping the moment a section appears that has never been seen, with its URL. This is the primary drop signal on this site - leave it on. |
| `ENRICH_PRODUCTS` | `true` | Open each new product's page for name/price/image. Set `false` for bare-URL alerts about 1s faster. |
| `CATEGORIES` | *(blank)* | Pin to specific sections, e.g. `/scents,/baccarat`. Blank = seed list + auto-discovery. |
| `DISCOVER_CATEGORIES` | `true` | Read the nav and sitemap each cycle for new sections. |
| `ROLE_MENTION` | *(blank)* | e.g. `<@&123456789012345678>` to ping a role on every alert. |
| `MAX_ALERTS_PER_CYCLE` | `40` | Safety valve so a catalog reshuffle can't dump 200 messages. |
| `HEARTBEAT_HOURS` | `12` | Periodic "still working" message with the tracked product count and how long since the last new product. `0` disables. Fires only on healthy cycles, so an outage produces silence plus a failure alert rather than a false all-clear. |
| `FAILURE_ALERT_THRESHOLD` | `10` | Consecutive failed cycles before it warns you in Discord. |
| `PROXY_URL` | *(blank)* | Route through an HTTP proxy if your VPS IP gets blocked. |

---

## Operating notes

**Interval.** 60s across ~6 categories is roughly 6 requests/minute with jitter
and conditional requests on top, which is gentle. Pushing below 30s raises the
odds of a CDN block without meaningfully improving catch rate on this site.

**robots.txt.** The monitor only requests the homepage, the sitemap, category
pages and product pages — all of which Chrome Hearts' robots.txt allows. It never
touches `/cart`, `/checkout`, or the disallowed refinement-parameter URLs, and
it uses no anti-bot evasion. Keep it that way if you edit it.

**Resetting the baseline.** If you want it to forget everything and re-baseline:

```bash
docker compose run --rm chrome-hearts-monitor python monitor.py --reseed
```

Or delete `data/state.db` and restart. Either way the next run alerts on nothing
and re-records the whole catalog.

**Watching a second site later.** Point `BASE_URL` at another Salesforce Commerce
Cloud storefront and the same URL-shape logic usually works with no code change —
run `--selftest` against it to confirm before trusting it.

---

## Troubleshooting

**No alerts ever, but the logs look healthy.** Expected until something new
actually lands — Chrome Hearts' online catalog is small and moves slowly. Confirm
the pipe works end to end with `--test-webhook`.

**`--selftest` prints 0 products.** Either the site is blocking your VPS IP or
the storefront changed shape. Check the logs for `403` — if you see them, set a
`PROXY_URL` or move to a residential/datacenter IP with a better reputation. If
you see `200` but zero products, the URL pattern changed; the fix is the
`PRODUCT_PATH_RE` regex at the top of `scraper.py`.

**Repeated 403s.** Raise `REQUEST_DELAY_SECONDS` and `POLL_INTERVAL_SECONDS`
first. A blocked sweep is explicitly *not* treated as an empty catalog, so a
block will never cause a flood of false "new product" alerts when access returns.

**Duplicate alerts after redeploy.** The `./data` volume isn't mounted — check
`docker compose config` and make sure `data/state.db` persists on the host.

**Embeds show the product ID instead of a name.** The tile parser missed and
enrichment failed. Detection still worked. Run `--selftest` to see what it's
getting.

---

## Files

```
monitor.py     main loop, diffing, alert orchestration
scraper.py     fetching, category discovery, HTML parsing, enrichment
store.py       SQLite state (products, categories, HTTP cache)
jsonstore.py   JSON-file state, same interface - used by the Actions deployment
.github/       GitHub Actions workflows (monitor, selftest, keepalive)
notifier.py    Discord webhook delivery, embeds, rate-limit handling
config.py      environment-driven configuration
tests/         offline test suite + HTML fixtures
```
