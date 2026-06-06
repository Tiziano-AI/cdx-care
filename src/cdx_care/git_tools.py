"""Git helpers for local hygiene lanes."""

from __future__ import annotations

import subprocess
from pathlib import Path

from cdx_care.errors import CdxCareError
from cdx_care.types import JsonObject


def run_git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a git command in a repository."""
    return subprocess.run(["git", "-C", str(repo), *args], check=False, capture_output=True, text=True, timeout=30)


def tracked_paths(repo: Path, paths: list[str]) -> list[str]:
    """Return paths tracked by git under repo."""
    if not (repo / ".git").exists():
        return []
    result = run_git(repo, ["ls-files", "--", *paths])
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def ignored_paths(repo: Path, paths: list[str]) -> list[str]:
    """Return paths ignored by git rules even when they are currently tracked."""
    result = run_git(repo, ["check-ignore", "--no-index", "--", *paths])
    if result.returncode == 1:
        return []
    if result.returncode != 0:
        raise CdxCareError(f"git check-ignore failed in {repo}: {result.stderr.strip()}", code="git_preflight_failed")
    return [line for line in result.stdout.splitlines() if line]


def tracked_status_paths(repo: Path) -> list[str]:
    """Return tracked dirty/staged paths from porcelain status."""
    result = run_git(repo, ["status", "--porcelain=v1", "--untracked-files=no", "--"])
    if result.returncode != 0:
        raise CdxCareError(f"git status failed in {repo}: {result.stderr.strip()}", code="git_preflight_failed")
    rows: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        rows.append(path)
    return rows


def git_hygiene_preflight(repo: Path, paths: list[str]) -> JsonObject:
    """Verify memory git hygiene can mutate only ignored target paths."""
    before = tracked_paths(repo, paths)
    if sorted(before) != sorted(paths):
        raise CdxCareError(
            f"git tracked paths changed before apply: expected {sorted(paths)}, found {sorted(before)}",
            code="git_index_changed",
        )
    ignored = ignored_paths(repo, paths)
    if sorted(ignored) != sorted(paths):
        raise CdxCareError(
            f"git paths are not ignored and would be re-addable: {sorted(set(paths) - set(ignored))}",
            code="git_ignore_missing",
        )
    dirty = tracked_status_paths(repo)
    unrelated_dirty = sorted(path for path in dirty if path not in set(paths))
    if unrelated_dirty:
        raise CdxCareError(
            f"memory git repo has unrelated tracked changes: {unrelated_dirty}",
            code="git_dirty_unrelated",
        )
    return {"repo": str(repo), "tracked": before, "ignored": ignored, "dirty_tracked": dirty}


def git_rm_cached(repo: Path, paths: list[str], *, require_exact_tracked: bool = False) -> JsonObject:
    """Remove tracked paths from the git index while preserving working files."""
    before = tracked_paths(repo, paths)
    if require_exact_tracked and sorted(before) != sorted(paths):
        raise CdxCareError(
            f"git tracked paths changed before apply: expected {sorted(paths)}, found {sorted(before)}",
            code="git_index_changed",
        )
    if not before:
        return {"repo": str(repo), "requested_paths": paths, "removed": [], "already_untracked": True}
    result = run_git(repo, ["rm", "--cached", "--", *before])
    if result.returncode != 0:
        raise CdxCareError(
            f"git rm --cached failed in {repo}: {result.stderr.strip()}",
            code="git_rm_cached_failed",
        )
    after = tracked_paths(repo, paths)
    if after:
        raise CdxCareError(f"git paths still tracked after rm --cached: {after}", code="git_readback_failed")
    return {"repo": str(repo), "requested_paths": paths, "removed": before, "remaining_tracked": after}
