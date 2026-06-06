"""Apply receipt builders and exact-once receipt writes."""

from __future__ import annotations

import json
import os
import shlex
import sqlite3
from pathlib import Path

from cdx_care import VERSION
from cdx_care.errors import CdxCareError
from cdx_care.filesystem import ensure_private_dir
from cdx_care.paths import StorePaths
from cdx_care.timeutil import iso_now
from cdx_care.types import JsonObject, JsonValue


def failure_receipt(
    stores: StorePaths,
    run_id: str,
    profile: str,
    approved_policy: str,
    actions: list[JsonObject],
    backups: list[JsonObject],
    applied: list[JsonValue],
    git_preflights: list[JsonObject],
    error: CdxCareError | OSError | sqlite3.Error,
    *,
    partial: bool,
) -> JsonObject:
    """Build a receipt for a partial mutation failure."""
    code = error.code if isinstance(error, CdxCareError) else error.__class__.__name__
    return {
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
        "backup_root": str(stores.care_root / "backups" / run_id),
        "backup_root_present": path_present(stores.care_root / "backups" / run_id),
        "backups": backups,
        "git_preflights": git_preflights,
        "applied_actions": applied,
        "partial": partial,
        "next_commands": post_apply_next_commands(stores, actions, applied),
        "ok": False,
        "error": {"code": code, "message": str(error)},
    }


def write_receipt_file(receipt_path: Path, receipt: JsonObject) -> None:
    """Write a receipt exactly once."""
    if receipt_path.exists() or receipt_path.is_symlink():
        raise CdxCareError(f"receipt already exists: {receipt_path}", code="receipt_exists")
    ensure_private_dir(receipt_path.parent)
    fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


def post_apply_next_commands(
    stores: StorePaths, planned_actions: list[JsonObject], applied_actions: list[JsonValue]
) -> list[str]:
    """Return machine-readable post-write proof steps."""
    root = shlex.quote(str(stores.codex_home))
    commands = [f"cdx-care --json --codex-home {root} doctor"]
    planned_lanes = action_lanes(planned_actions)
    applied_lanes = action_lanes(applied_actions)
    if "automations.clear_current_badge" in applied_lanes:
        commands.append("Restart Codex and verify the app badge and automations review list in the UI.")
    elif "automations.clear_current_badge" in planned_lanes:
        commands.append(
            "Badge-clear did not complete; inspect the receipt, rerun doctor, then generate a fresh "
            "clear-current-badge plan."
        )
    else:
        commands.append(
            "This run did not clear valid automation badge rows; if the badge is still the target, run "
            "cdx-care --json prep --profile clear-current-badge."
        )
        commands.append(
            "Verify only the lanes applied by this receipt, such as sessions, logs, memory, or git hygiene."
        )
    return commands


def action_lanes(actions: list[JsonObject] | list[JsonValue]) -> set[str]:
    """Return stable lane names from planned or applied action objects."""
    lanes: set[str] = set()
    for action in actions:
        if isinstance(action, dict):
            lane = action.get("lane")
            if isinstance(lane, str):
                lanes.add(lane)
    return lanes


def path_present(path: Path) -> bool:
    """Return true for an existing path or symlink."""
    return path.exists() or path.is_symlink()
