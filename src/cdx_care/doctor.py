"""Read-only Codex local-state doctor."""

from __future__ import annotations

import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from cdx_care import VERSION
from cdx_care.memory_reports import memories_report
from cdx_care.paths import StorePaths
from cdx_care.processes import existing_lsof_targets, lsof_handles
from cdx_care.report_utils import ROW_LIMIT, collection_metadata, finding
from cdx_care.reports import logs_report, session_index_report
from cdx_care.sqlite_tools import (
    connect_readonly,
    quick_check,
    schema_fingerprint,
    table_columns,
    table_names,
    value_hash,
)
from cdx_care.timeutil import iso_now
from cdx_care.types import JsonObject, JsonValue, require_json_object_list

ACTIONABLE_RUN_STATUSES = ("PENDING_REVIEW", "ACCEPTED")


def doctor_report(stores: StorePaths) -> JsonObject:
    """Return a read-only doctor report."""
    db_paths = stores.db_paths()
    lsof_available, handles = lsof_handles(list(db_paths.values()))
    lsof_target_count = len(existing_lsof_targets(list(db_paths.values())))
    findings: list[JsonValue] = []
    dbs: dict[str, JsonValue] = {}
    for name, path in db_paths.items():
        dbs[name] = db_info(path)
    if not stores.codex_home.exists():
        findings.append(
            finding(
                "codex.support_root.missing",
                "error",
                "Codex support root does not exist; check --codex-home or initialize Codex first.",
                {"support_root": str(stores.codex_home)},
            )
        )
    for name, path in db_paths.items():
        if not path.exists():
            findings.append(
                finding(
                    "codex.db.missing",
                    "error",
                    "Expected Codex local-state DB is missing.",
                    {"db": name, "path": str(path)},
                )
            )
    state = state_report(stores)
    state_available, state_ids = load_state_thread_ids_checked(stores.db_path("state"))
    codex_dev = codex_dev_report(stores, state_available, state_ids)
    memories = memories_report(stores)
    sessions = session_index_report(stores, state_ids, state)
    logs = logs_report(stores)
    ds_store = ds_store_report(stores.memories_root)
    if not lsof_available:
        findings.append(
            finding(
                "cdx-care.lsof.unavailable",
                "error",
                "lsof is unavailable or ambiguous; apply will deny DB writes until handle proof works.",
                {"db_count": len(db_paths)},
            )
        )
    if handles:
        findings.append(
            finding(
                "codex.app.open_db_handles",
                "warn",
                "Codex has open handles on local DB files; apply will deny DB writes.",
                {"handle_count": len(handles)},
            )
        )
    if db_paths["codex-dev"].exists() and not state_available:
        findings.append(
            finding(
                "codex.state_threads.unavailable",
                "error",
                "state_5.sqlite threads proof is unavailable; state-dependent automation/inbox diagnosis is unknown.",
                {"state_db": str(stores.db_path("state"))},
            )
        )
    for row in require_json_object_list(codex_dev["findings"], "codex_dev.findings"):
        findings.append(row)
    for row in require_json_object_list(memories["findings"], "memories.findings"):
        findings.append(row)
    for row in require_json_object_list(ds_store["findings"], "ds_store.findings"):
        findings.append(row)
    return {
        "schema_version": 1,
        "tool": "cdx-care",
        "version": VERSION,
        "generated_at": iso_now(),
        "ok": not any(isinstance(row, dict) and row.get("severity") == "error" for row in findings),
        "support_root": str(stores.codex_home),
        "codex_closed": lsof_available and lsof_target_count > 0 and not handles,
        "lsof": {"available": lsof_available, "target_count": lsof_target_count, "handles": handles},
        "dbs": dbs,
        "codex_dev": codex_dev["data"],
        "state": state,
        "memories": memories["data"],
        "sessions": sessions,
        "logs": logs,
        "memory_git": ds_store["data"],
        "findings": findings,
    }


def db_info(path: Path) -> JsonObject:
    """Return basic DB file and integrity metadata."""
    wal = Path(str(path) + "-wal")
    shm = Path(str(path) + "-shm")
    info: JsonObject = {
        "path": str(path),
        "exists": path.exists(),
        "bytes": path.stat().st_size if path.exists() else None,
        "wal_bytes": wal.stat().st_size if wal.exists() else None,
        "shm_bytes": shm.stat().st_size if shm.exists() else None,
        "quick_check": None,
    }
    if path.exists():
        try:
            info["quick_check"] = quick_check(path)
        except sqlite3.Error as error:
            info["quick_check_error"] = str(error)
    return info


