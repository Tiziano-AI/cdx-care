"""Logs compaction lane tests."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from cdx_care_fixtures import first_action, make_fixture

from cdx_care.apply import apply_plan
from cdx_care.errors import CdxCareError
from cdx_care.plan import generate_plan
from cdx_care.sqlite_tools import connect_readonly, schema_fingerprint


class LogsCompactionLaneTest(unittest.TestCase):
    """Verify guarded logs_2.sqlite VACUUM planning and apply."""

    def test_apply_denies_tampered_logs_compaction_stats_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "logs-compact-freelist")
            reclaimable = action["reclaimable_bytes"]
            if not isinstance(reclaimable, int):
                raise AssertionError("reclaimable_bytes must be an integer")
            action["reclaimable_bytes"] = reclaimable + 4096

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)

            self.assertEqual("db_changed", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{plan['run_id']}.json").exists())

    def test_apply_denies_logs_db_drift_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            plan["planned_actions"] = [first_action(plan, "logs-compact-freelist")]
            logs_db = stores.db_path("logs")
            with closing(sqlite3.connect(logs_db)) as conn:
                conn.execute("INSERT INTO logs(level, feedback_log_body) VALUES ('INFO', 'new')")
                conn.commit()

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)

            self.assertEqual("db_changed", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{plan['run_id']}.json").exists())

    def test_apply_denies_logs_compaction_quick_check_failure_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            plan["planned_actions"] = [first_action(plan, "logs-compact-freelist")]

            with (
                patch("cdx_care.logs_compact.quick_check", return_value="malformed"),
                self.assertRaises(CdxCareError) as caught,
            ):
                apply_plan(stores, plan)

            self.assertEqual("quick_check_failed", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{plan['run_id']}.json").exists())

    def test_apply_denies_logs_compaction_without_disk_headroom_before_backup(self) -> None:
        class DiskUsage:
            free = 0

        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            plan["planned_actions"] = [first_action(plan, "logs-compact-freelist")]

            with (
                patch("cdx_care.logs_compact.shutil.disk_usage", return_value=DiskUsage()),
                self.assertRaises(CdxCareError) as caught,
            ):
                apply_plan(stores, plan)

            self.assertEqual("insufficient_disk_space", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{plan['run_id']}.json").exists())

    def test_apply_denies_logs_compaction_schema_subset_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "logs-compact-freelist")
            with closing(connect_readonly(stores.db_path("logs"))) as conn:
                action["schema_tables"] = ["logs"]
                action["schema_fingerprint"] = schema_fingerprint(conn, ["logs"])
            plan["planned_actions"] = [action]

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)

            self.assertEqual("schema_changed", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{plan['run_id']}.json").exists())
