"""Apply cdx-care plans with backups, drift checks, and receipts."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from cdx_care import VERSION
from cdx_care.db_apply import apply_db_actions, group_db_actions, preflight_db_actions, verify_db_stat, verify_schema
from cdx_care.doctor import load_state_thread_ids_checked
from cdx_care.errors import CdxCareError
from cdx_care.filesystem import ensure_private_dir
from cdx_care.git_tools import git_hygiene_preflight, git_untrack_and_commit
from cdx_care.logs_compact import compact_logs_db, preflight_logs_compaction
from cdx_care.paths import StorePaths
from cdx_care.policy import DS_STORE_PATHS, ApplyContext, admitted_db_paths, validate_action_targets
from cdx_care.policy_checks import string_list
from cdx_care.processes import lsof_handles
from cdx_care.receipts import failure_receipt, path_present, post_apply_next_commands, write_receipt_file
from cdx_care.session_repair import (
    desired_session_index_bytes,
    file_stat,
    sha256_bytes,
    verify_file_stat,
    verify_session_file_alignment,
    verify_state_source,
)
from cdx_care.sqlite_tools import connect_readonly, copy_db_family, copy_private_file, file_sha256
from cdx_care.support_paths import preflight_managed_artifact_path, preflight_support_paths
from cdx_care.timeutil import epoch_ms, epoch_seconds, iso_now
from cdx_care.types import JsonObject, JsonValue, require_json_object, require_json_object_list


def load_plan(path: Path) -> JsonObject:
    """Read a plan JSON object."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    try:
        return require_json_object(payload, "plan")
    except TypeError as error:
        raise CdxCareError("plan must be a JSON object", code="invalid_plan") from error


def apply_plan(stores: StorePaths, plan: JsonObject) -> JsonObject:
    """Apply a plan after safety gates."""
    validate_plan_header(plan, stores)
    actions = as_action_list(plan.get("planned_actions"))
    validate_plan_policy(plan, actions)
    validate_action_targets(stores, actions)
    db_paths = admitted_db_paths(stores, actions)
    preflight_support_paths(stores, db_paths, actions)
    context = apply_context(stores, actions)
    lsof_available, handles = lsof_handles(list(stores.db_paths().values()))
    if not lsof_available:
        raise CdxCareError("lsof is unavailable or timed out; refusing DB writes", code="lsof_unavailable")
    if handles:
        raise CdxCareError("Codex DB handles are open; quit Codex before apply", code="codex_db_handles_open")
    run_id = str(plan["run_id"])
    backup_root = stores.care_root / "backups" / run_id
    receipt_root = stores.care_root / "receipts"
    receipt_path = receipt_root / f"{run_id}.json"
    preflight_managed_artifact_path(stores, backup_root, "backup root")
    preflight_managed_artifact_path(stores, receipt_path, "receipt file")
    if backup_root.exists() or receipt_path.exists():
        raise CdxCareError("plan run_id already has backup or receipt state", code="run_id_reused")
    profile = str(plan["profile"])
    approved_policy = str(plan["approved_policy"])
    db_groups = group_db_actions(actions)
    for db_path, db_actions in db_groups.items():
        preflight_db_actions(db_path, db_actions, context)
    compact_preflights = preflight_compact_actions(stores, actions)
    file_preflights = preflight_jsonl_actions(stores, actions)
    git_preflights = preflight_git_actions(actions)
    ensure_private_dir(receipt_root)
    backups: list[JsonObject] = []
    applied: list[JsonValue] = []
    mutation_started = False
    try:
        backups = backup_dbs(db_paths, backup_root)
        backups.extend(backup_jsonl_targets(actions, backup_root))
        for db_path, db_actions in db_groups.items():
            db_applied = apply_db_actions(db_path, db_actions, context)
            applied.extend(db_applied)
            mutation_started = mutation_started or bool(db_applied)
        for action in actions:
            if action.get("type") == "sqlite_compact":
                mutation_started = True
                applied.append(apply_compact_action(stores, action))
        for action in actions:
            if action.get("type") == "jsonl_rewrite":
                mutation_started = True
                applied.append(apply_jsonl_action(stores, action, backup_root))
        for action in actions:
            if action.get("type") == "git_rm_cached":
                mutation_started = True
                applied.append(apply_git_action(action))
    except (CdxCareError, OSError, sqlite3.Error) as error:
        if not backups:
            backups = discover_backup_files(backup_root)
        failed_receipt: JsonObject | None = None
        if backups or mutation_started or path_present(backup_root):
            failed_receipt = failure_receipt(
                stores,
                run_id,
                profile,
                approved_policy,
                actions,
                backups,
                applied,
                git_preflights,
                error,
                partial=mutation_started,
            )
            write_receipt_file(receipt_path, failed_receipt)
            failed_receipt["receipt_path"] = str(receipt_path)
        raise apply_failure_error(error, run_id, failed_receipt) from error
    success_receipt: JsonObject = {
        "schema_version": 1,
        "tool": "cdx-care",
        "version": VERSION,
        "run_id": run_id,
        "profile": profile,
        "approved_policy": approved_policy,
        "applied_at": iso_now(),
        "support_root": str(stores.codex_home),
        "codex_closed": True,
        "planned_actions": actions,
        "plan_action_count": len(actions),
        "backups": backups,
        "compact_preflights": compact_preflights,
        "file_preflights": file_preflights,
        "git_preflights": git_preflights,
        "applied_actions": applied,
        "next_commands": post_apply_next_commands(stores, actions, applied),
        "ok": True,
    }
    write_receipt_file(receipt_path, success_receipt)
    success_receipt["receipt_path"] = str(receipt_path)
    return success_receipt


