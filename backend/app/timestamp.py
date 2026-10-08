"""Zeitangaben, wie sie den Server verlassen.

Die Datenbank führt naive UTC-Zeiten (`datetime.utcnow`). `isoformat()` einer
solchen Zeit ergibt `2026-10-05T15:56:21` - **ohne** Hinweis auf die Zone. Ein
Browser liest genau das als Ortszeit: In Mitteleuropäischer Sommerzeit stand
dann 15:56 Uhr, wo es 17:56 war, und jeder Vergleich mit `Date.now()` lag zwei
Stunden daneben.

Deshalb verlässt jede Zeit den Server als UTC mit `Z`. Ein einziger Ort für
diese Regel, damit sie nicht an der vierten Stelle vergessen wird.
"""
from datetime import datetime, timezone


def utc_iso(timestamp: datetime | None) -> str | None:
    """`2026-10-05T15:56:21.646968Z` - oder None.

    Naive Zeiten gelten als UTC (so führt sie die Datenbank); eine Zeit mit
    Zone wird nach UTC umgerechnet, nicht abgeschnitten.
    """
    if timestamp is None:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
