"""SQLite apply transaction helpers."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from cdx_care.errors import CdxCareError
from cdx_care.policy import ApplyContext, verify_lane_eligibility
from cdx_care.policy_checks import action_schema_tables, normalized_path, object_map
from cdx_care.sqlite_tools import (
    connect_readonly,
    connect_write,
    quote_ident,
    schema_fingerprint,
    table_names,
    trigger_names_for_tables,
    value_hash,
)
from cdx_care.types import JsonObject, JsonValue, require_json_object_list


def group_db_actions(actions: list[JsonObject]) -> dict[Path, list[JsonObject]]:
    """Group DB actions by path."""
    grouped: dict[Path, list[JsonObject]] = defaultdict(list)
    for action in actions:
        if action.get("type") in ("sqlite_update", "sqlite_insert"):
            grouped[normalized_path(Path(str(action["db_path"])))].append(action)
    return dict(grouped)


def preflight_db_actions(db_path: Path, actions: list[JsonObject], context: ApplyContext) -> None:
    """Verify DB identity, schema, and semantic values before any backup target can be touched."""
    verify_db_stat(db_path, actions)
    with closing(connect_readonly(db_path)) as conn:
        verify_schema(conn, actions)
        for action in actions:
            preflight_db_action(conn, action, context)


def apply_db_actions(db_path: Path, actions: list[JsonObject], context: ApplyContext) -> list[JsonObject]:
    """Apply SQLite actions in one transaction per DB."""
    verify_db_stat(db_path, actions)
    with closing(connect_write(db_path)) as conn:
        verify_db_stat(db_path, actions)
        conn.execute("BEGIN IMMEDIATE")
        try:
            verify_schema(conn, actions)
            applied = [apply_db_action(conn, action, context) for action in actions]
        except (sqlite3.Error, CdxCareError):
            conn.rollback()
            raise
        conn.commit()
    return applied


def verify_db_stat(db_path: Path, actions: list[JsonObject]) -> None:
    """Deny when DB identity changed since plan."""
    stat = db_path.stat()
    for action in actions:
        planned = action.get("db_stat")
        if not isinstance(planned, dict):
            raise CdxCareError("DB action missing db_stat", code="invalid_plan")
        if planned.get("device") != stat.st_dev or planned.get("inode") != stat.st_ino:
            raise CdxCareError(f"DB identity changed before apply: {db_path}", code="db_identity_changed")
        if planned.get("bytes") != stat.st_size or planned.get("mtime_ns") != stat.st_mtime_ns:
            raise CdxCareError(f"DB file changed before apply: {db_path}", code="db_changed")


def verify_schema(conn: sqlite3.Connection, actions: list[JsonObject]) -> None:
    """Deny when schema fingerprints changed."""
    tables_by_fingerprint: dict[str, set[str]] = defaultdict(set)
    existing_tables = set(table_names(conn))
    for action in actions:
        expected = str(action["schema_fingerprint"])
        schema_tables = action_schema_tables(action)
        if (
            action.get("lane") in {"memory.stage1_retry_terminal_errors", "memory.force_global_consolidation"}
            and "stage1_outputs" in existing_tables
            and "stage1_outputs" not in schema_tables
        ):
            raise CdxCareError("memory schema_tables omit an existing stage1_outputs table", code="schema_changed")
        for table in schema_tables:
            if table not in existing_tables:
                raise CdxCareError("DB schema table disappeared before apply", code="schema_changed")
            tables_by_fingerprint[expected].add(table)
        triggers = trigger_names_for_tables(conn, schema_tables)
        if triggers:
            raise CdxCareError("SQLite triggers on admitted write tables are not supported", code="schema_side_effects")
    for expected, tables in tables_by_fingerprint.items():
        actual = schema_fingerprint(conn, tables)
        if actual != expected:
            raise CdxCareError("DB schema fingerprint changed before apply", code="schema_changed")


def apply_db_action(conn: sqlite3.Connection, action: JsonObject, context: ApplyContext) -> JsonObject:
    """Apply a single SQLite action."""
    action_type = action.get("type")
    if action_type == "sqlite_update":
        return apply_sqlite_update(conn, action, context)
    if action_type == "sqlite_insert":
        return apply_sqlite_insert(conn, action, context)
    raise CdxCareError(f"unsupported DB action type: {action_type}", code="unsupported_action")


def apply_sqlite_update(conn: sqlite3.Connection, action: JsonObject, context: ApplyContext) -> JsonObject:
    """Apply one row update with drift preconditions."""
    table = str(action["table"])
    key = object_map(action.get("key"), "key")
    preconditions = precondition_list(action.get("preconditions"))
    row = fetch_one_by_key(conn, table, key)
    verify_lane_eligibility(row, action, context)
    verify_preconditions(row, preconditions)
    updates = object_map(action.get("updates"), "updates")
    verify_memory_global_update_watermark(conn, action, row, updates, context)
    set_sql = ", ".join(f"{quote_ident(col)} = ?" for col in updates)
    where_sql = " AND ".join(f"{quote_ident(col)} = ?" for col in key)
    params = [updates[col] for col in updates]
    params.extend(key[col] for col in key)
    cursor = conn.execute(f"UPDATE {quote_ident(table)} SET {set_sql} WHERE {where_sql}", params)
    if cursor.rowcount != 1:
        raise CdxCareError(f"expected to update one row, updated {cursor.rowcount}", code="affected_row_mismatch")
    readback = fetch_one_by_key(conn, table, key)
    for col, expected in updates.items():
        if readback[col] != expected:
            raise CdxCareError(f"readback mismatch for {table}.{col}", code="readback_mismatch")
    return {
        "id": str(action["id"]),
        "type": "sqlite_update",
        "lane": str(action["lane"]),
        "table": table,
        "key": key,
        "updated_columns": sorted(updates),
    }


def apply_sqlite_insert(conn: sqlite3.Connection, action: JsonObject, context: ApplyContext) -> JsonObject:
    """Insert one row with readback."""
    table = str(action["table"])
    values = object_map(action.get("values"), "values")
    verify_absence_precondition(conn, action)
    verify_memory_global_insert_watermark(conn, action, values, context)
    columns = list(values)
    placeholders = ", ".join("?" for _ in columns)
    col_sql = ", ".join(quote_ident(col) for col in columns)
    params = [values[col] for col in columns]
    cursor = conn.execute(f"INSERT INTO {quote_ident(table)} ({col_sql}) VALUES ({placeholders})", params)
    if cursor.rowcount != 1:
        raise CdxCareError(f"expected to insert one row, inserted {cursor.rowcount}", code="affected_row_mismatch")
    readback_key = insert_readback_key(action, values)
    readback = fetch_one_by_key(conn, table, readback_key)
    for col, expected in values.items():
        if readback[col] != expected:
            raise CdxCareError(f"readback mismatch for inserted {table}.{col}", code="readback_mismatch")
    return {
        "id": str(action["id"]),
        "type": "sqlite_insert",
        "lane": str(action["lane"]),
        "table": table,
        "inserted_columns": sorted(values),
    }


def insert_readback_key(action: JsonObject, values: JsonObject) -> JsonObject:
    """Return the deterministic readback key for admitted insert actions."""
    key = object_map(action.get("key"), "key")
    if (
        action.get("lane") == "memory.force_global_consolidation"
        and action.get("table") == "jobs"
        and values.get("kind") == "memory_consolidate_global"
        and values.get("job_key") == "global"
        and key == {"kind": "memory_consolidate_global", "job_key": "global"}
    ):
        return key
    raise CdxCareError("insert action has no admitted readback key", code="action_target_denied")


def preflight_db_action(conn: sqlite3.Connection, action: JsonObject, context: ApplyContext) -> None:
    """Read-only semantic checks repeated later inside the write transaction."""
    action_type = action.get("type")
    if action_type == "sqlite_update":
        table = str(action["table"])
        key = object_map(action.get("key"), "key")
        row = fetch_one_by_key(conn, table, key)
        verify_lane_eligibility(row, action, context)
        verify_preconditions(row, precondition_list(action.get("preconditions")))
        updates = object_map(action.get("updates"), "updates")
        verify_memory_global_update_watermark(conn, action, row, updates, context)
        return
    if action_type == "sqlite_insert":
        values = object_map(action.get("values"), "values")
        verify_absence_precondition(conn, action)
        verify_memory_global_insert_watermark(conn, action, values, context)
        return
    raise CdxCareError(f"unsupported DB action type: {action_type}", code="unsupported_action")


def verify_absence_precondition(conn: sqlite3.Connection, action: JsonObject) -> None:
    """Verify an admitted insert row is still absent before backup and inside the transaction."""
    preconditions = precondition_list(action.get("preconditions"))
    if preconditions != [{"row_absent": True}]:
        raise CdxCareError("insert action must have a row_absent precondition", code="action_target_denied")
    table = str(action["table"])
    key = object_map(action.get("key"), "key")
    where_sql = " AND ".join(f"{quote_ident(col)} = ?" for col in key)
    row = conn.execute(f"SELECT 1 FROM {quote_ident(table)} WHERE {where_sql}", [key[col] for col in key]).fetchone()
    if row is not None:
        raise CdxCareError(f"planned inserted row already exists in {table}", code="row_drift")


def verify_memory_global_update_watermark(
    conn: sqlite3.Connection, action: JsonObject, row: sqlite3.Row, updates: JsonObject, context: ApplyContext
) -> None:
    """Deny tampered global-consolidation updates that do not cover current Stage 1 output state."""
    if action.get("lane") != "memory.force_global_consolidation":
        return
    watermark = updates.get("input_watermark")
    if not isinstance(watermark, int):
        raise CdxCareError("memory global input_watermark must be an integer", code="action_target_denied")
    current_input = int(row["input_watermark"] or 0)
    last_success = int(row["last_success_watermark"] or 0)
    current_stage1 = max_stage1_source_updated_at(conn)
    required = max(current_input + 1, last_success, current_stage1)
    if watermark < required:
        raise CdxCareError("memory global input_watermark is stale for current Stage 1 outputs", code="row_drift")
    allowed = max(current_input + 1, last_success, current_stage1, context.now_seconds)
    if watermark > allowed:
        raise CdxCareError("memory global input_watermark is in the future", code="action_target_denied")


def verify_memory_global_insert_watermark(
    conn: sqlite3.Connection, action: JsonObject, values: JsonObject, context: ApplyContext
) -> None:
    """Deny tampered global-consolidation inserts that start behind current Stage 1 output state."""
    if action.get("lane") != "memory.force_global_consolidation":
        return
    watermark = values.get("input_watermark")
    if not isinstance(watermark, int):
        raise CdxCareError("memory global input_watermark must be an integer", code="action_target_denied")
    current_stage1 = max_stage1_source_updated_at(conn)
    required = max(1, current_stage1)
    if watermark < required:
        raise CdxCareError("memory global input_watermark is stale for current Stage 1 outputs", code="row_drift")
    allowed = max(1, current_stage1, context.now_seconds)
    if watermark > allowed:
        raise CdxCareError("memory global input_watermark is in the future", code="action_target_denied")


def max_stage1_source_updated_at(conn: sqlite3.Connection) -> int:
    """Return the current highest Stage 1 output source timestamp when the table exists."""
    if "stage1_outputs" not in table_names(conn):
        return 0
    row = conn.execute("SELECT MAX(source_updated_at) FROM stage1_outputs").fetchone()
    return int(row[0] or 0) if row else 0


def fetch_one_by_key(conn: sqlite3.Connection, table: str, key: JsonObject) -> sqlite3.Row:
    """Fetch one row by exact key."""
    where_sql = " AND ".join(f"{quote_ident(col)} = ?" for col in key)
    row = conn.execute(f"SELECT * FROM {quote_ident(table)} WHERE {where_sql}", [key[col] for col in key]).fetchone()
    if row is None:
        raise CdxCareError(f"planned row not found in {table}", code="row_missing")
    return row


def verify_preconditions(row: sqlite3.Row, preconditions: list[JsonObject]) -> None:
    """Check row preconditions inside the write transaction."""
    for condition in preconditions:
        column = str(condition["column"])
        value = row[column]
        if condition.get("is_null") is True:
            if value is not None:
                raise CdxCareError(f"precondition failed: {column} is not NULL", code="row_drift")
        elif "equals" in condition:
            if value != condition["equals"]:
                raise CdxCareError(f"precondition failed: {column} changed", code="row_drift")
        elif "sha256" in condition:
            if value_hash(value) != condition["sha256"]:
                raise CdxCareError(f"precondition failed: {column} hash changed", code="row_drift")
        else:
            raise CdxCareError("unsupported precondition", code="invalid_plan")


def precondition_list(value: JsonValue | None) -> list[JsonObject]:
    """Validate preconditions."""
    try:
        return require_json_object_list(value, "preconditions")
    except TypeError as error:
        raise CdxCareError("preconditions must be a list", code="invalid_plan") from error
