"""Local process probes."""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from cdx_care.types import JsonObject

TRUSTED_LSOF_PATH = Path("/usr/sbin/lsof")
LSOF_TIMEOUT_SECONDS = 15
LSOF_KILL_GRACE_SECONDS = 1


def lsof_handles(paths: list[Path]) -> tuple[bool, list[JsonObject]]:
    """Return whether lsof ran and handles for existing paths."""
    existing = existing_lsof_targets(paths)
    if not existing:
        return True, []
    lsof_path = trusted_lsof_path()
    if lsof_path is None:
        return False, []
    result = run_lsof_command([str(lsof_path), *[str(path) for path in existing]])
    if result is None:
        return False, []
    stderr = result.stderr.strip()
    if stderr:
        return False, []
    if result.returncode == 1 and not result.stdout.strip():
        return True, []
    if result.returncode not in (0, 1):
        return False, []
    parsed, rows = parse_lsof_stdout(result.stdout)
    return parsed, rows


def run_lsof_command(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Run lsof without allowing an unkillable child wait to block cdx-care."""
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError:
        return None
    try:
        stdout, stderr = proc.communicate(timeout=LSOF_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        kill_process_group(proc)
        return None
    return subprocess.CompletedProcess(args=args, returncode=proc.returncode, stdout=stdout, stderr=stderr)


def kill_process_group(proc: subprocess.Popen[str]) -> None:
    """Best-effort kill of a timed-out lsof process group without blocking forever."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            proc.kill()
        except ProcessLookupError:
            return
    try:
        proc.communicate(timeout=LSOF_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return


def trusted_lsof_path() -> Path | None:
    """Return the admitted absolute lsof binary path, or None to fail closed."""
    if TRUSTED_LSOF_PATH.is_file() and os.access(TRUSTED_LSOF_PATH, os.X_OK):
        return TRUSTED_LSOF_PATH
    return None


def existing_lsof_targets(paths: list[Path]) -> list[Path]:
    """Return existing DB/WAL/SHM paths that should be passed to lsof."""
    candidates: list[Path] = []
    for path in paths:
        candidates.append(path)
        candidates.append(Path(str(path) + "-wal"))
        candidates.append(Path(str(path) + "-shm"))
    seen: set[str] = set()
    existing: list[Path] = []
    for path in candidates:
        key = str(path)
        if key not in seen and path.exists():
            existing.append(path)
            seen.add(key)
    return existing


def parse_lsof_stdout(stdout: str) -> tuple[bool, list[JsonObject]]:
    """Parse lsof stdout, failing closed on ambiguous handle lines."""
    rows: list[JsonObject] = []
    lines = stdout.splitlines()
    if not lines:
        return True, rows
    header = lines[0].split()
    if "COMMAND" not in header or "PID" not in header or "NAME" not in header:
        return False, []
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split(None, 8)
        if len(parts) < 9:
            return False, []
        rows.append(
            {
                "command": parts[0],
                "pid": parts[1],
                "user": parts[2],
                "fd": parts[3],
                "type": parts[4],
                "name": parts[8],
            }
        )
    return True, rows
