"""Public CLI plan/apply/run envelope tests."""

from __future__ import annotations

import json
import os
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

    def test_cli_plan_allows_existing_symlink_parent_like_macos_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            real_parent = Path(tmp) / "private-tmp"
            real_parent.mkdir()
            symlink_parent = Path(tmp) / "tmp-link"
            os.symlink(real_parent, symlink_parent, target_is_directory=True)
            plan_path = symlink_parent / "plan.json"

            exit_code, payload, stderr = run_cli_json(
                ["--json", "--codex-home", str(stores.codex_home), "plan", "--out", str(plan_path)]
            )

            self.assertEqual(0, exit_code)
            self.assertEqual("", stderr)
            self.assertTrue(plan_path.exists())
            self.assertEqual(str(plan_path), payload["plan_path"])

    def test_relative_codex_home_plan_is_bound_to_absolute_support_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_cwd = root / "plan-cwd"
            apply_cwd = root / "apply-cwd"
            plan_cwd.mkdir()
            apply_cwd.mkdir()
            make_fixture(plan_cwd)
            make_fixture(apply_cwd)
            plan_path = root / "relative-plan.json"
            original_cwd = Path.cwd()

            try:
                os.chdir(plan_cwd)
                exit_code, payload, stderr = run_cli_json(
                    ["--json", "--codex-home", ".codex", "plan", "--out", str(plan_path)]
                )
                self.assertEqual(0, exit_code)
                self.assertEqual("", stderr)
                self.assertTrue(Path(str(payload["support_root"])).is_absolute())
                self.assertTrue(str(payload["support_root"]).endswith("/plan-cwd/.codex"))
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
                self.assertEqual(str(payload["support_root"]), plan["support_root"])

                os.chdir(apply_cwd)
                exit_code, payload, stderr = run_cli_json(
                    ["--json", "--codex-home", ".codex", "apply", "--plan", str(plan_path)]
                )
            finally:
                os.chdir(original_cwd)

            self.assertEqual(1, exit_code)
            self.assertEqual("", stderr)
            self.assertEqual("support_root_mismatch", require_json_object(payload["error"], "error")["code"])
            self.assertTrue(str(payload["support_root"]).endswith("/apply-cwd/.codex"))
            self.assertNotEqual(str(plan["support_root"]), str(payload["support_root"]))
            run_id = str(plan["run_id"])
            plan_care = Path(str(plan["support_root"])) / "cdx-care"
            apply_care = Path(str(payload["support_root"])) / "cdx-care"
            self.assertFalse((plan_care / "backups" / run_id).exists())
            self.assertFalse((plan_care / "receipts" / f"{run_id}.json").exists())
            self.assertFalse((apply_care / "backups" / run_id).exists())
            self.assertFalse((apply_care / "receipts" / f"{run_id}.json").exists())

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
