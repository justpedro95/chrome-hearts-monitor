"""Discord webhook delivery with rate-limit handling."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import List

import requests

import config

log = logging.getLogger("notifier")

MAX_EMBEDS_PER_MESSAGE = 10
FOOTER = {"text": "Chrome Hearts Monitor"}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_embed(product, kind: str = "new", old_price: str = None) -> dict:
    if kind == "restock":
        title = product.name or product.pid
        color = config.RESTOCK_COLOR
        author = "Back in stock"
    elif kind == "price":
        title = product.name or product.pid
        color = config.PRICE_COLOR
        author = "Price change"
    else:
        title = product.name or product.pid
        color = config.EMBED_COLOR
        author = "New product"

    fields = []
    if kind == "price" and old_price:
        fields.append({"name": "Price", "value": f"~~{old_price}~~ → **{product.price or '?'}**", "inline": True})
    elif product.price:
        fields.append({"name": "Price", "value": product.price, "inline": True})

    fields.append({
        "name": "Availability",
        "value": "In stock" if product.in_stock else "Sold out",
        "inline": True,
    })
    if product.category:
        fields.append({"name": "Category", "value": product.category.lstrip("/"), "inline": True})
    fields.append({"name": "Product ID", "value": f"`{product.pid}`", "inline": True})

    embed = {
        "author": {"name": author},
        "title": title[:250],
        "url": product.url,
        "color": color,
        "fields": fields,
        "footer": FOOTER,
        "timestamp": _iso_now(),
    }
    if product.image:
        embed["thumbnail"] = {"url": product.image}
        embed["image"] = {"url": product.image}
    description = (product.extras or {}).get("description")
    if description:
        embed["description"] = description[:400]
    return embed


def _post(payload: dict, attempts: int = 4) -> bool:
    if not config.DISCORD_WEBHOOK_URL:
        log.error("DISCORD_WEBHOOK_URL is not set - dropping notification")
        return False

    for attempt in range(1, attempts + 1):
        try:
            resp = requests.post(config.DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        except requests.RequestException as exc:
            log.warning("discord post failed (%s/%s): %s", attempt, attempts, exc)
            time.sleep(2 * attempt)
            continue

        if resp.status_code in (200, 204):
            return True

        if resp.status_code == 429:
            try:
                retry_after = float(resp.json().get("retry_after", 2))
            except Exception:
                retry_after = 2.0
            # Discord returns seconds on webhooks; guard against ms-style values.
            if retry_after > 60:
                retry_after /= 1000.0
            log.info("discord rate limited, sleeping %.2fs", retry_after)
            time.sleep(retry_after + 0.25)
            continue

        if 500 <= resp.status_code < 600:
            time.sleep(2 * attempt)
            continue

        log.error("discord rejected payload: %s %s", resp.status_code, resp.text[:400])
        return False

    return False


def send_embeds(embeds: List[dict], content: str = None) -> bool:
    """Send embeds in chunks of 10, mentioning a role on the first chunk only."""
    ok = True
    if not embeds:
        return True
    for index in range(0, len(embeds), MAX_EMBEDS_PER_MESSAGE):
        chunk = embeds[index:index + MAX_EMBEDS_PER_MESSAGE]
        payload = {"embeds": chunk, "username": config.DISCORD_USERNAME}
        if config.DISCORD_AVATAR_URL:
            payload["avatar_url"] = config.DISCORD_AVATAR_URL
        if index == 0:
            text = " ".join(p for p in (config.ROLE_MENTION, content) if p).strip()
            if text:
                payload["content"] = text[:1900]
        ok = _post(payload) and ok
        time.sleep(0.6)
    return ok


def send_text(content: str) -> bool:
    payload = {"content": content[:1900], "username": config.DISCORD_USERNAME}
    if config.DISCORD_AVATAR_URL:
        payload["avatar_url"] = config.DISCORD_AVATAR_URL
    return _post(payload)
