from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


DATABASE_PATH = os.getenv("DATABASE_PATH", "/data/charity-hub.db")


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'new-api',
    status TEXT NOT NULL DEFAULT 'active',
    note TEXT NOT NULL DEFAULT '',
    last_probe_at TEXT,
    last_probe_ok INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(platform, url)
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    username TEXT NOT NULL DEFAULT '',
    credential TEXT NOT NULL,
    credential_type TEXT NOT NULL DEFAULT 'token',
    platform_user_id INTEGER,
    balance REAL,
    used REAL,
    quota REAL,
    status TEXT NOT NULL DEFAULT 'active',
    checkin_enabled INTEGER NOT NULL DEFAULT 1,
    last_checkin_at TEXT,
    last_balance_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS checkin_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    reward TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_probe_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    base_url TEXT NOT NULL,
    model TEXT NOT NULL,
    alias TEXT NOT NULL DEFAULT '',
    family TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    http_status INTEGER,
    latency_ms INTEGER,
    message TEXT NOT NULL DEFAULT '',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_ok_at TEXT,
    checked_at TEXT NOT NULL,
    UNIQUE(provider, model)
);

CREATE TABLE IF NOT EXISTS cpa_provider_state (
    provider TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    disabled_by_hub INTEGER NOT NULL DEFAULT 0,
    removed_models TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cpa_sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    provider TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cpa_backups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reason TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_accounts_site ON accounts(site_id);
CREATE INDEX IF NOT EXISTS idx_logs_created ON checkin_logs(created_at DESC);
"""


def _connect() -> sqlite3.Connection:
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connection() as conn:
        conn.executescript(SCHEMA)
        site_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sites)").fetchall()}
        for column in ("cpa_key", "cpa_status", "cpa_synced_at"):
            if column not in site_columns:
                conn.execute(f"ALTER TABLE sites ADD COLUMN {column} TEXT")
        defaults = {
            "checkin_enabled": os.getenv("CHECKIN_ENABLED", "true").lower(),
            "checkin_time": os.getenv("CHECKIN_TIME", "02:00"),
            "last_scheduler_run": "",
            "last_scheduler_day": "",
            "admin_password_hash": "",
            "model_probe_enabled": os.getenv("MODEL_PROBE_ENABLED", "true").lower(),
            "model_probe_interval": os.getenv("MODEL_PROBE_INTERVAL_MINUTES", "60"),
            "model_probe_last_run": "",
            "model_probe_last_error": "",
            "model_probe_meta": "{}",
            "cpa_sync_enabled": os.getenv("CPA_SYNC_ENABLED", "true").lower(),
        }
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows_dict(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
