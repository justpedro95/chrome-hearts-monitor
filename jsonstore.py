"""JSON-file state backend, interface-compatible with store.Store.

Used by the GitHub Actions deployment: each run reads state/products.json from
the repo, and the workflow commits it back afterwards. Plain JSON (rather than
the SQLite file) so the state is diffable in the repo - you can see exactly what
the monitor noticed and when, straight from the commit history.
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, Optional


class JsonStore:
    def __init__(self, path: str = None):
        import config

        self.path = path or config.STATE_JSON
        self.dirty = False
        self.data = {"version": 1, "meta": {}, "products": {}, "categories": {}, "http_cache": {}}
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict) and "products" in loaded:
                    self.data.update(loaded)
            except (ValueError, OSError):
                # Corrupt state is recoverable: treat as a fresh baseline rather
                # than crash-looping. Worst case is one silent re-seed.
                pass

    # --- meta ---------------------------------------------------------------
    def get_meta(self, key: str, default=None):
        return self.data["meta"].get(key, default)

    def set_meta(self, key: str, value) -> None:
        self.data["meta"][key] = str(value)
        self.dirty = True

    # --- products -----------------------------------------------------------
    def known_pids(self) -> set:
        return set(self.data["products"].keys())

    def get_product(self, pid: str) -> Optional[dict]:
        return self.data["products"].get(pid)

    def product_count(self) -> int:
        return len(self.data["products"])

    def upsert(self, product) -> None:
        now = time.time()
        existing = self.data["products"].get(product.pid, {})
        record = {
            "url": product.url,
            "category": product.category or existing.get("category"),
            "name": product.name or existing.get("name"),
            "price": product.price or existing.get("price"),
            "image": product.image or existing.get("image"),
            "in_stock": 1 if product.in_stock else 0,
            "first_seen": existing.get("first_seen", now),
            "last_seen": now,
        }
        if existing != record:
            self.dirty = True
        self.data["products"][product.pid] = record

    # --- categories ---------------------------------------------------------
    def known_categories(self) -> set:
        return set(self.data["categories"].keys())

    def add_category(self, path: str) -> None:
        if path not in self.data["categories"]:
            self.data["categories"][path] = time.time()
            self.dirty = True

    # --- http cache ---------------------------------------------------------
    def load_etags(self) -> Dict[str, dict]:
        return dict(self.data.get("http_cache", {}))

    def save_etags(self, cache: Dict[str, dict]) -> None:
        self.data["http_cache"] = cache
        self.dirty = True
        self.commit()

    # --- persistence --------------------------------------------------------
    def commit(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = self.path + ".tmp"
        payload = dict(self.data)
        payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, self.path)

    def close(self) -> None:
        self.commit()
