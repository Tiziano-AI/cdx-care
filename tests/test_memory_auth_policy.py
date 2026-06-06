"""Memory auth-blocker tests for cdx-care."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cdx_care_fixtures import (
    first_action,
    make_fixture,
    require_actions,
    set_global_consolidation_job,
    set_stage1_error,
    stat_snapshot,
)

from cdx_care.apply import apply_plan
from cdx_care.doctor import doctor_report
from cdx_care.errors import CdxCareError
from cdx_care.memory_reports import memory_error_category
from cdx_care.plan import generate_plan
from cdx_care.sqlite_tools import value_hash
from cdx_care.types import require_json_object_list


class CdxCareMemoryAuthPolicyTest(unittest.TestCase):
    """Verify memory auth failures are surfaced instead of blindly retried."""

    def test_global_consolidation_auth_error_is_plan_denial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            set_global_consolidation_job(
                stores.db_path("memories"),
                status="error",
                worker_id=None,
                ownership_token=None,
                lease_until=None,
                last_error="401 Unauthorized: Missing bearer token",
                input_watermark=10,
                last_success_watermark=10,
            )

            plan = generate_plan(stores, "workstation")
            action_ids = {str(action["id"]) for action in require_actions(plan)}
            denials = plan["denials"]
            if not isinstance(denials, list):
                raise AssertionError("denials must be a list")

            self.assertNotIn("memory-global-consolidation-enqueue", action_ids)
            denial_codes = {str(row.get("code")) for row in denials if isinstance(row, dict)}
            self.assertIn("memory.global_consolidation.auth_blocked", denial_codes)
            self.assertNotIn("Missing bearer", str(plan))

    def test_apply_denies_tampered_global_auth_enqueue_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "memory-global-consolidation-enqueue")
            set_global_consolidation_job(
                stores.db_path("memories"),
                status="error",
                worker_id=None,
                ownership_token=None,
                lease_until=None,
                last_error="401 Unauthorized: Missing bearer token",
                input_watermark=10,
                last_success_watermark=10,
            )
            action["db_stat"] = stat_snapshot(stores.db_path("memories"))
            plan["planned_actions"] = [action]

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)

            self.assertEqual("row_not_eligible", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{plan['run_id']}.json").exists())

    def test_memory_auth_errors_are_denied_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            set_stage1_error(
                stores.db_path("memories"),
                job_key="thread-memory-error",
                last_error="401 Unauthorized: Missing bearer or basic authentication in header",
                retry_remaining=0,
            )

            plan = generate_plan(stores, "workstation")
            action_ids = {str(action["id"]) for action in require_actions(plan)}
            denials = require_json_object_list(plan["denials"], "denials")

            self.assertEqual("auth", memory_error_category("401 Unauthorized: Missing bearer token"))
            self.assertNotIn("memory-stage1-retry:thread-memory-error", action_ids)
            self.assertIn("memory.stage1_retry.auth_blocked", {str(row.get("code")) for row in denials})
            self.assertNotIn("Missing bearer", str(plan))

    def test_apply_denies_tampered_auth_memory_retry_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "memory-stage1-retry:thread-memory-error")
            auth_error = "401 Unauthorized: Missing bearer or basic authentication in header"
            set_stage1_error(
                stores.db_path("memories"),
                job_key="thread-memory-error",
                last_error=auth_error,
                retry_remaining=0,
            )
            action["db_stat"] = stat_snapshot(stores.db_path("memories"))
            preconditions = action["preconditions"]
            if not isinstance(preconditions, list):
                raise AssertionError("preconditions must be a list")
            for row in preconditions:
                if isinstance(row, dict) and row.get("column") == "last_error":
                    row["sha256"] = value_hash(auth_error)
            plan["planned_actions"] = [action]

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)

            self.assertEqual("row_not_eligible", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())
            self.assertFalse((stores.care_root / "receipts" / f"{plan['run_id']}.json").exists())

    def test_doctor_reports_auth_memory_errors_without_raw_error_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            set_stage1_error(
                stores.db_path("memories"),
                job_key="thread-memory-error",
                last_error="401 Unauthorized: Missing bearer or basic authentication in header",
                retry_remaining=0,
            )

            report = doctor_report(stores)
            payload = str(report)
            findings = require_json_object_list(report["findings"], "findings")

            self.assertIn("codex.memory.stage1_auth_errors", {str(row.get("code")) for row in findings})
            self.assertIn("'stage1_auth_error_count': 1", payload)
            self.assertIn("'error_category': 'auth'", payload)
            self.assertNotIn("Missing bearer", payload)
            self.assertNotIn("Unauthorized", payload)

    def test_doctor_reports_global_auth_memory_error_without_raw_error_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            set_global_consolidation_job(
                stores.db_path("memories"),
                status="error",
                worker_id=None,
                ownership_token=None,
                lease_until=None,
                last_error="401 Unauthorized: Missing bearer token",
                input_watermark=10,
                last_success_watermark=10,
            )

            report = doctor_report(stores)
            payload = str(report)
            findings = require_json_object_list(report["findings"], "findings")

            self.assertIn("codex.memory.global_auth_error", {str(row.get("code")) for row in findings})
            self.assertIn("'error_category': 'auth'", payload)
            self.assertNotIn("Missing bearer", payload)
            self.assertNotIn("Unauthorized", payload)