def codex_dev_report(stores: StorePaths, state_available: bool, state_ids: set[str]) -> JsonObject:
    """Inspect the Codex app DB."""
    path = stores.db_path("codex-dev")
    if not path.exists():
        return {"data": {"exists": False}, "findings": []}
    toml_ids = {p.parent.name for p in stores.automations_root.glob("*/automation.toml")}
    findings: list[JsonValue] = []
    with closing(connect_readonly(path)) as conn:
        tables = table_names(conn)
        fingerprints = schema_fingerprint(
            conn, [table for table in ("automation_runs", "automations", "inbox_items") if table in tables]
        )
        runs = automation_runs_report(conn, state_available, state_ids)
        inbox = inbox_report(conn, state_available, state_ids)
        automations = automations_report(conn, toml_ids)
    for table, report in (("automation_runs", runs), ("inbox_items", inbox), ("automations", automations)):
        if report.get("exists") is False:
            findings.append(
                finding(
                    "codex.codex_dev.table_missing",
                    "error",
                    "Expected codex-dev table is missing; diagnosis is incomplete until schema drift is reviewed.",
                    {"table": table, "db": "codex-dev", "path": str(path)},
                )
            )
    if runs.get("unread_actionable_count"):
        findings.append(
            finding(
                "codex.automation_badge.unread_run_instances",
                "warn",
                "Automation badge is counting unread run instances, not visible automation definitions.",
                {"count": runs["unread_actionable_count"], "statuses": list(ACTIONABLE_RUN_STATUSES)},
            )
        )
    if runs.get("broken_unread_count"):
        findings.append(
            finding(
                "codex.automation_runs.broken_unread",
                "warn",
                "Unread automation run rows are not navigable and can be marked read by cdx-care.",
                {"count": runs["broken_unread_count"]},
            )
        )
    if inbox.get("orphan_unread_count"):
        findings.append(
            finding(
                "codex.inbox.orphan_unread",
                "warn",
                "Unread inbox rows point at missing state threads.",
                {"count": inbox["orphan_unread_count"]},
            )
        )
    if automations.get("db_only_paused_count"):
        findings.append(
            finding(
                "codex.automations.db_only_paused",
                "info",
                "DB contains paused automation definitions with no TOML definition.",
                {"count": automations["db_only_paused_count"]},
            )
        )
    return {
        "data": {
            "exists": True,
            "path": str(path),
            "schema_fingerprint": fingerprints,
            "state_threads_available": state_available,
            "tables": tables,
            "automation_runs": runs,
            "inbox": inbox,
            "automations": automations,
        },
        "findings": findings,
    }


def load_state_thread_ids(path: Path) -> set[str]:
    """Load known state thread IDs."""
    available, thread_ids = load_state_thread_ids_checked(path)
    if not available:
        return set()
    return thread_ids


def load_state_thread_ids_checked(path: Path) -> tuple[bool, set[str]]:
    """Load known state thread IDs with an availability proof bit."""
    if not path.exists():
        return False, set()
    try:
        with closing(connect_readonly(path)) as conn:
            if "threads" not in table_names(conn):
                return False, set()
            return True, {str(row[0]) for row in conn.execute("SELECT id FROM threads").fetchall()}
    except sqlite3.Error:
        return False, set()


def automation_runs_report(conn: sqlite3.Connection, state_available: bool, state_ids: set[str]) -> JsonObject:
    """Summarize automation run state."""
    if "automation_runs" not in table_names(conn):
        return {"exists": False}
    rows = conn.execute(
        """
        SELECT thread_id, automation_id, status, read_at, thread_title, created_at, updated_at
        FROM automation_runs
        ORDER BY updated_at DESC
        """
    ).fetchall()
    unread_by_status: Counter[str] = Counter()
    review_rows: list[JsonValue] = []
    broken_rows: list[JsonValue] = []
    unknown_unread = 0
    for row in rows:
        status = str(row["status"])
        unread = row["read_at"] is None
        thread_id = str(row["thread_id"]) if row["thread_id"] is not None else ""
        if unread:
            unread_by_status[status] += 1
        if unread and status in ACTIONABLE_RUN_STATUSES:
            title = str(row["thread_title"] or "")
            state_thread_exists: JsonValue = thread_id in state_ids if state_available else None
            item: JsonObject = {
                "thread_id": thread_id,
                "automation_id": str(row["automation_id"]),
                "status": status,
                "updated_at": int(row["updated_at"]),
                "thread_title_present": bool(title),
                "thread_title_sha256": value_hash(title) if title else None,
                "state_thread_proof_available": state_available,
                "state_thread_exists": state_thread_exists,
            }
            review_rows.append(item)
        if unread and (status == "ARCHIVED" or (state_available and thread_id not in state_ids)):
            broken_rows.append(
                {
                    "thread_id": thread_id,
                    "automation_id": str(row["automation_id"]),
                    "status": status,
                    "read_at": None,
                    "updated_at": int(row["updated_at"]),
                    "state_thread_proof_available": state_available,
                    "state_thread_exists": thread_id in state_ids if state_available else None,
                }
            )
        elif unread and not state_available:
            unknown_unread += 1
    returned_review_rows = review_rows[:ROW_LIMIT]
    returned_broken_rows = broken_rows[:ROW_LIMIT]
    return {
        "exists": True,
        "total": len(rows),
        "state_threads_available": state_available,
        "unread_by_status": dict(sorted(unread_by_status.items())),
        "unread_actionable_count": sum(unread_by_status[status] for status in ACTIONABLE_RUN_STATUSES),
        "review_rows": returned_review_rows,
        "review_rows_meta": collection_metadata(len(review_rows), len(returned_review_rows)),
        "broken_unread_count": len(broken_rows),
        "broken_unread_rows": returned_broken_rows,
        "broken_unread_rows_meta": collection_metadata(len(broken_rows), len(returned_broken_rows)),
        "state_unknown_unread_count": unknown_unread,
    }


