"""SQLite storage, schema bootstrap and transaction helpers.

A single connection is used for writes and guarded by a process-wide lock;
every simulated request takes ``BEGIN IMMEDIATE`` so concurrent requests for
the same session are serialised at the database and can never fire the same
transition twice.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from . import config

_write_lock = threading.RLock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        config.DB_PATH,
        timeout=config.BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,  # transactions are managed explicitly
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {config.BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


_write_conn: Optional[sqlite3.Connection] = None
_read_conn: Optional[sqlite3.Connection] = None
_init_lock = threading.Lock()


SCHEMA = """
CREATE TABLE IF NOT EXISTS scenarios (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    version     INTEGER NOT NULL DEFAULT 1,
    description TEXT,
    definition  TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id  INTEGER NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
    token        TEXT NOT NULL,
    state        TEXT NOT NULL,
    vars         TEXT NOT NULL DEFAULT '{}',
    fires        TEXT NOT NULL DEFAULT '{}',
    request_count INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (scenario_id, token)
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    ts          TEXT NOT NULL DEFAULT (datetime('now')),
    method      TEXT NOT NULL,
    path        TEXT NOT NULL,
    rule        TEXT,
    from_state  TEXT,
    to_state    TEXT,
    status      INTEGER,
    detail      TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_session ON events (session_id, seq);
"""


def init_db() -> None:
    """Create the data directory and schema; safe to call repeatedly."""
    global _write_conn, _read_conn
    with _init_lock:
        db_dir = os.path.dirname(os.path.abspath(config.DB_PATH))
        os.makedirs(db_dir, exist_ok=True)
        conn = _connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()
        _write_conn = _connect()
        _read_conn = _connect()


def get_read_conn() -> sqlite3.Connection:
    assert _read_conn is not None, "init_db() must run before serving requests"
    return _read_conn


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """Yield a connection holding an immediate write transaction."""
    assert _write_conn is not None, "init_db() must run before serving requests"
    conn = _write_conn
    with _write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


@contextmanager
def read_only() -> Iterator[sqlite3.Connection]:
    """Open a short-lived read connection (admin/query endpoints)."""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)
