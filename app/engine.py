"""Matching, templating and the single-transaction simulation engine."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from jinja2 import Undefined
from jinja2.sandbox import SandboxedEnvironment

from .errors import MatchFailure, ScenarioError
from .models import MatchDef, RuleDef, ScenarioDef
from . import db


# --------------------------------------------------------------------------- #
# Request matching
# --------------------------------------------------------------------------- #

_PATH_TOKEN = re.compile(r"[^.\[\]]+|\[\d+\]|\['(?:[^'\\]|\\.)*'\]|\[\"(?:[^\"\\]|\\.)*\"\]")


def json_get(data: Any, path: str) -> tuple[bool, Any]:
    """Fetch a value by a small JSON-Path dialect.

    Supports: ``a.b.c``, ``a[0]``, ``a['key with.dot']``.
    Returns ``(found, value)``.
    """
    current = data
    for raw_token in _PATH_TOKEN.findall(path):
        token = raw_token
        if token.startswith("["):
            inner = token[1:-1]
            if inner.isdigit():
                index = int(inner)
                if not isinstance(current, list) or index >= len(current):
                    return False, None
                current = current[index]
                continue
            token = inner[1:-1].encode("utf-8").decode("unicode_escape")
        if not isinstance(current, dict) or token not in current:
            return False, None
        current = current[token]
    return True, current


def _actual_value(condition: MatchDef, req: dict[str, Any]) -> tuple[bool, Any]:
    if condition.location == "header":
        headers = {k.lower(): v for k, v in req["headers"].items()}
        key = condition.location_key.lower()
        return key in headers, headers.get(key)
    if condition.location == "query":
        return condition.location_key in req["query"], req["query"].get(condition.location_key)
    if not isinstance(req["body"], (dict, list)):
        return False, None
    return json_get(req["body"], condition.location_key)
    return False, None  # pragma: no cover


def _numbers(a: Any, b: Any) -> tuple[float, float]:
    try:
        return float(a), float(b)
    except (TypeError, ValueError) as exc:
        raise ScenarioError(f"ordering operator requires numeric operands, got {a!r} / {b!r}") from exc


def _check_operator(condition: MatchDef, found: bool, actual: Any) -> bool:
    if condition.exists is not None:
        return found is bool(condition.exists)
    if not found:
        return False

    def compare(op: str, expected: Any) -> bool:
        left, right = _numbers(actual, expected)
        return {
            "gt": left > right,
            "gte": left >= right,
            "lt": left < right,
            "lte": left <= right,
        }[op]

    checks = [
        ("value", lambda v: actual == v),
        ("equals", lambda v: actual == v),
        ("not_equals", lambda v: actual != v),
        ("contains", lambda v: (v in actual) if isinstance(actual, (str, list, dict)) else False),
        ("not_contains", lambda v: (v not in actual) if isinstance(actual, (str, list, dict)) else True),
        ("starts_with", lambda v: isinstance(actual, str) and actual.startswith(v)),
        ("ends_with", lambda v: isinstance(actual, str) and actual.endswith(v)),
        ("gt", lambda v: compare("gt", v)),
        ("gte", lambda v: compare("gte", v)),
        ("lt", lambda v: compare("lt", v)),
        ("lte", lambda v: compare("lte", v)),
        ("regex", lambda v: re.search(v, "" if actual is None else str(actual)) is not None),
        ("in_", lambda v: actual in v),
    ]
    for field, predicate in checks:
        expected = getattr(condition, field)
        if expected is not None and not predicate(expected):
            return False
    return True


def rule_matches(
    rule: RuleDef,
    state: str,
    req: dict[str, Any],
    context: Optional[dict[str, Any]] = None,
) -> bool:
    if rule.when_state is not None and state != rule.when_state:
        return False
    if rule.when_state_not is not None and state == rule.when_state_not:
        return False
    for condition in rule.match:
        found, actual = _actual_value(condition, req)
        rendered = _render_condition(condition, context)
        if not _check_operator(rendered, found, actual):
            return False
    return True


def _render_condition(condition: MatchDef, context: Optional[dict[str, Any]]) -> MatchDef:
    """Clone a condition with string expectations rendered as Jinja templates.

    Rendering happens per evaluation so matchers can compare against live
    session variables (e.g. equals: "{{ vars.last_amount }}").
    """
    if context is None:
        return condition

    def maybe_render(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        # Whole-template expressions keep the JSON type of the variable
        # ({{ vars.last_amount }} vs numeric 100 compares equal).
        return _render_whole(value, context)

    fields = (
        "value", "equals", "not_equals", "contains", "not_contains",
        "starts_with", "ends_with", "regex",
    )
    updates = {field: maybe_render(getattr(condition, field)) for field in fields}
    for field in ("gt", "gte", "lt", "lte"):
        raw = getattr(condition, field)
        if raw is not None:
            rendered = maybe_render(raw)
            try:
                updates[field] = float(rendered)
            except (TypeError, ValueError):
                raise ScenarioError(
                    f"operator {field} in match on {condition.location} "
                    f"{condition.location_key!r} must render numeric, got {rendered!r}",
                )
    if condition.in_ is not None:
        updates["in_"] = [maybe_render(item) for item in condition.in_]
    return condition.model_copy(update=updates)


def find_rule(
    scenario: ScenarioDef,
    req: dict[str, Any],
    state: str,
    context: Optional[dict[str, Any]] = None,
) -> tuple[Optional[Any], Optional[RuleDef]]:
    """Locate route + highest-priority matching rule (lower priority wins)."""
    route = next(
        (r for r in scenario.routes if r.path == req["path"] and req["method"] in r.methods),
        None,
    )
    if route is None:
        return None, None
    ordered = sorted(enumerate(route.rules), key=lambda pair: (pair[1].priority, pair[0]))
    for _, rule in ordered:
        if rule_matches(rule, state, req, context):
            return route, rule
    return route, None


# ---------------------------------------------------------------------------#
# Templating
# ---------------------------------------------------------------------------#

class _BlankUndefined(Undefined):
    """Missing variables render as an empty string instead of raising."""

    def __str__(self) -> str:  # noqa: D401
        return ""

    def __iter__(self):  # type: ignore[override]
        return iter(())

    def __bool__(self) -> bool:  # type: ignore[override]
        return False


class TemplateAbort(Exception):
    """Raised from templates through the ``raise()`` helper."""


def _now(fmt: str = "%Y-%m-%dT%H:%M:%SZ") -> str:
    return datetime.now(timezone.utc).strftime(fmt)


def _uuid() -> str:
    return uuid.uuid4().hex


def _raise(message: str = "aborted by scenario") -> None:
    raise TemplateAbort(str(message))


def build_jinja_env() -> SandboxedEnvironment:
    env = SandboxedEnvironment(undefined=_BlankUndefined, autoescape=False)
    env.globals["now"] = _now
    env.globals["uuid"] = _uuid
    env.globals["raise"] = _raise

    # A missing key on an existing dict still yields a strict Undefined in
    # Jinja; make |default work on it the same way top-level blanks do.
    def default_filter(value: Any, default_value: Any = "") -> Any:
        if isinstance(value, Undefined) or value is None:
            return default_value
        return value

    env.filters["default"] = default_filter
    env.filters["d"] = default_filter
    return env


JINJA = build_jinja_env()


def render_text(text: str, context: dict[str, Any]) -> str:
    return JINJA.from_string(text).render(**context)


def _render_node(node: Any, context: dict[str, Any]) -> Any:
    if isinstance(node, str):
        return _render_whole(node, context)
    if isinstance(node, dict):
        return {key: _render_node(value, context) for key, value in node.items()}
    if isinstance(node, list):
        return [_render_node(value, context) for value in node]
    return node


def _coerce_scalar(rendered: str) -> Any:
    """Turn a whole-template rendering into its native JSON scalar type."""
    stripped = rendered.strip()
    if not stripped:
        return rendered
    try:
        if re.fullmatch(r"-?\d+", stripped):
            return int(stripped)
        return float(stripped)
    except ValueError:
        pass
    if stripped in ("true", "false"):
        return stripped == "true"
    if stripped in ("null", "none"):
        return None
    return rendered


def _render_whole(value: str, context: dict[str, Any]) -> Any:
    """Render a template; ``{{ expr }}``-only values keep native types."""
    rendered = render_text(value, context)
    if value.startswith("{{") and value.rstrip().endswith("}}") and "{{" not in value[2:-2]:
        return _coerce_scalar(rendered)
    return rendered


def _render_vars(values: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    rendered: dict[str, Any] = {}
    for key, value in values.items():
        if key == "state":
            if not isinstance(value, str):
                raise ScenarioError("transition.set.state must be a string template")
            rendered[key] = render_text(value, context)
        else:
            rendered[key] = _render_node(value, context)
    return rendered


# ---------------------------------------------------------------------------#
# Transactional simulation
# ---------------------------------------------------------------------------#

def _load_or_create_session(conn: Any, scenario_id: int, scenario: ScenarioDef, token: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, state, vars, fires, request_count FROM sessions "
        "WHERE scenario_id = ? AND token = ?",
        (scenario_id, token),
    ).fetchone()
    if row is not None:
        return {
            "id": row["id"],
            "state": row["state"],
            "vars": db.loads(row["vars"], {}),
            "fires": db.loads(row["fires"], {}),
            "request_count": int(row["request_count"]),
            "created": False,
        }
    cur = conn.execute(
        "INSERT INTO sessions (scenario_id, token, state, vars) VALUES (?, ?, ?, ?)",
        (scenario_id, token, scenario.initial_state, db.dumps(scenario.initial_vars)),
    )
    return {
        "id": cur.lastrowid,
        "state": scenario.initial_state,
        "vars": dict(scenario.initial_vars),
        "fires": {},
        "request_count": 0,
        "created": True,
    }


def _next_event_seq(conn: Any, session_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM events WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return int(row["seq"])


def run_simulation(
    conn: Any,
    scenario_id: int,
    scenario: ScenarioDef,
    token: str,
    req: dict[str, Any],
) -> dict[str, Any]:
    """Execute one simulated request inside an already-open write transaction.

    Session lookup/create, rule selection, response rendering and state
    advancement all happen against the same transaction snapshot.
    """
    session = _load_or_create_session(conn, scenario_id, scenario, token)
    from_state = session["state"]

    # Match-time context lets matchers compare against session variables.
    fires_preview = dict(session["fires"])
    match_context = {
        "state": from_state,
        "vars": session["vars"],
        "session": {
            "token": token,
            "fires": fires_preview,
            "request_count": session["request_count"],
        },
        "request": req,
    }

    route, rule = find_rule(scenario, req, from_state, match_context)
    seq = _next_event_seq(conn, session["id"])

    if rule is None:
        conn.execute(
            "UPDATE sessions SET request_count = request_count + 1, updated_at = datetime('now') "
            "WHERE id = ?",
            (session["id"],),
        )
        reason = "route_not_found" if route is None else "no_rule_matched"
        conn.execute(
            "INSERT INTO events (session_id, seq, method, path, rule, from_state, to_state, "
            "status, detail) VALUES (?, ?, ?, ?, NULL, ?, ?, NULL, ?)",
            (session["id"], seq, req["method"], req["path"], from_state, from_state,
             db.dumps({"reason": reason})),
        )
        raise MatchFailure(
            f"no rule matched {req['method']} {req['path']} in state {from_state!r}",
            detail={"reason": reason, "state": from_state, "seq": seq},
        )

    # Register the fire *before* rendering so templates observe the new count.
    fires = fires_preview
    fires[rule.name] = fires.get(rule.name, 0) + 1

    context = {
        **match_context,
        "session": {
            "token": token,
            "fires": fires,
            "request_count": session["request_count"],
        },
        "rule": {"name": rule.name, "priority": rule.priority},
    }

    rendered_body = _render_node(rule.response.body, context) if rule.response.body is not None else None
    rendered_headers = {
        key: render_text(value, context) for key, value in rule.response.headers.items()
    }

    new_state = from_state
    new_vars = dict(session["vars"])
    if rule.transition is not None:
        updates = _render_vars(rule.transition.set, context)
        if "state" in updates:
            new_state = updates.pop("state")
        new_vars.update(updates)
        for key in rule.transition.remove:
            new_vars.pop(key, None)

    conn.execute(
        "UPDATE sessions SET state = ?, vars = ?, fires = ?, request_count = request_count + 1, "
        "updated_at = datetime('now') WHERE id = ?",
        (new_state, db.dumps(new_vars), db.dumps(fires), session["id"]),
    )
    conn.execute(
        "INSERT INTO events (session_id, seq, method, path, rule, from_state, to_state, "
        "status, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session["id"], seq, req["method"], req["path"], rule.name, from_state, new_state,
         rule.response.status, db.dumps({"vars_after": new_vars, "fires_after": fires})),
    )

    return {
        "status": rule.response.status,
        "headers": rendered_headers,
        "body": rendered_body,
        "rule": rule.name,
        "from_state": from_state,
        "to_state": new_state,
        "session_created": session["created"],
        "seq": seq,
    }
