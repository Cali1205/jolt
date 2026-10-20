"""Fetch the manufacturer cloud's snapshot in the background."""
from datetime import datetime, timezone

from .. import models
from .snapshot import Snapshot


def store(db, vehicle_id: int, source: str, snap: Snapshot,
          now: datetime | None = None) -> bool:
    """Keep the snapshot unless this report is already stored."""
    captured_at = snap.captured_at.astimezone(timezone.utc).replace(tzinfo=None)
    known = (db.query(models.VehicleSnapshot.id)
             .filter_by(vehicle_id=vehicle_id, source=source,
                        captured_at=captured_at)
             .first())
    if known:
        return False
    db.add(models.VehicleSnapshot(vehicle_id=vehicle_id, source=source,
                                  captured_at=captured_at,
                                  received_at=now or datetime.utcnow(),
                                  values=dict(snap.values)))
    db.commit()
    return True
