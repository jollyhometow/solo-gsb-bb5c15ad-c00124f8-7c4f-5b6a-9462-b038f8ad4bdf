"""Pydantic models for user submitted YAML/JSON scenarios."""
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResponseDef(StrictModel):
    """Templated response emitted by a matched rule."""

    status: int = 200
    headers: dict[str, str] = Field(default_factory=dict)
    # body may be a mapping/list (rendered value-by-value) or a Jinja string.
    body: Any = None


class MatchDef(StrictModel):
    """A single request matching condition.

    - value: the condition is shorthand for {equals: value}.
    - mapping: operator -> expected value; operators after the first pass
      are evaluated left to right and must all hold (AND semantics).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    header: Optional[str] = None
    query: Optional[str] = None
    body: Optional[str] = None  # JSON path inside the request body

    value: Any = None
    equals: Any = None
    not_equals: Any = Field(default=None, alias="not_equals")
    contains: Any = None
    not_contains: Any = None
    starts_with: Optional[str] = Field(default=None, alias="starts_with")
    ends_with: Optional[str] = Field(default=None, alias="ends_with")
    gt: Optional[float] = None
    gte: Optional[float] = None
    lt: Optional[float] = None
    lte: Optional[float] = None
    regex: Optional[str] = None
    exists: Optional[bool] = None
    in_: Optional[list[Any]] = Field(default=None, alias="in")

    @model_validator(mode="after")
    def _exactly_one_location(self) -> "MatchDef":
        locations = [name for name in ("header", "query", "body") if getattr(self, name) is not None]
        if len(locations) != 1:
            raise ValueError("exactly one of 'header', 'query' or 'body' must be set")
        return self

    @property
    def location(self) -> str:
        return self.header and "header" or self.query and "query" or "body"

    @property
    def location_key(self) -> str:
        return self.header or self.query or self.body  # type: ignore[return-value]


class TransitionDef(StrictModel):
    """State mutation performed after a rule fires."""

    set: dict[str, Any] = Field(default_factory=dict)
    remove: list[str] = Field(default_factory=list)


class RuleDef(StrictModel):
    """A priority ordered candidate inside a route."""

    name: str
    priority: int = 100
    when_state: Optional[str] = Field(default=None, alias="when_state")
    when_state_not: Optional[str] = Field(default=None, alias="when_state_not")
    match: list[MatchDef] = Field(default_factory=list)
    response: ResponseDef
    transition: Optional[TransitionDef] = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("rule name must not be blank")
        return v


class RouteDef(StrictModel):
    path: str
    methods: list[str] = Field(default_factory=lambda: ["GET", "POST"])
    rules: list[RuleDef]

    @field_validator("path")
    @classmethod
    def _path_absolute(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("route path must start with '/'")
        return v

    @field_validator("methods")
    @classmethod
    def _methods_known(cls, values: list[str]) -> list[str]:
        normalized = [m.upper() for m in values]
        for method in normalized:
            if method not in HTTP_METHODS:
                raise ValueError(f"unsupported HTTP method: {method}")
        if not normalized:
            raise ValueError("at least one HTTP method is required")
        return normalized

    @field_validator("rules")
    @classmethod
    def _rules_present(cls, values: list[RuleDef]) -> list[RuleDef]:
        if not values:
            raise ValueError("route must contain at least one rule")
        return values


class ScenarioDef(StrictModel):
    name: str
    description: Optional[str] = None
    initial_state: str = Field(default="init", alias="initial_state")
    initial_vars: dict[str, Any] = Field(default_factory=dict, alias="initial_vars")
    routes: list[RouteDef]

    @model_validator(mode="after")
    def _validate_references(self) -> "ScenarioDef":
        if not self.name or not self.name.strip():
            raise ValueError("scenario name must not be blank")
        if not self.routes:
            raise ValueError("scenario must contain at least one route")

        known_states = {self.initial_state}
        rule_names: set[str] = set()
        route_paths: set[str] = set()
        for route in self.routes:
            if route.path in route_paths:
                raise ValueError(f"duplicate route path: {route.path}")
            route_paths.add(route.path)
            for rule in route.rules:
                if rule.name in rule_names:
                    raise ValueError(f"duplicate rule name: {rule.name}")
                rule_names.add(rule.name)
                if rule.when_state:
                    known_states.add(rule.when_state)
                if rule.transition and "state" in rule.transition.set:
                    target = rule.transition.set["state"]
                    if not isinstance(target, str):
                        raise ValueError(
                            f"transition.set.state for rule '{rule.name}' must be a string"
                        )
                    known_states.add(target)
        return self


# Used while loading raw documents.
ScenarioInput = Union[ScenarioDef, dict[str, Any]]
