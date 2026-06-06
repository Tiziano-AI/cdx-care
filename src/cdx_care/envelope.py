"""Public JSON envelope normalization."""

from __future__ import annotations

import uuid

from cdx_care.types import JsonObject


def success_envelope(payload: JsonObject) -> JsonObject:
    """Return a success payload with the stable top-level CLI keys present."""
    result = dict(payload)
    if "run_id" not in result:
        result["run_id"] = str(uuid.uuid4())
    if "codex_closed" not in result:
        result["codex_closed"] = None
    if "findings" not in result:
        result["findings"] = []
    if "planned_actions" not in result:
        result["planned_actions"] = []
    if "applied_actions" not in result:
        result["applied_actions"] = []
    if "denials" not in result:
        result["denials"] = []
    if "receipt_path" not in result:
        result["receipt_path"] = None
    return result
