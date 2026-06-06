"""Domain errors."""

from __future__ import annotations

from cdx_care.types import JsonObject


class CdxCareError(RuntimeError):
    """Base error rendered by the CLI."""

    code: str
    details: JsonObject

    def __init__(self, message: str, *, code: str = "runtime_error", details: JsonObject | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}
