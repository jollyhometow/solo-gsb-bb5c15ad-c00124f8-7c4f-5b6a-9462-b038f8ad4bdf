"""Runtime configuration, all overridable through environment variables."""
from __future__ import annotations

import os


def _as_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# SQLite database file (a WAL sidecar file is created next to it).
DB_PATH: str = os.getenv("SIM_DB_PATH", "/data/sim.db")

# YAML scenario loaded automatically on startup when the name is unknown
# to the database and there is no active scenario yet.
SCENARIO_FILE: str = os.getenv("SIM_SCENARIO_FILE", "/app/scenarios/example.yaml")
AUTOLOAD_SCENARIO: bool = _as_bool(os.getenv("SIM_AUTOLOAD_SCENARIO", "true"), default=True)

# SQLite writer lock wait (ms). Concurrent simulated requests queue on
# BEGIN IMMEDIATE instead of failing with "database is locked".
BUSY_TIMEOUT_MS: int = int(os.getenv("SIM_BUSY_TIMEOUT_MS", "10000"))

# HTTP surface.
ADMIN_PREFIX: str = "/_sim"
SESSION_HEADER: str = "x-session-token"
SCENARIO_HEADER: str = "x-scenario-name"
