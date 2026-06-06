"""Closed target admission for non-row update plan actions."""

from __future__ import annotations

from pathlib import Path

from cdx_care.errors import CdxCareError
from cdx_care.paths import StorePaths
from cdx_care.policy_checks import normalized_path, require_schema_fingerprint, string_list
from cdx_care.types import JsonObject

DS_STORE_PATHS = {".DS_Store", "extensions/.DS_Store"}
DS_STORE_COMMIT_MESSAGE = "Untrack Codex memory Finder metadata"


def validate_sqlite_compact_target(stores: StorePaths, action: JsonObject) -> None:
    """Validate a planned SQLite compaction against the closed v1 policy."""
    if action.get("lane") != "logs.compact_freelist" or action.get("db") != "logs":
        raise CdxCareError("SQLite compaction action is outside cdx-care policy", code="action_target_denied")
    planned_path = action.get("db_path")
    if not isinstance(planned_path, str):
        raise CdxCareError("SQLite compaction action missing db_path", code="invalid_plan")
    if normalized_path(Path(planned_path)) != normalized_path(stores.db_path("logs")):
        raise CdxCareError("SQLite compaction path must be logs_2.sqlite", code="db_path_mismatch")
    if action.get("method") != "vacuum":
        raise CdxCareError("SQLite compaction method must be vacuum", code="action_target_denied")
    require_schema_fingerprint(action)
    schema_tables = action.get("schema_tables")
    if not isinstance(schema_tables, list) or not all(isinstance(row, str) for row in schema_tables):
        raise CdxCareError("SQLite compaction schema_tables must be strings", code="invalid_plan")
    reclaimable = action.get("reclaimable_bytes")
    if not isinstance(reclaimable, int) or reclaimable <= 0:
        raise CdxCareError("SQLite compaction requires positive reclaimable_bytes", code="action_target_denied")


def validate_jsonl_rewrite_target(stores: StorePaths, action: JsonObject) -> None:
    """Validate a planned JSONL rewrite against the closed v1 policy."""
    lane = action.get("lane")
    if lane == "sessions.rebuild_session_index":
        expected_path = stores.session_index
    else:
        raise CdxCareError("JSONL rewrite action is outside cdx-care policy", code="action_target_denied")
    planned_path = action.get("path")
    source_db = action.get("source_db")
    source_db_path = action.get("source_db_path")
    if not isinstance(planned_path, str) or not isinstance(source_db_path, str):
        raise CdxCareError("JSONL rewrite action paths must be strings", code="invalid_plan")
    if normalized_path(Path(planned_path)) != normalized_path(expected_path):
        raise CdxCareError("JSONL rewrite target path is outside cdx-care policy", code="action_target_denied")
    if source_db != "state" or normalized_path(Path(source_db_path)) != normalized_path(stores.db_path("state")):
        raise CdxCareError("JSONL rewrite source must be state_5.sqlite", code="action_target_denied")
    source = action.get("source")
    target_stat = action.get("target_stat")
    if not isinstance(source, dict) or not isinstance(target_stat, dict):
        raise CdxCareError("JSONL rewrite action missing source or target stat", code="invalid_plan")
    for key in ("desired_sha256", "desired_bytes"):
        if key not in action:
            raise CdxCareError(f"JSONL rewrite action missing {key}", code="invalid_plan")
    if not isinstance(action.get("desired_sha256"), str) or not isinstance(action.get("desired_bytes"), int):
        raise CdxCareError("JSONL rewrite desired output metadata is invalid", code="invalid_plan")


def validate_git_target(stores: StorePaths, action: JsonObject) -> None:
    """Validate a planned git hygiene action against the closed v1 policy."""
    if action.get("lane") != "memory.git_hygiene":
        raise CdxCareError("git action lane is outside cdx-care policy", code="git_target_denied")
    repo = action.get("repo")
    if not isinstance(repo, str):
        raise CdxCareError("git action repo must be a string", code="invalid_plan")
    if normalized_path(Path(repo)) != normalized_path(stores.memories_root):
        raise CdxCareError("git action repo must be the Codex memory repo", code="git_target_denied")
    paths_value = action.get("paths")
    if not isinstance(paths_value, list):
        raise CdxCareError("git action paths must be strings", code="invalid_plan")
    paths = string_list(paths_value, "paths")
    if not paths or len(set(paths)) != len(paths) or not set(paths).issubset(DS_STORE_PATHS):
        raise CdxCareError("git action paths must be the admitted memory .DS_Store paths", code="git_target_denied")
    if action.get("commit") is not True or action.get("commit_message") != DS_STORE_COMMIT_MESSAGE:
        raise CdxCareError("git action must commit the admitted .DS_Store untracking", code="git_target_denied")
