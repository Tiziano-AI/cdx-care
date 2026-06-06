"""Builders for planned cdx-care actions."""

from __future__ import annotations

from pathlib import Path

from cdx_care.types import JsonObject


def sqlite_update_action(
    *,
    action_id: str,
    lane: str,
    db_name: str,
    db_path: Path,
    schema_fingerprint_value: str,
    table: str,
    key: JsonObject,
    preconditions: list[JsonObject],
    updates: JsonObject,
    description: str,
    extra: JsonObject | None = None,
) -> JsonObject:
    """Build a SQLite update action."""
    return {
        "id": action_id,
        "type": "sqlite_update",
        "lane": lane,
        "description": description,
        "db": db_name,
        "db_path": str(db_path),
        "db_stat": stat_snapshot(db_path),
        "schema_fingerprint": schema_fingerprint_value,
        "schema_tables": [table],
        "table": table,
        "key": key,
        "preconditions": preconditions,
        "updates": updates,
        "extra": extra or {},
    }


def sqlite_insert_action(
    *,
    action_id: str,
    lane: str,
    db_name: str,
    db_path: Path,
    schema_fingerprint_value: str,
    table: str,
    values: JsonObject,
    description: str,
) -> JsonObject:
    """Build a SQLite insert action."""
    return {
        "id": action_id,
        "type": "sqlite_insert",
        "lane": lane,
        "description": description,
        "db": db_name,
        "db_path": str(db_path),
        "db_stat": stat_snapshot(db_path),
        "schema_fingerprint": schema_fingerprint_value,
        "schema_tables": [table],
        "table": table,
        "key": {"kind": "memory_consolidate_global", "job_key": "global"},
        "preconditions": [{"row_absent": True}],
        "values": values,
    }


def stat_snapshot(path: Path) -> JsonObject:
    """Return file identity for drift checks."""
    if not path.exists():
        return {"exists": False}
    stat = path.stat()
    return {
        "exists": True,
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
