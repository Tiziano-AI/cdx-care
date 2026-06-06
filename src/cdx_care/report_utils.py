"""Shared report row builders."""

from __future__ import annotations

from cdx_care.types import JsonObject

ROW_LIMIT = 500


def finding(code: str, severity: str, message: str, evidence: JsonObject) -> JsonObject:
    """Build a finding."""
    return {"code": code, "severity": severity, "message": message, "evidence": evidence}


def collection_metadata(total_count: int, returned_count: int) -> JsonObject:
    """Return machine-visible completeness metadata for bounded row arrays."""
    truncated = total_count > returned_count
    return {
        "limit": ROW_LIMIT,
        "returned_count": returned_count,
        "total_count": total_count,
        "truncated": truncated,
        "next_command": "Use raw sql with a narrower query for the remaining rows." if truncated else None,
    }
