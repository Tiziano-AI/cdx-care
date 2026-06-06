"""Command line interface for cdx-care."""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from argparse import ArgumentParser, Namespace
from pathlib import Path

from cdx_care import VERSION
from cdx_care.apply import apply_plan, load_plan
from cdx_care.cli_output import (
    CdxCareArgumentParser,
    CdxCareUsageError,
    enrich_error,
    prune_doctor_details,
    render_error,
    render_result,
    render_usage_error,
    string_or_empty,
)
from cdx_care.diagnostics import blank_page_pack
from cdx_care.doctor import doctor_report
from cdx_care.errors import CdxCareError
from cdx_care.filesystem import ensure_private_dir
from cdx_care.paths import DB_RELATIVE_PATHS, StorePaths, store_paths
from cdx_care.plan import generate_plan, write_plan
from cdx_care.raw_sql import raw_sql_readonly
from cdx_care.support_paths import preflight_managed_artifact_path
from cdx_care.types import JsonObject

PLAN_PROFILES = ("workstation", "clear-current-badge")


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
    plan_parser.add_argument(
        "--profile",
        default="workstation",
        choices=PLAN_PROFILES,
        help="Policy profile: workstation hides broken rows; clear-current-badge also marks valid review runs read.",
    )
    plan_parser.add_argument("--out", type=Path, required=True, help="Plan output JSON path.")

    apply_parser = subparsers.add_parser("apply", help="Apply a previously generated plan.")
    apply_parser.add_argument("--plan", type=Path, required=True, help="Plan JSON path.")

    run_parser = subparsers.add_parser("run", help="Generate and apply an approved profile after Codex is closed.")
    run_parser.add_argument(
        "--profile",
        default="workstation",
        choices=PLAN_PROFILES,
        help=(
            "Policy profile. workstation is the only one-shot profile; "
            "clear-current-badge is review-first and denied."
        ),
    )
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
    return render_result(result)


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
    badge_count = current_badge_count(report)
    lsof = report.get("lsof")
    if isinstance(lsof, dict) and lsof.get("available") is False:
        return ["Restore lsof visibility, then rerun: cdx-care --json doctor"]
    if not bool(report.get("ok")):
        commands = ["Fix error findings, then rerun: cdx-care --json doctor"]
        if badge_count > 0:
            commands.append(
                "Explicit badge clear after Codex is closed: cdx-care --json plan "
                "--profile clear-current-badge --out /tmp/cdx-care-clear-badge-plan.json"
            )
        return commands
    if not bool(report.get("codex_closed")):
        commands = [
            "For review only: cdx-care --json plan --profile workstation --out /tmp/cdx-care-plan.json",
            "Quit Codex before any: cdx-care --json apply --plan /tmp/cdx-care-plan.json",
        ]
    else:
        commands = [
            "Review a fresh plan: cdx-care --json plan --profile workstation --out /tmp/cdx-care-plan.json",
            "Apply only after review: cdx-care --json apply --plan /tmp/cdx-care-plan.json",
        ]
    if badge_count > 0:
        commands.append(
            "Explicit badge clear: cdx-care --json plan --profile clear-current-badge "
            "--out /tmp/cdx-care-clear-badge-plan.json"
        )
    return commands


def current_badge_count(report: JsonObject) -> int:
    """Return the current actionable automation badge count when present."""
    codex_dev = report.get("codex_dev")
    if not isinstance(codex_dev, dict):
        return 0
    runs = codex_dev.get("automation_runs")
    if not isinstance(runs, dict):
        return 0
    count = runs.get("unread_actionable_count")
    return count if isinstance(count, int) else 0


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
    if str(args.profile) == "clear-current-badge":
        raise CdxCareError(
            "clear-current-badge is review-first; run plan --profile clear-current-badge, inspect it, then apply",
            code="manual_profile_requires_plan_review",
        )
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


if __name__ == "__main__":
    raise SystemExit(main())
