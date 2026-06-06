"""Support-root path admission for guarded write targets."""

from __future__ import annotations

from pathlib import Path

from cdx_care.errors import CdxCareError
from cdx_care.paths import StorePaths
from cdx_care.policy import normalized_path
from cdx_care.types import JsonObject


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
        ensure_support_path(stores.codex_home, db_path, "DB file")
        for suffix in ("-wal", "-shm"):
            sibling = Path(str(db_path) + suffix)
            if sibling.exists() or sibling.is_symlink():
                ensure_support_path(stores.codex_home, sibling, "DB sibling")
    for action in actions:
        if action.get("type") == "git_rm_cached":
            ensure_support_path(stores.codex_home, Path(str(action["repo"])), "memory git repo")


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
