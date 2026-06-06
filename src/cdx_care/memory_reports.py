"""Memory job and artifact report lane for doctor."""

from __future__ import annotations

import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from cdx_care.paths import StorePaths
from cdx_care.report_utils import ROW_LIMIT, collection_metadata, finding
from cdx_care.sqlite_tools import connect_readonly, schema_fingerprint, table_names
from cdx_care.types import JsonObject, JsonValue


def memories_report(stores: StorePaths) -> JsonObject:
    """Inspect memory jobs and artifacts."""
    path = stores.db_path("memories")
    findings: list[JsonValue] = []
    if not path.exists():
        return {"data": {"exists": False}, "findings": findings}
    with closing(connect_readonly(path)) as conn:
        tables = table_names(conn)
        if "jobs" not in tables:
            return {"data": {"exists": True, "jobs_exists": False}, "findings": findings}
        jobs = memory_jobs_report(conn)
        selected_count = 0
        if "stage1_outputs" in tables:
            selected_count = int(
                conn.execute("SELECT COUNT(*) FROM stage1_outputs WHERE selected_for_phase2 = 1").fetchone()[0]
            )
        fingerprint = schema_fingerprint(conn, [table for table in ("jobs", "stage1_outputs") if table in tables])
    if jobs["stage1_terminal_error_count"]:
        findings.append(
            finding(
                "codex.memory.stage1_terminal_errors",
                "warn",
                "Stage 1 memory jobs have exhausted retries.",
                {"count": jobs["stage1_terminal_error_count"], "categories": jobs["stage1_terminal_error_categories"]},
            )
        )
    if jobs["stage1_auth_error_count"]:
        findings.append(
            finding(
                "codex.memory.stage1_auth_errors",
                "error",
                (
                    "Stage 1 memory jobs are failing on authentication; repair Codex/OpenAI credential "
                    "loading before retry."
                ),
                {
                    "count": jobs["stage1_auth_error_count"],
                    "terminal_count": jobs["stage1_auth_terminal_error_count"],
                    "retryable_count": jobs["stage1_auth_retryable_error_count"],
                },
            )
        )
    global_job = jobs.get("global_consolidation")
    if isinstance(global_job, dict) and global_job.get("error_category") == "auth":
        findings.append(
            finding(
                "codex.memory.global_auth_error",
                "error",
                (
                    "Global memory consolidation is failing on authentication; repair Codex/OpenAI "
                    "credential loading before enqueueing memory workers."
                ),
                {"status": str(global_job.get("status")), "retry_remaining": int(global_job.get("retry_remaining", 0))},
            )
        )
    artifacts = artifact_report(stores.memories_root)
    return {
        "data": {
            "exists": True,
            "path": str(path),
            "schema_fingerprint": fingerprint,
            "jobs": jobs,
            "selected_for_phase2_count": selected_count,
            "artifacts": artifacts,
        },
        "findings": findings,
    }


