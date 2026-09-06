"""Shared helpers for resumable v2 event streams."""

from __future__ import annotations


def normalize_event_cursor(value: str | None, fallback: int = 0) -> int:
    """Parse ``Last-Event-ID`` without allowing a cursor to move backwards."""

    try:
        return max(int(fallback or 0), int(value or 0), 0)
    except (TypeError, ValueError):
        return max(int(fallback or 0), 0)


__all__ = ["normalize_event_cursor"]
