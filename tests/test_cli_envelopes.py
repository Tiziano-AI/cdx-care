"""Public CLI plan/apply/run envelope tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from cdx_care_fixtures import make_fixture

import cdx_care.apply as apply_module
import cdx_care.cli as cli_module
from cdx_care.plan import generate_plan
from cdx_care.types import JsonObject, require_json_object, require_json_object_list


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


class CdxCareCliEnvelopeTest(unittest.TestCase):
    """Verify public write command envelopes."""

    def test_cli_plan_and_apply_success_envelopes_are_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan_path = Path(tmp) / "plan.json"

            exit_code, payload, stderr = run_cli_json(
                [
                    "--json",
                    "--codex-home",
                    str(stores.codex_home),
                    "plan",
                    "--profile",
                    "workstation",
                    "--out",
                    str(plan_path),
                ]
            )
            self.assertEqual(0, exit_code)
            self.assertEqual("", stderr)
            self.assertTrue(plan_path.exists())
            self.assertTrue(payload["ok"])
            self.assertEqual(str(plan_path), payload["plan_path"])
            planned_actions = require_json_object_list(payload["planned_actions"], "actions")
            self.assertEqual(payload["action_count"], len(planned_actions))
            self.assertIn("denials", payload)

            exit_code, payload, stderr = run_cli_json(
                ["--json", "--codex-home", str(stores.codex_home), "apply", "--plan", str(plan_path)]
            )
            self.assertEqual(0, exit_code)
            self.assertEqual("", stderr)
            self.assertTrue(payload["ok"])
            self.assertEqual(str(plan_path), payload["plan_path"])
            planned_actions = require_json_object_list(payload["planned_actions"], "actions")
            self.assertEqual(payload["plan_action_count"], len(planned_actions))
            self.assertTrue(require_json_object_list(payload["applied_actions"], "applied_actions"))
            self.assertTrue(Path(str(payload["receipt_path"])).exists())

    def test_cli_apply_and_run_failure_receipts_are_reported_in_json(self) -> None:
        def fail_git(_action: JsonObject) -> JsonObject:
            raise apply_module.CdxCareError("simulated git failure", code="simulated_git_failure")

        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            plan_path = Path(tmp) / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            original = apply_module.apply_git_action
            apply_module.apply_git_action = fail_git
            try:
                exit_code, payload, stderr = run_cli_json(
                    ["--json", "--codex-home", str(stores.codex_home), "apply", "--plan", str(plan_path)]
                )
            finally:
                apply_module.apply_git_action = original

            self.assertEqual(1, exit_code)
            self.assertEqual("", stderr)
            self.assertEqual("simulated_git_failure", require_json_object(payload["error"], "error")["code"])
            self.assertEqual(str(plan["run_id"]), payload["run_id"])
            self.assertEqual(str(plan_path), payload["plan_path"])
            self.assertTrue(payload["partial"])
            self.assertTrue(payload["backup_root_present"])
            receipt_path = Path(str(payload["receipt_path"]))
            self.assertTrue(receipt_path.exists())
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertFalse(receipt["ok"])
            self.assertTrue(receipt["partial"])
            self.assertEqual("simulated_git_failure", require_json_object(receipt["error"], "error")["code"])

        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            original = apply_module.apply_git_action
            apply_module.apply_git_action = fail_git
            try:
                exit_code, payload, stderr = run_cli_json(
                    ["--json", "--codex-home", str(stores.codex_home), "run", "--apply-approved"]
                )
            finally:
                apply_module.apply_git_action = original

            self.assertEqual(1, exit_code)
            self.assertEqual("", stderr)
            self.assertEqual("simulated_git_failure", require_json_object(payload["error"], "error")["code"])
            self.assertTrue(payload["partial"])
            self.assertTrue(payload["backup_root_present"])
            self.assertTrue(Path(str(payload["plan_path"])).exists())
            self.assertTrue(Path(str(payload["receipt_path"])).exists())
