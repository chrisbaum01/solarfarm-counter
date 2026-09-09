"""SQLite-backed cache for Overpass tiles and geocoding results."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from . import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = Path(config.CACHE_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(path), check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS kv ("
            "  key TEXT PRIMARY KEY,"
            "  value TEXT NOT NULL,"
            "  stored_at REAL NOT NULL"
            ")"
        )
        _conn.commit()
    return _conn


def get(key: str, ttl: float) -> Any | None:
    """Return the cached value, or None if absent or older than `ttl` seconds."""
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT value, stored_at FROM kv WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    value, stored_at = row
    if time.time() - stored_at > ttl:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def put(key: str, value: Any) -> None:
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT OR REPLACE INTO kv (key, value, stored_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), time.time()),
        )
        conn.commit()


def stats() -> dict[str, int]:
    with _lock:
        conn = _connect()
        total = conn.execute("SELECT COUNT(*) FROM kv").fetchone()[0]
        tiles = conn.execute(
            "SELECT COUNT(*) FROM kv WHERE key LIKE 'tile:%'"
        ).fetchone()[0]
    return {"entries": total, "tiles": tiles}
