"""Disposable Codex support-root fixtures for cdx-care tests."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

from cdx_care.paths import StorePaths
from cdx_care.types import JsonObject


def require_actions(plan: dict[str, object]) -> list[dict[str, object]]:
    """Return typed planned actions from a plan fixture."""
    value = plan["planned_actions"]
    if not isinstance(value, list):
        raise AssertionError("planned_actions must be a list")
    actions: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            raise AssertionError("planned action must be an object")
        actions.append(item)
    return actions


def first_action(plan: dict[str, object], action_id: str) -> dict[str, object]:
    """Return a named action from a plan fixture."""
    for action in require_actions(plan):
        if action.get("id") == action_id:
            return action
    raise AssertionError(f"missing action: {action_id}")


def make_fixture(root: Path) -> StorePaths:
    """Create a disposable Codex support root fixture."""
    codex_home = root / ".codex"
    (codex_home / "sqlite").mkdir(parents=True)
    (codex_home / "automations" / "auto-good").mkdir(parents=True)
    (codex_home / "automations" / "auto-good" / "automation.toml").write_text('id = "auto-good"\n')
    stores = StorePaths(codex_home)
    create_state_db(stores.db_path("state"))
    create_codex_dev_db(stores.db_path("codex-dev"))
    create_memories_db(stores.db_path("memories"))
    create_logs_db(stores.db_path("logs"))
    create_empty_db(stores.db_path("goals"))
    create_memory_git(stores.memories_root)
    create_session_rollouts(stores.codex_home)
    stores.session_index.write_text(
        '{"id":"thread-good","thread_name":"stale","updated_at":"2020-01-01T00:00:00.000000Z"}\n'
        '{"id":"thread-default","thread_name":"legacy fallback","updated_at":"2020-01-01T00:00:00.000000Z"}\n'
        '{"id":"thread-index-only","thread_name":"orphan","updated_at":"2020-01-01T00:00:00.000000Z"}\n',
        encoding="utf-8",
    )
    stores.history.write_text(
        '{"session_id":"thread-good","text":"private","ts":1}\n'
        '{"session_id":"thread-history-orphan","text":"private orphan","ts":2}\n',
        encoding="utf-8",
    )
    return stores


def create_state_db(path: Path) -> None:
    """Create a minimal state DB."""
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE threads (
              id TEXT PRIMARY KEY,
              rollout_path TEXT,
              has_user_event INTEGER,
              title TEXT,
              first_user_message TEXT,
              updated_at_ms INTEGER
            );
            INSERT INTO threads(id, rollout_path, has_user_event, title, first_user_message, updated_at_ms)
            VALUES ('thread-good', 'sessions/2026/01/01/rollout-thread-good.jsonl', 1, 'valid', 'first good', 1000);
            INSERT INTO threads(id, rollout_path, has_user_event, title, first_user_message, updated_at_ms)
            VALUES (
              'thread-default', 'sessions/2026/01/01/rollout-thread-default.jsonl', 1,
              'same text', 'same text', 1500
            );
            INSERT INTO threads(id, rollout_path, has_user_event, title, first_user_message, updated_at_ms)
            VALUES (
              'thread-accepted', 'sessions/2026/01/01/rollout-thread-accepted.jsonl', 1,
              'accepted', 'first accepted', 2000
            );
            """
        )


def create_codex_dev_db(path: Path) -> None:
    """Create a minimal codex-dev DB."""
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE automation_runs (
              thread_id TEXT PRIMARY KEY,
              automation_id TEXT,
              status TEXT,
              read_at INTEGER,
              thread_title TEXT,
              created_at INTEGER,
              updated_at INTEGER
            );
            CREATE TABLE inbox_items (
              id TEXT PRIMARY KEY,
              title TEXT,
              description TEXT,
              thread_id TEXT,
              read_at INTEGER,
              created_at INTEGER
            );
            CREATE TABLE automations (
              id TEXT PRIMARY KEY,
              status TEXT,
              updated_at INTEGER
            );
            INSERT INTO automation_runs
            VALUES ('thread-good', 'auto-good', 'PENDING_REVIEW', NULL, 'valid', 10, 20);
            INSERT INTO automation_runs
            VALUES ('thread-accepted', 'auto-good', 'ACCEPTED', NULL, 'accepted', 10, 23);
            INSERT INTO automation_runs
            VALUES ('thread-archived', 'auto-good', 'ARCHIVED', NULL, 'archived', 11, 21);
            INSERT INTO automation_runs
            VALUES ('thread-missing', 'auto-good', 'PENDING_REVIEW', NULL, 'missing', 12, 22);
            INSERT INTO inbox_items
            VALUES ('inbox-orphan', 'private title', 'private description', 'thread-missing', NULL, 30);
            INSERT INTO inbox_items
            VALUES ('inbox-valid', 'private title', 'private description', 'thread-good', NULL, 31);
            INSERT INTO automations VALUES ('auto-good', 'ACTIVE', 40);
            INSERT INTO automations VALUES ('db-only-paused', 'PAUSED', 41);
            """
        )


def create_memories_db(path: Path) -> None:
    """Create a minimal memories DB."""
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE jobs (
              kind TEXT,
              job_key TEXT,
              status TEXT,
              worker_id TEXT,
              ownership_token TEXT,
              started_at INTEGER,
              finished_at INTEGER,
              lease_until INTEGER,
              retry_at INTEGER,
              retry_remaining INTEGER,
              last_error TEXT,
              input_watermark INTEGER,
              last_success_watermark INTEGER,
              PRIMARY KEY(kind, job_key)
            );
            CREATE TABLE stage1_outputs (
              thread_id TEXT PRIMARY KEY,
              source_updated_at INTEGER,
              selected_for_phase2 INTEGER
            );
            INSERT INTO jobs
            VALUES ('memory_stage1', 'thread-memory-error', 'error', 'old-worker', 'old-token', 100, 200, 0, NULL, 0,
                    'context window exceeded', 0, 0);
            INSERT INTO jobs
            VALUES ('memory_stage1', 'thread-memory-retryable', 'error', NULL, NULL, NULL, NULL, NULL, NULL, 1,
                    'temporary error', 0, 0);
            INSERT INTO jobs
            VALUES ('memory_consolidate_global', 'global', 'done', NULL, NULL, 300, 400, NULL, NULL, 3,
                    NULL, 10, 10);
            INSERT INTO stage1_outputs VALUES ('thread-memory-error', 500, 1);
            """
        )


