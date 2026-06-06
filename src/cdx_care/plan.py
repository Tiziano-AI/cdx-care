"""Plan cdx-care reconciliations without mutating Codex state."""

from __future__ import annotations

import json
import os
import uuid
from contextlib import closing
from pathlib import Path

from cdx_care import VERSION
from cdx_care.doctor import load_state_thread_ids_checked
from cdx_care.errors import CdxCareError
from cdx_care.filesystem import ensure_parent_dir
from cdx_care.git_tools import head_tracked_paths, tracked_paths
from cdx_care.logs_compact import LOG_COMPACT_MIN_RECLAIMABLE_BYTES, logs_physical_report, logs_schema_fingerprint
from cdx_care.memory_plan import plan_memory_actions
from cdx_care.paths import StorePaths
from cdx_care.plan_actions import sqlite_update_action
from cdx_care.processes import existing_lsof_targets, lsof_handles
from cdx_care.session_repair import (
    desired_session_index_bytes,
    file_sha256_or_none,
    file_stat,
    jsonl_id_set,
    sha256_bytes,
    state_source_stat,
    verify_session_file_alignment,
)
from cdx_care.sqlite_tools import connect_readonly, schema_fingerprint, table_names
from cdx_care.timeutil import epoch_ms, iso_now
from cdx_care.types import JsonObject, JsonValue

PROFILE_WORKSTATION = "workstation"
PROFILE_CLEAR_CURRENT_BADGE = "clear-current-badge"
SUPPORTED_PROFILES = {PROFILE_WORKSTATION, PROFILE_CLEAR_CURRENT_BADGE}


def generate_plan(stores: StorePaths, profile: str) -> JsonObject:
    """Generate a workstation reconciliation plan."""
    if profile not in SUPPORTED_PROFILES:
        raise CdxCareError(f"unsupported profile: {profile}", code="unsupported_profile")
    run_id = str(uuid.uuid4())
    planned_at = iso_now()
    actions: list[JsonValue] = []
    denials: list[JsonValue] = []
    clear_current_badge = profile == PROFILE_CLEAR_CURRENT_BADGE
    codex_dev_actions, codex_dev_denials = plan_codex_dev_actions(stores, planned_at, clear_current_badge)
    actions.extend(codex_dev_actions)
    denials.extend(codex_dev_denials)
    memory_actions, memory_denials = plan_memory_actions(stores, planned_at)
    actions.extend(memory_actions)
    denials.extend(memory_denials)
    session_actions, session_denials = plan_session_file_actions(stores)
    actions.extend(session_actions)
    denials.extend(session_denials)
    actions.extend(plan_logs_compaction_actions(stores))
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
        "approved_policy": approved_policy_name(profile),
        "planned_actions": actions,
        "action_count": len(actions),
        "denials": denials,
    }
    return plan


def approved_policy_name(profile: str) -> str:
    """Return the explicit closed-policy label for a profile."""
    if profile == PROFILE_CLEAR_CURRENT_BADGE:
        return "manual-clear-current-badge"
    return "workstation-hide-broken-only"


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


def plan_codex_dev_actions(
    stores: StorePaths, planned_at: str, clear_current_badge: bool
) -> tuple[list[JsonValue], list[JsonValue]]:
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
                automation_id = str(row["automation_id"])
                if status == "ARCHIVED" or thread_id not in state_ids:
                    actions.append(
                        automation_mark_read_action(
                            action_id=f"automation-run-mark-read:{thread_id}:{automation_id}",
                            lane="automations.hide_broken_only",
                            path=path,
                            fingerprint=fingerprint,
                            thread_id=thread_id,
                            automation_id=automation_id,
                            status=status,
                            updated_at=int(row["updated_at"]),
                            read_at=read_at,
                            description="Mark a non-actionable unread automation run as read.",
                        )
                    )
                elif clear_current_badge and status in {"PENDING_REVIEW", "ACCEPTED"}:
                    actions.append(
                        automation_mark_read_action(
                            action_id=f"automation-run-clear-badge:{thread_id}:{automation_id}",
                            lane="automations.clear_current_badge",
                            path=path,
                            fingerprint=fingerprint,
                            thread_id=thread_id,
                            automation_id=automation_id,
                            status=status,
                            updated_at=int(row["updated_at"]),
                            read_at=read_at,
                            description=(
                                "Explicitly mark a navigable unread automation review run as read "
                                "to clear the current app badge."
                            ),
                            extra={"manual_profile": PROFILE_CLEAR_CURRENT_BADGE},
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


def automation_mark_read_action(
    *,
    action_id: str,
    lane: str,
    path: Path,
    fingerprint: str,
    thread_id: str,
    automation_id: str,
    status: str,
    updated_at: int,
    read_at: int,
    description: str,
    extra: JsonObject | None = None,
) -> JsonObject:
    """Build an admitted automation_runs mark-read action."""
    return sqlite_update_action(
        action_id=action_id,
        lane=lane,
        db_name="codex-dev",
        db_path=path,
        schema_fingerprint_value=fingerprint,
        table="automation_runs",
        key={"thread_id": thread_id, "automation_id": automation_id},
        preconditions=[
            {"column": "status", "equals": status},
            {"column": "read_at", "is_null": True},
            {"column": "updated_at", "equals": updated_at},
        ],
        updates={"read_at": read_at},
        description=description,
        extra=extra,
    )


def plan_git_hygiene_actions(stores: StorePaths) -> list[JsonValue]:
    """Plan memory git .DS_Store untracking."""
    repo = stores.memories_root
    targets = [".DS_Store", "extensions/.DS_Store"]
    tracked = sorted(set(tracked_paths(repo, targets)) | set(head_tracked_paths(repo, targets)))
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
            "commit": True,
            "commit_message": "Untrack Codex memory Finder metadata",
        }
    ]


