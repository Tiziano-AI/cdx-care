"""Apply-time safety denial tests for cdx-care."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from cdx_care_fixtures import fake_lsof_handles, fake_lsof_unavailable, first_action, make_fixture

import cdx_care.apply as apply_module
from cdx_care.apply import apply_plan
from cdx_care.errors import CdxCareError
from cdx_care.plan import generate_plan
from cdx_care.plan_actions import stat_snapshot
from cdx_care.sqlite_tools import connect_readonly, schema_fingerprint


class CdxCareApplySafetyTest(unittest.TestCase):
    """Verify apply refuses stale, unsafe, or unobservable DB writes before backup."""

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
            run_id = str(plan["run_id"])
            self.assertFalse((stores.care_root / "backups" / run_id).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{run_id}.json").exists())

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

    def test_apply_denies_symlinked_state_source_for_read_state_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            plan["planned_actions"] = [first_action(plan, "automation-run-mark-read:thread-archived:auto-good")]
            state_db = stores.db_path("state")
            outside = Path(tmp) / "outside-state.sqlite"
            outside.write_bytes(state_db.read_bytes())
            state_db.unlink()
            os.symlink(outside, state_db)

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)

            self.assertEqual("unsafe_support_path", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{plan['run_id']}.json").exists())

    def test_apply_denies_symlinked_state_wal_source_for_read_state_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            plan["planned_actions"] = [first_action(plan, "automation-run-mark-read:thread-archived:auto-good")]
            outside = Path(tmp) / "outside-state-wal"
            outside.write_text("outside", encoding="utf-8")
            os.symlink(outside, Path(str(stores.db_path("state")) + "-wal"))

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)

            self.assertEqual("unsafe_support_path", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{plan['run_id']}.json").exists())

    def test_apply_denies_tampered_run_id_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            plan["run_id"] = "../escape"

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("invalid_run_id", caught.exception.code)
