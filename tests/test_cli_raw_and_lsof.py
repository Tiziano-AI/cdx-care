"""Raw SQL and lsof CLI contract tests."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from cdx_care_fixtures import make_fixture

import cdx_care.cli as cli_module
from cdx_care.errors import CdxCareError
from cdx_care.processes import lsof_handles, parse_lsof_stdout
from cdx_care.raw_sql import raw_sql_readonly, validate_query_shape
from cdx_care.types import require_json_object


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


class CdxCareRawSqlAndLsofTest(unittest.TestCase):
    """Verify raw read escape hatches and lsof parsing."""

    def test_raw_sql_allows_reads_and_denies_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            query = Path(tmp) / "query.sql"
            query.write_text("SELECT thread_id FROM automation_runs ORDER BY thread_id", encoding="utf-8")

            result = raw_sql_readonly(stores, "codex-dev", query, 2)

            self.assertTrue(result["ok"])
            self.assertEqual("cdx-care", result["tool"])
            self.assertEqual(str(stores.codex_home), result["support_root"])
            self.assertEqual(2, result["row_count_returned"])
            meta = require_json_object(result["rows_meta"], "rows_meta")
            self.assertTrue(meta["truncated"])
            self.assertEqual(2, meta["returned_count"])
            with self.assertRaises(CdxCareError):
                validate_query_shape("UPDATE automation_runs SET read_at=1")
            with self.assertRaises(CdxCareError):
                validate_query_shape("SELECT 1; SELECT 2")
            with self.assertRaises(CdxCareError):
                validate_query_shape("ATTACH DATABASE '/tmp/other.db' AS other")
            with self.assertRaises(CdxCareError):
                validate_query_shape("PRAGMA journal_mode=WAL")
            with self.assertRaises(CdxCareError):
                validate_query_shape("PRAGMA journal_mode(WAL)")
            query.write_text("SELECT load_extension('/tmp/nope')", encoding="utf-8")
            with self.assertRaises(sqlite3.DatabaseError):
                raw_sql_readonly(stores, "codex-dev", query, 2)

    def test_cli_raw_sql_acceptance_and_denials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = make_fixture(Path(tmp))
            query = Path(tmp) / "query.sql"
            query.write_text("SELECT thread_id FROM automation_runs ORDER BY thread_id", encoding="utf-8")

            exit_code, payload, stderr = run_cli_json(
                [
                    "--json",
                    "--codex-home",
                    str(stores.codex_home),
                    "raw",
                    "sql",
                    "--db",
                    "codex-dev",
                    "--query-file",
                    str(query),
                    "--readonly",
                    "--limit",
                    "2",
                ]
            )
            self.assertEqual(0, exit_code)
            self.assertTrue(payload["ok"])
            self.assertEqual(2, payload["row_count_returned"])
            self.assertTrue(require_json_object(payload["rows_meta"], "rows_meta")["truncated"])
            self.assertEqual("", stderr)

            denial_cases = [
                ("UPDATE automation_runs SET read_at=1", "raw_sql_write_denied"),
                ("SELECT 1; SELECT 2", "multi_statement_denied"),
                ("ATTACH DATABASE '/tmp/other.db' AS other", "raw_sql_write_denied"),
                ("PRAGMA journal_mode=WAL", "raw_sql_write_denied"),
            ]
            for sql, code in denial_cases:
                query.write_text(sql, encoding="utf-8")
                exit_code, payload, stderr = run_cli_json(
                    [
                        "--json",
                        "--codex-home",
                        str(stores.codex_home),
                        "raw",
                        "sql",
                        "--db",
                        "codex-dev",
                        "--query-file",
                        str(query),
                        "--readonly",
                    ]
                )
                self.assertEqual(1, exit_code)
                self.assertEqual(code, require_json_object(payload["error"], "error")["code"])
                self.assertEqual("", stderr)

            query.write_text("SELECT 1", encoding="utf-8")
            exit_code, payload, stderr = run_cli_json(
                [
                    "--json",
                    "--codex-home",
                    str(stores.codex_home),
                    "raw",
                    "sql",
                    "--db",
                    "codex-dev",
                    "--query-file",
                    str(query),
                    "--readonly",
                    "--limit",
                    "0",
                ]
            )
            self.assertEqual(1, exit_code)
            self.assertEqual("invalid_limit", require_json_object(payload["error"], "error")["code"])
            self.assertEqual("", stderr)

            exit_code, payload, stderr = run_cli_json(
                [
                    "--json",
                    "--codex-home",
                    str(stores.codex_home),
                    "raw",
                    "sql",
                    "--db",
                    "codex-dev",
                    "--query-file",
                    str(query),
                ]
            )
            self.assertEqual(2, exit_code)
            self.assertEqual("usage_error", require_json_object(payload["error"], "error")["code"])
            self.assertEqual("", stderr)

    def test_lsof_parser_fails_closed_on_ambiguous_output(self) -> None:
        valid, rows = parse_lsof_stdout(
            "COMMAND   PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
            "Codex   1234 user  3u   REG   1,23      100  456 /Users/example/.codex/state_5.sqlite\n"
        )
        self.assertTrue(valid)
        self.assertEqual("1234", rows[0]["pid"])

        valid, rows = parse_lsof_stdout(
            "COMMAND   PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
            "Codex   1234 tiziano\n"
        )
        self.assertFalse(valid)
        self.assertEqual([], rows)

    def test_lsof_handles_fails_closed_on_stderr_only_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state_5.sqlite"
            db_path.write_bytes(b"not a real db")
            result = subprocess.CompletedProcess(
                args=["lsof", str(db_path)],
                returncode=1,
                stdout="",
                stderr="lsof: status error on file: Operation not permitted\n",
            )
            with patch("cdx_care.processes.subprocess.run", return_value=result):
                available, rows = lsof_handles([db_path])

            self.assertFalse(available)
            self.assertEqual([], rows)

    def test_lsof_handles_fails_closed_on_any_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state_5.sqlite"
            db_path.write_bytes(b"not a real db")
            result = subprocess.CompletedProcess(
                args=["lsof", str(db_path)],
                returncode=0,
                stdout="COMMAND   PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n",
                stderr="lsof: warning: partial visibility\n",
            )
            with patch("cdx_care.processes.subprocess.run", return_value=result):
                available, rows = lsof_handles([db_path])

            self.assertFalse(available)
            self.assertEqual([], rows)

    def test_lsof_handles_uses_trusted_absolute_binary_not_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "state_5.sqlite"
            db_path.write_bytes(b"not a real db")
            trusted = root / "trusted-lsof"
            trusted.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            trusted.chmod(0o755)
            fake_dir = root / "fake-bin"
            fake_dir.mkdir()
            fake = fake_dir / "lsof"
            fake.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
            fake.chmod(0o755)
            result = subprocess.CompletedProcess(args=[str(trusted), str(db_path)], returncode=1, stdout="", stderr="")
            with patch.dict(os.environ, {"PATH": str(fake_dir)}), patch(
                "cdx_care.processes.TRUSTED_LSOF_PATH", trusted
            ), patch("cdx_care.processes.subprocess.run", return_value=result) as run:
                available, rows = lsof_handles([db_path])

            self.assertTrue(available)
            self.assertEqual([], rows)
            args = run.call_args.args[0]
            self.assertEqual(str(trusted), args[0])
