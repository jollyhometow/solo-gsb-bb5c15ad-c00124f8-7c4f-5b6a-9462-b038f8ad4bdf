"""Scenario CRUD, compiled-scenario cache and session read models."""
from __future__ import annotations

import threading
from typing import Any, Optional

from . import db
from .models import ScenarioDef
from .scenario import validate_scenario

_cache_lock = threading.Lock()
_compiled: dict[tuple[str, int], ScenarioDef] = {}


class NotFound(LookupError):
    pass


class Conflict(ValueError):
    pass


def _row_to_summary(row: Any) -> dict[str, Any]:
    return {
        "name": row["name"],
        "version": row["version"],
        "description": row["description"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_compiled(conn: Any, name: str) -> tuple[int, int, ScenarioDef]:
    """Return (scenario_id, version, validated scenario), using an in-process cache."""
    row = conn.execute("SELECT id, version, definition FROM scenarios WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise NotFound(f"scenario {name!r} not found")
    key = (name, row["version"])
    with _cache_lock:
        scenario = _compiled.get(key)
        if scenario is None:
            import json

            scenario = validate_scenario(json.loads(row["definition"]))
            # Only the newest compiled version is worth keeping.
            for stale in [key for key in _compiled if key[0] == name]:
                _compiled.pop(stale, None)
            _compiled[key] = scenario
    return int(row["id"]), int(row["version"]), scenario


def invalidate(name: str) -> None:
    with _cache_lock:
        for key in [key for key in _compiled if key[0] == name]:
            _compiled.pop(key, None)


def get_active_name(conn: Any) -> Optional[str]:
    row = conn.execute("SELECT name FROM scenarios WHERE active = 1 ORDER BY id LIMIT 1").fetchone()
    return row["name"] if row else None


def resolve_scenario_name(conn: Any, requested: Optional[str]) -> str:
    name = requested or get_active_name(conn)
    if not name:
        raise NotFound("no active scenario; submit one via POST /_sim/scenarios")
    return name


def list_scenarios() -> list[dict[str, Any]]:
    with db.read_only() as conn:
        rows = conn.execute("SELECT * FROM scenarios ORDER BY id").fetchall()
        return [_row_to_summary(row) for row in rows]


def get_scenario(name: str) -> dict[str, Any]:
    import json

    with db.read_only() as conn:
        row = conn.execute("SELECT * FROM scenarios WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise NotFound(f"scenario {name!r} not found")
        result = _row_to_summary(row)
        result["definition"] = json.loads(row["definition"])
        return result


def upsert_scenario(scenario: ScenarioDef, definition_json: str, activate: bool) -> dict[str, Any]:
    """Create or version-bump a scenario; optionally activate it."""
    with db.write_tx() as conn:
        existing = conn.execute(
            "SELECT id, version FROM scenarios WHERE name = ?", (scenario.name,)
        ).fetchone()
        if existing is None:
            cur = conn.execute(
                "INSERT INTO scenarios (name, version, description, definition, active) "
                "VALUES (?, 1, ?, ?, ?)",
                (scenario.name, scenario.description, definition_json, 1 if activate else 0),
            )
            action, version = "created", 1
            if activate:
                conn.execute(
                    "UPDATE scenarios SET active = CASE WHEN id = ? THEN 1 ELSE 0 END",
                    (cur.lastrowid,),
                )
        else:
            version = int(existing["version"]) + 1
            conn.execute(
                "UPDATE scenarios SET version = ?, description = ?, definition = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (version, scenario.description, definition_json, existing["id"]),
            )
            if activate:
                conn.execute(
                    "UPDATE scenarios SET active = CASE WHEN id = ? THEN 1 ELSE 0 END",
                    (existing["id"],),
                )
            action = "updated"
        active_name = scenario.name if activate else None
        if not activate:
            row = conn.execute("SELECT name FROM scenarios WHERE active = 1").fetchone()
            active_name = row["name"] if row else None
    return {"name": scenario.name, "version": version, "action": action, "active": active_name}


def activate_scenario(name: str) -> None:
    with db.write_tx() as conn:
        row = conn.execute("SELECT id FROM scenarios WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise NotFound(f"scenario {name!r} not found")
        conn.execute(
            "UPDATE scenarios SET active = CASE WHEN id = ? THEN 1 ELSE 0 END", (row["id"],)
        )


def delete_scenario(name: str) -> None:
    with db.write_tx() as conn:
        cur = conn.execute("DELETE FROM scenarios WHERE name = ?", (name,))
        if cur.rowcount == 0:
            raise NotFound(f"scenario {name!r} not found")
    # Drop cached versions.
    invalidate(name)


# ---------------------------------------------------------------------------#
# Sessions
# ---------------------------------------------------------------------------#

def _session_row(conn: Any, scenario_id: int, token: str) -> Any:
    row = conn.execute(
        "SELECT * FROM sessions WHERE scenario_id = ? AND token = ?",
        (scenario_id, token),
    ).fetchone()
    if row is None:
        raise NotFound(f"session token {token!r} does not exist for this scenario")
    return row


def get_session(name: str, token: str, include_events: bool = True) -> dict[str, Any]:
    import json

    with db.read_only() as conn:
        scenario_id, version, scenario = get_compiled(conn, name)
        row = _session_row(conn, scenario_id, token)
        result = {
            "scenario": name,
            "version": version,
            "token": token,
            "state": row["state"],
            "vars": json.loads(row["vars"]),
            "fires": json.loads(row["fires"]),
            "request_count": row["request_count"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if include_events:
            result["events"] = list_events(conn, row["id"])
        return result


def list_sessions(name: str) -> list[dict[str, Any]]:
    with db.read_only() as conn:
        scenario_id, _, _ = get_compiled(conn, name)
        rows = conn.execute(
            "SELECT token, state, request_count, created_at, updated_at "
            "FROM sessions WHERE scenario_id = ? ORDER BY id",
            (scenario_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def list_events(conn: Any, session_id: int, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    import json

    rows = conn.execute(
        "SELECT seq, ts, method, path, rule, from_state, to_state, status, detail "
        "FROM events WHERE session_id = ? ORDER BY seq DESC LIMIT ? OFFSET ?",
        (session_id, limit, offset),
    ).fetchall()
    events = []
    for row in rows:
        event = dict(row)
        event["detail"] = json.loads(event["detail"]) if event["detail"] else None
        events.append(event)
    return events


def reset_session(name: str, token: str) -> None:
    with db.write_tx() as conn:
        scenario_id, _, scenario = get_compiled(conn, name)
        row = conn.execute(
            "SELECT id FROM sessions WHERE scenario_id = ? AND token = ?",
            (scenario_id, token),
        ).fetchone()
        if row is None:
            raise NotFound(f"session token {token!r} does not exist for this scenario")
        conn.execute("DELETE FROM events WHERE session_id = ?", (row["id"],))
        conn.execute(
            "UPDATE sessions SET state = ?, vars = ?, fires = '{}', request_count = 0, "
            "updated_at = datetime('now') WHERE id = ?",
            (scenario.initial_state, db.dumps(scenario.initial_vars), row["id"]),
        )
