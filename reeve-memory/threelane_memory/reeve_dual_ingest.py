"""Compatibility wrapper for the Reeve-only resilient ingester."""

from __future__ import annotations

from threelane_memory.reeve_ingest import ingest, main, retry_failed

__all__ = ["ingest", "main", "retry_failed"]


if __name__ == "__main__":
    main()