def apply_failure_error(
    error: CdxCareError | OSError | sqlite3.Error, run_id: str, receipt: JsonObject | None
) -> CdxCareError:
    """Preserve the original failure code while surfacing receipt recovery metadata."""
    code = error.code if isinstance(error, CdxCareError) else error.__class__.__name__
    details: JsonObject = {"run_id": run_id}
    if receipt is not None:
        details["receipt_path"] = str(receipt.get("receipt_path", ""))
        details["partial"] = bool(receipt.get("partial", False))
        details["backup_root_present"] = bool(receipt.get("backup_root_present", bool(receipt.get("backups"))))
    return CdxCareError(str(error), code=code, details=details)


def validate_plan_header(plan: JsonObject, stores: StorePaths) -> None:
    """Validate plan owner and support root."""
    if plan.get("schema_version") != 1 or plan.get("tool") != "cdx-care":
        raise CdxCareError("unsupported plan schema", code="unsupported_plan")
    if plan.get("version") != VERSION:
        raise CdxCareError("plan version does not match installed cdx-care", code="version_mismatch")
    if str(plan.get("support_root")) != str(stores.codex_home):
        raise CdxCareError("plan support_root does not match requested Codex home", code="support_root_mismatch")
    run_id = plan.get("run_id")
    if not isinstance(run_id, str):
        raise CdxCareError("plan is missing run_id", code="invalid_plan")
    try:
        parsed = uuid.UUID(run_id)
    except ValueError as error:
        raise CdxCareError("plan run_id must be a UUID", code="invalid_run_id") from error
    if str(parsed) != run_id:
        raise CdxCareError("plan run_id must be canonical UUID text", code="invalid_run_id")


def validate_plan_policy(plan: JsonObject, actions: list[JsonObject]) -> None:
    """Bind manual lanes to the reviewed profile/policy recorded in the plan header."""
    profile = plan.get("profile")
    approved_policy = plan.get("approved_policy")
    if profile == "workstation":
        if approved_policy != "workstation-hide-broken-only":
            raise CdxCareError("workstation plan approved_policy mismatch", code="plan_policy_mismatch")
    elif profile == "clear-current-badge":
        if approved_policy != "manual-clear-current-badge":
            raise CdxCareError("clear-current-badge plan approved_policy mismatch", code="plan_policy_mismatch")
    else:
        raise CdxCareError("unsupported plan profile", code="unsupported_profile")
    for action in actions:
        if action.get("lane") != "automations.clear_current_badge":
            continue
        if profile != "clear-current-badge" or approved_policy != "manual-clear-current-badge":
            raise CdxCareError(
                "clear-current-badge actions require the manual reviewed profile",
                code="plan_policy_mismatch",
            )
        extra = action.get("extra")
        if not isinstance(extra, dict) or extra.get("manual_profile") != "clear-current-badge":
            raise CdxCareError(
                "clear-current-badge action missing manual profile metadata",
                code="plan_policy_mismatch",
            )


