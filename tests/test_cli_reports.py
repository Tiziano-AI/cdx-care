"""CLI, doctor, diagnostics, and raw SQL contract tests."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from cdx_care_fixtures import fake_lsof_unavailable, make_fixture

import cdx_care.apply as apply_module
import cdx_care.cli as cli_module
import cdx_care.doctor as doctor_module
from cdx_care.doctor import doctor_report
from cdx_care.paths import StorePaths
from cdx_care.plan import generate_plan
from cdx_care.types import require_json_object, require_json_object_list


def run_cli_json(argv: list[str]) -> tuple[int, dict[str, object], str]:
    """Run the public CLI in-process and return parsed JSON plus stderr."""
    out = StringIO()
    err = StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        exit_code = cli_module.main(argv)
    payload = json.loads(out.getvalue())
    if not isinstance(payload, dict):
        raise AssertionError("CLI payload must be a JSON object")
    return exit_code, payload, err.getvalue()


def contains_key(value: object, key: str) -> bool:
    """Return whether a nested JSON-like value contains a key."""
    if isinstance(value, dict):
        return key in value or any(contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(contains_key(item, key) for item in value)
    return False


class CdxCareCliReportTest(unittest.TestCase):
    """Verify cdx-care report and CLI behavior on disposable state."""

    def test_doctor_reports_lsof_unavailable_as_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            original = doctor_module.lsof_handles
            doctor_module.lsof_handles = fake_lsof_unavailable
            try:
                exit_code, payload, stderr = run_cli_json(
                    ["--json", "--codex-home", str(stores.codex_home), "doctor"]
                )
            finally:
                doctor_module.lsof_handles = original

            findings = require_json_object_list(payload["findings"], "findings")
            self.assertEqual(0, exit_code)
            self.assertFalse(payload["ok"])
            self.assertFalse(payload["codex_closed"])
            self.assertIn("cdx-care.lsof.unavailable", {str(row["code"]) for row in findings})
            next_commands = payload["next_commands"]
            if not isinstance(next_commands, list):
                raise AssertionError("next_commands must be a list")
            self.assertIn("lsof", str(next_commands[0]))
            self.assertEqual("", stderr)

    def test_doctor_marks_state_dependent_rows_unknown_when_threads_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            stores.db_path("state").unlink()

            report = doctor_report(stores)
            findings = require_json_object_list(report["findings"], "findings")
            codex_dev = require_json_object(report["codex_dev"], "codex_dev")
            runs = require_json_object(codex_dev["automation_runs"], "automation_runs")
            inbox = require_json_object(codex_dev["inbox"], "inbox")

            self.assertFalse(report["ok"])
            self.assertIn("codex.state_threads.unavailable", {str(row["code"]) for row in findings})
            self.assertFalse(codex_dev["state_threads_available"])
            self.assertEqual(1, runs["broken_unread_count"])
            self.assertEqual(3, runs["state_unknown_unread_count"])
            self.assertEqual(0, inbox["orphan_unread_count"])
            unknown_count = inbox["orphan_detection_unknown_unread_count"]
            if not isinstance(unknown_count, int):
                raise AssertionError("unknown orphan count must be an integer")
            self.assertGreater(unknown_count, 0)

    def test_doctor_reports_missing_support_root_as_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = StorePaths(Path(tmp) / "missing-codex-home")

            report = doctor_report(stores)
            findings = require_json_object_list(report["findings"], "findings")
            codes = {str(row["code"]) for row in findings}

            self.assertFalse(report["ok"])
            self.assertFalse(report["codex_closed"])
            lsof = require_json_object(report["lsof"], "lsof")
            self.assertEqual(0, lsof["target_count"])
            self.assertIn("codex.support_root.missing", codes)
            self.assertIn("codex.db.missing", codes)

    def test_plan_denies_codex_dev_actions_when_state_threads_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            stores.db_path("state").unlink()

            plan = generate_plan(stores, "workstation")
            actions = require_json_object_list(plan["planned_actions"], "planned_actions")
            denials = require_json_object_list(plan["denials"], "denials")

            self.assertFalse(any(str(action.get("db")) == "codex-dev" for action in actions))
            self.assertIn("codex.state_threads.unavailable", {str(row["code"]) for row in denials})

    def test_cli_doctor_default_omits_rows_and_details_includes_bounded_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))

            exit_code, payload, stderr = run_cli_json(["--json", "--codex-home", str(stores.codex_home), "doctor"])

            self.assertEqual(0, exit_code)
            self.assertEqual("", stderr)
            self.assertFalse(require_json_object(payload["details"], "details")["included"])
            self.assertFalse(contains_key(payload, "review_rows"))
            self.assertFalse(contains_key(payload, "handles"))
            findings = require_json_object_list(payload["findings"], "findings")
            self.assertIn("codex.automation_badge.unread_run_instances", {str(row["code"]) for row in findings})
            next_commands = payload["next_commands"]
            if not isinstance(next_commands, list):
                raise AssertionError("next_commands must be a list")
            self.assertIn("prep --profile clear-current-badge", " ".join(str(row) for row in next_commands))
            lsof = require_json_object(payload["lsof"], "lsof")
            self.assertIn("handle_count", lsof)
            codex_dev = require_json_object(payload["codex_dev"], "codex_dev")
            runs = require_json_object(codex_dev["automation_runs"], "automation_runs")
            review_meta = require_json_object(runs["review_rows_meta"], "review_rows_meta")
            self.assertTrue(review_meta["details_omitted"])
            self.assertEqual(0, review_meta["returned_count"])

            exit_code, payload, stderr = run_cli_json(
                ["--json", "--codex-home", str(stores.codex_home), "doctor", "--details", "--limit", "1"]
            )

            self.assertEqual(0, exit_code)
            self.assertEqual("", stderr)
            self.assertTrue(require_json_object(payload["details"], "details")["included"])
            lsof = require_json_object(payload["lsof"], "lsof")
            self.assertIn("handles", lsof)
            codex_dev = require_json_object(payload["codex_dev"], "codex_dev")
            runs = require_json_object(codex_dev["automation_runs"], "automation_runs")
            review_rows = require_json_object_list(runs["review_rows"], "review_rows")
            review_meta = require_json_object(runs["review_rows_meta"], "review_rows_meta")
            self.assertLessEqual(len(review_rows), 1)
            self.assertNotIn("details_omitted", review_meta)
            self.assertEqual(len(review_rows), review_meta["returned_count"])

    def test_cli_json_usage_errors_are_json(self) -> None:
        out = StringIO()
        err = StringIO()
        support_root = "/tmp/example-codex-home"
        with redirect_stdout(out), redirect_stderr(err):
            exit_code = cli_module.main(
                ["--json", "--codex-home", support_root, "raw", "sql", "--db", "codex-dev", "--readonly"]
            )

        payload = json.loads(out.getvalue())
        self.assertEqual(2, exit_code)
        self.assertFalse(payload["ok"])
        self.assertEqual("usage_error", payload["error"]["code"])
        self.assertEqual(support_root, payload["support_root"])
        self.assertEqual("raw", payload["command"])
        self.assertEqual("", err.getvalue())

    def test_cli_run_denial_and_fixture_success_are_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            out = StringIO()
            err = StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                denied = cli_module.main(["--json", "--codex-home", str(stores.codex_home), "run"])
            denied_payload = json.loads(out.getvalue())
            self.assertEqual(1, denied)
            self.assertFalse(denied_payload["ok"])
            self.assertEqual("apply_not_approved", denied_payload["error"]["code"])
            self.assertIn("next_step", denied_payload["error"])
            self.assertEqual("", err.getvalue())

            out = StringIO()
            err = StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                exit_code = cli_module.main(
                    ["--json", "--codex-home", str(stores.codex_home), "run", "--apply-approved"]
                )
            payload = json.loads(out.getvalue())
            self.assertEqual(0, exit_code)
            self.assertTrue(payload["ok"])
            self.assertEqual("workstation", payload["profile"])
            self.assertEqual("workstation-hide-broken-only", payload["approved_policy"])
            plan_path = Path(str(payload["plan_path"]))
            self.assertTrue(plan_path.exists())
            self.assertEqual(0o700, plan_path.parent.stat().st_mode & 0o777)
            self.assertEqual(0o600, plan_path.stat().st_mode & 0o777)
            self.assertTrue(Path(str(payload["receipt_path"])).exists())
            receipt_path = Path(str(payload["receipt_path"]))
            self.assertEqual(0o700, receipt_path.parent.stat().st_mode & 0o777)
            self.assertEqual(0o600, receipt_path.stat().st_mode & 0o777)
            self.assertIn("doctor", str(payload["next_commands"]))
            self.assertIn("did not clear valid automation badge rows", str(payload["next_commands"]))
            self.assertEqual("", err.getvalue())

    def test_cli_run_denies_clear_current_badge_without_manual_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))

            exit_code, payload, stderr = run_cli_json(
                [
                    "--json",
                    "--codex-home",
                    str(stores.codex_home),
                    "run",
                    "--profile",
                    "clear-current-badge",
                    "--apply-approved",
                ]
            )

            self.assertEqual(1, exit_code)
            self.assertFalse(payload["ok"])
            self.assertEqual(
                "manual_profile_requires_acknowledgement",
                require_json_object(payload["error"], "error")["code"],
            )
            next_step = str(require_json_object(payload["error"], "error")["next_step"])
            self.assertIn("--manual-clear-current-badge", next_step)
            self.assertNotIn("rerun the same command", next_step.lower())
            self.assertFalse((stores.care_root / "plans").exists())
            self.assertFalse((stores.care_root / "receipts").exists())
            with closing(sqlite3.connect(stores.db_path("codex-dev"))) as conn:
                read_count = conn.execute(
                    "SELECT COUNT(*) FROM automation_runs WHERE thread_id IN ('thread-good', 'thread-accepted') "
                    "AND read_at IS NOT NULL"
                ).fetchone()[0]
            self.assertEqual(0, read_count)
            self.assertEqual("", stderr)

    def test_cli_run_denies_clear_current_badge_without_apply_approved_as_manual_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))

            exit_code, payload, stderr = run_cli_json(
                [
                    "--json",
                    "--codex-home",
                    str(stores.codex_home),
                    "run",
                    "--profile",
                    "clear-current-badge",
                ]
            )

            self.assertEqual(1, exit_code)
            self.assertFalse(payload["ok"])
            error = require_json_object(payload["error"], "error")
            self.assertEqual("manual_profile_requires_approval", error["code"])
            next_step = str(error["next_step"])
            self.assertIn("--apply-approved", next_step)
            self.assertIn("--manual-clear-current-badge", next_step)
            self.assertFalse((stores.care_root / "plans").exists())
            self.assertFalse((stores.care_root / "receipts").exists())
            self.assertEqual("", stderr)

    def test_cli_run_clear_current_badge_one_shot_requires_explicit_manual_ack_and_applies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))

            exit_code, payload, stderr = run_cli_json(
                [
                    "--json",
                    "--codex-home",
                    str(stores.codex_home),
                    "run",
                    "--profile",
                    "clear-current-badge",
                    "--apply-approved",
                    "--manual-clear-current-badge",
                ]
            )

            self.assertEqual(0, exit_code)
            self.assertTrue(payload["ok"])
            self.assertEqual("clear-current-badge", payload["profile"])
            self.assertEqual("manual-clear-current-badge", payload["approved_policy"])
            self.assertTrue(Path(str(payload["plan_path"])).exists())
            self.assertTrue(Path(str(payload["receipt_path"])).exists())
            applied_actions = require_json_object_list(payload["applied_actions"], "applied")
            applied_lanes = {str(action["lane"]) for action in applied_actions}
            self.assertIn("automations.clear_current_badge", applied_lanes)
            self.assertIn("verify the app badge", str(payload["next_commands"]))
            self.assertNotIn("did not clear valid automation badge rows", str(payload["next_commands"]))
            with closing(sqlite3.connect(stores.db_path("codex-dev"))) as conn:
                read_count = conn.execute(
                    "SELECT COUNT(*) FROM automation_runs WHERE thread_id IN ('thread-good', 'thread-accepted') "
                    "AND read_at IS NOT NULL"
                ).fetchone()[0]
            self.assertEqual(2, read_count)
            self.assertEqual("", stderr)

    def test_cli_apply_denials_are_json_and_do_not_create_receipts_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            action = require_json_object_list(plan["planned_actions"], "planned_actions")[0]
            preconditions = action["preconditions"]
            if not isinstance(preconditions, list):
                raise AssertionError("preconditions must be a list")
            preconditions[0] = "not an object"
            plan_path = Path(tmp) / "tampered-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            exit_code, payload, stderr = run_cli_json(
                ["--json", "--codex-home", str(stores.codex_home), "apply", "--plan", str(plan_path)]
            )

            self.assertEqual(1, exit_code)
            self.assertFalse(payload["ok"])
            self.assertEqual(str(stores.codex_home), payload["support_root"])
            self.assertEqual("invalid_plan", require_json_object(payload["error"], "error")["code"])
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{plan['run_id']}.json").exists())
            self.assertEqual("", stderr)

    def test_cli_apply_and_run_deny_lsof_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            plan_path = Path(tmp) / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            original = apply_module.lsof_handles
            apply_module.lsof_handles = fake_lsof_unavailable
            try:
                exit_code, payload, stderr = run_cli_json(
                    ["--json", "--codex-home", str(stores.codex_home), "apply", "--plan", str(plan_path)]
                )
                self.assertEqual(1, exit_code)
                self.assertEqual("lsof_unavailable", require_json_object(payload["error"], "error")["code"])
                self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())
                self.assertEqual("", stderr)

                exit_code, payload, stderr = run_cli_json(
                    ["--json", "--codex-home", str(stores.codex_home), "run", "--apply-approved"]
                )
                self.assertEqual(1, exit_code)
                self.assertEqual("lsof_unavailable", require_json_object(payload["error"], "error")["code"])
                self.assertEqual("", stderr)
            finally:
                apply_module.lsof_handles = original

    def test_cli_apply_and_run_deny_missing_support_root_without_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = StorePaths(Path(tmp) / "missing-codex-home")
            plan = generate_plan(stores, "workstation")
            plan_path = Path(tmp) / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            exit_code, payload, stderr = run_cli_json(
                ["--json", "--codex-home", str(stores.codex_home), "apply", "--plan", str(plan_path)]
            )
            self.assertEqual(1, exit_code)
            self.assertEqual("support_root_missing", require_json_object(payload["error"], "error")["code"])
            self.assertFalse(stores.care_root.exists())
            self.assertEqual("", stderr)

            exit_code, payload, stderr = run_cli_json(
                ["--json", "--codex-home", str(stores.codex_home), "run", "--apply-approved"]
            )
            self.assertEqual(1, exit_code)
            self.assertEqual("support_root_missing", require_json_object(payload["error"], "error")["code"])
            self.assertFalse(stores.care_root.exists())
            self.assertEqual("", stderr)

    def test_cli_doctor_includes_recovery_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            out = StringIO()
            err = StringIO()

            with redirect_stdout(out), redirect_stderr(err):
                exit_code = cli_module.main(["--json", "--codex-home", str(stores.codex_home), "doctor"])

            payload = json.loads(out.getvalue())
            self.assertEqual(0, exit_code)
            self.assertEqual("doctor", payload["command"])
            self.assertEqual("flag", payload["codex_home_source"])
            self.assertIsInstance(payload["run_id"], str)
            self.assertIn("support_root", payload)
            self.assertIn("codex_closed", payload)
            self.assertIsInstance(payload["findings"], list)
            self.assertEqual([], payload["planned_actions"])
            self.assertEqual([], payload["applied_actions"])
            self.assertEqual([], payload["denials"])
            self.assertIsNone(payload["receipt_path"])
            self.assertIn("next_commands", payload)
            self.assertEqual("", err.getvalue())
