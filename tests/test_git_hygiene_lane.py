"""Memory git hygiene lane tests."""

from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from cdx_care_fixtures import first_action, make_fixture

import cdx_care.git_tools as git_tools
from cdx_care.apply import apply_plan
from cdx_care.errors import CdxCareError
from cdx_care.git_tools import head_tracked_paths, run_git, tracked_paths
from cdx_care.plan import generate_plan


class GitHygieneLaneTest(unittest.TestCase):
    """Verify bounded memory .DS_Store untracking."""

    def test_git_hygiene_commits_head_only_ds_store_deletions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            result = run_git(stores.memories_root, ["rm", "--cached", "--", ".DS_Store", "extensions/.DS_Store"])
            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            self.assertEqual([], tracked_paths(stores.memories_root, [".DS_Store", "extensions/.DS_Store"]))
            self.assertEqual(
                [".DS_Store", "extensions/.DS_Store"],
                head_tracked_paths(stores.memories_root, [".DS_Store", "extensions/.DS_Store"]),
            )
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "memory-git-untrack-ds-store")
            self.assertEqual([".DS_Store", "extensions/.DS_Store"], action["paths"])

            receipt = apply_plan(stores, plan)

            self.assertTrue(receipt["ok"])
            self.assertEqual([], tracked_paths(stores.memories_root, [".DS_Store", "extensions/.DS_Store"]))
            self.assertEqual([], head_tracked_paths(stores.memories_root, [".DS_Store", "extensions/.DS_Store"]))

    def test_git_hygiene_commit_disables_repo_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            hook = stores.memories_root / ".git" / "hooks" / "pre-commit"
            sentinel = stores.memories_root / "hook-ran"
            hook.write_text(f"#!/bin/sh\necho hook > {sentinel}\nexit 1\n", encoding="utf-8")
            hook.chmod(0o755)
            plan = generate_plan(stores, "workstation")

            receipt = apply_plan(stores, plan)

            self.assertTrue(receipt["ok"])
            self.assertFalse(sentinel.exists())
            self.assertEqual([], head_tracked_paths(stores.memories_root, [".DS_Store", "extensions/.DS_Store"]))

    def test_git_hygiene_uses_absolute_git_not_path_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            fake_bin = Path(tmp) / "fake-bin"
            fake_bin.mkdir()
            sentinel = Path(tmp) / "fake-git-ran"
            fake_git = fake_bin / "git"
            fake_git.write_text(f"#!/bin/sh\necho fake > {sentinel}\nexit 99\n", encoding="utf-8")
            fake_git.chmod(0o755)
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = str(fake_bin)
            try:
                importlib.reload(git_tools)
                result = git_tools.run_git(stores.memories_root, ["status", "--porcelain=v1"])
            finally:
                os.environ["PATH"] = old_path
                importlib.reload(git_tools)

            self.assertEqual(0, result.returncode)
            self.assertFalse(sentinel.exists())

    def test_git_hygiene_denies_symlinked_git_dir_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            git_dir = stores.memories_root / ".git"
            outside_git = Path(tmp) / "outside-git-dir"
            git_dir.rename(outside_git)
            os.symlink(outside_git, git_dir)

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)

            self.assertEqual("unsafe_support_path", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{plan['run_id']}.json").exists())

    def test_git_hygiene_denies_gitfile_indirection_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            git_dir = stores.memories_root / ".git"
            outside_git = Path(tmp) / "outside-git-dir"
            git_dir.rename(outside_git)
            git_dir.write_text(f"gitdir: {outside_git}\n", encoding="utf-8")

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)

            self.assertEqual("unsafe_support_path", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{plan['run_id']}.json").exists())

    def test_git_hygiene_denies_common_dir_indirection_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            outside_git = Path(tmp) / "outside-common-dir"
            outside_git.mkdir()
            (stores.memories_root / ".git" / "commondir").write_text(f"{outside_git}\n", encoding="utf-8")

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)

            self.assertEqual("unsafe_support_path", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{plan['run_id']}.json").exists())

    def test_git_hygiene_denies_outside_core_worktree_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            outside_worktree = Path(tmp) / "outside-worktree"
            outside_worktree.mkdir()
            result = run_git(stores.memories_root, ["config", "core.worktree", str(outside_worktree)])
            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)

            self.assertEqual("unsafe_support_path", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{plan['run_id']}.json").exists())

    def test_git_hygiene_denies_omitted_still_tracked_ds_store_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "memory-git-untrack-ds-store")
            action["paths"] = [".DS_Store"]

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)

            self.assertEqual("git_index_changed", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{plan['run_id']}.json").exists())
            self.assertEqual(
                [".DS_Store", "extensions/.DS_Store"],
                head_tracked_paths(stores.memories_root, [".DS_Store", "extensions/.DS_Store"]),
            )

    def test_git_hygiene_failure_after_index_mutation_writes_partial_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            plan["planned_actions"] = [first_action(plan, "memory-git-untrack-ds-store")]
            original = git_tools.current_head

            def fail_current_head(_repo: Path) -> str:
                raise CdxCareError("forced post-commit readback failure", code="forced_git_readback")

            git_tools.current_head = fail_current_head
            try:
                with self.assertRaises(CdxCareError) as caught:
                    apply_plan(stores, plan)
            finally:
                git_tools.current_head = original

            self.assertEqual("forced_git_readback", caught.exception.code)
            receipt_path = stores.care_root / "receipts" / f"{plan['run_id']}.json"
            self.assertTrue(receipt_path.exists())
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertFalse(receipt["ok"])
            self.assertTrue(receipt["partial"])
