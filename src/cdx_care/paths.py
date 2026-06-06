"""Codex support-root path discovery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cdx_care.errors import CdxCareError

DB_RELATIVE_PATHS: dict[str, str] = {
    "state": "state_5.sqlite",
    "goals": "goals_1.sqlite",
    "memories": "memories_1.sqlite",
    "logs": "logs_2.sqlite",
    "codex-dev": "sqlite/codex-dev.db",
}


@dataclass(frozen=True)
class StorePaths:
    """Resolved Codex support-root paths."""

    codex_home: Path

    def db_path(self, name: str) -> Path:
        """Return the path for a known DB name."""
        if name not in DB_RELATIVE_PATHS:
            known = ", ".join(sorted(DB_RELATIVE_PATHS))
            raise CdxCareError(f"unknown DB name: {name}; known: {known}", code="unknown_db")
        return self.codex_home / DB_RELATIVE_PATHS[name]

    def db_paths(self) -> dict[str, Path]:
        """Return all known DB paths."""
        return {name: self.db_path(name) for name in DB_RELATIVE_PATHS}

    @property
    def automations_root(self) -> Path:
        """Return the file-backed automation definition root."""
        return self.codex_home / "automations"

    @property
    def memories_root(self) -> Path:
        """Return the memory git workspace root."""
        return self.codex_home / "memories"

    @property
    def session_index(self) -> Path:
        """Return the session index path."""
        return self.codex_home / "session_index.jsonl"

    @property
    def history(self) -> Path:
        """Return the history path."""
        return self.codex_home / "history.jsonl"

    @property
    def care_root(self) -> Path:
        """Return cdx-care's support directory."""
        return self.codex_home / "cdx-care"


def default_codex_home() -> Path:
    """Return the default user Codex home."""
    return Path.home() / ".codex"


def store_paths(codex_home: str | None) -> StorePaths:
    """Resolve a user supplied Codex home or the default."""
    root = Path(codex_home).expanduser() if codex_home else default_codex_home()
    return StorePaths(root)