def create_logs_db(path: Path) -> None:
    """Create a minimal logs DB."""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.executescript(
            """
            CREATE TABLE logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              level TEXT,
              feedback_log_body TEXT
            );
            INSERT INTO logs(level, feedback_log_body) VALUES ('INFO', 'private');
            """
        )
        for index in range(20):
            conn.execute(
                "INSERT INTO logs(level, feedback_log_body) VALUES ('DEBUG', ?)",
                ("x" * 20_000 if index % 2 == 0 else "y" * 20_000,),
            )
        conn.execute("DELETE FROM logs WHERE level='DEBUG'")
        conn.commit()


def create_empty_db(path: Path) -> None:
    """Create an empty SQLite DB."""
    conn = sqlite3.connect(path)
    conn.close()


def create_memory_git(path: Path) -> None:
    """Create a memory git repo with tracked ignored .DS_Store files."""
    path.mkdir(parents=True)
    (path / "extensions").mkdir()
    (path / ".gitignore").write_text(".DS_Store\n", encoding="utf-8")
    (path / ".DS_Store").write_text("finder", encoding="utf-8")
    (path / "extensions" / ".DS_Store").write_text("finder", encoding="utf-8")
    run(["git", "init", str(path)])
    run(["git", "-C", str(path), "add", "-f", ".gitignore", ".DS_Store", "extensions/.DS_Store"])
    run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=cdx-care-fixture",
            "-c",
            "user.email=cdx-care-fixture@local.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "Initialize fixture memory repo",
        ]
    )


def create_session_rollouts(codex_home: Path) -> None:
    """Create rollout files whose session_meta rows prove state thread IDs."""
    root = codex_home / "sessions" / "2026" / "01" / "01"
    root.mkdir(parents=True)
    for thread_id in ("thread-good", "thread-default", "thread-accepted"):
        row = {"type": "session_meta", "payload": {"id": thread_id}}
        (root / f"rollout-{thread_id}.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")


def run(args: list[str]) -> None:
    """Run a fixture shell command."""
    result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise AssertionError(f"command failed: {args}: {result.stderr}")


def fake_lsof_handles(_paths: list[Path]) -> tuple[bool, list[JsonObject]]:
    """Pretend a DB file has an open process handle."""
    return True, [{"pid": "123", "name": "state_5.sqlite"}]


def fake_lsof_unavailable(_paths: list[Path]) -> tuple[bool, list[JsonObject]]:
    """Pretend lsof cannot provide a reliable handle proof."""
    return False, []


def remove_global_consolidation_job(db_path: Path) -> None:
    """Force the insert branch for global consolidation tests."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("DELETE FROM jobs WHERE kind='memory_consolidate_global' AND job_key='global'")
        conn.commit()


def insert_global_consolidation_job(db_path: Path, *, status: str, input_watermark: int) -> None:
    """Insert the native global memory row after an insert plan is created."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO jobs
            VALUES ('memory_consolidate_global', 'global', ?, NULL, NULL, NULL, NULL, NULL, NULL, 3,
                    NULL, ?, 0)
            """,
            (status, input_watermark),
        )
        conn.commit()


def set_global_consolidation_job(
    db_path: Path,
    *,
    status: str,
    worker_id: str | None,
    ownership_token: str | None,
    lease_until: int | None,
    last_error: str | None,
    input_watermark: int,
    last_success_watermark: int,
) -> None:
    """Set the native global memory row to a precise planner state."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status=?, worker_id=?, ownership_token=?, lease_until=?, last_error=?,
                input_watermark=?, last_success_watermark=?
            WHERE kind='memory_consolidate_global' AND job_key='global'
            """,
            (status, worker_id, ownership_token, lease_until, last_error, input_watermark, last_success_watermark),
        )
        conn.commit()


def set_stage1_error(db_path: Path, *, job_key: str, last_error: str, retry_remaining: int) -> None:
    """Set one Stage 1 job error for planner tests."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status='error', last_error=?, retry_remaining=?, worker_id=NULL, ownership_token=NULL,
                started_at=100, finished_at=200, lease_until=0, retry_at=NULL
            WHERE kind='memory_stage1' AND job_key=?
            """,
            (last_error, retry_remaining, job_key),
        )
        conn.commit()


def stat_snapshot(path: Path) -> dict[str, object]:
    """Mirror the public plan db_stat shape for tampered-plan tests."""
    stat = path.stat()
    return {"device": stat.st_dev, "inode": stat.st_ino, "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def actions(plan: dict[str, object]) -> list[dict[str, object]]:
    """Return planned actions as JSON objects."""
    value = plan["planned_actions"]
    if not isinstance(value, list):
        raise AssertionError("planned_actions must be a list")
    return [row for row in value if isinstance(row, dict)]
