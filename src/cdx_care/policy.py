"""Closed v1 action policy for cdx-care plans."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from cdx_care.errors import CdxCareError
from cdx_care.paths import StorePaths
from cdx_care.policy_checks import (
    DEFAULT_STAGE1_RETRY_REMAINING,
    normalized_path,
    object_map,
    require_absence_precondition,
    require_exact_updates,
    require_exact_value_keys,
    require_job_key,
    require_keys,
    require_mark_read_update,
    require_pending_job_values,
    require_preconditions,
    require_schema_fingerprint,
    require_schema_tables,
    string_list,
)
from cdx_care.types import JsonObject

DS_STORE_PATHS = {".DS_Store", "extensions/.DS_Store"}


@dataclass(frozen=True)
class ApplyContext:
    """Current apply-time evidence needed to recheck row eligibility."""

    state_thread_ids: set[str]
    now_seconds: int
    now_ms: int


def validate_action_targets(stores: StorePaths, actions: list[JsonObject]) -> None:
    """Treat the editable plan body as untrusted and admit only known lanes."""
    for action in actions:
        action_type = action.get("type")
        if action_type == "sqlite_update":
            validate_sqlite_update_target(stores, action)
        elif action_type == "sqlite_insert":
            validate_sqlite_insert_target(stores, action)
        elif action_type == "git_rm_cached":
            validate_git_target(stores, action)
        else:
            raise CdxCareError(f"unsupported action type: {action_type}", code="unsupported_action")


def admitted_db_paths(stores: StorePaths, actions: list[JsonObject]) -> list[Path]:
    """Return canonical DB paths admitted by the plan action policy."""
    paths = {
        stores.db_path(str(action["db"]))
        for action in actions
        if action.get("type") in ("sqlite_update", "sqlite_insert")
    }
    return sorted(paths)


def validate_sqlite_common(stores: StorePaths, action: JsonObject, *, db: str, table: str, lane: str) -> None:
    """Validate shared SQLite action ownership fields."""
    if action.get("db") != db or action.get("table") != table or action.get("lane") != lane:
        raise CdxCareError("plan action target is outside cdx-care policy", code="action_target_denied")
    planned_path = action.get("db_path")
    if not isinstance(planned_path, str):
        raise CdxCareError("DB action missing db_path", code="invalid_plan")
    if normalized_path(Path(planned_path)) != normalized_path(stores.db_path(db)):
        raise CdxCareError("DB action path does not match its admitted DB name", code="db_path_mismatch")
    require_schema_fingerprint(action)


def validate_sqlite_update_target(stores: StorePaths, action: JsonObject) -> None:
    """Validate a planned SQLite update against the closed v1 policy."""
    lane = str(action.get("lane"))
    table = str(action.get("table"))
    db = str(action.get("db"))
    key = object_map(action.get("key"), "key")
    updates = object_map(action.get("updates"), "updates")
    if (lane, db, table) == ("automations.hide_broken_only", "codex-dev", "automation_runs"):
        validate_sqlite_common(stores, action, db="codex-dev", table="automation_runs", lane=lane)
        require_schema_tables(
            action,
            required={"automation_runs"},
            allowed={"automation_runs"},
            label="automation_runs schema_tables",
        )
        require_keys(key, {"thread_id", "automation_id"}, "automation_runs key")
        require_preconditions(
            action,
            {"status": "equals", "read_at": "is_null", "updated_at": "equals"},
            "automation_runs preconditions",
        )
        require_mark_read_update(updates, "automation_runs updates")
        return
    if (lane, db, table) == ("inbox.orphan_mark_read", "codex-dev", "inbox_items"):
        validate_sqlite_common(stores, action, db="codex-dev", table="inbox_items", lane=lane)
        require_schema_tables(
            action,
            required={"inbox_items"},
            allowed={"inbox_items"},
            label="inbox_items schema_tables",
        )
        require_keys(key, {"id"}, "inbox_items key")
        require_preconditions(
            action,
            {"thread_id": "equals", "read_at": "is_null", "created_at": "equals"},
            "inbox_items preconditions",
        )
        require_mark_read_update(updates, "inbox_items updates")
        return
    if (lane, db, table) == ("memory.stage1_retry_terminal_errors", "memories", "jobs"):
        validate_sqlite_common(stores, action, db="memories", table="jobs", lane=lane)
        require_schema_tables(
            action,
            required={"jobs"},
            allowed={"jobs", "stage1_outputs"},
            label="memory schema_tables",
        )
        require_job_key(key, "memory_stage1")
        require_preconditions(
            action,
            {
                "status": "equals",
                "worker_id": "sha256",
                "ownership_token": "sha256",
                "started_at": "equals",
                "finished_at": "equals",
                "lease_until": "equals",
                "retry_at": "equals",
                "retry_remaining": "equals",
                "last_error": "sha256",
                "input_watermark": "equals",
                "last_success_watermark": "equals",
            },
            "memory stage1 retry preconditions",
        )
        require_exact_updates(
            updates,
            {
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
            "memory stage1 retry updates",
        )
        return
    if (lane, db, table) == ("memory.force_global_consolidation", "memories", "jobs"):
        validate_sqlite_common(stores, action, db="memories", table="jobs", lane=lane)
        require_schema_tables(
            action,
            required={"jobs"},
            allowed={"jobs", "stage1_outputs"},
            label="memory schema_tables",
        )
        require_job_key(key, "memory_consolidate_global", job_key="global")
        require_preconditions(
            action,
            {
                "status": "equals",
                "worker_id": "sha256",
                "ownership_token": "sha256",
                "started_at": "equals",
                "finished_at": "equals",
                "lease_until": "equals",
                "retry_at": "equals",
                "retry_remaining": "equals",
                "input_watermark": "equals",
                "last_success_watermark": "equals",
                "last_error": "sha256",
            },
            "memory global consolidation preconditions",
        )
        require_exact_value_keys(
            updates,
            {
                "status",
                "worker_id",
                "ownership_token",
                "started_at",
                "finished_at",
                "lease_until",
                "retry_at",
                "retry_remaining",
                "last_error",
                "input_watermark",
            },
            "memory global consolidation updates",
        )
        require_pending_job_values(updates, "memory global consolidation updates")
        watermark = updates.get("input_watermark")
        if not isinstance(watermark, int) or watermark <= 0:
            raise CdxCareError(
                "memory global consolidation input_watermark must be positive", code="action_target_denied"
            )
        return
    raise CdxCareError("SQLite update action is outside cdx-care policy", code="action_target_denied")


def validate_sqlite_insert_target(stores: StorePaths, action: JsonObject) -> None:
    """Validate a planned SQLite insert against the closed v1 policy."""
    lane = str(action.get("lane"))
    db = str(action.get("db"))
    table = str(action.get("table"))
    if (lane, db, table) != ("memory.force_global_consolidation", "memories", "jobs"):
        raise CdxCareError("SQLite insert action is outside cdx-care policy", code="action_target_denied")
    validate_sqlite_common(stores, action, db="memories", table="jobs", lane=lane)
    require_schema_tables(
        action,
        required={"jobs"},
        allowed={"jobs", "stage1_outputs"},
        label="memory schema_tables",
    )
    key = object_map(action.get("key"), "key")
    require_job_key(key, "memory_consolidate_global", job_key="global")
    require_absence_precondition(action, "memory global consolidation insert preconditions")
    values = object_map(action.get("values"), "values")
    require_exact_value_keys(
        values,
        {
            "kind",
            "job_key",
            "status",
            "worker_id",
            "ownership_token",
            "started_at",
            "finished_at",
            "lease_until",
            "retry_at",
            "retry_remaining",
            "last_error",
            "input_watermark",
            "last_success_watermark",
        },
        "memory global consolidation insert",
    )
    if values.get("kind") != "memory_consolidate_global" or values.get("job_key") != "global":
        raise CdxCareError("memory global insert must target the native global job", code="action_target_denied")
    require_pending_job_values(values, "memory global consolidation insert")
    watermark = values.get("input_watermark")
    if not isinstance(watermark, int) or watermark <= 0:
        raise CdxCareError("memory global insert input_watermark must be positive", code="action_target_denied")
    if values.get("last_success_watermark") != 0:
        raise CdxCareError("memory global insert last_success_watermark must be zero", code="action_target_denied")


def validate_git_target(stores: StorePaths, action: JsonObject) -> None:
    """Validate a planned git hygiene action against the closed v1 policy."""
    if action.get("lane") != "memory.git_hygiene":
        raise CdxCareError("git action lane is outside cdx-care policy", code="git_target_denied")
    repo = action.get("repo")
    if not isinstance(repo, str):
        raise CdxCareError("git action repo must be a string", code="invalid_plan")
    if normalized_path(Path(repo)) != normalized_path(stores.memories_root):
        raise CdxCareError("git action repo must be the Codex memory repo", code="git_target_denied")
    paths_value = action.get("paths")
    if not isinstance(paths_value, list):
        raise CdxCareError("git action paths must be strings", code="invalid_plan")
    paths = string_list(paths_value, "paths")
    if not paths or len(set(paths)) != len(paths) or not set(paths).issubset(DS_STORE_PATHS):
        raise CdxCareError("git action paths must be the admitted memory .DS_Store paths", code="git_target_denied")


def verify_lane_eligibility(row: sqlite3.Row, action: JsonObject, context: ApplyContext) -> None:
    """Re-check current row eligibility independently from plan preconditions."""
    lane = str(action.get("lane"))
    if lane == "automations.hide_broken_only":
        thread_id = str(row["thread_id"] or "")
        status = str(row["status"] or "")
        if row["read_at"] is not None or (status != "ARCHIVED" and thread_id in context.state_thread_ids):
            raise CdxCareError("automation run is not eligible for hide-broken mark-read", code="row_not_eligible")
        verify_mark_read_timestamp(row, action, context, lower_column="updated_at", label="automation read_at")
        return
    if lane == "inbox.orphan_mark_read":
        thread_id = str(row["thread_id"] or "")
        if row["read_at"] is not None or not thread_id or thread_id in context.state_thread_ids:
            raise CdxCareError("inbox row is not an unread orphan", code="row_not_eligible")
        verify_mark_read_timestamp(row, action, context, lower_column="created_at", label="inbox read_at")
        return
    if lane == "memory.stage1_retry_terminal_errors":
        if (
            str(row["kind"]) != "memory_stage1"
            or str(row["status"]) != "error"
            or int(row["retry_remaining"] or 0) > 0
            or int(row["lease_until"] or 0) > context.now_seconds
        ):
            raise CdxCareError("memory Stage 1 job is not an exhausted idle error", code="row_not_eligible")
        return
    if lane == "memory.force_global_consolidation":
        if str(row["kind"]) != "memory_consolidate_global" or str(row["job_key"]) != "global":
            raise CdxCareError("memory global action must target the native global job", code="row_not_eligible")
        if int(row["lease_until"] or 0) > context.now_seconds:
            raise CdxCareError("memory global job has a future lease", code="row_not_eligible")
        updates = object_map(action.get("updates"), "updates")
        next_watermark = updates.get("input_watermark")
        current_watermark = int(row["input_watermark"] or 0)
        last_success = int(row["last_success_watermark"] or 0)
        if not isinstance(next_watermark, int) or next_watermark <= current_watermark or next_watermark < last_success:
            raise CdxCareError(
                "memory global input_watermark must advance the current job watermark",
                code="action_target_denied",
            )
        return
    raise CdxCareError("action lane has no eligibility policy", code="action_target_denied")


def verify_mark_read_timestamp(
    row: sqlite3.Row, action: JsonObject, context: ApplyContext, *, lower_column: str, label: str
) -> None:
    """Deny untrusted mark-read plans that move read_at outside the current row/time window."""
    updates = object_map(action.get("updates"), "updates")
    read_at = updates.get("read_at")
    if not isinstance(read_at, int):
        raise CdxCareError(f"{label} must be an integer timestamp", code="action_target_denied")
    lower = row[lower_column]
    if lower is not None and read_at < int(lower):
        raise CdxCareError(f"{label} is earlier than row {lower_column}", code="action_target_denied")
    if read_at > context.now_ms:
        raise CdxCareError(f"{label} is in the future", code="action_target_denied")
