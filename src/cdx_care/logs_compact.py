"""Guarded SQLite log compaction helpers."""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from cdx_care.errors import CdxCareError
from cdx_care.sqlite_tools import connect_readonly, connect_write, quick_check, schema_fingerprint, table_names
from cdx_care.types import JsonObject, JsonValue

LOG_COMPACT_MIN_RECLAIMABLE_BYTES = 256 * 1024
LOG_COMPACT_FREE_SPACE_MARGIN_BYTES = 1_073_741_824


def logs_physical_report(path: Path) -> JsonObject:
    """Return physical stats for the logs DB without exposing log bodies."""
    if not path.exists():
        return {"exists": False}
    with closing(connect_readonly(path)) as conn:
        return logs_physical_report_from_conn(conn)


def logs_physical_report_from_conn(conn: sqlite3.Connection) -> JsonObject:
    """Return physical stats using an already-open SQLite connection."""
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    freelist_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    auto_vacuum = int(conn.execute("PRAGMA auto_vacuum").fetchone()[0])
    journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
    tables = table_names(conn)
    row_count = 0
    by_level: dict[str, JsonValue] = {}
    if "logs" in tables:
        row_count = int(conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0])
        for row in conn.execute("SELECT level, COUNT(*) FROM logs GROUP BY level ORDER BY level").fetchall():
            by_level[str(row[0])] = int(row[1])
    reclaimable = page_size * freelist_count
    return {
        "exists": True,
        "page_size": page_size,
        "page_count": page_count,
        "freelist_count": freelist_count,
        "auto_vacuum": auto_vacuum,
        "journal_mode": journal_mode,
        "reclaimable_bytes": reclaimable,
        "row_count": row_count,
        "by_level": by_level,
        "compaction_apply_supported": True,
        "compaction_planned": reclaimable >= LOG_COMPACT_MIN_RECLAIMABLE_BYTES,
        "compaction_method": "vacuum",
    }


def logs_schema_fingerprint(path: Path) -> tuple[str, list[str]]:
    """Return schema fingerprint and table list for logs compaction plans."""
    with closing(connect_readonly(path)) as conn:
        tables = table_names(conn)
        return schema_fingerprint(conn, tables), tables


def preflight_logs_compaction(db_path: Path, action: JsonObject) -> JsonObject:
    """Verify a logs compaction action against current physical stats."""
    if quick_check(db_path) != "ok":
        raise CdxCareError("logs DB quick_check is not ok before compaction", code="quick_check_failed")
    with closing(connect_readonly(db_path)) as conn:
        verify_logs_compaction_schema(conn, action)
    current = logs_physical_report(db_path)
    if current.get("exists") is not True:
        raise CdxCareError("logs DB disappeared before compaction", code="db_changed")
    planned_reclaimable = action.get("reclaimable_bytes")
    current_reclaimable = current.get("reclaimable_bytes")
    if not isinstance(planned_reclaimable, int) or not isinstance(current_reclaimable, int):
        raise CdxCareError("logs compaction action has invalid reclaimable stats", code="invalid_plan")
    if current_reclaimable < LOG_COMPACT_MIN_RECLAIMABLE_BYTES:
        raise CdxCareError("logs DB no longer has reclaimable pages", code="row_not_eligible")
    if current_reclaimable != planned_reclaimable:
        raise CdxCareError("logs DB reclaimable pages changed before compaction", code="db_changed")
    db_bytes = db_path.stat().st_size
    free_bytes = shutil.disk_usage(db_path.parent).free
    required_free = db_bytes * 2 + LOG_COMPACT_FREE_SPACE_MARGIN_BYTES
    if free_bytes < required_free:
        raise CdxCareError(
            "not enough free disk for logs DB backup plus VACUUM temp space",
            code="insufficient_disk_space",
            details={"free_bytes": free_bytes, "required_free_bytes": required_free},
        )
    return current


def compact_logs_db(db_path: Path, action: JsonObject) -> JsonObject:
    """Run VACUUM on the logs DB and return before/after physical stats."""
    before = preflight_logs_compaction(db_path, action)
    before_bytes = db_path.stat().st_size
    try:
        with closing(connect_write(db_path)) as conn:
            before = preflight_logs_compaction_conn(conn, db_path, action)
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as error:
        raise CdxCareError(f"logs DB VACUUM failed: {error}", code="logs_compaction_failed") from error
    if quick_check(db_path) != "ok":
        raise CdxCareError("logs DB quick_check is not ok after compaction", code="quick_check_failed")
    after = logs_physical_report(db_path)
    after_bytes = db_path.stat().st_size
    return {
        "before": before,
        "after": after,
        "before_bytes": before_bytes,
        "after_bytes": after_bytes,
        "bytes_reclaimed": max(0, before_bytes - after_bytes),
    }


def preflight_logs_compaction_conn(conn: sqlite3.Connection, db_path: Path, action: JsonObject) -> JsonObject:
    """Verify the planned logs DB identity/schema/physical stats at the write edge."""
    planned = action.get("db_stat")
    if not isinstance(planned, dict):
        raise CdxCareError("logs compaction action missing db_stat", code="invalid_plan")
    stat = db_path.stat()
    if planned.get("device") != stat.st_dev or planned.get("inode") != stat.st_ino:
        raise CdxCareError("logs DB identity changed before compaction", code="db_identity_changed")
    if planned.get("bytes") != stat.st_size or planned.get("mtime_ns") != stat.st_mtime_ns:
        raise CdxCareError("logs DB changed before compaction", code="db_changed")
    verify_logs_compaction_schema(conn, action)
    quick_row = conn.execute("PRAGMA quick_check").fetchone()
    if quick_row is None or str(quick_row[0]) != "ok":
        raise CdxCareError("logs DB quick_check is not ok before compaction", code="quick_check_failed")
    current = logs_physical_report_from_conn(conn)
    planned_reclaimable = action.get("reclaimable_bytes")
    current_reclaimable = current.get("reclaimable_bytes")
    if not isinstance(planned_reclaimable, int) or not isinstance(current_reclaimable, int):
        raise CdxCareError("logs compaction action has invalid reclaimable stats", code="invalid_plan")
    if current_reclaimable < LOG_COMPACT_MIN_RECLAIMABLE_BYTES:
        raise CdxCareError("logs DB no longer has reclaimable pages", code="row_not_eligible")
    if current_reclaimable != planned_reclaimable:
        raise CdxCareError("logs DB reclaimable pages changed before compaction", code="db_changed")
    return current


def verify_logs_compaction_schema(conn: sqlite3.Connection, action: JsonObject) -> None:
    """Deny compact plans that do not prove the complete current logs DB schema."""
    schema_tables = action.get("schema_tables")
    if not isinstance(schema_tables, list) or not all(isinstance(row, str) for row in schema_tables):
        raise CdxCareError("logs compaction schema_tables must be strings", code="invalid_plan")
    planned_tables = sorted(set(schema_tables))
    current_tables = table_names(conn)
    if planned_tables != current_tables:
        raise CdxCareError(
            "logs compaction schema_tables must match all current logs DB tables",
            code="schema_changed",
        )
    if schema_fingerprint(conn, current_tables) != action.get("schema_fingerprint"):
        raise CdxCareError("logs DB schema changed before compaction", code="schema_changed")
