"""Deterministic JSONL repair helpers for Codex session-index surfaces."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from cdx_care.errors import CdxCareError
from cdx_care.sqlite_tools import connect_readonly, quote_ident, schema_fingerprint, table_columns, table_names
from cdx_care.types import JsonObject


def file_sha256_or_none(path: Path) -> str | None:
    """Return a file hash, or None when the file is absent."""
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_stat(path: Path) -> JsonObject:
    """Return file identity and content hash for JSONL file preconditions."""
    if not path.exists():
        return {"exists": False, "sha256": None}
    stat = path.stat()
    return {
        "exists": True,
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": file_sha256_or_none(path),
    }


def state_source_stat(db_path: Path) -> JsonObject:
    """Return state DB source identity for JSONL rebuild plans."""
    stat = db_path.stat()
    with closing(connect_readonly(db_path)) as conn:
        if "threads" not in table_names(conn):
            raise CdxCareError("state DB has no threads table", code="state_threads_unavailable")
        fingerprint = schema_fingerprint(conn, ["threads"])
    return {
        "db_path": str(db_path),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "schema_fingerprint": fingerprint,
    }


def verify_state_source(db_path: Path, expected: JsonObject) -> None:
    """Verify the state DB source used by a file repair plan did not drift."""
    stat = db_path.stat()
    if (
        expected.get("device") != stat.st_dev
        or expected.get("inode") != stat.st_ino
        or expected.get("bytes") != stat.st_size
        or expected.get("mtime_ns") != stat.st_mtime_ns
    ):
        raise CdxCareError("state DB changed before JSONL repair apply", code="db_changed")
    with closing(connect_readonly(db_path)) as conn:
        if "threads" not in table_names(conn):
            raise CdxCareError("state DB has no threads table", code="state_threads_unavailable")
        actual = schema_fingerprint(conn, ["threads"])
    if actual != expected.get("schema_fingerprint"):
        raise CdxCareError("state DB threads schema changed before JSONL repair apply", code="schema_changed")


def verify_file_stat(path: Path, expected: JsonObject) -> None:
    """Verify a target JSONL file did not drift since plan creation."""
    exists = bool(expected.get("exists"))
    if not exists:
        if path.exists() or path.is_symlink():
            raise CdxCareError("JSONL target appeared before apply", code="file_changed")
        return
    if path.is_symlink() or not path.exists():
        raise CdxCareError("JSONL target disappeared or became a symlink before apply", code="file_changed")
    stat = path.stat()
    if (
        expected.get("device") != stat.st_dev
        or expected.get("inode") != stat.st_ino
        or expected.get("bytes") != stat.st_size
        or expected.get("mtime_ns") != stat.st_mtime_ns
        or expected.get("sha256") != file_sha256_or_none(path)
    ):
        raise CdxCareError("JSONL target changed before apply", code="file_changed")


def state_thread_ids(db_path: Path) -> set[str]:
    """Return state thread ids from the current state DB."""
    with closing(connect_readonly(db_path)) as conn:
        if "threads" not in table_names(conn):
            return set()
        return {str(row[0]) for row in conn.execute("SELECT id FROM threads").fetchall()}


def session_file_id_set(root: Path) -> set[str]:
    """Collect rollout IDs from filenames, falling back to the first session_meta row."""
    if not root.exists():
        return set()
    ids: set[str] = set()
    for path in root.glob("*/*/*/rollout-*.jsonl"):
        parsed = session_id_from_rollout_filename(path)
        if parsed is None:
            parsed = session_id_from_rollout_first_line(path)
        if parsed:
            ids.add(parsed)
    return ids


def session_id_from_rollout_filename(path: Path) -> str | None:
    """Parse the UUID suffix from a standard Codex rollout filename."""
    name = path.name
    if not name.startswith("rollout-") or not name.endswith(".jsonl"):
        return None
    parts = name.removesuffix(".jsonl").split("-")
    if len(parts) < 7:
        return None
    return "-".join(parts[-5:])


def session_id_from_rollout_first_line(path: Path) -> str | None:
    """Read only the first rollout JSONL row to find session_meta.payload.id."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            line = handle.readline()
    except OSError:
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping) or payload.get("type") != "session_meta":
        return None
    body = payload.get("payload")
    if not isinstance(body, Mapping):
        return None
    value = body.get("id")
    return value if isinstance(value, str) else None


