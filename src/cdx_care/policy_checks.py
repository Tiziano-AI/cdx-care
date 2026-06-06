"""Small validation helpers for the closed action policy."""

from __future__ import annotations

import os
from pathlib import Path

from cdx_care.errors import CdxCareError
from cdx_care.types import JsonObject, JsonValue, require_json_object

DEFAULT_STAGE1_RETRY_REMAINING = 3


def object_map(value: JsonValue | None, label: str) -> JsonObject:
    """Validate a JSON object map."""
    try:
        return require_json_object(value, label)
    except TypeError as error:
        raise CdxCareError(f"action {label} must be an object", code="invalid_plan") from error


def string_list(value: list[object], label: str) -> list[str]:
    """Validate a list of strings."""
    rows: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise CdxCareError(f"{label} must contain only strings", code="invalid_plan")
        rows.append(item)
    return rows


def normalized_path(path: Path) -> Path:
    """Return an absolute normalized path without following symlinks."""
    return Path(os.path.abspath(path))


def require_schema_fingerprint(action: JsonObject) -> None:
    """Require a stable SHA-256 schema fingerprint field."""
    value = action.get("schema_fingerprint")
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise CdxCareError(
            "SQLite action schema_fingerprint must be a lowercase SHA-256 hex string",
            code="invalid_plan",
        )


def action_schema_tables(action: JsonObject) -> list[str]:
    """Return validated schema table names from an admitted SQLite action."""
    value = action.get("schema_tables")
    if not isinstance(value, list):
        raise CdxCareError("SQLite action schema_tables must be a list", code="invalid_plan")
    tables = string_list(value, "schema_tables")
    if not tables or len(set(tables)) != len(tables):
        raise CdxCareError("SQLite action schema_tables must be non-empty and unique", code="action_target_denied")
    return tables


def require_schema_tables(action: JsonObject, *, required: set[str], allowed: set[str], label: str) -> None:
    """Require an exact closed schema-table ownership set for a SQLite action."""
    tables = set(action_schema_tables(action))
    if not required.issubset(tables) or not tables.issubset(allowed):
        raise CdxCareError(f"{label} are outside cdx-care policy", code="action_target_denied")


def require_keys(value: JsonObject, expected: set[str], label: str) -> None:
    """Require an exact JSON object key set."""
    if set(value) != expected:
        raise CdxCareError(f"{label} must contain exactly {sorted(expected)}", code="action_target_denied")


def require_mark_read_update(value: JsonObject, label: str) -> None:
    """Require a closed positive timestamp mark-read update."""
    require_exact_value_keys(value, {"read_at"}, label)
    read_at = value.get("read_at")
    if not isinstance(read_at, int) or read_at <= 0:
        raise CdxCareError(f"{label} read_at must be a positive integer timestamp", code="action_target_denied")


def require_preconditions(action: JsonObject, expected: dict[str, str], label: str) -> None:
    """Require exact old-value precondition columns and operators."""
    value = action.get("preconditions")
    if not isinstance(value, list) or len(value) != len(expected):
        raise CdxCareError(f"{label} must contain exactly {sorted(expected)}", code="action_target_denied")
    seen: set[str] = set()
    for item in value:
        try:
            condition = require_json_object(item, "precondition")
        except TypeError as error:
            raise CdxCareError(f"{label} entries must be objects", code="invalid_plan") from error
        column = condition.get("column")
        if not isinstance(column, str) or column not in expected or column in seen:
            raise CdxCareError(f"{label} has an unexpected column", code="action_target_denied")
        if precondition_operator(condition) != expected[column]:
            raise CdxCareError(f"{label} has an unexpected operator for {column}", code="action_target_denied")
        seen.add(column)


def require_absence_precondition(action: JsonObject, label: str) -> None:
    """Require the exact row-absence precondition used by admitted inserts."""
    value = action.get("preconditions")
    if not isinstance(value, list) or len(value) != 1:
        raise CdxCareError(f"{label} must contain a row_absent precondition", code="action_target_denied")
    try:
        condition = require_json_object(value[0], "precondition")
    except TypeError as error:
        raise CdxCareError(f"{label} entries must be objects", code="invalid_plan") from error
    if set(condition) != {"row_absent"} or condition.get("row_absent") is not True:
        raise CdxCareError(f"{label} must contain exactly row_absent=true", code="action_target_denied")


def precondition_operator(condition: JsonObject) -> str:
    """Return the single admitted precondition operator."""
    operators = [key for key in ("equals", "is_null", "sha256") if key in condition]
    if len(operators) != 1:
        raise CdxCareError("precondition must contain exactly one operator", code="action_target_denied")
    if operators[0] == "is_null" and condition.get("is_null") is not True:
        raise CdxCareError("is_null precondition must be true", code="action_target_denied")
    return operators[0]


def require_exact_value_keys(value: JsonObject, expected: set[str], label: str) -> None:
    """Require exact value columns for an inserted row."""
    if set(value) != expected:
        raise CdxCareError(f"{label} columns must be exactly {sorted(expected)}", code="action_target_denied")


def require_exact_updates(value: JsonObject, expected: JsonObject, label: str) -> None:
    """Require exact update keys and values."""
    require_exact_value_keys(value, set(expected), label)
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise CdxCareError(f"{label} has unexpected value for {key}", code="action_target_denied")


def require_pending_job_values(value: JsonObject, label: str) -> None:
    """Require admitted job reset/enqueue values."""
    expected_nulls = {
        "worker_id",
        "ownership_token",
        "started_at",
        "finished_at",
        "lease_until",
        "retry_at",
        "last_error",
    }
    if value.get("status") != "pending":
        raise CdxCareError(f"{label} status must become pending", code="action_target_denied")
    if value.get("retry_remaining") != DEFAULT_STAGE1_RETRY_REMAINING:
        raise CdxCareError(f"{label} retry_remaining must reset to default", code="action_target_denied")
    for key in expected_nulls:
        if value.get(key) is not None:
            raise CdxCareError(f"{label} must clear {key}", code="action_target_denied")


def require_job_key(key: JsonObject, kind: str, *, job_key: str | None = None) -> None:
    """Validate a jobs-table composite key."""
    require_keys(key, {"kind", "job_key"}, "jobs key")
    if key.get("kind") != kind:
        raise CdxCareError("jobs key kind is outside cdx-care policy", code="action_target_denied")
    if job_key is not None and key.get("job_key") != job_key:
        raise CdxCareError("jobs key job_key is outside cdx-care policy", code="action_target_denied")
