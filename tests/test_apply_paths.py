"""Apply path-admission tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from cdx_care_fixtures import first_action, make_fixture

from cdx_care.apply import apply_plan
from cdx_care.errors import CdxCareError
from cdx_care.plan import generate_plan


class CdxCareApplyPathTest(unittest.TestCase):
    """Verify plan target and support-root artifact path admission."""

    def test_apply_denies_tampered_db_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "automation-run-mark-read:thread-archived:auto-good")
            action["db_path"] = str(Path(tmp) / "outside.sqlite")

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("db_path_mismatch", caught.exception.code)

    def test_apply_denies_tampered_git_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "memory-git-untrack-ds-store")
            action["repo"] = str(Path(tmp))

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("git_target_denied", caught.exception.code)

    def test_apply_denies_symlinked_support_write_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            sqlite_dir = stores.codex_home / "sqlite"
            outside_sqlite = Path(tmp) / "outside-sqlite"
            sqlite_dir.rename(outside_sqlite)
            os.symlink(outside_sqlite, sqlite_dir, target_is_directory=True)
            plan = generate_plan(stores, "workstation")

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("unsafe_support_path", caught.exception.code)

        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            original_db = stores.db_path("codex-dev")
            outside_db = Path(tmp) / "outside-codex-dev.db"
            original_db.rename(outside_db)
            os.symlink(outside_db, original_db)
            plan = generate_plan(stores, "workstation")

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("unsafe_support_path", caught.exception.code)

        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            outside_memories = Path(tmp) / "outside-memories"
            stores.memories_root.rename(outside_memories)
            os.symlink(outside_memories, stores.memories_root, target_is_directory=True)
            plan = generate_plan(stores, "workstation")

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("unsafe_support_path", caught.exception.code)

        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            outside_care = Path(tmp) / "outside-care"
            outside_care.mkdir()
            os.symlink(outside_care, stores.care_root, target_is_directory=True)
            plan = generate_plan(stores, "workstation")

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("unsafe_support_path", caught.exception.code)
            self.assertFalse((outside_care / "backups").exists())
            self.assertFalse((outside_care / "receipts").exists())
