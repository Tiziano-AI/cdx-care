"""Support-root path admission for guarded write targets."""

from __future__ import annotations

from pathlib import Path

from cdx_care.errors import CdxCareError
from cdx_care.git_tools import git_repository_layout
from cdx_care.paths import StorePaths
from cdx_care.policy import normalized_path
from cdx_care.types import JsonObject

READ_STATE_LANES = {"automations.hide_broken_only", "automations.clear_current_badge", "inbox.orphan_mark_read"}


def support_root_resource_count(stores: StorePaths) -> int:
    """Count known Codex state resources under the support root."""
    candidates = [
        *stores.db_paths().values(),
        stores.automations_root,
        stores.memories_root,
        stores.session_index,
        stores.history,
    ]
    return sum(1 for path in candidates if path.exists() or path.is_symlink())


def preflight_support_root_for_writes(stores: StorePaths) -> None:
    """Deny managed writes when the support root is missing or not Codex-shaped."""
    root = stores.codex_home
    if root.is_symlink():
        raise CdxCareError(f"Codex support root is a symlink: {root}", code="unsafe_support_path")
    if not root.exists():
        raise CdxCareError(f"Codex support root does not exist: {root}", code="support_root_missing")
    if not root.is_dir():
        raise CdxCareError(f"Codex support root is not a directory: {root}", code="support_root_invalid")
    if support_root_resource_count(stores) == 0:
        raise CdxCareError(
            f"Codex support root has no known local-state resources: {root}",
            code="support_root_unrecognized",
        )


def preflight_managed_artifact_path(stores: StorePaths, path: Path, label: str) -> None:
    """Deny cdx-care artifact writes through symlinked or escaped support paths."""
    preflight_support_root_for_writes(stores)
    ensure_support_path(stores.codex_home, path, label)


def preflight_support_paths(stores: StorePaths, db_paths: list[Path], actions: list[JsonObject]) -> None:
    """Deny writes when target paths traverse symlinks or escape the support root."""
    preflight_support_root_for_writes(stores)
    for db_path in db_paths:
        ensure_db_family_support_path(stores, db_path, "DB file")
    if any(action.get("lane") in READ_STATE_LANES for action in actions):
        ensure_db_family_support_path(stores, stores.db_path("state"), "state source DB")
    for action in actions:
        if action.get("type") == "git_rm_cached":
            ensure_git_repo_support_path(stores, Path(str(action["repo"])))
        if action.get("type") == "jsonl_rewrite":
            ensure_support_path(stores.codex_home, Path(str(action["path"])), "JSONL rewrite target")
            ensure_db_family_support_path(stores, Path(str(action["source_db_path"])), "JSONL source DB")
            if action.get("lane") == "sessions.rebuild_session_index":
                ensure_sessions_source_paths(stores)
        if action.get("type") == "sqlite_compact":
            ensure_support_path(stores.codex_home, Path(str(action["db_path"])), "SQLite compaction DB")


def ensure_db_family_support_path(stores: StorePaths, db_path: Path, label: str) -> None:
    """Deny a DB file family when the DB or existing WAL/SHM siblings are unsafe sources/targets."""
    ensure_support_path(stores.codex_home, db_path, label)
    for suffix in ("-wal", "-shm"):
        sibling = Path(str(db_path) + suffix)
        if sibling.exists() or sibling.is_symlink():
            ensure_support_path(stores.codex_home, sibling, f"{label} sibling")


def ensure_sessions_source_paths(stores: StorePaths) -> None:
    """Deny session-index repair when rollout source proof traverses symlinks."""
    sessions_root = stores.codex_home / "sessions"
    ensure_support_path(stores.codex_home, sessions_root, "sessions source root")
    if not sessions_root.exists():
        return
    for path in sorted({*sessions_root.glob("*"), *sessions_root.glob("*/*"), *sessions_root.glob("*/*/*")}):
        ensure_support_path(stores.codex_home, path, "sessions source directory")
    for path in sorted(sessions_root.glob("*/*/*/rollout-*.jsonl")):
        ensure_support_path(stores.codex_home, path, "rollout source file")


def ensure_git_repo_support_path(stores: StorePaths, repo: Path) -> None:
    """Deny git mutations unless all git authority paths stay inside the support root."""
    ensure_support_path(stores.codex_home, repo, "memory git repo")
    layout = git_repository_layout(repo)
    worktree = Path(str(layout["worktree"]))
    git_dir = Path(str(layout["git_dir"]))
    git_common_dir = Path(str(layout["git_common_dir"]))
    expected_git_dir = stores.memories_root / ".git"
    if worktree.resolve(strict=False) != stores.memories_root.resolve(strict=False):
        raise CdxCareError("memory git worktree must be the Codex memory repo", code="unsafe_support_path")
    if git_dir.resolve(strict=False) != expected_git_dir.resolve(strict=False):
        raise CdxCareError("memory git dir must be the Codex memory repo .git directory", code="unsafe_support_path")
    if git_common_dir.resolve(strict=False) != expected_git_dir.resolve(strict=False):
        raise CdxCareError(
            "memory git common dir must be the Codex memory repo .git directory",
            code="unsafe_support_path",
        )
    ensure_support_path(stores.codex_home, stores.memories_root, "memory git worktree")
    ensure_support_path(stores.codex_home, expected_git_dir, "memory git dir")
    ensure_support_path(stores.codex_home, git_common_dir, "memory git common dir")


def ensure_support_path(root: Path, path: Path, label: str) -> None:
    """Fail closed if a write target leaves root or uses symlink components."""
    normalized_root = normalized_path(root)
    normalized_target = normalized_path(path)
    if normalized_target != normalized_root and normalized_root not in normalized_target.parents:
        raise CdxCareError(f"{label} escapes Codex support root: {path}", code="unsafe_support_path")
    cursor = normalized_root
    if cursor.is_symlink():
        raise CdxCareError(f"Codex support root is a symlink: {root}", code="unsafe_support_path")
    relative_parts = normalized_target.relative_to(normalized_root).parts
    for part in relative_parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise CdxCareError(f"{label} traverses a symlink: {cursor}", code="unsafe_support_path")
