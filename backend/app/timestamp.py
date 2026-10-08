"""Timestamps as they leave the server.

The database stores naive UTC times (`datetime.utcnow`). `isoformat()` of such a
time gives `2026-10-05T15:56:21` - **without** any hint of the zone. A browser
reads exactly that as local time: in Central European summer time it showed
15:56 where it was 17:56, and every comparison with `Date.now()` was two hours
off.

That is why every time leaves the server as UTC with `Z`. One single place for
this rule, so it is not forgotten at the fourth call site.
"""
from datetime import datetime, timezone


def utc_iso(timestamp: datetime | None) -> str | None:
    """`2026-10-05T15:56:21.646968Z` - or None.

    Naive times count as UTC (that is how the database stores them); a time
    with a zone is converted to UTC, not truncated.
    """
    if timestamp is None:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
