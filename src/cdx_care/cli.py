"""Command line interface for cdx-care."""

from __future__ import annotations

import json
import shlex
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
from cdx_care.types import JsonObject, require_json_object_list

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

    prep_parser = subparsers.add_parser(
        "prep",
        help="Pre-scan, write a private managed plan, and print the exact apply command.",
    )
    prep_parser.add_argument(
        "--profile",
        default="workstation",
        choices=PLAN_PROFILES,
        help="Policy profile: workstation hides broken rows; clear-current-badge also marks valid review runs read.",
    )
    prep_parser.add_argument(
        "--out",
        type=Path,
        help="Optional plan output JSON path. Defaults to ~/.codex/cdx-care/plans/<run_id>.json.",
    )

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
            "Policy profile. workstation is the default one-shot profile; "
            "clear-current-badge also requires --manual-clear-current-badge."
        ),
    )
    run_parser.add_argument(
        "--apply-approved", action="store_true", help="Acknowledge Codex is closed and apply gates may run."
    )
    run_parser.add_argument(
        "--manual-clear-current-badge",
        action="store_true",
        help=(
            "Second explicit acknowledgement for one-shot clear-current-badge. "
            "Marks current valid automation review rows read after the same closed-Codex apply gates."
        ),
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
    if command == "prep":
        return command_prep(args, stores)
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
            commands.append("Badge clear pre-scan: cdx-care --json prep --profile clear-current-badge")
        return commands
    if not bool(report.get("codex_closed")):
        commands = [
            "Conservative pre-scan (does not clear valid badge rows): cdx-care --json prep --profile workstation",
            "Quit Codex before running the apply_command returned by prep.",
        ]
    else:
        commands = [
            "Conservative pre-scan (does not clear valid badge rows): cdx-care --json prep --profile workstation",
            "Apply only after review: run the apply_command returned by prep.",
        ]
    if badge_count > 0:
        commands.insert(0, "Badge clear pre-scan: cdx-care --json prep --profile clear-current-badge")
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


def command_prep(args: Namespace, stores: StorePaths) -> JsonObject:
    """Generate a private plan and return a compact operator handoff."""
    plan = generate_plan(stores, str(args.profile))
    out_path = optional_path(args, "out")
    plan_path = out_path if out_path else managed_plan_path(stores, plan)
    if not out_path:
        preflight_managed_artifact_path(stores, plan_path, "plan file")
        ensure_private_dir(plan_path.parent)
    write_plan(plan, plan_path)
    actions = require_json_object_list(plan["planned_actions"], "planned_actions")
    denials = require_json_object_list(plan["denials"], "denials")
    action_count_value = plan.get("action_count")
    action_count = action_count_value if isinstance(action_count_value, int) else len(actions)
    codex_closed = bool(plan.get("codex_closed"))
    apply_command = f"cdx-care --json apply --plan {shlex.quote(str(plan_path))}"
    operator_status = prep_operator_status(action_count=action_count, codex_closed=codex_closed)
    return {
        "schema_version": 1,
        "tool": "cdx-care",
        "version": VERSION,
        "command": "prep",
        "ok": True,
        "run_id": str(plan["run_id"]),
        "support_root": str(stores.codex_home),
        "profile": str(plan["profile"]),
        "approved_policy": str(plan["approved_policy"]),
        "codex_closed": codex_closed,
        "plan_path": str(plan_path),
        "action_count": action_count,
        "action_summary": summarize_actions(actions),
        "denial_count": len(denials),
        "denial_summary": summarize_denials(denials),
        "safe_to_apply_now": operator_status == "ready_to_apply",
        "operator_status": operator_status,
        "operator_message": prep_operator_message(operator_status),
        "apply_command": apply_command,
        "next_commands": prep_next_commands(operator_status, apply_command, str(plan["profile"])),
        "receipt_path": None,
    }


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
        if not bool(args.apply_approved):
            raise CdxCareError(
                "clear-current-badge one-shot requires --apply-approved and --manual-clear-current-badge",
                code="manual_profile_requires_approval",
            )
        if not bool(args.manual_clear_current_badge):
            raise CdxCareError(
                "clear-current-badge one-shot requires --manual-clear-current-badge",
                code="manual_profile_requires_acknowledgement",
            )
    if not bool(args.apply_approved):
        raise CdxCareError(
            "run requires --apply-approved; use prep for the review-first workflow",
            code="apply_not_approved",
        )
    plan = generate_plan(stores, str(args.profile))
    plan_path = managed_plan_path(stores, plan)
    preflight_managed_artifact_path(stores, plan_path, "plan file")
    ensure_private_dir(plan_path.parent)
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
        "profile": str(plan["profile"]),
        "approved_policy": str(plan["approved_policy"]),
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


def optional_path(args: Namespace, name: str) -> Path | None:
    """Read an optional Path attribute from argparse."""
    value = getattr(args, name, None)
    if value is None:
        return None
    if not isinstance(value, Path):
        raise CdxCareError(f"invalid path argument: --{name.replace('_', '-')}", code="missing_path")
    return value


def managed_plan_path(stores: StorePaths, plan: JsonObject) -> Path:
    """Return the private managed plan path for a generated plan."""
    return stores.care_root / "plans" / f"{plan['run_id']}.json"


def summarize_actions(actions: list[JsonObject]) -> list[JsonObject]:
    """Summarize planned actions by lane/type/db for operator review."""
    counts: dict[tuple[str, str, str], int] = {}
    for action in actions:
        key = (stringish(action.get("lane")), stringish(action.get("type")), stringish(action.get("db")))
        counts[key] = counts.get(key, 0) + 1
    return [
        {"lane": lane, "type": action_type, "db": db_name or None, "count": count}
        for (lane, action_type, db_name), count in sorted(counts.items())
    ]


def summarize_denials(denials: list[JsonObject]) -> list[JsonObject]:
    """Summarize denials by stable code for compact pre-scan output."""
    counts: dict[tuple[str, str], int] = {}
    for denial in denials:
        code = stringish(denial.get("code"))
        reason = stringish(denial.get("message")) or stringish(denial.get("reason"))
        key = (code, reason)
        counts[key] = counts.get(key, 0) + 1
    return [{"code": code, "reason": reason, "count": count} for (code, reason), count in sorted(counts.items())]


def prep_operator_status(*, action_count: int, codex_closed: bool) -> str:
    """Classify the pre-scan outcome for humans and scripts."""
    if action_count == 0:
        return "nothing_to_apply"
    if not codex_closed:
        return "quit_codex_then_apply"
    return "ready_to_apply"


def prep_operator_message(operator_status: str) -> str:
    """Return one compact human message for a pre-scan status."""
    if operator_status == "nothing_to_apply":
        return "No planned write actions. Keep the plan for evidence or rerun prep after Codex state changes."
    if operator_status == "quit_codex_then_apply":
        return "Pre-scan complete, but Codex still has DB handles. Quit Codex completely before apply."
    return "Pre-scan complete. Review action_summary/denial_summary, then run apply_command if approved."


def prep_next_commands(operator_status: str, apply_command: str, profile: str) -> list[str]:
    """Return a tiny copy-paste path after prep."""
    if operator_status == "nothing_to_apply":
        return ["cdx-care --json prep --profile workstation"]
    if operator_status == "quit_codex_then_apply":
        return ["Quit Codex completely.", apply_command]
    if profile == "clear-current-badge":
        return ["Review this manual badge-clear plan.", apply_command]
    return [apply_command]


def stringish(value: object) -> str:
    """Return a string value or an empty string."""
    return value if isinstance(value, str) else ""


if __name__ == "__main__":
    raise SystemExit(main())
