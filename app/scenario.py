"""Parse and validate user submitted scenario documents.

Validation performed here, beyond pydantic structural checks:

* YAML must be a mapping.
* Every response template (status is a literal int, headers/body strings)
  must compile as a Jinja2 template.
* Transition targets must reference reachable state names.
"""
from __future__ import annotations

from typing import Any, Optional

import yaml
from jinja2 import Environment, TemplateSyntaxError
from pydantic import ValidationError

from .errors import ScenarioError
from .models import ResponseDef, RuleDef, ScenarioDef

# Jinja is only used to *compile* templates during validation; the runtime
# environment (with its sandbox + globals) lives in rendering.py.
_CHECK_ENV = Environment(autoescape=False)


def _check_template(text: str, where: str) -> None:
    try:
        _CHECK_ENV.parse(text)
    except TemplateSyntaxError as exc:
        raise ScenarioError(
            f"invalid Jinja2 template in {where} (line {exc.lineno}): {exc.message}",
        ) from exc


def _check_response(resp: ResponseDef, where: str) -> None:
    for key, value in resp.headers.items():
        if isinstance(value, str):
            _check_template(value, f"{where}.headers.{key}")
    if isinstance(resp.body, str):
        _check_template(resp.body, f"{where}.body")
    elif isinstance(resp.body, (dict, list)):
        _walk_templates(resp.body, f"{where}.body")


def _walk_templates(node: Any, where: str) -> None:
    if isinstance(node, str):
        _check_template(node, where)
    elif isinstance(node, dict):
        for key, value in node.items():
            _walk_templates(value, f"{where}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk_templates(value, f"{where}[{index}]")


def validate_scenario(raw: Any) -> ScenarioDef:
    """Validate a decoded (dict) scenario, raising ScenarioError on problems."""
    if not isinstance(raw, dict):
        raise ScenarioError("scenario document must be a mapping")

    try:
        scenario = ScenarioDef.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(p) for p in first["loc"]) or "<root>"
        raise ScenarioError(f"{location}: {first['msg']}") from exc

    # Jinja templates compile-check.
    for route in scenario.routes:
        for rule in route.rules:
            _check_response(rule.response, f"route '{route.path}' rule '{rule.name}'.response")

    # State reference check: every when_state / transition target must be the
    # initial state or some rule's transition target.
    reachable = {scenario.initial_state}
    for route in scenario.routes:
        for rule in route.rules:
            if rule.transition and isinstance(rule.transition.set.get("state"), str):
                reachable.add(rule.transition.set["state"])
    for route in scenario.routes:
        for rule in route.rules:
            for attr in ("when_state", "when_state_not"):
                expected = getattr(rule, attr)
                if expected is not None and expected not in reachable:
                    raise ScenarioError(
                        f"route '{route.path}' rule '{rule.name}': "
                        f"{attr} '{expected}' is never entered "
                        f"(initial_state={scenario.initial_state!r})",
                    )

    return scenario


def parse_document(text: str, content_type: Optional[str] = None) -> ScenarioDef:
    """Parse a raw request body as JSON or YAML into a validated scenario."""
    stripped = text.lstrip()
    try:
        if content_type and "json" in content_type:
            import json

            raw = json.loads(text)
        else:
            raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ScenarioError(f"invalid YAML: {exc}") from exc
    except ValueError as exc:
        raise ScenarioError(f"invalid JSON: {exc}") from exc

    if not stripped:
        raise ScenarioError("scenario document is empty")
    return validate_scenario(raw)
