"""Plan cdx-care reconciliations without mutating Codex state."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from cdx_care import VERSION
from cdx_care.doctor import load_state_thread_ids_checked
from cdx_care.errors import CdxCareError
from cdx_care.filesystem import ensure_parent_dir
from cdx_care.git_tools import tracked_paths
from cdx_care.memory_reports import memory_error_category
from cdx_care.paths import StorePaths
from cdx_care.plan_actions import sqlite_insert_action, sqlite_update_action
from cdx_care.processes import existing_lsof_targets, lsof_handles
from cdx_care.sqlite_tools import connect_readonly, schema_fingerprint, table_names, value_hash
from cdx_care.timeutil import epoch_ms, epoch_seconds, iso_now
from cdx_care.types import JsonObject, JsonValue

DEFAULT_STAGE1_RETRY_REMAINING = 3


def generate_plan(stores: StorePaths, profile: str) -> JsonObject:
    """Generate a workstation reconciliation plan."""
    run_id = str(uuid.uuid4())
    planned_at = iso_now()
    actions: list[JsonValue] = []
    denials: list[JsonValue] = []
    codex_dev_actions, codex_dev_denials = plan_codex_dev_actions(stores, planned_at)
    actions.extend(codex_dev_actions)
    denials.extend(codex_dev_denials)
    memory_actions, memory_denials = plan_memory_actions(stores, planned_at)
    actions.extend(memory_actions)
    denials.extend(memory_denials)
    actions.extend(plan_git_hygiene_actions(stores))
    db_paths = list(stores.db_paths().values())
    lsof_available, handles = lsof_handles(db_paths)
    lsof_target_count = len(existing_lsof_targets(db_paths))
    plan: JsonObject = {
        "schema_version": 1,
        "tool": "cdx-care",
        "version": VERSION,
        "ok": True,
        "run_id": run_id,
        "created_at": planned_at,
        "profile": profile,
        "support_root": str(stores.codex_home),
        "codex_closed": lsof_available and lsof_target_count > 0 and not handles,
        "lsof": {"available": lsof_available, "target_count": lsof_target_count, "handle_count": len(handles)},
        "approved_policy": "workstation-hide-broken-only",
        "planned_actions": actions,
        "action_count": len(actions),
        "denials": denials,
    }
    return plan


def write_plan(plan: JsonObject, out_path: Path) -> None:
    """Write a plan JSON file."""
    write_new_text(out_path, json.dumps(plan, indent=2, sort_keys=True) + "\n", mode=0o600)


def write_new_text(path: Path, text: str, *, mode: int) -> None:
    """Write text to a new file only, never clobbering an existing path."""
    if path.exists() or path.is_symlink():
        raise CdxCareError(f"output path already exists: {path}", code="output_exists")
    ensure_parent_dir(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def plan_codex_dev_actions(stores: StorePaths, planned_at: str) -> tuple[list[JsonValue], list[JsonValue]]:
    """Plan safe app DB read-state repairs."""
    path = stores.db_path("codex-dev")
    if not path.exists():
        return [], []
    state_available, state_ids = load_state_thread_ids_checked(stores.db_path("state"))
    if not state_available:
        return [], [
            {
                "code": "codex.state_threads.unavailable",
                "reason": "state_5.sqlite threads proof is unavailable; refusing automation/inbox read-state planning",
            }
        ]
    actions: list[JsonValue] = []
    read_at = epoch_ms()
    with closing(connect_readonly(path)) as conn:
        tables = table_names(conn)
        if "automation_runs" in tables:
            fingerprint = schema_fingerprint(conn, ["automation_runs"])
            rows = conn.execute(
                """
                SELECT thread_id, automation_id, status, read_at, updated_at
                FROM automation_runs
                WHERE read_at IS NULL
                ORDER BY updated_at
                """
            ).fetchall()
            for row in rows:
                thread_id = str(row["thread_id"])
                status = str(row["status"])
                if status == "ARCHIVED" or thread_id not in state_ids:
                    actions.append(
                        sqlite_update_action(
                            action_id=f"automation-run-mark-read:{thread_id}:{row['automation_id']}",
                            lane="automations.hide_broken_only",
                            db_name="codex-dev",
                            db_path=path,
                            schema_fingerprint_value=fingerprint,
                            table="automation_runs",
                            key={"thread_id": thread_id, "automation_id": str(row["automation_id"])},
                            preconditions=[
                                {"column": "status", "equals": status},
                                {"column": "read_at", "is_null": True},
                                {"column": "updated_at", "equals": int(row["updated_at"])},
                            ],
                            updates={"read_at": read_at},
                            description="Mark a non-actionable unread automation run as read.",
                        )
                    )
        if "inbox_items" in tables:
            fingerprint = schema_fingerprint(conn, ["inbox_items"])
            rows = conn.execute(
                "SELECT id, thread_id, read_at, created_at FROM inbox_items WHERE read_at IS NULL ORDER BY created_at"
            ).fetchall()
            for row in rows:
                thread_id = str(row["thread_id"] or "")
                if thread_id and thread_id not in state_ids:
                    actions.append(
                        sqlite_update_action(
                            action_id=f"inbox-orphan-mark-read:{row['id']}",
                            lane="inbox.orphan_mark_read",
                            db_name="codex-dev",
                            db_path=path,
                            schema_fingerprint_value=fingerprint,
                            table="inbox_items",
                            key={"id": str(row["id"])},
                            preconditions=[
                                {"column": "thread_id", "equals": thread_id},
                                {"column": "read_at", "is_null": True},
                                {"column": "created_at", "equals": int(row["created_at"])},
                            ],
                            updates={"read_at": read_at},
                            description="Mark an unread orphan inbox item as read.",
                        )
                    )
    _ = planned_at
    return actions, []


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
                        {"column": "input_watermark", "equals": int(row["input_watermark"] or 0)},
                        {
                            "column": "last_success_watermark",
                            "equals": int(row["last_success_watermark"] or 0),
                        },
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
            {"column": "input_watermark", "equals": current_input},
            {"column": "last_success_watermark", "equals": last_success},
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
        extra={"previous_error_sha256": value_hash(current["last_error"])},
    )
    action["schema_tables"] = schema_tables
    return [action], []


def plan_git_hygiene_actions(stores: StorePaths) -> list[JsonValue]:
    """Plan memory git .DS_Store untracking."""
    repo = stores.memories_root
    targets = [".DS_Store", "extensions/.DS_Store"]
    tracked = tracked_paths(repo, targets)
    if not tracked:
        return []
    return [
        {
            "id": "memory-git-untrack-ds-store",
            "type": "git_rm_cached",
            "lane": "memory.git_hygiene",
            "description": (
                "Untrack Finder .DS_Store files in the memory git repo while preserving local ignored files."
            ),
            "repo": str(repo),
            "paths": tracked,
        }
    ]
