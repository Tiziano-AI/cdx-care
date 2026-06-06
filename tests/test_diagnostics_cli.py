"""Doctor and blank-page diagnostic contract tests."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from cdx_care_fixtures import make_fixture

import cdx_care.cli as cli_module
from cdx_care.diagnostics import blank_page_pack
from cdx_care.doctor import doctor_report
from cdx_care.errors import CdxCareError
from cdx_care.plan import generate_plan, write_plan
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


class CdxCareDiagnosticsCliTest(unittest.TestCase):
    """Verify doctor and diagnose evidence packs."""

    def test_doctor_and_blank_page_pack_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))

            report = doctor_report(stores)
            pack = blank_page_pack(stores, Path(tmp) / "blank")

            self.assertTrue(report["ok"])
            self.assertIn("codex_dev", report)
            codex_dev = require_json_object(report["codex_dev"], "codex_dev")
            runs = require_json_object(codex_dev["automation_runs"], "automation_runs")
            review_rows = require_json_object_list(runs["review_rows"], "review_rows")
            self.assertNotIn("thread_title", review_rows[0])
            self.assertIn("thread_title_sha256", review_rows[0])
            self.assertTrue(pack["ok"])
            self.assertEqual("cdx-care", pack["tool"])
            self.assertEqual(str(stores.codex_home), pack["support_root"])
            diagnosis_path = Path(str(pack["diagnosis_json"]))
            self.assertTrue(diagnosis_path.exists())
            payload = json.loads(diagnosis_path.read_text(encoding="utf-8"))
            self.assertEqual("blank-page-diagnosis", payload["purpose"])
            dbs = require_json_object(report["dbs"], "dbs")
            codex_dev_db = require_json_object(dbs["codex-dev"], "dbs.codex-dev")
            sessions = require_json_object(report["sessions"], "sessions")
            logs = require_json_object(report["logs"], "logs")
            memories = require_json_object(report["memories"], "memories")
            jobs = require_json_object(memories["jobs"], "memories.jobs")
            global_job = require_json_object(jobs["global_consolidation"], "global_consolidation")
            self.assertEqual("ok", codex_dev_db["quick_check"])
            self.assertIn("schema_fingerprint", require_json_object(report["codex_dev"], "codex_dev"))
            self.assertTrue(sessions["session_index_rebuild_apply_supported"])
            self.assertFalse(sessions["history_apply_supported"])
            self.assertTrue(logs["compaction_apply_supported"])
            self.assertIn("last_error_present", global_job)
            self.assertEqual(0o700, diagnosis_path.parent.stat().st_mode & 0o777)
            self.assertEqual(0o600, diagnosis_path.stat().st_mode & 0o777)

    def test_plan_and_diagnose_do_not_clobber_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            existing_plan = Path(tmp) / "existing-plan.json"
            existing_plan.write_text("keep me", encoding="utf-8")
            out_dir = Path(tmp) / "blank"
            out_dir.mkdir()
            (out_dir / "diagnosis.json").write_text("keep me", encoding="utf-8")

            with self.assertRaises(CdxCareError) as caught_plan:
                write_plan(plan, existing_plan)
            self.assertEqual("output_exists", caught_plan.exception.code)
            with self.assertRaises(CdxCareError) as caught_pack:
                blank_page_pack(stores, out_dir)
            self.assertEqual("output_exists", caught_pack.exception.code)

            partial_dir = Path(tmp) / "partial"
            partial_dir.mkdir()
            (partial_dir / "README.md").write_text("keep me", encoding="utf-8")
            with self.assertRaises(CdxCareError) as caught_partial:
                blank_page_pack(stores, partial_dir)
            self.assertEqual("output_exists", caught_partial.exception.code)
            self.assertFalse((partial_dir / "diagnosis.json").exists())

            existing_empty_dir = Path(tmp) / "existing-empty"
            existing_empty_dir.mkdir()
            existing_empty_dir.chmod(0o755)
            with self.assertRaises(CdxCareError) as caught_existing_empty:
                blank_page_pack(stores, existing_empty_dir)
            self.assertEqual("output_exists", caught_existing_empty.exception.code)
            self.assertEqual(0o755, existing_empty_dir.stat().st_mode & 0o777)

            nested_plan = Path(tmp) / "new-plan-parent" / "child" / "plan.json"
            write_plan(plan, nested_plan)
            self.assertEqual(0o700, nested_plan.parent.stat().st_mode & 0o777)
            self.assertEqual(0o600, nested_plan.stat().st_mode & 0o777)

            nested_pack = Path(tmp) / "new-pack-parent" / "pack"
            pack = blank_page_pack(stores, nested_pack)
            diagnosis_path = Path(str(pack["diagnosis_json"]))
            readme_path = Path(str(pack["readme"]))
            self.assertEqual(0o700, nested_pack.stat().st_mode & 0o777)
            self.assertEqual(0o600, diagnosis_path.stat().st_mode & 0o777)
            self.assertEqual(0o600, readme_path.stat().st_mode & 0o777)

            real_dir = Path(tmp) / "real-dir"
            real_dir.mkdir()
            link_dir = Path(tmp) / "link-dir"
            os.symlink(real_dir, link_dir, target_is_directory=True)
            symlink_parent_plan = link_dir / "plan.json"
            write_plan(plan, symlink_parent_plan)
            self.assertTrue(symlink_parent_plan.exists())
            self.assertEqual(0o600, symlink_parent_plan.stat().st_mode & 0o777)
            with self.assertRaises(CdxCareError) as caught_link_pack:
                blank_page_pack(stores, link_dir)
            self.assertEqual("unsafe_output_path", caught_link_pack.exception.code)

    def test_cli_diagnose_blank_page_no_clobber_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            populated = Path(tmp) / "populated"
            populated.mkdir()
            (populated / "diagnosis.json").write_text("keep me", encoding="utf-8")
            exit_code, payload, stderr = run_cli_json(
                [
                    "--json",
                    "--codex-home",
                    str(stores.codex_home),
                    "diagnose",
                    "blank-page",
                    "--out-dir",
                    str(populated),
                ]
            )
            self.assertEqual(1, exit_code)
            self.assertEqual("output_exists", require_json_object(payload["error"], "error")["code"])
            self.assertEqual("keep me", (populated / "diagnosis.json").read_text(encoding="utf-8"))
            self.assertEqual("", stderr)

            empty = Path(tmp) / "empty"
            empty.mkdir()
            empty.chmod(0o755)
            exit_code, payload, stderr = run_cli_json(
                [
                    "--json",
                    "--codex-home",
                    str(stores.codex_home),
                    "diagnose",
                    "blank-page",
                    "--out-dir",
                    str(empty),
                ]
            )
            self.assertEqual(1, exit_code)
            self.assertEqual("output_exists", require_json_object(payload["error"], "error")["code"])
            self.assertEqual(0o755, empty.stat().st_mode & 0o777)
            self.assertEqual("", stderr)

            real_dir = Path(tmp) / "real"
            real_dir.mkdir()
            link_dir = Path(tmp) / "link"
            os.symlink(real_dir, link_dir, target_is_directory=True)
            exit_code, payload, stderr = run_cli_json(
                [
                    "--json",
                    "--codex-home",
                    str(stores.codex_home),
                    "diagnose",
                    "blank-page",
                    "--out-dir",
                    str(link_dir),
                ]
            )
            self.assertEqual(1, exit_code)
            self.assertEqual("unsafe_output_path", require_json_object(payload["error"], "error")["code"])
            self.assertEqual("", stderr)

            fresh = Path(tmp) / "fresh-pack"
            exit_code, payload, stderr = run_cli_json(
                [
                    "--json",
                    "--codex-home",
                    str(stores.codex_home),
                    "diagnose",
                    "blank-page",
                    "--out-dir",
                    str(fresh),
                ]
            )
            self.assertEqual(0, exit_code)
            self.assertTrue(payload["ok"])
            self.assertEqual(0o700, fresh.stat().st_mode & 0o777)
            self.assertEqual(0o600, Path(str(payload["diagnosis_json"])).stat().st_mode & 0o777)
            self.assertEqual("", stderr)
