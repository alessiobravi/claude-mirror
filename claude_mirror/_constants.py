"""Shared internal constants. Import-only; no runtime behaviour."""
from __future__ import annotations

PARALLEL_WORKERS: int = 5
"""Maximum concurrent workers for ThreadPoolExecutor across the codebase.
Tunes parallelism for blob uploads, snapshot copies, recursive listings."""
