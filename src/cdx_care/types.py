"""Shared JSON envelope helpers."""

from __future__ import annotations

JsonValue = object
JsonObject = dict[str, JsonValue]


def require_json_object(value: object, label: str) -> JsonObject:
    """Validate a value as a JSON object with string keys."""
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    result: JsonObject = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError(f"{label} keys must be strings")
        result[key] = item
    return result


def require_json_object_list(value: object, label: str) -> list[JsonObject]:
    """Validate a value as a list of JSON objects."""
    if not isinstance(value, list):
        raise TypeError(f"{label} must be a list")
    rows: list[JsonObject] = []
    for item in value:
        rows.append(require_json_object(item, label))
    return rows
