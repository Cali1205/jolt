"""What the manufacturer cloud reported about a vehicle, and how fetching goes."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import cloud, deps, models
from ..cloud import poller
from ..database import get_db
from ..timestamp import utc_iso

router = APIRouter(tags=["cloud"], dependencies=[Depends(deps.current_session)])


@router.get("/api/vehicles/{vehicle_id}/snapshot")
def vehicle_snapshot(vehicle_id: int, db: Session = Depends(get_db)):
    row = (db.query(models.VehicleSnapshot)
           .filter_by(vehicle_id=vehicle_id)
           .order_by(models.VehicleSnapshot.captured_at.desc(),
                     models.VehicleSnapshot.id.desc())
           .first())
    if row is None:
        raise HTTPException(404, "Keine Daten aus der Hersteller-Cloud.")
    return {"source": row.source, "captured_at": utc_iso(row.captured_at),
            "received_at": utc_iso(row.received_at), "values": row.values}


@router.get("/api/cloud/status")
def cloud_status():
    return {"configured": cloud.configured(), "sources": poller.status_list()}
