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

    print(f"\n{'='*60}\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
