"""Configuration for the Chrome Hearts monitor. All values come from the environment."""
import os

try:  # optional: lets you run outside Docker with a plain .env file
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:  # pragma: no cover
    pass


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _list(name: str) -> list:
    raw = os.environ.get(name, "")
    return [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]


BASE_URL = os.environ.get("BASE_URL", "https://www.chromehearts.com").rstrip("/")

# --- Discord -----------------------------------------------------------------
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_USERNAME = os.environ.get("DISCORD_USERNAME", "Chrome Hearts Monitor")
DISCORD_AVATAR_URL = os.environ.get("DISCORD_AVATAR_URL", "").strip()
ROLE_MENTION = os.environ.get("ROLE_MENTION", "").strip()
EMBED_COLOR = _int("EMBED_COLOR", 0x000000)
RESTOCK_COLOR = _int("RESTOCK_COLOR", 0x2ECC71)
PRICE_COLOR = _int("PRICE_COLOR", 0xE67E22)

# --- Polling -----------------------------------------------------------------
POLL_INTERVAL_SECONDS = _int("POLL_INTERVAL_SECONDS", 60)
JITTER_SECONDS = _int("JITTER_SECONDS", 15)
REQUEST_DELAY_SECONDS = _float("REQUEST_DELAY_SECONDS", 1.5)
REQUEST_TIMEOUT = _int("REQUEST_TIMEOUT", 20)

# --- Scope -------------------------------------------------------------------
# Blank CATEGORIES => use SEED_CATEGORIES plus anything auto-discovered.
CATEGORIES = _list("CATEGORIES")
DISCOVER_CATEGORIES = _bool("DISCOVER_CATEGORIES", True)
SEED_CATEGORIES = _list("SEED_CATEGORIES") or [
    "/scents",
    "/baccarat",
    "/boxers-leggings",
    "/intimates",
    "/socks",
    "/scarf",
]

# --- Alerting ----------------------------------------------------------------
ALERT_NEW_PRODUCTS = _bool("ALERT_NEW_PRODUCTS", True)
ALERT_RESTOCKS = _bool("ALERT_RESTOCKS", False)
ALERT_PRICE_CHANGES = _bool("ALERT_PRICE_CHANGES", False)
ALERT_NEW_CATEGORIES = _bool("ALERT_NEW_CATEGORIES", False)
ENRICH_PRODUCTS = _bool("ENRICH_PRODUCTS", True)
MAX_ALERTS_PER_CYCLE = _int("MAX_ALERTS_PER_CYCLE", 40)

# --- Behaviour ---------------------------------------------------------------
STATE_BACKEND = os.environ.get("STATE_BACKEND", "sqlite").strip().lower()  # sqlite | json
STATE_DB = os.environ.get("STATE_DB", "/data/state.db")
STATE_JSON = os.environ.get("STATE_JSON", "state/products.json")
SEED_ON_FIRST_RUN = _bool("SEED_ON_FIRST_RUN", True)
HEARTBEAT_HOURS = _float("HEARTBEAT_HOURS", 0)
FAILURE_ALERT_THRESHOLD = _int("FAILURE_ALERT_THRESHOLD", 10)
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
)
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
