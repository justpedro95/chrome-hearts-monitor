"""SQLite-backed state. Survives container restarts so you never get duplicate alerts."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Dict, Optional

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    pid         TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    category    TEXT,
    name        TEXT,
    price       TEXT,
    image       TEXT,
    in_stock    INTEGER DEFAULT 1,
    first_seen  REAL,
    last_seen   REAL
);
CREATE TABLE IF NOT EXISTS categories (
    path        TEXT PRIMARY KEY,
    first_seen  REAL
);
CREATE TABLE IF NOT EXISTS http_cache (
    url             TEXT PRIMARY KEY,
    etag            TEXT,
    last_modified   TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    def __init__(self, path: str = None):
        path = path or config.STATE_DB
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- meta ---------------------------------------------------------------
    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    # --- products -----------------------------------------------------------
    def known_pids(self) -> set:
        return {r["pid"] for r in self.conn.execute("SELECT pid FROM products")}

    def get_product(self, pid: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM products WHERE pid = ?", (pid,)).fetchone()

    def product_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]

    def upsert(self, product) -> None:
        now = time.time()
        self.conn.execute(
            """
            INSERT INTO products (pid, url, category, name, price, image, in_stock, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pid) DO UPDATE SET
                url       = excluded.url,
                category  = COALESCE(excluded.category, products.category),
                name      = COALESCE(excluded.name, products.name),
                price     = COALESCE(excluded.price, products.price),
                image     = COALESCE(excluded.image, products.image),
                in_stock  = excluded.in_stock,
                last_seen = excluded.last_seen
            """,
            (
                product.pid, product.url, product.category, product.name,
                product.price, product.image, 1 if product.in_stock else 0, now, now,
            ),
        )

    def commit(self) -> None:
        self.conn.commit()

    # --- categories ---------------------------------------------------------
    def known_categories(self) -> set:
        return {r["path"] for r in self.conn.execute("SELECT path FROM categories")}

    def add_category(self, path: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO categories (path, first_seen) VALUES (?, ?)", (path, time.time())
        )

    # --- http cache ---------------------------------------------------------
    def load_etags(self) -> Dict[str, dict]:
        return {
            r["url"]: {"etag": r["etag"], "last_modified": r["last_modified"]}
            for r in self.conn.execute("SELECT * FROM http_cache")
        }

    def save_etags(self, cache: Dict[str, dict]) -> None:
        self.conn.executemany(
            "INSERT INTO http_cache (url, etag, last_modified) VALUES (?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET etag = excluded.etag, last_modified = excluded.last_modified",
            [(u, v.get("etag"), v.get("last_modified")) for u, v in cache.items()],
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
