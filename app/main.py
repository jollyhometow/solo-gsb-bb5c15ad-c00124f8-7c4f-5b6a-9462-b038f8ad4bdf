"""HTTP layer: administration endpoints + the catch-all simulation entry."""
from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from . import config, db, store
from .engine import TemplateAbort, run_simulation
from .errors import MatchFailure, ScenarioError
from .scenario import parse_document

logger = logging.getLogger("simulator")

_CATCH_ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    payload: dict[str, Any] = {"error": code, "message": message}
    payload.update(extra)
    return JSONResponse(status_code=status, content=payload)


def _autoload() -> None:
    if not config.AUTOLOAD_SCENARIO:
        return
    import os

    path = config.SCENARIO_FILE
    if not os.path.exists(path):
        logger.info("autoload: %s does not exist, skipping", path)
        return
    with db.read_only() as conn:
        if store.get_active_name(conn):
            return
    try:
        with open(path, "r", encoding="utf-8") as handle:
            scenario = parse_document(handle.read())
        with db.write_tx() as conn:
            existing = conn.execute(
                "SELECT id FROM scenarios WHERE name = ?", (scenario.name,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO scenarios (name, version, description, definition, active) "
                    "VALUES (?, 1, ?, ?, 1)",
                    (scenario.name, scenario.description,
                     json.dumps(scenario.model_dump(by_alias=True), ensure_ascii=False)),
                )
                logger.info("autoload: loaded scenario %r from %s", scenario.name, path)
            else:
                conn.execute(
                    "UPDATE scenarios SET active = 1 WHERE id = ?", (existing["id"],)
                )
                logger.info("autoload: activated existing scenario %r", scenario.name)
            conn.execute(
                "UPDATE scenarios SET active = CASE WHEN name = ? THEN 1 ELSE 0 END",
                (scenario.name,),
            )
    except (OSError, ScenarioError) as exc:
        logger.warning("autoload failed: %s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init_db()
    logger.info("database initialised at %s", config.DB_PATH)
    _autoload()
    yield


app = FastAPI(
    title="Stateful API Scenario Simulator",
    version="1.0.0",
    description="Submit YAML scenarios; requests are matched per session and drive state.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------#
# Administration
# ---------------------------------------------------------------------------#

@app.get(f"{config.ADMIN_PREFIX}/health", tags=["admin"])
def health() -> dict[str, Any]:
    with db.read_only() as conn:
        return {"status": "ok", "active_scenario": store.get_active_name(conn)}


@app.post(f"{config.ADMIN_PREFIX}/scenarios/validate", tags=["scenarios"])
async def validate_scenario_endpoint(request: Request) -> JSONResponse:
    """Parse + validate a scenario document without persisting it."""
    text = (await request.body()).decode("utf-8")
    try:
        scenario = parse_document(text, request.headers.get("content-type"))
    except ScenarioError as exc:
        return _error(422, "invalid_scenario", str(exc))
    return JSONResponse(
        status_code=200,
        content={
            "valid": True,
            "summary": {
                "name": scenario.name,
                "routes": len(scenario.routes),
                "rules": sum(len(r.rules) for r in scenario.routes),
                "initial_state": scenario.initial_state,
            },
        },
    )


@app.get(f"{config.ADMIN_PREFIX}/scenarios", tags=["scenarios"])
def list_scenarios_endpoint() -> dict[str, Any]:
    return {"scenarios": store.list_scenarios()}


@app.post(f"{config.ADMIN_PREFIX}/scenarios", tags=["scenarios"])
async def create_scenario_endpoint(request: Request, activate: bool = True) -> JSONResponse:
    """Create or version-bump a scenario. Activates by default."""
    text = (await request.body()).decode("utf-8")
    try:
        scenario = parse_document(text, request.headers.get("content-type"))
    except ScenarioError as exc:
        return _error(422, "invalid_scenario", str(exc))
    definition = json.dumps(scenario.model_dump(by_alias=True), ensure_ascii=False)
    result = store.upsert_scenario(scenario, definition, activate=activate)
    return JSONResponse(status_code=201, content=result)


@app.get(f"{config.ADMIN_PREFIX}/scenarios/{{name}}", tags=["scenarios"])
def get_scenario_endpoint(name: str) -> JSONResponse:
    try:
        return JSONResponse(store.get_scenario(name))
    except store.NotFound as exc:
        return _error(404, "not_found", str(exc))


@app.post(f"{config.ADMIN_PREFIX}/scenarios/{{name}}/activate", tags=["scenarios"])
def activate_scenario_endpoint(name: str) -> JSONResponse:
    try:
        store.activate_scenario(name)
    except store.NotFound as exc:
        return _error(404, "not_found", str(exc))
    return JSONResponse({"active": name})


@app.delete(f"{config.ADMIN_PREFIX}/scenarios/{{name}}", tags=["scenarios"])
def delete_scenario_endpoint(name: str) -> JSONResponse:
    try:
        store.delete_scenario(name)
    except store.NotFound as exc:
        return _error(404, "not_found", str(exc))
    return JSONResponse({"deleted": name})


@app.get(f"{config.ADMIN_PREFIX}/sessions", tags=["sessions"])
def list_sessions_endpoint(scenario: Optional[str] = None) -> JSONResponse:
    try:
        with db.read_only() as conn:
            name = store.resolve_scenario_name(conn, scenario)
        return JSONResponse({"scenario": name, "sessions": store.list_sessions(name)})
    except store.NotFound as exc:
        return _error(404, "not_found", str(exc))


@app.get(f"{config.ADMIN_PREFIX}/sessions/{{token}}", tags=["sessions"])
def get_session_endpoint(
    token: str, scenario: Optional[str] = None, events: bool = True
) -> JSONResponse:
    try:
        with db.read_only() as conn:
            name = store.resolve_scenario_name(conn, scenario)
        return JSONResponse(store.get_session(name, token, include_events=events))
    except store.NotFound as exc:
        return _error(404, "not_found", str(exc))


@app.get(f"{config.ADMIN_PREFIX}/sessions/{{token}}/events", tags=["sessions"])
def get_events_endpoint(
    token: str,
    scenario: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> JSONResponse:
    try:
        with db.read_only() as conn:
            name = store.resolve_scenario_name(conn, scenario)
            scenario_id, _, _ = store.get_compiled(conn, name)
            row = conn.execute(
                "SELECT id FROM sessions WHERE scenario_id = ? AND token = ?",
                (scenario_id, token),
            ).fetchone()
            if row is None:
                raise store.NotFound(f"session token {token!r} does not exist for this scenario")
            events_list = store.list_events(conn, row["id"], limit=limit, offset=offset)
        return JSONResponse({"scenario": name, "token": token, "events": events_list})
    except store.NotFound as exc:
        return _error(404, "not_found", str(exc))


@app.post(f"{config.ADMIN_PREFIX}/sessions/{{token}}/reset", tags=["sessions"])
def reset_session_endpoint(token: str, scenario: Optional[str] = None) -> JSONResponse:
    try:
        with db.read_only() as conn:
            name = store.resolve_scenario_name(conn, scenario)
        store.reset_session(name, token)
        return JSONResponse(store.get_session(name, token))
    except store.NotFound as exc:
        return _error(404, "not_found", str(exc))


# ---------------------------------------------------------------------------#
# Catch-all simulation
# ---------------------------------------------------------------------------#

def _parse_body(request: Request, raw: bytes) -> Any:
    if not raw:
        return None
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type or raw[:1] in (b"{", b"["):
        try:
            return json.loads(raw)
        except ValueError:
            pass
    return raw.decode("utf-8", errors="replace")


def _build_response(result: dict[str, Any], token: str, scenario_name: str) -> Response:
    body = result["body"]
    headers = dict(result["headers"])
    headers.setdefault("x-session-token", token)
    headers.setdefault("x-sim-scenario", scenario_name)
    headers.setdefault("x-sim-rule", result["rule"])
    headers.setdefault("x-sim-state", result["to_state"])

    if body is None:
        return Response(status_code=result["status"], headers=headers)
    if isinstance(body, (dict, list)):
        content_type = headers.pop("content-type", None) or headers.pop("Content-Type", None)
        response = JSONResponse(status_code=result["status"], content=body, headers=headers)
        if content_type:
            response.headers["content-type"] = content_type
        return response
    media_type = headers.pop("content-type", None) or headers.pop("Content-Type", None)
    return Response(
        content=str(body),
        status_code=result["status"],
        headers=headers,
        media_type=media_type or "text/plain; charset=utf-8",
    )


@app.api_route(
    "/{full_path:path}",
    methods=_CATCH_ALL_METHODS,
    include_in_schema=False,
)
async def simulate(full_path: str, request: Request) -> Response:
    path = "/" + full_path
    if path == "/" or full_path == "":
        path = "/"
    if path.startswith(config.ADMIN_PREFIX):
        return _error(404, "not_found", f"unknown administration path: {path}")

    raw = await request.body()
    req = {
        "method": request.method,
        "path": path,
        "query": dict(request.query_params),
        "headers": {k: v for k, v in request.headers.items()},
        "body": _parse_body(request, raw),
    }

    token = request.headers.get(config.SESSION_HEADER) or uuid.uuid4().hex
    requested_scenario = request.headers.get(config.SCENARIO_HEADER)

    try:
        with db.write_tx() as conn:
            name = store.resolve_scenario_name(conn, requested_scenario)
            scenario_id, _, scenario = store.get_compiled(conn, name)
            result = run_simulation(conn, scenario_id, scenario, token, req)
    except store.NotFound as exc:
        return _error(503, "no_scenario", str(exc))
    except MatchFailure as exc:
        return JSONResponse(
            status_code=501,
            content={
                "error": "no_match",
                "message": str(exc),
                **exc.detail,
                "x-session-token": token,
            },
            headers={"x-session-token": token},
        )
    except TemplateAbort as exc:
        return _error(422, "template_abort", str(exc))
    except ScenarioError as exc:
        return _error(422, "scenario_error", str(exc))

    return _build_response(result, token, name)
