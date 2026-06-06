"""Command line interface for cdx-care."""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import NoReturn

from cdx_care import VERSION
from cdx_care.apply import apply_plan, load_plan
from cdx_care.diagnostics import blank_page_pack
from cdx_care.doctor import doctor_report
from cdx_care.envelope import success_envelope
from cdx_care.errors import CdxCareError
from cdx_care.filesystem import ensure_private_dir
from cdx_care.paths import DB_RELATIVE_PATHS, StorePaths, store_paths
from cdx_care.plan import generate_plan, write_plan
from cdx_care.raw_sql import raw_sql_readonly
from cdx_care.support_paths import preflight_managed_artifact_path
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


def build_parser() -> ArgumentParser:
    """Build the cdx-care argument parser."""
    parser = CdxCareArgumentParser(
        prog="cdx-care", description="JSON-first local Codex DB/state doctor and guarded reconciler."
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON errors as well as successes.")
    parser.add_argument("--codex-home", help="Override Codex support root. Defaults to ~/.codex.")
    parser.add_argument("--version", action="version", version=f"cdx-care {VERSION}")

    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=CdxCareArgumentParser)
    doctor_parser = subparsers.add_parser("doctor", help="Read-only state and DB health report.")
    doctor_parser.add_argument("--details", action="store_true", help="Include bounded row arrays and lsof handles.")
    doctor_parser.add_argument("--limit", type=int, default=50, help="Maximum rows per detailed doctor list.")

    plan_parser = subparsers.add_parser("plan", help="Create a reviewable reconciliation plan.")
    plan_parser.add_argument("--profile", default="workstation", choices=["workstation"])
    plan_parser.add_argument("--out", type=Path, required=True, help="Plan output JSON path.")

    apply_parser = subparsers.add_parser("apply", help="Apply a previously generated plan.")
    apply_parser.add_argument("--plan", type=Path, required=True, help="Plan JSON path.")

    run_parser = subparsers.add_parser("run", help="Generate and apply the approved workstation policy.")
    run_parser.add_argument("--profile", default="workstation", choices=["workstation"])
    run_parser.add_argument(
        "--apply-approved", action="store_true", help="Acknowledge Codex is closed and apply gates may run."
    )

    diagnose_parser = subparsers.add_parser("diagnose", help="Write read-only evidence packs.")
    diagnose_subparsers = diagnose_parser.add_subparsers(
        dest="diagnostic", required=True, parser_class=CdxCareArgumentParser
    )
    blank_parser = diagnose_subparsers.add_parser("blank-page", help="Collect blank automations page evidence.")
    blank_parser.add_argument("--out-dir", type=Path, required=True)

    raw_parser = subparsers.add_parser("raw", help="Low-level read-only escape hatches.")
    raw_subparsers = raw_parser.add_subparsers(dest="raw_command", required=True, parser_class=CdxCareArgumentParser)
    sql_parser = raw_subparsers.add_parser("sql", help="Execute one read-only SQL statement from a file.")
    sql_parser.add_argument("--db", required=True, choices=sorted(DB_RELATIVE_PATHS))
    sql_parser.add_argument("--query-file", type=Path, required=True)
    sql_parser.add_argument("--readonly", action="store_true", required=True)
    sql_parser.add_argument("--limit", type=int, default=200)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run cdx-care."""
    argv_list = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv_list)
    except CdxCareUsageError as error:
        return render_usage_error(parser, "--json" in argv_list, error.message, argv_list)
    try:
        stores = store_paths(args.codex_home)
        result = dispatch(args, stores)
    except CdxCareError as error:
        return render_error(args, error.code, str(error), details=error.details)
    except json.JSONDecodeError as error:
        return render_error(args, "invalid_json", str(error))
    except sqlite3.Error as error:
        return render_error(args, "sqlite_error", str(error))
    except OSError as error:
        return render_error(args, "os_error", str(error))
    except (KeyError, TypeError) as error:
        return render_error(args, "invalid_plan", str(error))
    return render_result(args, result)


def dispatch(args: Namespace, stores: StorePaths) -> JsonObject:
    """Dispatch a parsed command."""
    command = str(args.command)
    if command == "doctor":
        return command_doctor(args, stores)
    if command == "plan":
        return command_plan(args, stores)
    if command == "apply":
        return command_apply(args, stores)
    if command == "run":
        return command_run(args, stores)
    if command == "diagnose":
        return command_diagnose(args, stores)
    if command == "raw":
        return command_raw(args, stores)
    raise CdxCareError(f"unsupported command: {command}", code="unsupported_command")


def command_doctor(args: Namespace, stores: StorePaths) -> JsonObject:
    """Return doctor report with CLI recovery metadata."""
    report = doctor_report(stores)
    detail_limit = int(args.limit)
    if detail_limit < 1 or detail_limit > 500:
        raise CdxCareError("doctor --limit must be between 1 and 500", code="invalid_limit")
    report["command"] = "doctor"
    report["codex_home_source"] = "flag" if getattr(args, "codex_home", None) else "default"
    report["invocation_path"] = shutil.which("cdx-care")
    lsof = report.get("lsof")
    report["dependencies"] = {"lsof": {"available": lsof.get("available") if isinstance(lsof, dict) else None}}
    report["next_commands"] = doctor_next_commands(report)
    report["details"] = {"included": bool(args.details), "limit": detail_limit if bool(args.details) else 0}
    return prune_doctor_details(report, include_details=bool(args.details), limit=detail_limit)


def doctor_next_commands(report: JsonObject) -> list[str]:
    """Return concise next operator commands for the current doctor state."""
    lsof = report.get("lsof")
    if isinstance(lsof, dict) and lsof.get("available") is False:
        return ["Restore lsof visibility, then rerun: cdx-care --json doctor"]
    if not bool(report.get("ok")):
        return ["Fix error findings, then rerun: cdx-care --json doctor"]
    if not bool(report.get("codex_closed")):
        return [
            "For review only: cdx-care --json plan --profile workstation --out /tmp/cdx-care-plan.json",
            "Quit Codex before any: cdx-care --json apply --plan /tmp/cdx-care-plan.json",
        ]
    return [
        "Review a fresh plan: cdx-care --json plan --profile workstation --out /tmp/cdx-care-plan.json",
        "Apply only after review: cdx-care --json apply --plan /tmp/cdx-care-plan.json",
    ]


def command_plan(args: Namespace, stores: StorePaths) -> JsonObject:
    """Generate and write a plan."""
    plan = generate_plan(stores, str(args.profile))
    out_path = require_path(args, "out")
    write_plan(plan, out_path)
    plan["plan_path"] = str(out_path)
    return plan


def command_apply(args: Namespace, stores: StorePaths) -> JsonObject:
    """Apply a plan file."""
    plan_path = require_path(args, "plan")
    plan = load_plan(plan_path)
    try:
        receipt = apply_plan(stores, plan)
    except CdxCareError as error:
        raise enrich_error(
            error,
            {"plan_path": str(plan_path), "run_id": string_or_empty(plan.get("run_id"))},
        ) from error
    receipt["plan_path"] = str(plan_path)
    return receipt


def command_run(args: Namespace, stores: StorePaths) -> JsonObject:
    """Generate and apply the default approved policy."""
    if not bool(args.apply_approved):
        raise CdxCareError("run requires --apply-approved; use plan for read-only preview", code="apply_not_approved")
    plan = generate_plan(stores, str(args.profile))
    plan_root = stores.care_root / "plans"
    plan_path = plan_root / f"{plan['run_id']}.json"
    preflight_managed_artifact_path(stores, plan_path, "plan file")
    ensure_private_dir(plan_root)
    write_plan(plan, plan_path)
    try:
        receipt = apply_plan(stores, plan)
    except CdxCareError as error:
        raise enrich_error(error, {"plan_path": str(plan_path), "run_id": str(plan["run_id"])}) from error
    return {
        "schema_version": 1,
        "tool": "cdx-care",
        "version": VERSION,
        "ok": True,
        "run_id": str(plan["run_id"]),
        "support_root": str(stores.codex_home),
        "codex_closed": receipt.get("codex_closed", False),
        "plan_path": str(plan_path),
        "planned_actions": plan["planned_actions"],
        "applied_actions": receipt["applied_actions"],
        "denials": plan["denials"],
        "next_commands": receipt["next_commands"],
        "receipt_path": receipt["receipt_path"],
    }


def command_diagnose(args: Namespace, stores: StorePaths) -> JsonObject:
    """Run a diagnostic command."""
    diagnostic = str(args.diagnostic)
    if diagnostic == "blank-page":
        return blank_page_pack(stores, require_path(args, "out_dir"))
    raise CdxCareError(f"unsupported diagnostic: {diagnostic}", code="unsupported_diagnostic")


def command_raw(args: Namespace, stores: StorePaths) -> JsonObject:
    """Run a raw read-only command."""
    raw_command = str(args.raw_command)
    if raw_command != "sql":
        raise CdxCareError(f"unsupported raw command: {raw_command}", code="unsupported_raw_command")
    if not bool(args.readonly):
        raise CdxCareError("raw sql requires --readonly", code="raw_sql_requires_readonly")
    limit = int(args.limit)
    if limit < 1 or limit > 5000:
        raise CdxCareError("raw sql --limit must be between 1 and 5000", code="invalid_limit")
    return raw_sql_readonly(stores, str(args.db), require_path(args, "query_file"), limit)


def require_path(args: Namespace, name: str) -> Path:
    """Read a required Path attribute from argparse."""
    value = getattr(args, name)
    if not isinstance(value, Path):
        raise CdxCareError(f"missing path argument: --{name.replace('_', '-')}", code="missing_path")
    return value


def render_result(args: Namespace, result: JsonObject) -> int:
    """Render a successful command."""
    _ = args
    print(json.dumps(success_envelope(result), indent=2, sort_keys=True))
    return 0


def render_error(
    args: Namespace, code: str, message: str, *, exit_code: int = 1, details: JsonObject | None = None
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
        payload["support_root"] = str(store_paths(args.codex_home).codex_home)
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
    if code in {"output_exists", "receipt_exists"}:
        return "Choose a new output path or inspect the existing file before retrying."
    if code in {"support_root_missing", "support_root_invalid", "support_root_unrecognized", "unsafe_support_path"}:
        return "Check --codex-home points at a real Codex support root and rerun cdx-care --json doctor."
    if code == "run_id_reused":
        return "Generate a fresh plan; reused plan run_ids are denied."
    if code in {"state_threads_unavailable", "schema_changed", "db_changed", "row_drift", "row_not_eligible"}:
        return "Rerun cdx-care --json doctor and generate a fresh plan from current DB state."
    return "Fix the reported issue, then rerun the same command."


if __name__ == "__main__":
    raise SystemExit(main())
