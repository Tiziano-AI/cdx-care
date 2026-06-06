"""Git helpers for local hygiene lanes."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from cdx_care.errors import CdxCareError
from cdx_care.types import JsonObject

TRUSTED_GIT_PATHS = ("/usr/bin/git", "/opt/homebrew/bin/git")
GIT_BIN = next((path for path in TRUSTED_GIT_PATHS if Path(path).is_file()), "/usr/bin/git")
SAFE_GIT_PATH = "/usr/bin:/bin:/opt/homebrew/bin:/opt/homebrew/sbin"
SAFE_GIT_CONFIG = [
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
]


def run_git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a git command in a repository."""
    return subprocess.run(
        [GIT_BIN, *SAFE_GIT_CONFIG, "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=safe_git_env(),
    )


def git_repository_layout(repo: Path) -> JsonObject:
    """Return the direct memory git repo layout, denying worktree/gitdir indirection."""
    dot_git = repo / ".git"
    if dot_git.is_symlink():
        raise CdxCareError(f"git directory is a symlink: {dot_git}", code="unsafe_support_path")
    if not dot_git.exists():
        raise CdxCareError(f"git directory is missing: {dot_git}", code="git_preflight_failed")
    if not dot_git.is_dir():
        raise CdxCareError(f"git directory must be a direct directory: {dot_git}", code="unsafe_support_path")
    common_dir_file = dot_git / "commondir"
    if common_dir_file.exists() or common_dir_file.is_symlink():
        raise CdxCareError(f"git common-dir indirection is not admitted: {common_dir_file}", code="unsafe_support_path")
    top = git_rev_parse(repo, "--show-toplevel")
    git_dir = git_rev_parse(repo, "--absolute-git-dir")
    common_dir = normalize_git_layout_path(repo, git_rev_parse(repo, "--git-common-dir"))
    if Path(top).resolve(strict=False) != repo.resolve(strict=False):
        raise CdxCareError(
            f"git worktree root does not match admitted repo: {top}",
            code="unsafe_support_path",
        )
    return {"worktree": top, "git_dir": git_dir, "git_common_dir": str(common_dir)}


def normalize_git_layout_path(repo: Path, raw_path: str) -> Path:
    """Normalize a git-reported layout path relative to the admitted repo."""
    path = Path(raw_path)
    if not path.is_absolute():
        path = repo / path
    return path


def git_rev_parse(repo: Path, arg: str) -> str:
    """Run one rev-parse query in the bounded git environment."""
    result = run_git(repo, ["rev-parse", arg])
    if result.returncode != 0:
        raise CdxCareError(
            f"git rev-parse {arg} failed in {repo}: {result.stderr.strip()}",
            code="git_preflight_failed",
        )
    return result.stdout.strip()


def safe_git_env() -> dict[str, str]:
    """Return a bounded git environment that does not read global config or prompt."""
    env: dict[str, str] = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "PATH": SAFE_GIT_PATH,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    if "SSH_AUTH_SOCK" in os.environ:
        env["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
    return env


def tracked_paths(repo: Path, paths: list[str]) -> list[str]:
    """Return paths tracked by git under repo."""
    if not (repo / ".git").exists():
        return []
    result = run_git(repo, ["ls-files", "--", *paths])
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def head_tracked_paths(repo: Path, paths: list[str]) -> list[str]:
    """Return paths present in HEAD under repo."""
    if not (repo / ".git").exists():
        return []
    result = run_git(repo, ["ls-tree", "-r", "--name-only", "HEAD", "--", *paths])
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


def git_hygiene_preflight(repo: Path, paths: list[str], *, complete_paths: list[str] | None = None) -> JsonObject:
    """Verify memory git hygiene can mutate only ignored target paths."""
    layout = git_repository_layout(repo)
    checked_paths = complete_paths or paths
    all_tracked = sorted(set(tracked_paths(repo, checked_paths)) | set(head_tracked_paths(repo, checked_paths)))
    if sorted(paths) != all_tracked:
        raise CdxCareError(
            "git action paths do not match current tracked admitted paths: "
            f"expected {all_tracked}, got {sorted(paths)}",
            code="git_index_changed",
        )
    before_index = tracked_paths(repo, paths)
    before_head = head_tracked_paths(repo, paths)
    if not before_index and not before_head:
        raise CdxCareError(
            f"git tracked paths changed before apply: expected {sorted(paths)}, found none in index or HEAD",
            code="git_index_changed",
        )
    missing = sorted(set(paths) - set(before_index) - set(before_head))
    if missing:
        raise CdxCareError(f"git target paths are no longer tracked: {missing}", code="git_index_changed")
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
    return {
        "repo": str(repo),
        "tracked_index": before_index,
        "tracked_head": before_head,
        "ignored": ignored,
        "dirty_tracked": dirty,
        "worktree": layout["worktree"],
        "git_dir": layout["git_dir"],
        "git_common_dir": layout["git_common_dir"],
        "hooks_disabled": True,
        "global_config_disabled": True,
    }


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


def git_untrack_and_commit(
    repo: Path, paths: list[str], message: str, *, complete_paths: list[str] | None = None
) -> JsonObject:
    """Remove tracked paths from the index and commit the deletion from HEAD."""
    preflight = git_hygiene_preflight(repo, paths, complete_paths=complete_paths)
    before_index = tracked_paths(repo, paths)
    before_head = head_tracked_paths(repo, paths)
    removed: list[str] = []
    if before_index:
        removal = git_rm_cached(repo, before_index, require_exact_tracked=False)
        removed_value = removal.get("removed")
        if isinstance(removed_value, list):
            removed = [str(path) for path in removed_value]
    after_index = tracked_paths(repo, paths)
    if after_index:
        raise CdxCareError(f"git paths still tracked after rm --cached: {after_index}", code="git_readback_failed")
    commit_hash: str | None = None
    if staged_changes_present(repo, paths):
        commit = run_git(
            repo,
            [
                "-c",
                "user.name=cdx-care",
                "-c",
                "user.email=cdx-care@local.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-m",
                message,
            ],
        )
        if commit.returncode != 0:
            raise CdxCareError(f"git commit failed in {repo}: {commit.stderr.strip()}", code="git_commit_failed")
        commit_hash = current_head(repo)
    after_head = head_tracked_paths(repo, paths)
    if after_head:
        raise CdxCareError(f"git paths still tracked in HEAD after commit: {after_head}", code="git_readback_failed")
    return {
        "repo": str(repo),
        "requested_paths": paths,
        "preflight": preflight,
        "removed_from_index": removed,
        "previous_head_tracked": before_head,
        "remaining_index_tracked": after_index,
        "remaining_head_tracked": after_head,
        "commit": commit_hash,
    }


def staged_changes_present(repo: Path, paths: list[str]) -> bool:
    """Return whether the target pathspec has staged changes."""
    result = run_git(repo, ["diff", "--cached", "--quiet", "--", *paths])
    if result.returncode == 0:
        return False
    if result.returncode == 1:
        return True
    raise CdxCareError(f"git diff --cached failed in {repo}: {result.stderr.strip()}", code="git_preflight_failed")


def current_head(repo: Path) -> str:
    """Return current HEAD commit hash."""
    return git_rev_parse(repo, "HEAD")
