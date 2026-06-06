"""Memory lane tamper tests for cdx-care."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cdx_care_fixtures import (
    actions,
    first_action,
    insert_global_consolidation_job,
    make_fixture,
    remove_global_consolidation_job,
    set_global_consolidation_job,
    stat_snapshot,
)

from cdx_care.apply import apply_plan
from cdx_care.errors import CdxCareError
from cdx_care.plan import generate_plan


class CdxCareMemoryPolicyTest(unittest.TestCase):
    """Verify memory plan values are bounded by current DB state."""

    def test_apply_denies_tampered_non_terminal_memory_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "memory-stage1-retry:thread-memory-error")
            action["key"] = {"kind": "memory_stage1", "job_key": "thread-memory-retryable"}

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("row_not_eligible", caught.exception.code)

    def test_apply_denies_tampered_global_memory_update_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "memory-global-consolidation-enqueue")
            updates = action["updates"]
            if not isinstance(updates, dict):
                raise AssertionError("updates must be a dict")
            updates["worker_id"] = "tampered-worker"

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("action_target_denied", caught.exception.code)

    def test_apply_denies_regressed_global_memory_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "memory-global-consolidation-enqueue")
            updates = action["updates"]
            if not isinstance(updates, dict):
                raise AssertionError("updates must be a dict")
            updates["input_watermark"] = 11

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("row_drift", caught.exception.code)

    def test_apply_denies_future_global_memory_update_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "memory-global-consolidation-enqueue")
            updates = action["updates"]
            if not isinstance(updates, dict):
                raise AssertionError("updates must be a dict")
            updates["input_watermark"] = 9_999_999_999

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("action_target_denied", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())

    def test_apply_denies_current_global_watermark_advanced_after_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "memory-global-consolidation-enqueue")
            updates = action["updates"]
            if not isinstance(updates, dict) or not isinstance(updates.get("input_watermark"), int):
                raise AssertionError("input_watermark update must be an integer")
            set_global_consolidation_job(
                stores.db_path("memories"),
                status="done",
                worker_id=None,
                ownership_token=None,
                lease_until=None,
                last_error=None,
                input_watermark=int(updates["input_watermark"]),
                last_success_watermark=10,
            )
            action["db_stat"] = stat_snapshot(stores.db_path("memories"))
            plan["planned_actions"] = [action]

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)

            self.assertEqual("action_target_denied", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())

    def test_plan_omits_volatile_memory_watermark_preconditions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "memory-global-consolidation-enqueue")
            preconditions = action["preconditions"]
            if not isinstance(preconditions, list):
                raise AssertionError("preconditions must be a list")
            columns = {str(row.get("column")) for row in preconditions if isinstance(row, dict)}

            self.assertNotIn("input_watermark", columns)
            self.assertNotIn("last_success_watermark", columns)

    def test_apply_denies_removed_required_memory_precondition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "memory-global-consolidation-enqueue")
            preconditions = action["preconditions"]
            if not isinstance(preconditions, list):
                raise AssertionError("preconditions must be a list")
            action["preconditions"] = [
                row
                for row in preconditions
                if isinstance(row, dict) and row.get("column") != "retry_remaining"
            ]

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("action_target_denied", caught.exception.code)

    def test_apply_denies_tampered_global_memory_insert_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            remove_global_consolidation_job(stores.db_path("memories"))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "memory-global-consolidation-insert")
            values = action["values"]
            if not isinstance(values, dict):
                raise AssertionError("values must be a dict")
            values["lease_until"] = 9_999_999_999

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("action_target_denied", caught.exception.code)

    def test_apply_denies_regressed_global_memory_insert_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            remove_global_consolidation_job(stores.db_path("memories"))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "memory-global-consolidation-insert")
            values = action["values"]
            if not isinstance(values, dict):
                raise AssertionError("values must be a dict")
            values["input_watermark"] = 1

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("row_drift", caught.exception.code)

    def test_apply_denies_future_global_memory_insert_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            remove_global_consolidation_job(stores.db_path("memories"))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "memory-global-consolidation-insert")
            values = action["values"]
            if not isinstance(values, dict):
                raise AssertionError("values must be a dict")
            values["input_watermark"] = 9_999_999_999

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)
            self.assertEqual("action_target_denied", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())

    def test_apply_denies_insert_row_appeared_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            remove_global_consolidation_job(stores.db_path("memories"))
            plan = generate_plan(stores, "workstation")
            action = first_action(plan, "memory-global-consolidation-insert")
            insert_global_consolidation_job(stores.db_path("memories"), status="done", input_watermark=500)
            action["db_stat"] = stat_snapshot(stores.db_path("memories"))
            plan["planned_actions"] = [action]

            with self.assertRaises(CdxCareError) as caught:
                apply_plan(stores, plan)

            self.assertEqual("row_drift", caught.exception.code)
            self.assertFalse((stores.care_root / "backups" / str(plan["run_id"])).exists())

    def test_global_consolidation_pending_job_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            set_global_consolidation_job(
                stores.db_path("memories"),
                status="pending",
                worker_id=None,
                ownership_token=None,
                lease_until=None,
                last_error=None,
                input_watermark=500,
                last_success_watermark=10,
            )

            plan = generate_plan(stores, "workstation")
            action_ids = {str(action["id"]) for action in actions(plan)}

            self.assertNotIn("memory-global-consolidation-enqueue", action_ids)

    def test_global_consolidation_future_lease_is_plan_denial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            set_global_consolidation_job(
                stores.db_path("memories"),
                status="running",
                worker_id="worker",
                ownership_token="token",
                lease_until=9_999_999_999,
                last_error=None,
                input_watermark=10,
                last_success_watermark=10,
            )

            plan = generate_plan(stores, "workstation")
            action_ids = {str(action["id"]) for action in actions(plan)}
            denials = plan["denials"]
            if not isinstance(denials, list):
                raise AssertionError("denials must be a list")

            self.assertNotIn("memory-global-consolidation-enqueue", action_ids)
            denial_codes = {str(row.get("code")) for row in denials if isinstance(row, dict)}
            self.assertIn("memory.global_consolidation.active_lease", denial_codes)

    def test_repeated_apply_is_global_consolidation_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            first = generate_plan(stores, "workstation")
            apply_plan(stores, first)

            second = generate_plan(stores, "workstation")
            action_ids = {str(action["id"]) for action in actions(second)}

            self.assertNotIn("memory-global-consolidation-enqueue", action_ids)