def as_action_list(value: JsonValue | None) -> list[JsonObject]:
    """Validate and return a planned action list."""
    try:
        return require_json_object_list(value, "planned_actions")
    except TypeError as error:
        raise CdxCareError("plan planned_actions must be a list", code="invalid_plan") from error


def apply_context(stores: StorePaths, actions: list[JsonObject]) -> ApplyContext:
    """Build apply-time context, failing closed when read-state proof is unavailable."""
    read_state_lanes = {"automations.hide_broken_only", "automations.clear_current_badge", "inbox.orphan_mark_read"}
    needs_state = any(action.get("lane") in read_state_lanes for action in actions)
    now_seconds = epoch_seconds()
    now_ms = epoch_ms()
    if not needs_state:
        return ApplyContext(set(), now_seconds, now_ms)
    state_available, state_ids = load_state_thread_ids_checked(stores.db_path("state"))
    if not state_available:
        raise CdxCareError(
            "state_5.sqlite threads proof is unavailable; refusing codex-dev read-state writes",
            code="state_threads_unavailable",
        )
    return ApplyContext(state_ids, now_seconds, now_ms)


def backup_dbs(db_paths: list[Path], backup_root: Path) -> list[JsonObject]:
    """Back up DB files before writes."""
    rows: list[JsonObject] = []
    for db_path in db_paths:
        rows.extend(copy_db_family(db_path, backup_root / db_path.name))
    return rows


