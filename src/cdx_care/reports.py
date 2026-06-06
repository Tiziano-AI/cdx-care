"""Read-only reports shared by doctor and diagnostics."""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path

from cdx_care.paths import StorePaths
from cdx_care.sqlite_tools import connect_readonly, table_names
from cdx_care.types import JsonObject, JsonValue, require_json_object


def session_index_report(stores: StorePaths, state_ids: set[str], state: JsonObject) -> JsonObject:
    """Compare state thread IDs with JSONL index IDs without planning writes."""
    index_ids = jsonl_id_set(stores.session_index, "id")
    history_ids = jsonl_id_set(stores.history, "session_id")
    session_file_ids = session_file_id_set(stores.codex_home / "sessions")
    return {
        "state_thread_count": len(state_ids),
        "session_file_id_count": len(session_file_ids),
        "session_file_ids_not_in_state": len(session_file_ids - state_ids),
        "state_not_in_session_file_ids": len(state_ids - session_file_ids),
        "session_index_count": len(index_ids),
        "history_session_count": len(history_ids),
        "session_index_not_in_state": len(index_ids - state_ids),
        "state_not_in_session_index": len(state_ids - index_ids),
        "history_not_in_state": len(history_ids - state_ids),
        "reindex_apply_supported": False,
        "state_summary": state,
    }


def session_file_id_set(root: Path) -> set[str]:
    """Collect session IDs from rollout file names without reading transcript bodies."""
    if not root.exists():
        return set()
    ids: set[str] = set()
    for path in root.glob("*/*/*/rollout-*.jsonl"):
        name = path.name
        if not name.startswith("rollout-") or not name.endswith(".jsonl"):
            continue
        parts = name.removesuffix(".jsonl").split("-")
        if len(parts) >= 7:
            ids.add("-".join(parts[-5:]))
    return ids


def jsonl_id_set(path: Path, key: str) -> set[str]:
    """Read a JSONL file and collect a top-level string ID key."""
    if not path.exists():
        return set()
    ids: set[str] = set()
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, Mapping):
                row = require_json_object(payload, "jsonl row")
                value = row.get(key)
                if isinstance(value, str):
                    ids.add(value)
    return ids


def logs_report(stores: StorePaths) -> JsonObject:
    """Inspect logs DB physical health only."""
    path = stores.db_path("logs")
    if not path.exists():
        return {"exists": False}
    with closing(connect_readonly(path)) as conn:
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        tables = table_names(conn)
        row_count = 0
        by_level: dict[str, JsonValue] = {}
        if "logs" in tables:
            row_count = int(conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0])
            for row in conn.execute("SELECT level, COUNT(*) FROM logs GROUP BY level ORDER BY level").fetchall():
                by_level[str(row[0])] = int(row[1])
    return {
        "exists": True,
        "page_size": page_size,
        "page_count": page_count,
        "freelist_count": freelist_count,
        "reclaimable_bytes": page_size * freelist_count,
        "row_count": row_count,
        "by_level": by_level,
        "compaction_apply_supported": False,
    }
