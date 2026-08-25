"""Simulates the GitHub Actions deployment: separate processes, state on disk only."""
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ["DISCORD_WEBHOOK_URL"] = "https://discord.com/api/webhooks/test/test"

import config  # noqa: E402
import notifier  # noqa: E402
from monitor import run_cycle  # noqa: E402
from test_monitor import FakeFetcher, check, PASS, FAIL  # noqa: E402

BEFORE = (HERE / "fixture_category.html").read_text()
AFTER = (HERE / "fixture_category_after.html").read_text()
PDP = (HERE / "fixture_pdp.html").read_text()

sent = []
notifier._post = lambda payload, attempts=4: (sent.append(payload), True)[1]


def fresh_run(state_path, html):
    """Each call mimics a brand-new CI runner: nothing in memory, state read from disk."""
    from jsonstore import JsonStore

    config.STATE_JSON = state_path
    store = JsonStore(state_path)
    result = run_cycle(store, FakeFetcher(html, pdp_html=PDP))
    store.close()
    return result


if __name__ == "__main__":
    config.CATEGORIES = ["/scents"]
    config.DISCOVER_CATEGORIES = False
    config.STATE_BACKEND = "json"

    with tempfile.TemporaryDirectory() as tmp:
        state = os.path.join(tmp, "state", "products.json")
        print("\n[JSON state backend across separate runs]")

        result = fresh_run(state, BEFORE)
        check("run 1 seeds the baseline", result.get("seeded") == 3, result)
        check("state file written to disk", os.path.exists(state))

        sent.clear()
        result = fresh_run(state, BEFORE)
        check("run 2 (new process) recalls state, alerts nothing", result.get("new") == 0 and not sent, result)

        sent.clear()
        result = fresh_run(state, AFTER)
        check("run 3 detects the new product", result.get("new") == 1, result)
        check("run 3 posts one Discord message", len(sent) == 1, len(sent))

        sent.clear()
        result = fresh_run(state, AFTER)
        check("run 4 does not re-alert", result.get("new") == 0 and not sent, result)

        import json
        blob = json.loads(open(state).read())
        check("state is human-readable JSON with all 4 products", len(blob["products"]) == 4, len(blob["products"]))
        check("state records first_seen per product",
              all("first_seen" in v for v in blob["products"].values()))
        check("state carries the seeded flag", blob["meta"].get("seeded") == "1")

        print("\n[corrupt state recovery]")
        open(state, "w").write("{ this is not json")
        result = fresh_run(state, AFTER)
        check("corrupt state re-seeds instead of crashing", result.get("seeded") == 4, result)

        print("\n[failure alerting survives process restarts]")
        state2 = os.path.join(tmp, "state2", "products.json")
        config.FAILURE_ALERT_THRESHOLD = 3

        class BlockedFetcher(FakeFetcher):
            def get(self, url, use_cache=True, attempts=3):
                return 403, None

        from jsonstore import JsonStore

        def blocked_run(path):
            config.STATE_JSON = path
            store = JsonStore(path)
            r = run_cycle(store, BlockedFetcher(""))
            from monitor import record_failure, record_success
            if r.get("error"):
                record_failure(store, r["error"], r.get("detail", ""), immediate=bool(r.get("immediate")))
            else:
                record_success(store)
            store.close()
            return r

        # Seed a healthy baseline first so we exercise the non-seed failure path.
        fresh_run(state2, BEFORE)
        sent.clear()

        blocked_run(state2)
        check("failure 1 of 3 stays quiet", len(sent) == 0, len(sent))
        blocked_run(state2)
        check("failure 2 of 3 stays quiet", len(sent) == 0, len(sent))
        blocked_run(state2)
        check("failure 3 of 3 alerts once", len(sent) == 1, len(sent))
        blocked_run(state2)
        check("failure 4 does not re-alert", len(sent) == 1, len(sent))

        sent.clear()
        fresh_run(state2, BEFORE)
        config.STATE_JSON = state2
        store = JsonStore(state2)
        from monitor import record_success
        record_success(store)
        store.close()
        check("recovery posts exactly one message", len(sent) == 1, len(sent))
        check("counter reset after recovery",
              JsonStore(state2).get_meta("consecutive_failures") in (0, "0"))

        print("\n[12-hour heartbeat across separate processes]")
        import time as _time
        from monitor import maybe_heartbeat
        from jsonstore import JsonStore

        state3 = os.path.join(tmp, "state3", "products.json")
        config.STATE_JSON = state3
        config.HEARTBEAT_HOURS = 12
        fresh_run(state3, BEFORE)          # seed
        sent.clear()

        def hb(path):
            store = JsonStore(path)
            fired = maybe_heartbeat(store)
            store.close()
            return fired

        check("first healthy cycle starts the clock, sends nothing",
              hb(state3) is False and not sent, len(sent))
        check("a cycle 5 minutes later stays quiet", hb(state3) is False and not sent, len(sent))

        # Wind the stored timestamp back 12h+ to simulate a real day passing.
        store = JsonStore(state3)
        store.set_meta("last_heartbeat", _time.time() - (12 * 3600 + 60))
        store.set_meta("last_new_product_at", _time.time() - (30 * 3600))
        store.close()

        check("fires once past 12 hours", hb(state3) is True and len(sent) == 1, len(sent))
        body = sent[0]["content"] if sent else ""
        check("message reports the tracked product count", "3 products" in body, body)
        check("message reports when the last new product was", "Last new product" in body, body)
        check("does not fire again on the next cycle", hb(state3) is False and len(sent) == 1, len(sent))

        config.HEARTBEAT_HOURS = 0
        check("HEARTBEAT_HOURS=0 disables it", hb(state3) is False)

    print(f"\n{'='*60}\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
