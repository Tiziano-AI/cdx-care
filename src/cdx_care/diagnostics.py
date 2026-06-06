"""Evidence-pack diagnostics."""

from __future__ import annotations

import json
import plistlib
from pathlib import Path

from cdx_care import VERSION
from cdx_care.doctor import doctor_report
from cdx_care.errors import CdxCareError
from cdx_care.filesystem import create_private_dir
from cdx_care.paths import StorePaths
from cdx_care.plan import write_new_text
from cdx_care.timeutil import iso_now
from cdx_care.types import JsonObject, JsonValue


def blank_page_pack(stores: StorePaths, out_dir: Path) -> JsonObject:
    """Write a read-only blank automation page evidence pack."""
    if out_dir.is_symlink():
        raise CdxCareError(f"output directory is a symlink: {out_dir}", code="unsafe_output_path")
    if out_dir.exists() and not out_dir.is_dir():
        raise CdxCareError(f"output directory is not an owned directory: {out_dir}", code="output_exists")
    json_path = out_dir / "diagnosis.json"
    readme_path = out_dir / "README.md"
    for path in (json_path, readme_path):
        if path.exists() or path.is_symlink():
            raise CdxCareError(f"output path already exists: {path}", code="output_exists")
    create_private_dir(out_dir)
    doctor = doctor_report(stores)
    pack: JsonObject = {
        "schema_version": 1,
        "tool": "cdx-care",
        "version": VERSION,
        "ok": True,
        "generated_at": iso_now(),
        "support_root": str(stores.codex_home),
        "purpose": "blank-page-diagnosis",
        "app": app_metadata(),
        "doctor": doctor,
        "log_locations": log_locations(),
        "notes": [
            "This pack is read-only and does not prove rendered UI state by itself.",
            "Capture the specific blank automation thread/run ID and renderer logs next if the page is still blank.",
        ],
    }
    write_new_text(json_path, json.dumps(pack, indent=2, sort_keys=True) + "\n", mode=0o600)
    write_new_text(
        readme_path,
        "# cdx-care blank-page diagnosis\n\n"
        "This evidence pack contains DB/source-adjacent metadata only. It does not mutate Codex state.\n"
        "If the app still opens a blank page, record the exact automation/run/thread ID and renderer log window.\n",
        mode=0o600,
    )
    return {
        "schema_version": 1,
        "tool": "cdx-care",
        "version": VERSION,
        "ok": True,
        "generated_at": iso_now(),
        "support_root": str(stores.codex_home),
        "codex_closed": doctor.get("codex_closed", False),
        "out_dir": str(out_dir),
        "diagnosis_json": str(json_path),
        "readme": str(readme_path),
    }


def app_metadata() -> JsonObject:
    """Read Codex.app metadata when present."""
    plist = Path("/Applications/Codex.app/Contents/Info.plist")
    if not plist.exists():
        return {"exists": False}
    with plist.open("rb") as handle:
        payload = plistlib.load(handle)
    return {
        "exists": True,
        "path": str(plist),
        "CFBundleShortVersionString": str(payload.get("CFBundleShortVersionString", "")),
        "CFBundleVersion": str(payload.get("CFBundleVersion", "")),
        "CFBundleIdentifier": str(payload.get("CFBundleIdentifier", "")),
    }


def log_locations() -> list[JsonValue]:
    """Return likely Codex log locations and metadata only."""
    candidates = [
        Path.home() / "Library/Logs/Codex",
        Path.home() / "Library/Application Support/Codex/logs",
        Path.home() / ".codex",
    ]
    rows: list[JsonValue] = []
    for path in candidates:
        rows.append(
            {
                "path": str(path),
                "exists": path.exists(),
                "bytes": path.stat().st_size if path.exists() and path.is_file() else None,
            }
        )
    return rows
