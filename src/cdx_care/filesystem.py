"""Private filesystem helpers for cdx-care artifacts."""

from __future__ import annotations

import os
from pathlib import Path

from cdx_care.errors import CdxCareError


def ensure_private_dir(path: Path) -> None:
    """Ensure an exact managed directory exists with private permissions."""
    if path.is_symlink():
        raise CdxCareError(f"directory path is a symlink: {path}", code="unsafe_output_path")
    if path.exists():
        if not path.is_dir():
            raise CdxCareError(f"directory path is not a directory: {path}", code="output_exists")
        os.chmod(path, 0o700)
        return
    create_private_dir_tree(path)


def create_private_dir(path: Path) -> None:
    """Create an exact new private directory without chmodding existing user paths."""
    if path.is_symlink():
        raise CdxCareError(f"directory path is a symlink: {path}", code="unsafe_output_path")
    if path.exists():
        raise CdxCareError(f"directory path already exists: {path}", code="output_exists")
    ensure_parent_dir(path)
    try:
        os.mkdir(path, 0o700)
        os.chmod(path, 0o700)
    except FileExistsError as error:
        raise CdxCareError(f"directory path already exists: {path}", code="output_exists") from error


def ensure_parent_dir(path: Path) -> None:
    """Ensure missing parent directories are private without chmodding existing parents."""
    parent = path.parent
    if parent.is_symlink():
        raise CdxCareError(f"parent directory is a symlink: {parent}", code="unsafe_output_path")
    if parent.exists():
        if not parent.is_dir():
            raise CdxCareError(f"parent path is not a directory: {parent}", code="output_exists")
        return
    create_private_dir_tree(parent)


def create_private_dir_tree(path: Path) -> None:
    """Create a missing directory tree with 0700 directories and no symlink parents."""
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            raise CdxCareError(f"cannot create directory tree from filesystem root: {path}", code="unsafe_output_path")
        cursor = parent
    if cursor.is_symlink() or not cursor.is_dir():
        raise CdxCareError(f"existing parent is not an owned directory: {cursor}", code="unsafe_output_path")
    for directory in reversed(missing):
        if directory.is_symlink():
            raise CdxCareError(f"directory path is a symlink: {directory}", code="unsafe_output_path")
        try:
            os.mkdir(directory, 0o700)
            os.chmod(directory, 0o700)
        except FileExistsError as error:
            if directory.is_symlink() or not directory.is_dir():
                raise CdxCareError(
                    f"directory path appeared but is not a directory: {directory}",
                    code="unsafe_output_path",
                ) from error
            os.chmod(directory, 0o700)
