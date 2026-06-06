"""Time helpers."""

from __future__ import annotations

import time
from datetime import UTC, datetime


def iso_now() -> str:
    """Return a UTC ISO timestamp."""
    return datetime.now(UTC).isoformat()


def epoch_ms() -> int:
    """Return current Unix time in milliseconds."""
    return int(time.time() * 1000)


def epoch_seconds() -> int:
    """Return current Unix time in seconds."""
    return int(time.time())
