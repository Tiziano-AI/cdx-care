"""SQLite helpers with read-only defaults."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from cdx_care.errors import CdxCareError
from cdx_care.filesystem import ensure_private_dir
from cdx_care.types import JsonObject, JsonValue


def connect_readonly(path: Path) -> sqlite3.Connection:
    """Open a SQLite DB in read-only mode."""
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    conn.row_factory = sqlite3.Row
    return conn


def connect_write(path: Path) -> sqlite3.Connection:
    """Open a SQLite DB for guarded writes."""
    uri = f"file:{quote(str(path), safe='/')}?mode=rw"
    conn = sqlite3.connect(uri, uri=True, timeout=0.2, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=0")
    return conn


def quick_check(path: Path) -> str | None:
    """Return PRAGMA quick_check(1) or None when the DB is absent."""
    if not path.exists():
        return None
    with closing(connect_readonly(path)) as conn:
        row = conn.execute("PRAGMA quick_check(1)").fetchone()
    if row is None:
        return None
    return str(row[0])


def table_names(conn: sqlite3.Connection) -> list[str]:
    """List user tables."""
    return [
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    ]


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return column names for a table."""
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()]


def schema_fingerprint(conn: sqlite3.Connection, tables: Iterable[str]) -> str:
    """Return a stable schema fingerprint for selected tables and dependent schema objects."""
    digest = hashlib.sha256()
    for table in sorted(set(tables)):
        for row in conn.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE (type='table' AND name=?) OR (type IN ('index', 'trigger') AND tbl_name=?)
            ORDER BY type, name
            """,
            (table, table),
        ).fetchall():
            digest.update("|".join(str(value) for value in row).encode("utf-8"))
            digest.update(b"\0")
        for info in conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall():
            digest.update("|".join(str(value) for value in info).encode("utf-8"))
            digest.update(b"\0")
    return digest.hexdigest()


def trigger_names_for_tables(conn: sqlite3.Connection, tables: Iterable[str]) -> list[str]:
    """Return trigger names attached to admitted write tables."""
    table_list = sorted(set(tables))
    if not table_list:
        return []
    placeholders = ", ".join("?" for _ in table_list)
    return [
        str(row[0])
        for row in conn.execute(
            f"SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name IN ({placeholders}) ORDER BY name",
            table_list,
        ).fetchall()
    ]


def quote_ident(identifier: str) -> str:
    """Quote a SQLite identifier."""
    if "\x00" in identifier:
        raise CdxCareError("identifier contains NUL", code="invalid_identifier")
    return '"' + identifier.replace('"', '""') + '"'


def row_to_json(row: sqlite3.Row) -> JsonObject:
    """Convert a SQLite row to a JSON object."""
    keys = list(row.keys())
    return {key: sqlite_value_to_json(row[key]) for key in keys}


def sqlite_value_to_json(value: object) -> JsonValue:
    """Convert a SQLite scalar to JSON."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "byte_count": len(value)}
    return str(value)


def value_hash(value: object) -> str:
    """Hash a DB value without exposing it."""
    if value is None:
        data = b"<NULL>"
    elif isinstance(value, bytes):
        data = value
    else:
        data = str(value).encode("utf-8", errors="replace")
    return hashlib.sha256(data).hexdigest()


def copy_db_family(db_path: Path, backup_dir: Path) -> list[JsonObject]:
    """Copy a SQLite DB and WAL/SHM siblings to a backup directory."""
    ensure_private_dir(backup_dir)
    copied: list[JsonObject] = []
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(db_path) + suffix)
        if not source.exists():
            continue
        target = backup_dir / source.name
        if target.exists():
            raise CdxCareError(f"backup target already exists: {target}", code="backup_exists")
        copy_private_file(source, target)
        row: JsonObject = {
            "source": str(source),
            "target": str(target),
            "bytes": target.stat().st_size,
            "sha256": file_sha256(target),
        }
        copied.append(row)
    main_target = backup_dir / db_path.name
    if main_target.exists():
        for row in copied:
            if row.get("target") == str(main_target):
                row["quick_check"] = quick_check(main_target)
                break
    return copied


def copy_private_file(source: Path, target: Path) -> None:
    """Copy a file into a new private target without a permissive create window."""
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with source.open("rb") as reader, os.fdopen(fd, "wb") as writer:
            fd = -1
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                writer.write(chunk)
    finally:
        if fd >= 0:
            os.close(fd)


def file_sha256(path: Path) -> str:
    """Hash a local file in bounded chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
