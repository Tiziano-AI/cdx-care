"""Apply receipt failure-mode tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cdx_care_fixtures import make_fixture

import cdx_care.apply as apply_module
from cdx_care.apply import apply_plan
from cdx_care.errors import CdxCareError
from cdx_care.plan import generate_plan
from cdx_care.policy import ApplyContext
from cdx_care.types import JsonObject


class CdxCareApplyReceiptsTest(unittest.TestCase):
    """Verify success-adjacent failure receipts are exact and private."""

    def test_apply_writes_partial_receipt_after_committed_db_then_git_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            run_id = str(plan["run_id"])
            original = apply_module.apply_git_action

            def fail_git(_action: JsonObject) -> JsonObject:
                raise CdxCareError("simulated git failure", code="simulated_git_failure")

            apply_module.apply_git_action = fail_git
            try:
                with self.assertRaises(CdxCareError) as caught:
                    apply_plan(stores, plan)
            finally:
                apply_module.apply_git_action = original

            self.assertEqual("simulated_git_failure", caught.exception.code)
            receipt_path = stores.care_root / "receipts" / f"{run_id}.json"
            self.assertTrue(receipt_path.exists())
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertFalse(receipt["ok"])
            self.assertTrue(receipt["partial"])
            self.assertIn("doctor", str(receipt["next_commands"]))
            self.assertEqual("simulated_git_failure", receipt["error"]["code"])
            self.assertTrue(receipt["applied_actions"])

    def test_apply_writes_failure_receipt_after_backup_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            run_id = str(plan["run_id"])
            original = apply_module.apply_db_actions

            def fail_db(_db_path: Path, _actions: list[JsonObject], _context: ApplyContext) -> list[JsonObject]:
                raise CdxCareError("simulated DB failure", code="simulated_db_failure")

            apply_module.apply_db_actions = fail_db
            try:
                with self.assertRaises(CdxCareError) as caught:
                    apply_plan(stores, plan)
            finally:
                apply_module.apply_db_actions = original

            self.assertEqual("simulated_db_failure", caught.exception.code)
            receipt_path = stores.care_root / "receipts" / f"{run_id}.json"
            self.assertTrue(receipt_path.exists())
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertFalse(receipt["ok"])
            self.assertFalse(receipt["partial"])
            self.assertEqual([], receipt["applied_actions"])

    def test_apply_writes_failure_receipt_after_partial_backup_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            run_id = str(plan["run_id"])
            original = apply_module.copy_db_family

            def fail_after_copy(db_path: Path, backup_dir: Path) -> list[JsonObject]:
                backup_dir.mkdir(parents=True, mode=0o700)
                (backup_dir / db_path.name).write_bytes(b"partial backup")
                raise CdxCareError("simulated backup failure", code="simulated_backup_failure")

            apply_module.copy_db_family = fail_after_copy
            try:
                with self.assertRaises(CdxCareError) as caught:
                    apply_plan(stores, plan)
            finally:
                apply_module.copy_db_family = original

            self.assertEqual("simulated_backup_failure", caught.exception.code)
            receipt_path = stores.care_root / "receipts" / f"{run_id}.json"
            self.assertTrue(receipt_path.exists())
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertFalse(receipt["ok"])
            self.assertFalse(receipt["partial"])
            self.assertEqual("simulated_backup_failure", receipt["error"]["code"])
            backups = receipt["backups"]
            if not isinstance(backups, list):
                raise AssertionError("receipt backups must be a list")
            self.assertTrue(any(isinstance(row, dict) and row.get("discovered_after_failure") for row in backups))

    def test_apply_writes_failure_receipt_after_backup_directory_only_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            run_id = str(plan["run_id"])
            original = apply_module.copy_db_family

            def fail_after_dir(_db_path: Path, backup_dir: Path) -> list[JsonObject]:
                backup_dir.mkdir(parents=True, mode=0o700)
                raise CdxCareError("simulated backup directory failure", code="simulated_backup_dir_failure")

            apply_module.copy_db_family = fail_after_dir
            try:
                with self.assertRaises(CdxCareError) as caught:
                    apply_plan(stores, plan)
            finally:
                apply_module.copy_db_family = original

            self.assertEqual("simulated_backup_dir_failure", caught.exception.code)
            receipt_path = stores.care_root / "receipts" / f"{run_id}.json"
            self.assertTrue(receipt_path.exists())
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertFalse(receipt["ok"])
            self.assertFalse(receipt["partial"])
            self.assertTrue(receipt["backup_root_present"])
            self.assertEqual([], receipt["backups"])
