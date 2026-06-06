"""Apply cdx-care plans with backups, drift checks, and receipts."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from cdx_care import VERSION
from cdx_care.db_apply import apply_db_actions, group_db_actions, preflight_db_actions
from cdx_care.doctor import load_state_thread_ids_checked
from cdx_care.errors import CdxCareError
from cdx_care.filesystem import ensure_private_dir
from cdx_care.git_tools import git_hygiene_preflight, git_rm_cached
from cdx_care.paths import StorePaths
from cdx_care.policy import ApplyContext, admitted_db_paths, validate_action_targets
from cdx_care.policy_checks import string_list
from cdx_care.processes import lsof_handles
from cdx_care.receipts import failure_receipt, path_present, post_apply_next_commands, write_receipt_file
from cdx_care.sqlite_tools import copy_db_family, file_sha256
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
    db_groups = group_db_actions(actions)
    for db_path, db_actions in db_groups.items():
        preflight_db_actions(db_path, db_actions, context)
    git_preflights = preflight_git_actions(actions)
    ensure_private_dir(receipt_root)
    backups: list[JsonObject] = []
    applied: list[JsonValue] = []
    mutation_started = False
    try:
        backups = backup_dbs(db_paths, backup_root)
        for db_path, db_actions in db_groups.items():
            db_applied = apply_db_actions(db_path, db_actions, context)
            applied.extend(db_applied)
            mutation_started = mutation_started or bool(db_applied)
        for action in actions:
            if action.get("type") == "git_rm_cached":
                applied.append(apply_git_action(action))
                mutation_started = True
    except (CdxCareError, OSError, sqlite3.Error) as error:
        if not backups:
            backups = discover_backup_files(backup_root)
        failed_receipt: JsonObject | None = None
        if backups or mutation_started or path_present(backup_root):
            failed_receipt = failure_receipt(
                stores,
                run_id,
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
        "applied_at": iso_now(),
        "support_root": str(stores.codex_home),
        "codex_closed": True,
        "planned_actions": actions,
        "plan_action_count": len(actions),
        "backups": backups,
        "git_preflights": git_preflights,
        "applied_actions": applied,
        "next_commands": post_apply_next_commands(stores),
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


def as_action_list(value: JsonValue | None) -> list[JsonObject]:
    """Validate and return a planned action list."""
    try:
        return require_json_object_list(value, "planned_actions")
    except TypeError as error:
        raise CdxCareError("plan planned_actions must be a list", code="invalid_plan") from error


def apply_context(stores: StorePaths, actions: list[JsonObject]) -> ApplyContext:
    """Build apply-time context, failing closed when read-state proof is unavailable."""
    read_state_lanes = {"automations.hide_broken_only", "inbox.orphan_mark_read"}
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
            rows.append(git_hygiene_preflight(Path(str(action["repo"])), string_list(paths_value, "paths")))
    return rows


def apply_git_action(action: JsonObject) -> JsonObject:
    """Apply a git hygiene action."""
    if action.get("type") != "git_rm_cached":
        raise CdxCareError("unsupported git action", code="unsupported_action")
    paths_value = action.get("paths")
    if not isinstance(paths_value, list):
        raise CdxCareError("git action paths must be strings", code="invalid_plan")
    paths = string_list(paths_value, "paths")
    result = git_rm_cached(Path(str(action["repo"])), paths, require_exact_tracked=True)
    return {"id": str(action["id"]), "type": "git_rm_cached", "lane": str(action["lane"]), "result": result}