def memory_jobs_report(conn: sqlite3.Connection) -> JsonObject:
    """Summarize memory job rows without exposing raw errors."""
    by_status_kind: dict[str, JsonValue] = {}
    for row in conn.execute(
        "SELECT status, kind, COUNT(*) FROM jobs GROUP BY status, kind ORDER BY status, kind"
    ).fetchall():
        by_status_kind[f"{row[0]}:{row[1]}"] = int(row[2])
    error_rows = conn.execute(
        """
        SELECT job_key, retry_remaining, last_error, started_at, finished_at
        FROM jobs
        WHERE kind='memory_stage1' AND status='error'
        ORDER BY finished_at DESC
        """
    ).fetchall()
    terminal_rows: list[JsonValue] = []
    auth_rows: list[JsonValue] = []
    categories: Counter[str] = Counter()
    all_categories: Counter[str] = Counter()
    retryable_categories: Counter[str] = Counter()
    auth_terminal_count = 0
    auth_retryable_count = 0
    for row in error_rows:
        retry_remaining = int(row["retry_remaining"])
        category = memory_error_category(str(row["last_error"] or ""))
        all_categories[category] += 1
        if retry_remaining > 0:
            retryable_categories[category] += 1
        if category == "auth":
            if retry_remaining <= 0:
                auth_terminal_count += 1
            else:
                auth_retryable_count += 1
            auth_rows.append(
                {
                    "job_key": str(row["job_key"]),
                    "retry_remaining": retry_remaining,
                    "error_category": category,
                    "started_at": int(row["started_at"] or 0),
                    "finished_at": int(row["finished_at"] or 0),
                }
            )
        if retry_remaining <= 0:
            categories[category] += 1
            terminal_rows.append(
                {
                    "job_key": str(row["job_key"]),
                    "retry_remaining": retry_remaining,
                    "error_category": category,
                    "started_at": int(row["started_at"] or 0),
                    "finished_at": int(row["finished_at"] or 0),
                }
            )
    global_data = memory_global_job_report(conn)
    returned_terminal_rows = terminal_rows[:ROW_LIMIT]
    returned_auth_rows = auth_rows[:ROW_LIMIT]
    return {
        "by_status_kind": by_status_kind,
        "stage1_error_count": len(error_rows),
        "stage1_error_categories": dict(sorted(all_categories.items())),
        "stage1_retryable_error_count": sum(retryable_categories.values()),
        "stage1_retryable_error_categories": dict(sorted(retryable_categories.items())),
        "stage1_terminal_error_count": len(terminal_rows),
        "stage1_terminal_error_categories": dict(sorted(categories.items())),
        "stage1_terminal_error_rows": returned_terminal_rows,
        "stage1_terminal_error_rows_meta": collection_metadata(len(terminal_rows), len(returned_terminal_rows)),
        "stage1_auth_error_count": len(auth_rows),
        "stage1_auth_terminal_error_count": auth_terminal_count,
        "stage1_auth_retryable_error_count": auth_retryable_count,
        "stage1_auth_error_rows": returned_auth_rows,
        "stage1_auth_error_rows_meta": collection_metadata(len(auth_rows), len(returned_auth_rows)),
        "global_consolidation": global_data,
    }


def memory_global_job_report(conn: sqlite3.Connection) -> JsonObject | None:
    """Return the native global memory consolidation job row without raw error text."""
    global_row = conn.execute(
        """
        SELECT status, retry_remaining, input_watermark, last_success_watermark,
               started_at, finished_at, lease_until, retry_at, last_error
        FROM jobs
        WHERE kind='memory_consolidate_global' AND job_key='global'
        """
    ).fetchone()
    if not global_row:
        return None
    return {
        "status": str(global_row["status"]),
        "retry_remaining": int(global_row["retry_remaining"]),
        "input_watermark": int(global_row["input_watermark"] or 0),
        "last_success_watermark": int(global_row["last_success_watermark"] or 0),
        "started_at": int(global_row["started_at"] or 0),
        "finished_at": int(global_row["finished_at"] or 0),
        "lease_until": int(global_row["lease_until"] or 0),
        "retry_at": int(global_row["retry_at"] or 0),
        "last_error_present": global_row["last_error"] is not None,
        "error_category": memory_error_category(str(global_row["last_error"] or ""))
        if global_row["last_error"] is not None
        else None,
    }


def memory_error_category(error: str) -> str:
    """Categorize a memory error without returning the raw body."""
    lower = error.lower()
    if (
        "401" in lower
        or "unauthorized" in lower
        or "authentication" in lower
        or "missing bearer" in lower
        or "basic authentication" in lower
        or "invalid api key" in lower
        or "api key" in lower
        or "credential" in lower
        or "credentials" in lower
        or "bearer token" in lower
    ):
        return "auth"
    if "context" in lower or "token" in lower:
        return "context_or_tokens"
    if "max output" in lower or "output" in lower:
        return "output"
    if "rate" in lower:
        return "rate_limit"
    return "other"


def artifact_report(root: Path) -> JsonObject:
    """Return memory artifact mtimes/sizes."""
    result: JsonObject = {}
    for name in ("MEMORY.md", "memory_summary.md", "raw_memories.md"):
        path = root / name
        result[name] = {
            "exists": path.exists(),
            "bytes": path.stat().st_size if path.exists() else None,
            "mtime": path.stat().st_mtime if path.exists() else None,
        }
    return result
