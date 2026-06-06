"""CLI rendering and detail-pruning helpers."""

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser
from typing import NoReturn

from cdx_care import VERSION
from cdx_care.envelope import success_envelope
from cdx_care.errors import CdxCareError
from cdx_care.paths import store_paths
from cdx_care.types import JsonObject, JsonValue


class CdxCareUsageError(RuntimeError):
    """Argparse usage error that can be rendered as JSON when requested."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class CdxCareArgumentParser(ArgumentParser):
    """ArgumentParser that does not print plain text before JSON rendering."""

    def error(self, message: str) -> NoReturn:
        raise CdxCareUsageError(message)


def render_result(result: JsonObject) -> int:
    """Render a successful command."""
    print(json.dumps(success_envelope(result), indent=2, sort_keys=True))
    return 0


def render_error(
    args: object, code: str, message: str, *, exit_code: int = 1, details: JsonObject | None = None
) -> int:
    """Render a command error."""
    error_obj: JsonObject = {"code": code, "message": message, "next_step": next_step_for_error(code)}
    if details:
        error_obj["details"] = details
    payload: JsonObject = {
        "schema_version": 1,
        "ok": False,
        "tool": "cdx-care",
        "version": VERSION,
        "command": str(getattr(args, "command", "")),
        "error": error_obj,
    }
    if details:
        for key in ("run_id", "plan_path", "receipt_path", "partial", "backup_root_present"):
            if key in details:
                payload[key] = details[key]
    if hasattr(args, "codex_home"):
        codex_home = vars(args).get("codex_home")
        payload["support_root"] = str(store_paths(codex_home if isinstance(codex_home, str) else None).codex_home)
    if bool(getattr(args, "json", False)):
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"cdx-care error [{code}]: {message}", file=sys.stderr)
    return exit_code


def render_usage_error(parser: ArgumentParser, wants_json: bool, message: str, argv: list[str]) -> int:
    """Render argparse errors without losing the JSON contract."""
    metadata = usage_metadata(argv)
    payload: JsonObject = {
        "schema_version": 1,
        "ok": False,
        "tool": "cdx-care",
        "version": VERSION,
        "error": {"code": "usage_error", "message": message, "next_step": "Run cdx-care --help for valid syntax."},
    }
    payload.update(metadata)
    if wants_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        parser.print_usage(sys.stderr)
        print(f"cdx-care error [usage_error]: {message}", file=sys.stderr)
    return 2


def usage_metadata(argv: list[str]) -> JsonObject:
    """Best-effort global context for JSON usage errors before argparse succeeds."""
    codex_home: str | None = None
    command = ""
    skip_next = False
    for index, item in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if item == "--codex-home":
            if index + 1 < len(argv):
                codex_home = argv[index + 1]
                skip_next = True
            continue
        if item.startswith("--codex-home="):
            codex_home = item.split("=", 1)[1]
            continue
        if item.startswith("-"):
            continue
        command = item
        break
    metadata: JsonObject = {"support_root": str(store_paths(codex_home).codex_home)}
    if command:
        metadata["command"] = command
    return metadata


def prune_doctor_details(report: JsonObject, *, include_details: bool, limit: int) -> JsonObject:
    """Omit bulky row arrays from default doctor output while preserving counts and metadata."""
    pruned = prune_json_details(report, include_details=include_details, limit=limit)
    if not isinstance(pruned, dict):
        raise CdxCareError("doctor report must be a JSON object", code="runtime_error")
    return pruned


def prune_json_details(value: JsonValue, *, include_details: bool, limit: int) -> JsonValue:
    """Recursively prune row/detail arrays from a doctor report."""
    if isinstance(value, list):
        return [prune_json_details(item, include_details=include_details, limit=limit) for item in value]
    if not isinstance(value, dict):
        return value
    result: JsonObject = {}
    handled_meta: set[str] = set()
    for key, item in value.items():
        if key in handled_meta:
            continue
        if key.endswith("_rows") and isinstance(item, list):
            meta_key = f"{key}_meta"
            meta = value.get(meta_key)
            total = (
                int(meta["total_count"])
                if isinstance(meta, dict) and isinstance(meta.get("total_count"), int)
                else len(item)
            )
            if include_details:
                limited = item[:limit]
                result[key] = [
                    prune_json_details(row, include_details=include_details, limit=limit) for row in limited
                ]
                result[meta_key] = row_meta(meta, total=total, returned=len(limited), limit=limit, omitted=False)
            else:
                result[meta_key] = row_meta(meta, total=total, returned=0, limit=0, omitted=True)
            handled_meta.add(meta_key)
            continue
        if key == "handles" and isinstance(item, list):
            result["handle_count"] = len(item)
            if include_details:
                result[key] = item[:limit]
            continue
        result[key] = prune_json_details(item, include_details=include_details, limit=limit)
    return result


def row_meta(meta: object, *, total: int, returned: int, limit: int, omitted: bool) -> JsonObject:
    """Update row metadata after CLI detail pruning."""
    result = dict(meta) if isinstance(meta, dict) else {}
    result["limit"] = limit
    result["returned_count"] = returned
    result["total_count"] = total
    result["truncated"] = total > returned
    if omitted:
        result["details_omitted"] = True
        result["next_command"] = "Rerun cdx-care --json doctor --details --limit 500 for bounded row details."
    elif total > returned:
        result["next_command"] = "Rerun cdx-care --json doctor --details --limit 500 or use raw sql to narrow rows."
    return result


def enrich_error(error: CdxCareError, details: JsonObject) -> CdxCareError:
    """Return the same error code/message with additional safe CLI recovery metadata."""
    merged = dict(details)
    merged.update(error.details)
    return CdxCareError(str(error), code=error.code, details=merged)


def string_or_empty(value: JsonValue | None) -> str:
    """Return a string JSON value or an empty marker."""
    return value if isinstance(value, str) else ""


def next_step_for_error(code: str) -> str:
    """Map stable error codes to concise recovery guidance."""
    if code == "codex_db_handles_open":
        return "Quit Codex, rerun cdx-care --json doctor until codex_closed is true, then rerun apply."
    if code == "lsof_unavailable":
        return "Install or restore lsof visibility; cdx-care refuses DB writes without the handle check."
    if code == "apply_not_approved":
        return "Use plan for read-only preview, or pass --apply-approved only after review and after quitting Codex."
    if code == "manual_profile_requires_plan_review":
        return (
            "Run cdx-care --json plan --profile clear-current-badge --out /tmp/cdx-care-clear-badge-plan.json, "
            "inspect planned_actions and denials, quit Codex until doctor reports codex_closed true, "
            "then apply that exact reviewed plan with cdx-care --json apply --plan /tmp/cdx-care-clear-badge-plan.json."
        )
    if code in {"output_exists", "receipt_exists"}:
        return "Choose a new output path or inspect the existing file before retrying."
    if code in {"support_root_missing", "support_root_invalid", "support_root_unrecognized", "unsafe_support_path"}:
        return "Check --codex-home points at a real Codex support root and rerun cdx-care --json doctor."
    if code == "run_id_reused":
        return "Generate a fresh plan; reused plan run_ids are denied."
    if code in {"state_threads_unavailable", "schema_changed", "db_changed", "row_drift", "row_not_eligible"}:
        return "Rerun cdx-care --json doctor and generate a fresh plan from current DB state."
    return "Fix the reported issue, then rerun the same command."