def discover_backup_files(backup_root: Path) -> list[JsonObject]:
    """Discover backup files created before a backup-stage failure."""
    if not backup_root.exists() or backup_root.is_symlink():
        return []
    rows: list[JsonObject] = []
    for path in sorted(backup_root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        row: JsonObject = {
            "target": str(path),
            "bytes": path.stat().st_size,
            "sha256": file_sha256(path),
            "discovered_after_failure": True,
        }
        rows.append(row)
    return rows


def preflight_git_actions(actions: list[JsonObject]) -> list[JsonObject]:
    """Verify git hygiene actions before any DB write starts."""
    rows: list[JsonObject] = []
    for action in actions:
        if action.get("type") == "git_rm_cached":
            paths_value = action.get("paths")
            if not isinstance(paths_value, list):
                raise CdxCareError("git action paths must be strings", code="invalid_plan")
            rows.append(
                git_hygiene_preflight(
                    Path(str(action["repo"])),
                    string_list(paths_value, "paths"),
                    complete_paths=sorted(DS_STORE_PATHS),
                )
            )
    return rows


def preflight_compact_actions(stores: StorePaths, actions: list[JsonObject]) -> list[JsonObject]:
    """Verify SQLite compaction actions before backup."""
    rows: list[JsonObject] = []
    for action in actions:
        if action.get("type") == "sqlite_compact":
            if action.get("db") != "logs":
                raise CdxCareError("SQLite compaction action must target logs DB", code="action_target_denied")
            db_path = stores.db_path("logs")
            verify_db_stat(db_path, [action])
            with closing(connect_readonly(db_path)) as conn:
                verify_schema(conn, [action])
            rows.append(preflight_logs_compaction(db_path, action))
    return rows


def preflight_jsonl_actions(stores: StorePaths, actions: list[JsonObject]) -> list[JsonObject]:
    """Verify JSONL rewrite actions before backup."""
    rows: list[JsonObject] = []
    for action in actions:
        if action.get("type") == "jsonl_rewrite":
            rows.append(preflight_jsonl_action(stores, action))
    return rows


def preflight_jsonl_action(stores: StorePaths, action: JsonObject) -> JsonObject:
    """Verify one JSONL rewrite action without exposing row bodies."""
    target = Path(str(action["path"]))
    source = action.get("source")
    if not isinstance(source, dict):
        raise CdxCareError("JSONL rewrite action missing source state", code="invalid_plan")
    verify_state_source(stores.db_path("state"), source)
    verify_file_stat(target, require_json_object(action.get("target_stat"), "target_stat"))
    alignment = verify_session_file_alignment(stores.db_path("state"), stores.codex_home / "sessions")
    if alignment["state_not_in_session_file_ids"] or alignment["session_file_ids_not_in_state"]:
        raise CdxCareError("state thread IDs and rollout files differ before JSONL repair", code="row_drift")
    lane = str(action.get("lane"))
    if lane == "sessions.rebuild_session_index":
        content, count = desired_session_index_bytes(stores.db_path("state"), stores.session_index)
        actual_sha = sha256_bytes(content)
        if actual_sha != action.get("desired_sha256") or len(content) != action.get("desired_bytes"):
            raise CdxCareError("session index desired output changed before apply", code="row_drift")
        return {"lane": lane, "path": str(target), "desired_sha256": actual_sha, "desired_row_count": count}
    raise CdxCareError("JSONL rewrite action is outside cdx-care policy", code="action_target_denied")


def backup_jsonl_targets(actions: list[JsonObject], backup_root: Path) -> list[JsonObject]:
    """Back up JSONL rewrite targets before replacement."""
    rows: list[JsonObject] = []
    file_root = backup_root / "files"
    for action in actions:
        if action.get("type") != "jsonl_rewrite":
            continue
        target = Path(str(action["path"]))
        ensure_private_dir(file_root)
        backup_name = target.name
        backup_path = file_root / backup_name
        if target.exists():
            copy_private_file(target, backup_path)
            rows.append(
                {
                    "source": str(target),
                    "target": str(backup_path),
                    "bytes": backup_path.stat().st_size,
                    "sha256": file_sha256(backup_path),
                    "kind": "jsonl",
                }
            )
    return rows


def apply_compact_action(stores: StorePaths, action: JsonObject) -> JsonObject:
    """Apply a SQLite compaction action."""
    preflight_compact_actions(stores, [action])
    result = compact_logs_db(stores.db_path("logs"), action)
    return {"id": str(action["id"]), "type": "sqlite_compact", "lane": str(action["lane"]), "result": result}


def apply_jsonl_action(stores: StorePaths, action: JsonObject, backup_root: Path) -> JsonObject:
    """Apply an admitted JSONL rewrite action."""
    lane = str(action.get("lane"))
    target = Path(str(action["path"]))
    if lane == "sessions.rebuild_session_index":
        preflight_jsonl_action(stores, action)
        content, row_count = desired_session_index_bytes(stores.db_path("state"), stores.session_index)
        replace_file_from_private_temp(target, content, backup_root / "session_index.new")
        return {
            "id": str(action["id"]),
            "type": "jsonl_rewrite",
            "lane": lane,
            "path": str(target),
            "sha256": sha256_bytes(content),
            "row_count": row_count,
            "target_stat": file_stat(target),
        }
    raise CdxCareError("JSONL rewrite action is outside cdx-care policy", code="action_target_denied")


def replace_file_from_private_temp(target: Path, content: bytes, temp_path: Path) -> None:
    """Write bytes to a private temp path, then atomically replace the target."""
    write_private_file(temp_path, content)
    os.replace(temp_path, target)
    os.chmod(target, 0o600)


def write_private_file(path: Path, content: bytes) -> None:
    """Write a new private file exactly once."""
    if path.exists() or path.is_symlink():
        raise CdxCareError(f"private write target already exists: {path}", code="output_exists")
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)


def apply_git_action(action: JsonObject) -> JsonObject:
    """Apply a git hygiene action."""
    if action.get("type") != "git_rm_cached":
        raise CdxCareError("unsupported git action", code="unsupported_action")
    paths_value = action.get("paths")
    if not isinstance(paths_value, list):
        raise CdxCareError("git action paths must be strings", code="invalid_plan")
    paths = string_list(paths_value, "paths")
    message = str(action.get("commit_message", "Untrack Codex memory Finder metadata"))
    result = git_untrack_and_commit(Path(str(action["repo"])), paths, message, complete_paths=sorted(DS_STORE_PATHS))
    return {"id": str(action["id"]), "type": "git_rm_cached", "lane": str(action["lane"]), "result": result}
