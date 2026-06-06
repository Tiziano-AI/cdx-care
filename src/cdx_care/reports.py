"""Read-only reports shared by doctor and diagnostics."""

from __future__ import annotations

from cdx_care.logs_compact import logs_physical_report
from cdx_care.paths import StorePaths
from cdx_care.session_repair import (
    jsonl_id_set,
    session_file_id_set,
    state_thread_ids,
    verify_session_file_alignment,
)
from cdx_care.types import JsonObject


def session_index_report(stores: StorePaths, state_ids: set[str], state: JsonObject) -> JsonObject:
    """Compare state thread IDs with JSONL session/history files."""
    index_ids = jsonl_id_set(stores.session_index, "id")
    history_ids = jsonl_id_set(stores.history, "session_id")
    session_file_ids = session_file_id_set(stores.codex_home / "sessions")
    valid_ids = state_ids or state_thread_ids(stores.db_path("state")) if stores.db_path("state").exists() else set()
    alignment = (
        verify_session_file_alignment(stores.db_path("state"), stores.codex_home / "sessions") if valid_ids else {}
    )
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
        "state_not_in_history": len(state_ids - history_ids),
        "session_index_rebuild_apply_supported": True,
        "history_apply_supported": False,
        "history_contract": "diagnostic_only_message_history",
        "session_file_alignment": alignment,
        "state_summary": state,
    }


def logs_report(stores: StorePaths) -> JsonObject:
    """Inspect logs DB physical health and compaction eligibility."""
    return logs_physical_report(stores.db_path("logs"))