def plan_session_file_actions(stores: StorePaths) -> tuple[list[JsonValue], list[JsonValue]]:
    """Plan deterministic session_index JSONL repair."""
    state_db = stores.db_path("state")
    if not state_db.exists():
        return [], [
            {
                "code": "sessions.state_threads.unavailable",
                "reason": "state_5.sqlite is missing; refusing session/history file repair planning",
            }
        ]
    alignment = verify_session_file_alignment(state_db, stores.codex_home / "sessions")
    if alignment["state_not_in_session_file_ids"] or alignment["session_file_ids_not_in_state"]:
        return [], [
            {
                "code": "sessions.rollout_alignment_drift",
                "reason": (
                    "state thread IDs and rollout files differ; refusing JSONL repair until the owner is reviewed"
                ),
                "details": alignment,
            }
        ]
    actions: list[JsonValue] = []
    try:
        source = state_source_stat(state_db)
        index_bytes, index_count = desired_session_index_bytes(state_db, stores.session_index)
    except CdxCareError as error:
        return [], [{"code": error.code, "reason": str(error)}]
    index_sha = sha256_bytes(index_bytes)
    current_index_sha = file_sha256_or_none(stores.session_index)
    index_ids = jsonl_id_set(stores.session_index, "id")
    desired_ids = jsonl_id_set_from_bytes(index_bytes, "id")
    if current_index_sha != index_sha or index_ids != desired_ids:
        actions.append(
            {
                "id": "sessions-rebuild-session-index",
                "type": "jsonl_rewrite",
                "lane": "sessions.rebuild_session_index",
                "description": "Rebuild session_index.jsonl from state_5.threads and rollout-file proof.",
                "path": str(stores.session_index),
                "source_db": "state",
                "source_db_path": str(state_db),
                "source": source,
                "target_stat": file_stat(stores.session_index),
                "desired_sha256": index_sha,
                "desired_bytes": len(index_bytes),
                "desired_row_count": index_count,
                "privacy": {"plan_contains_private_text": False, "content_recomputed_at_apply": True},
                "alignment": alignment,
            }
        )
    return actions, []


def jsonl_id_set_from_bytes(content: bytes, key: str) -> set[str]:
    """Collect a top-level string ID key from generated JSONL bytes."""
    ids: set[str] = set()
    for line in content.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            value = payload.get(key)
            if isinstance(value, str):
                ids.add(value)
    return ids


def plan_logs_compaction_actions(stores: StorePaths) -> list[JsonValue]:
    """Plan logs DB compaction when SQLite freelist pages are reclaimable."""
    path = stores.db_path("logs")
    if not path.exists():
        return []
    report = logs_physical_report(path)
    reclaimable = report.get("reclaimable_bytes")
    if not isinstance(reclaimable, int) or reclaimable < LOG_COMPACT_MIN_RECLAIMABLE_BYTES:
        return []
    fingerprint, tables = logs_schema_fingerprint(path)
    return [
        {
            "id": "logs-compact-freelist",
            "type": "sqlite_compact",
            "lane": "logs.compact_freelist",
            "description": "Compact logs_2.sqlite with VACUUM after DB-family backup; no log rows are deleted.",
            "db": "logs",
            "db_path": str(path),
            "db_stat": db_stat_for_path(path),
            "schema_fingerprint": fingerprint,
            "schema_tables": tables,
            "method": "vacuum",
            "page_size": report["page_size"],
            "page_count": report["page_count"],
            "freelist_count": report["freelist_count"],
            "reclaimable_bytes": reclaimable,
            "row_count": report["row_count"],
            "privacy": {"plan_contains_log_bodies": False},
        }
    ]


def db_stat_for_path(path: Path) -> JsonObject:
    """Return DB stat snapshot for non-row DB actions."""
    stat = path.stat()
    return {
        "exists": True,
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
