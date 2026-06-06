"""Session-index repair lane tests."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from cdx_care_fixtures import first_action, make_fixture

from cdx_care.apply import apply_plan
from cdx_care.errors import CdxCareError
from cdx_care.plan import generate_plan


class SessionRepairLaneTest(unittest.TestCase):
    """Verify guarded session_index.jsonl repair."""

    def test_apply_denies_session_index_target_drift_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            stores.session_index.write_text(
                '{"id":"drift","thread_name":"drift","updated_at":"now"}\n',
                encoding="utf-8",
            )

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)

            self.assertEqual("file_changed", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{plan['run_id']}.json").exists())

    def test_apply_denies_symlinked_sessions_root_before_session_index_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            plan["planned_actions"] = [first_action(plan, "sessions-rebuild-session-index")]
            sessions = stores.codex_home / "sessions"
            outside = Path(tmp) / "outside-sessions"
            sessions.rename(outside)
            os.symlink(outside, sessions)

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)

            self.assertEqual("unsafe_support_path", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{plan['run_id']}.json").exists())

    def test_apply_denies_symlinked_nested_sessions_directory_before_session_index_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            plan["planned_actions"] = [first_action(plan, "sessions-rebuild-session-index")]
            year_dir = stores.codex_home / "sessions" / "2026"
            outside = Path(tmp) / "outside-year"
            year_dir.rename(outside)
            os.symlink(outside, year_dir)

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)

            self.assertEqual("unsafe_support_path", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{plan['run_id']}.json").exists())

    def test_apply_denies_symlinked_rollout_file_before_session_index_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            plan["planned_actions"] = [first_action(plan, "sessions-rebuild-session-index")]
            rollout = stores.codex_home / "sessions" / "2026" / "01" / "01" / "rollout-thread-good.jsonl"
            outside = Path(tmp) / "outside-rollout.jsonl"
            outside.write_text(rollout.read_text(encoding="utf-8"), encoding="utf-8")
            rollout.unlink()
            os.symlink(outside, rollout)

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)

            self.assertEqual("unsafe_support_path", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{plan['run_id']}.json").exists())

    def test_apply_denies_session_index_source_db_drift_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            state_db = stores.db_path("state")
            before = state_db.stat()
            with closing(sqlite3.connect(state_db)) as conn:
                conn.execute("UPDATE threads SET title='drifted title' WHERE id='thread-good'")
                conn.commit()
            os.utime(state_db, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)

            self.assertEqual("db_changed", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{plan['run_id']}.json").exists())

    def test_apply_denies_session_rollout_alignment_drift_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            rollout = stores.codex_home / "sessions" / "2026" / "01" / "01" / "rollout-thread-good.jsonl"
            rollout.unlink()

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)

            self.assertEqual("row_drift", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{plan['run_id']}.json").exists())
