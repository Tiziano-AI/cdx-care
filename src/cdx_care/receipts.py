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
        "next_commands": post_apply_next_commands(stores),
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


def post_apply_next_commands(stores: StorePaths) -> list[str]:
    """Return machine-readable post-write proof steps."""
    root = shlex.quote(str(stores.codex_home))
    return [
        f"cdx-care --json --codex-home {root} doctor",
        "Restart Codex and verify the app badge, automations review list, blank-page case, and memory jobs in the UI.",
    ]


def path_present(path: Path) -> bool:
    """Return true for an existing path or symlink."""
    return path.exists() or path.is_symlink()
