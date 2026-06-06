"""Read-only raw SQL diagnostics."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from cdx_care import VERSION
from cdx_care.errors import CdxCareError
from cdx_care.paths import StorePaths
from cdx_care.processes import lsof_handles
from cdx_care.sqlite_tools import connect_readonly, row_to_json
from cdx_care.timeutil import iso_now
from cdx_care.types import JsonObject, JsonValue

SAFE_PRAGMAS = {
    "quick_check",
    "integrity_check",
    "table_info",
    "index_list",
    "index_info",
    "foreign_key_check",
    "freelist_count",
    "page_size",
    "page_count",
    "schema_version",
    "journal_mode",
}
ARGUMENT_READ_PRAGMAS = {
    "quick_check",
    "integrity_check",
    "table_info",
    "index_list",
    "index_info",
    "foreign_key_check",
}


def raw_sql_readonly(stores: StorePaths, db_name: str, query_file: Path, limit: int) -> JsonObject:
    """Execute one read-only SQL diagnostic query."""
    query = query_file.read_text(encoding="utf-8").strip()
    validate_query_shape(query)
    path = stores.db_path(db_name)
    lsof_available, handles = lsof_handles(list(stores.db_paths().values()))
    with closing(connect_readonly(path)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.set_authorizer(readonly_authorizer)
        cursor = conn.execute(query)
        rows: list[JsonValue] = []
        truncated = False
        for index, row in enumerate(cursor):
            if index >= limit:
                truncated = True
                break
            rows.append(row_to_json(row))
    return {
        "schema_version": 1,
        "tool": "cdx-care",
        "version": VERSION,
        "generated_at": iso_now(),
        "ok": True,
        "support_root": str(stores.codex_home),
        "codex_closed": lsof_available and not handles,
        "db": db_name,
        "db_path": str(path),
        "query_file": str(query_file),
        "limit": limit,
        "rows": rows,
        "row_count_returned": len(rows),
        "rows_meta": {
            "limit": limit,
            "returned_count": len(rows),
            "total_count": None,
            "truncated": truncated,
            "next_command": "Rerun raw sql with a narrower query or higher --limit." if truncated else None,
        },
    }


def validate_query_shape(query: str) -> None:
    """Deny multi-statement and non-read query shapes before SQLite authorizer."""
    if not query:
        raise CdxCareError("query file is empty", code="empty_query")
    stripped = query.strip()
    semicolon_body = stripped[:-1] if stripped.endswith(";") else stripped
    if ";" in semicolon_body:
        raise CdxCareError("raw SQL accepts one statement only", code="multi_statement_denied")
    lower = query.lstrip().lower()
    if lower.startswith(("select ", "with ", "explain ")):
        return
    if lower.startswith("pragma "):
        body = lower.removeprefix("pragma ").strip()
        name = body.split("(", 1)[0].split("=", 1)[0].strip()
        has_arguments = "(" in body
        if name in SAFE_PRAGMAS and "=" not in body and (not has_arguments or name in ARGUMENT_READ_PRAGMAS):
            return
    raise CdxCareError(
        "raw SQL is read-only; only SELECT/WITH/EXPLAIN and safe PRAGMA reads are allowed", code="raw_sql_write_denied"
    )


def readonly_authorizer(
    action: int, arg1: str | None, arg2: str | None, db_name: str | None, trigger: str | None
) -> int:
    """SQLite authorizer callback for raw read-only queries."""
    _ = (db_name, trigger)
    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
    }
    if action == sqlite3.SQLITE_FUNCTION and (arg1 or arg2 or "").lower() == "load_extension":
        return sqlite3.SQLITE_DENY
    if action in allowed:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and arg1 is not None and arg1.lower() in SAFE_PRAGMAS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY
