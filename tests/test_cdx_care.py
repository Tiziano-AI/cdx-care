"""Fixture tests for the cdx-care public contract."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from cdx_care_fixtures import fake_lsof_handles, fake_lsof_unavailable, first_action, make_fixture, require_actions

import cdx_care.apply as apply_module
from cdx_care.apply import apply_plan
from cdx_care.errors import CdxCareError
from cdx_care.git_tools import tracked_paths
from cdx_care.plan import generate_plan
from cdx_care.plan_actions import stat_snapshot
from cdx_care.sqlite_tools import connect_readonly, connect_write, schema_fingerprint


class CdxCareFixtureTest(unittest.TestCase):
    """Verify cdx-care on disposable local state."""

    def test_plan_preserves_valid_pending_and_plans_targeted_repairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))

            plan = generate_plan(stores, "workstation")
            actions = require_actions(plan)
            action_ids = {str(action["id"]) for action in actions}
            plan_json = json.dumps(plan, sort_keys=True)

            self.assertNotIn("automation-run-mark-read:thread-good:auto-good", action_ids)
            self.assertIn("automation-run-mark-read:thread-archived:auto-good", action_ids)
            self.assertIn("automation-run-mark-read:thread-missing:auto-good", action_ids)
            self.assertIn("inbox-orphan-mark-read:inbox-orphan", action_ids)
            self.assertIn("memory-stage1-retry:thread-memory-error", action_ids)
            self.assertIn("memory-global-consolidation-enqueue", action_ids)
            self.assertIn("memory-git-untrack-ds-store", action_ids)
            self.assertNotIn("old-token", plan_json)
            self.assertEqual([], plan["denials"])

    def test_apply_updates_only_planned_rows_and_writes_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")

            receipt = apply_plan(stores, plan)

            self.assertTrue(receipt["ok"])
            self.assertTrue(Path(str(receipt["receipt_path"])).exists())
            self.assertIn("doctor", str(receipt["next_commands"]))
            backups_value = receipt["backups"]
            if not isinstance(backups_value, list):
                raise AssertionError("receipt backups must be a list")
            backup_targets: list[Path] = []
            for row in backups_value:
                if isinstance(row, dict) and isinstance(row.get("target"), str):
                    backup_targets.append(Path(row["target"]))
            self.assertTrue(backup_targets)
            self.assertTrue(all(path.exists() for path in backup_targets))
            self.assertTrue(all(path.parent.stat().st_mode & 0o777 == 0o700 for path in backup_targets))
            self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in backup_targets))
            receipt_path = Path(str(receipt["receipt_path"]))
            self.assertEqual(0o700, receipt_path.parent.stat().st_mode & 0o777)
            self.assertEqual(0o600, receipt_path.stat().st_mode & 0o777)
            with closing(sqlite3.connect(stores.db_path("codex-dev"))) as conn:
                valid_read_at = conn.execute(
                    "SELECT read_at FROM automation_runs WHERE thread_id='thread-good'"
                ).fetchone()[0]
                archived_read_at = conn.execute(
                    "SELECT read_at FROM automation_runs WHERE thread_id='thread-archived'"
                ).fetchone()[0]
                inbox_read_at = conn.execute("SELECT read_at FROM inbox_items WHERE id='inbox-orphan'").fetchone()[0]
            self.assertIsNone(valid_read_at)
            self.assertIsNotNone(archived_read_at)
            self.assertIsNotNone(inbox_read_at)
            with closing(sqlite3.connect(stores.db_path("memories"))) as conn:
                row = conn.execute(
                    "SELECT status, retry_remaining, last_error FROM jobs WHERE kind='memory_stage1'"
                ).fetchone()
                global_row = conn.execute(
                    "SELECT status, retry_remaining FROM jobs WHERE kind='memory_consolidate_global'"
                ).fetchone()
            self.assertEqual(("pending", 3, None), row)
            self.assertEqual(("pending", 3), global_row)
            self.assertEqual([], tracked_paths(stores.memories_root, [".DS_Store", "extensions/.DS_Store"]))
            self.assertTrue((stores.memories_root / ".DS_Store").exists())
            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("run_id_reused", caught.exception.code)

    def test_apply_denies_when_db_changed_after_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            with closing(sqlite3.connect(stores.db_path("codex-dev"))) as conn:
                conn.execute("UPDATE automation_runs SET updated_at=999 WHERE thread_id='thread-archived'")
                conn.commit()

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertIn(caught.exception.code, {"db_changed", "row_drift"})

    def test_schema_fingerprint_tracks_triggers_and_apply_denies_them_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "automation-run-mark-read:thread-archived:auto-good")
            before = str(action["schema_fingerprint"])
            with closing(sqlite3.connect(stores.db_path("codex-dev"))) as conn:
                conn.execute(
                    """
                    CREATE TRIGGER automation_runs_side_effect
                    AFTER UPDATE ON automation_runs
                    BEGIN
                      UPDATE inbox_items SET read_at=123 WHERE id='inbox-valid';
                    END
                    """
                )
                conn.commit()
            with closing(connect_readonly(stores.db_path("codex-dev"))) as conn:
                self.assertNotEqual(before, schema_fingerprint(conn, ["automation_runs"]))
            action["db_stat"] = stat_snapshot(stores.db_path("codex-dev"))
            plan["planned_actions"] = [action]

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)

            self.assertEqual("schema_side_effects", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())

    def test_apply_denies_when_lsof_reports_handles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            original = apply_module.lsof_handles
            apply_module.lsof_handles = fake_lsof_handles
            try:
                with self.assertRaises(CdxCareError) as caught:
                    apply_plan(stores, plan)
            finally:
                apply_module.lsof_handles = original
            self.assertEqual("codex_db_handles_open", caught.exception.code)

    def test_apply_denies_when_lsof_is_unavailable_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            original = apply_module.lsof_handles
            apply_module.lsof_handles = fake_lsof_unavailable
            try:
                with self.assertRaises(CdxCareError) as caught:
                    apply_plan(stores, plan)
            finally:
                apply_module.lsof_handles = original

            self.assertEqual("lsof_unavailable", caught.exception.code)
            run_id = str(plan["run_id"])
            self.assertFalse((stores.care_root / "backups" / run_id).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{run_id}.json").exists())

    def test_apply_denies_tampered_run_id_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            plan["run_id"] = "../escape"

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("invalid_run_id", caught.exception.code)

    def test_apply_denies_tampered_eligible_automation_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "automation-run-mark-read:thread-archived:auto-good")
            action["key"] = {"thread_id": "thread-good", "automation_id": "auto-good"}
            action["preconditions"] = [
                {"column": "status", "equals": "PENDING_REVIEW"},
                {"column": "read_at", "is_null": True},
                {"column": "updated_at", "equals": 20},
            ]

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("row_not_eligible", caught.exception.code)

    def test_apply_denies_tampered_mark_read_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            automation_action = first_action(plan, "automation-run-mark-read:thread-archived:auto-good")
            automation_action["updates"] = {"read_at": None}

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("action_target_denied", caught.exception.code)

    def test_apply_denies_missing_state_threads_for_read_state_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            stores.db_path("state").unlink()

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("state_threads_unavailable", caught.exception.code)

    def test_write_connection_does_not_create_missing_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "missing.sqlite"

            with self.assertRaises(sqlite3.OperationalError):
                connect_write(db_path)

            self.assertFalse(db_path.exists())

    def test_apply_denies_removed_old_value_precondition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "automation-run-mark-read:thread-archived:auto-good")
            preconditions = action["preconditions"]
            if not isinstance(preconditions, list):
                raise AssertionError("preconditions must be a list")
            action["preconditions"] = [
                row for row in preconditions if isinstance(row, dict) and row.get("column") != "updated_at"
            ]

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("action_target_denied", caught.exception.code)

    def test_apply_denies_schema_table_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "automation-run-mark-read:thread-archived:auto-good")
            action["schema_tables"] = []

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("action_target_denied", caught.exception.code)

        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "automation-run-mark-read:thread-archived:auto-good")
            action["schema_tables"] = ["automation_runs", "inbox_items"]

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("action_target_denied", caught.exception.code)

        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "memory-global-consolidation-enqueue")
            action["schema_tables"] = ["jobs"]

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("schema_changed", caught.exception.code)

        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            inbox_action = first_action(plan, "inbox-orphan-mark-read:inbox-orphan")
            inbox_action["updates"] = {"read_at": "now"}

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("action_target_denied", caught.exception.code)

        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            automation_action = first_action(plan, "automation-run-mark-read:thread-archived:auto-good")
            automation_action["updates"] = {"read_at": 9_999_999_999_999_999}

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("action_target_denied", caught.exception.code)

        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            inbox_action = first_action(plan, "inbox-orphan-mark-read:inbox-orphan")
            inbox_action["updates"] = {"read_at": 1}

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("action_target_denied", caught.exception.code)

    def test_apply_denies_tampered_non_orphan_inbox_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "inbox-orphan-mark-read:inbox-orphan")
            action["key"] = {"id": "inbox-valid"}
            action["preconditions"] = [
                {"column": "thread_id", "equals": "thread-good"},
                {"column": "read_at", "is_null": True},
                {"column": "created_at", "equals": 31},
            ]

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("row_not_eligible", caught.exception.code)