def inbox_report(conn: sqlite3.Connection, state_available: bool, state_ids: set[str]) -> JsonObject:
    """Summarize inbox rows."""
    if "inbox_items" not in table_names(conn):
        return {"exists": False}
    rows = conn.execute("SELECT id, thread_id, read_at, created_at FROM inbox_items ORDER BY created_at").fetchall()
    orphans: list[JsonValue] = []
    unread_total = 0
    for row in rows:
        thread_id = str(row["thread_id"] or "")
        unread = row["read_at"] is None
        if unread:
            unread_total += 1
        if unread and state_available and thread_id and thread_id not in state_ids:
            orphans.append(
                {
                    "id": str(row["id"]),
                    "thread_id": thread_id,
                    "read_at": None,
                    "created_at": int(row["created_at"]),
                }
            )
    returned_orphans = orphans[:ROW_LIMIT]
    return {
        "exists": True,
        "total": len(rows),
        "state_threads_available": state_available,
        "unread_total": unread_total,
        "orphan_unread_count": len(orphans),
        "orphan_unread_rows": returned_orphans,
        "orphan_unread_rows_meta": collection_metadata(len(orphans), len(returned_orphans)),
        "orphan_detection_unknown_unread_count": unread_total if not state_available else 0,
    }


def automations_report(conn: sqlite3.Connection, toml_ids: set[str]) -> JsonObject:
    """Summarize automation definitions."""
    if "automations" not in table_names(conn):
        return {"exists": False}
    rows = conn.execute("SELECT id, status, updated_at FROM automations ORDER BY id").fetchall()
    by_status: Counter[str] = Counter(str(row["status"]) for row in rows)
    db_only_paused = [
        {"id": str(row["id"]), "status": str(row["status"]), "updated_at": int(row["updated_at"])}
        for row in rows
        if str(row["id"]) not in toml_ids and str(row["status"]) == "PAUSED"
    ]
    returned_db_only = db_only_paused[:ROW_LIMIT]
    return {
        "exists": True,
        "total": len(rows),
        "by_status": dict(sorted(by_status.items())),
        "toml_definition_count": len(toml_ids),
        "db_only_paused_count": len(db_only_paused),
        "db_only_paused_rows": returned_db_only,
        "db_only_paused_rows_meta": collection_metadata(len(db_only_paused), len(returned_db_only)),
    }


def state_report(stores: StorePaths) -> JsonObject:
    """Inspect state_5 threads metadata."""
    path = stores.db_path("state")
    if not path.exists():
        return {"exists": False}
    with closing(connect_readonly(path)) as conn:
        tables = table_names(conn)
        if "threads" not in tables:
            return {"exists": True, "threads_exists": False}
        total = int(conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0])
        cols = table_columns(conn, "threads")
        has_user_counts: dict[str, JsonValue] = {}
        if "has_user_event" in cols:
            for row in conn.execute("SELECT has_user_event, COUNT(*) FROM threads GROUP BY has_user_event").fetchall():
                has_user_counts[str(row[0])] = int(row[1])
        return {
            "exists": True,
            "schema_fingerprint": schema_fingerprint(conn, ["threads"]),
            "threads_total": total,
            "has_user_event_counts": has_user_counts,
        }


def ds_store_report(memory_root: Path) -> JsonObject:
    """Inspect tracked .DS_Store files in the memory git repo."""
    from cdx_care.git_tools import head_tracked_paths, tracked_paths

    targets = [".DS_Store", "extensions/.DS_Store"]
    tracked_index = tracked_paths(memory_root, targets)
    tracked_head = head_tracked_paths(memory_root, targets)
    tracked = sorted(set(tracked_index) | set(tracked_head))
    findings: list[JsonValue] = []
    if tracked:
        findings.append(
            finding(
                "codex.memory.git_tracked_ds_store",
                "warn",
                "Finder metadata is tracked in the memory git repo; it should be untracked and ignored.",
                {"paths": tracked, "index_paths": tracked_index, "head_paths": tracked_head},
            )
        )
    return {
        "data": {
            "repo": str(memory_root),
            "tracked_ds_store": tracked,
            "tracked_index_ds_store": tracked_index,
            "tracked_head_ds_store": tracked_head,
        },
        "findings": findings,
    }
