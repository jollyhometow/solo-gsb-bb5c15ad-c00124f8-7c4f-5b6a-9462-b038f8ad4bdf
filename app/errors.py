"""Typed errors shared by scenario parsing and the simulation engine."""
from __future__ import annotations

from typing import Any, Optional


class ScenarioError(ValueError):
    """A scenario document is structurally or semantically invalid."""

    def __init__(self, message: str, path: Optional[str] = None) -> None:
        self.path = path
        super().__init__(f"{path}: {message}" if path else message)


class MatchFailure(LookupError):
    """No route/rule matched an incoming request."""

    def __init__(self, message: str, detail: Optional[dict[str, Any]] = None) -> None:
        self.detail = detail or {}
        super().__init__(message)
