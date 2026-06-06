"""Plan native Codex memory job reconciliation actions."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from cdx_care.memory_reports import memory_auth_recovered, memory_auth_recovery_report, memory_error_category
from cdx_care.paths import StorePaths
from cdx_care.plan_actions import sqlite_insert_action, sqlite_update_action
from cdx_care.policy_checks import DEFAULT_STAGE1_RETRY_REMAINING
from cdx_care.sqlite_tools import connect_readonly, schema_fingerprint, table_names, value_hash
from cdx_care.timeutil import epoch_seconds
from cdx_care.types import JsonValue


def plan_memory_actions(stores: StorePaths, planned_at: str) -> tuple[list[JsonValue], list[JsonValue]]:
    """Plan memory retry and native phase-2 enqueue repairs."""
    path = stores.db_path("memories")
    if not path.exists():
        return [], []
    actions: list[JsonValue] = []
    denials: list[JsonValue] = []
    now = epoch_seconds()
    with closing(connect_readonly(path)) as conn:
        tables = table_names(conn)
        if "jobs" not in tables:
            return [], []
        fingerprint = schema_fingerprint(conn, [table for table in ("jobs", "stage1_outputs") if table in tables])
        memory_schema_tables = [table for table in ("jobs", "stage1_outputs") if table in tables]
        auth_recovery = memory_auth_recovery_report(conn)
        auth_denials = plan_memory_auth_denials(conn, recovered=bool(auth_recovery["recovered"]))
        if auth_denials:
            return [], auth_denials
        stage1_rows = conn.execute(
            """
            SELECT kind, job_key, status, worker_id, ownership_token, started_at, finished_at,
                   lease_until, retry_at, retry_remaining, last_error, input_watermark, last_success_watermark
            FROM jobs
            WHERE kind='memory_stage1' AND status='error' AND retry_remaining <= 0
            ORDER BY finished_at, job_key
            """
        ).fetchall()
        for row in stage1_rows:
            if has_future_lease(row, now):
                denials.append(
                    {
                        "code": "memory.stage1_retry.active_lease",
                        "job_key": str(row["job_key"]),
                        "reason": "job has worker, ownership token, or a future lease",
                    }
                )
                continue
            last_error = row["last_error"]
            error_category = memory_error_category(str(last_error or ""))
            if error_category == "auth":
                extra_recovery: dict[str, object] = {"auth_recovery": auth_recovery}
            else:
                extra_recovery = {}
            actions.append(
                sqlite_update_action(
                    action_id=f"memory-stage1-retry:{row['job_key']}",
                    lane="memory.stage1_retry_terminal_errors",
                    db_name="memories",
                    db_path=path,
                    schema_fingerprint_value=fingerprint,
                    table="jobs",
                    key={"kind": "memory_stage1", "job_key": str(row["job_key"])},
                    preconditions=[
                        {"column": "status", "equals": "error"},
                        {"column": "worker_id", "sha256": value_hash(row["worker_id"])},
                        {"column": "ownership_token", "sha256": value_hash(row["ownership_token"])},
                        {"column": "started_at", "equals": row["started_at"]},
                        {"column": "finished_at", "equals": row["finished_at"]},
                        {"column": "lease_until", "equals": row["lease_until"]},
                        {"column": "retry_at", "equals": row["retry_at"]},
                        {"column": "retry_remaining", "equals": int(row["retry_remaining"])},
                        {"column": "last_error", "sha256": value_hash(last_error)},
                    ],
                    updates={
                        "status": "pending",
                        "worker_id": None,
                        "ownership_token": None,
                        "started_at": None,
                        "finished_at": None,
                        "lease_until": None,
                        "retry_at": None,
                        "retry_remaining": DEFAULT_STAGE1_RETRY_REMAINING,
                        "last_error": None,
                    },
                    description="Reset a terminal Stage 1 memory error job to claimable pending state.",
                    extra={
                        "previous_error_category": memory_error_category(str(last_error or "")),
                        "previous_error_sha256": value_hash(last_error),
                        **extra_recovery,
                    },
                )
            )
            if isinstance(actions[-1], dict):
                actions[-1]["schema_tables"] = memory_schema_tables
        global_actions, global_denials = plan_global_consolidation(
            conn, path, fingerprint, now, memory_schema_tables, bool(actions)
        )
        actions.extend(global_actions)
        denials.extend(global_denials)
    _ = planned_at
    return actions, denials


def plan_memory_auth_denials(conn: sqlite3.Connection, *, recovered: bool) -> list[JsonValue]:
    """Return sanitized memory auth blockers that should stop native worker retries."""
    if recovered:
        return []
    rows = conn.execute(
        """
        SELECT kind, job_key, status, retry_remaining, last_error
        FROM jobs
        WHERE last_error IS NOT NULL
          AND kind IN ('memory_stage1', 'memory_consolidate_global')
        ORDER BY kind, job_key
        """
    ).fetchall()
    denials: list[JsonValue] = []
    for row in rows:
        last_error = row["last_error"]
        if memory_error_category(str(last_error or "")) != "auth":
            continue
        kind = str(row["kind"])
        if kind == "memory_stage1":
            denials.append(
                {
                    "code": "memory.stage1_retry.auth_blocked",
                    "job_key": str(row["job_key"]),
                    "status": str(row["status"]),
                    "retry_remaining": int(row["retry_remaining"] or 0),
                    "reason": (
                        "Stage 1 memory job failed with an authentication error; repair Codex/OpenAI "
                        "credential loading before retrying the native memory worker."
                    ),
                    "last_error_sha256": value_hash(last_error),
                }
            )
        elif kind == "memory_consolidate_global":
            denials.append(
                {
                    "code": "memory.global_consolidation.auth_blocked",
                    "job_key": str(row["job_key"]),
                    "status": str(row["status"]),
                    "retry_remaining": int(row["retry_remaining"] or 0),
                    "reason": (
                        "Global memory consolidation failed with an authentication error; repair "
                        "Codex/OpenAI credential loading before enqueueing the native worker."
                    ),
                    "last_error_sha256": value_hash(last_error),
                }
            )
    return denials


def has_future_lease(row: sqlite3.Row, now: int) -> bool:
    """Return whether a memory job row still has a live future lease."""
    lease_until = int(row["lease_until"] or 0)
    return lease_until > now


def plan_global_consolidation(
    conn: sqlite3.Connection,
    path: Path,
    fingerprint: str,
    now: int,
    schema_tables: list[str],
    force_due_to_stage1_retry: bool,
) -> tuple[list[JsonValue], list[JsonValue]]:
    """Plan a native global memory consolidation enqueue/reset."""
    max_source = 0
    if "stage1_outputs" in table_names(conn):
        row = conn.execute("SELECT MAX(source_updated_at) FROM stage1_outputs").fetchone()
        max_source = int(row[0] or 0) if row else 0
    current = conn.execute(
        """
        SELECT kind, job_key, status, worker_id, ownership_token, started_at, finished_at,
               lease_until, retry_at, retry_remaining, last_error, input_watermark, last_success_watermark
        FROM jobs
        WHERE kind='memory_consolidate_global' AND job_key='global'
        """
    ).fetchone()
    if current is None:
        action = sqlite_insert_action(
            action_id="memory-global-consolidation-insert",
            lane="memory.force_global_consolidation",
            db_name="memories",
            db_path=path,
            schema_fingerprint_value=fingerprint,
            table="jobs",
            values={
                "kind": "memory_consolidate_global",
                "job_key": "global",
                "status": "pending",
                "worker_id": None,
                "ownership_token": None,
                "started_at": None,
                "finished_at": None,
                "lease_until": None,
                "retry_at": None,
                "retry_remaining": DEFAULT_STAGE1_RETRY_REMAINING,
                "last_error": None,
                "input_watermark": max(max_source, now),
                "last_success_watermark": 0,
            },
            description="Create the native global memory consolidation job as pending.",
        )
        action["schema_tables"] = schema_tables
        return [action], []
    current_input = int(current["input_watermark"] or 0)
    last_success = int(current["last_success_watermark"] or 0)
    lease_until = int(current["lease_until"] or 0)
    recovered_auth = memory_auth_recovered(conn)
    if lease_until > now:
        return [], [
            {
                "code": "memory.global_consolidation.active_lease",
                "reason": "native global memory consolidation job has a future lease",
            }
        ]
    if (
        str(current["status"]) == "pending"
        and current["last_error"] is None
        and current["worker_id"] is None
        and current["ownership_token"] is None
        and current_input >= max(max_source, last_success)
    ):
        return [], []
    needs_consolidation = force_due_to_stage1_retry or str(current["status"]) != "done" or last_success < max_source
    if not needs_consolidation:
        return [], []
    target_watermark = max(current_input + 1, max_source, now)
    action = sqlite_update_action(
        action_id="memory-global-consolidation-enqueue",
        lane="memory.force_global_consolidation",
        db_name="memories",
        db_path=path,
        schema_fingerprint_value=fingerprint,
        table="jobs",
        key={"kind": "memory_consolidate_global", "job_key": "global"},
        preconditions=[
            {"column": "status", "equals": str(current["status"])},
            {"column": "worker_id", "sha256": value_hash(current["worker_id"])},
            {"column": "ownership_token", "sha256": value_hash(current["ownership_token"])},
            {"column": "started_at", "equals": current["started_at"]},
            {"column": "finished_at", "equals": current["finished_at"]},
            {"column": "lease_until", "equals": current["lease_until"]},
            {"column": "retry_at", "equals": current["retry_at"]},
            {"column": "retry_remaining", "equals": int(current["retry_remaining"])},
            {"column": "last_error", "sha256": value_hash(current["last_error"])},
        ],
        updates={
            "status": "pending",
            "worker_id": None,
            "ownership_token": None,
            "started_at": None,
            "finished_at": None,
            "lease_until": None,
            "retry_at": None,
            "retry_remaining": DEFAULT_STAGE1_RETRY_REMAINING,
            "last_error": None,
            "input_watermark": target_watermark,
        },
        description="Enqueue the native global memory consolidation job.",
        extra={
            "previous_error_sha256": value_hash(current["last_error"]),
            **({"auth_recovery": memory_auth_recovery_report(conn)} if recovered_auth else {}),
        },
    )
    action["schema_tables"] = schema_tables
    return [action], []