def desired_session_index_bytes(state_db: Path, current_index: Path | None = None) -> tuple[bytes, int]:
    """Build canonical session_index.jsonl bytes without deleting live fallback names."""
    rows = state_index_rows(state_db, current_index)
    output = b"".join(jsonl_bytes(row) for row in rows)
    return output, len(rows)


def state_index_rows(state_db: Path, current_index: Path | None = None) -> list[JsonObject]:
    """Return canonical legacy session-index rows.

    Codex reads state_5.threads titles first, then falls back to the append-only
    session_index.jsonl latest-entry-wins name when the SQLite title is blank or
    still equal to the first user message. A safe rebuild therefore replaces
    distinct state-owned titles but preserves current fallback names for state
    rows that still need the legacy index.
    """
    fallback_rows = latest_session_index_rows(current_index) if current_index is not None else {}
    with closing(connect_readonly(state_db)) as conn:
        if "threads" not in table_names(conn):
            raise CdxCareError("state DB has no threads table", code="state_threads_unavailable")
        cols = set(table_columns(conn, "threads"))
        if "title" not in cols or "first_user_message" not in cols:
            raise CdxCareError(
                "state DB threads table has no title/first_user_message metadata",
                code="state_threads_unavailable",
            )
        updated_column = first_existing(cols, ["updated_at_ms", "updated_at", "created_at_ms", "created_at"])
        updated_sql = quote_ident(updated_column) if updated_column else "0"
        query = (
            "SELECT id, title, first_user_message, "
            f"{updated_sql} AS updated_value "
            "FROM threads ORDER BY CAST(updated_value AS INTEGER), id"
        )
        rows = []
        for row in conn.execute(query).fetchall():
            thread_id = str(row["id"])
            title = str(row["title"] or "").strip()
            first_user_message = str(row["first_user_message"] or "").strip()
            if not title or title == first_user_message:
                fallback = fallback_rows.get(thread_id)
                if fallback is not None:
                    rows.append(fallback)
                continue
            rows.append(
                {
                    "id": thread_id,
                    "thread_name": title,
                    "updated_at": timestamp_to_iso(int(row["updated_value"] or 0)),
                }
            )
    return rows


def latest_session_index_rows(path: Path | None) -> dict[str, JsonObject]:
    """Return compact latest-entry-wins session_index rows keyed by id."""
    if path is None or not path.exists():
        return {}
    rows: dict[str, JsonObject] = {}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, Mapping):
                continue
            thread_id = payload.get("id")
            thread_name = payload.get("thread_name")
            updated_at = payload.get("updated_at")
            if not isinstance(thread_id, str) or not isinstance(thread_name, str) or not thread_name.strip():
                continue
            rows[thread_id] = {
                "id": thread_id,
                "thread_name": thread_name.strip(),
                "updated_at": updated_at if isinstance(updated_at, str) else "",
            }
    return rows


def first_existing(columns: set[str], candidates: list[str]) -> str | None:
    """Return the first candidate column present in a table."""
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def timestamp_to_iso(value: int) -> str:
    """Convert Codex second or millisecond epoch values to UTC ISO text."""
    seconds = value / 1000 if value > 10_000_000_000 else value
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def jsonl_bytes(row: JsonObject) -> bytes:
    """Serialize one compact JSONL row."""
    return (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    """Return a SHA-256 hex digest for bytes."""
    return hashlib.sha256(data).hexdigest()


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
                value = payload.get(key)
                if isinstance(value, str):
                    ids.add(value)
    return ids


def verify_session_file_alignment(state_db: Path, sessions_root: Path) -> JsonObject:
    """Return state-vs-rollout file alignment counts for repair preflight."""
    state_ids = state_thread_ids(state_db)
    file_ids = session_file_id_set(sessions_root)
    return {
        "state_thread_count": len(state_ids),
        "session_file_id_count": len(file_ids),
        "state_not_in_session_file_ids": len(state_ids - file_ids),
        "session_file_ids_not_in_state": len(file_ids - state_ids),
    }
