"""The logbook of a vehicle: drives and charges, newest first (cloud/logbook.py)."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from .. import deps, models
from ..database import get_db
from ..timestamp import utc_iso

router = APIRouter(tags=["logbook"], dependencies=[Depends(deps.current_session)])


def _vehicle(db: Session, vehicle_id: int) -> None:
    if db.get(models.Vehicle, vehicle_id) is None:
        raise HTTPException(404, "Fahrzeug nicht gefunden.")


def _page(db, model, vehicle_id: int, limit: int, before: datetime | None):
    query = db.query(model).filter(model.vehicle_id == vehicle_id)
    if before is not None:
        if before.tzinfo is not None:
            before = before.astimezone(timezone.utc).replace(tzinfo=None)
        query = query.filter(model.started_at < before)
    rows = query.order_by(model.started_at.desc()).limit(limit + 1).all()
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, utc_iso(rows[-1].started_at) if more else None


@router.get("/api/vehicles/{vehicle_id}/drives")
def drives(vehicle_id: int, limit: int = Query(30, ge=1, le=200),
           before: datetime | None = None, db: Session = Depends(get_db)):
    _vehicle(db, vehicle_id)
    rows, next_before = _page(db, models.VehicleDrive, vehicle_id, limit, before)
    sessions = [r.live_session_id for r in rows if r.live_session_id]
    trips = dict(db.query(models.LiveSession.id, models.LiveSession.trip_id)
                 .filter(models.LiveSession.id.in_(sessions)).all()) if sessions else {}
    D = models.VehicleDrive
    count, km, kwh, km_measured = (
        db.query(func.count(D.id), func.sum(D.distance_km), func.sum(D.energy_used_kwh),
                 func.sum(case((D.energy_used_kwh.isnot(None), D.distance_km))))
        .filter(D.vehicle_id == vehicle_id).one())
    return {
        "items": [{
            "id": r.id, "started_at": utc_iso(r.started_at),
            "ended_at": utc_iso(r.ended_at), "odo_start": r.odo_start,
            "odo_end": r.odo_end, "distance_km": r.distance_km,
            "soc_start": r.soc_start, "soc_end": r.soc_end,
            "energy_used_kwh": r.energy_used_kwh,
            "kwh_per_100km": (round(r.energy_used_kwh / r.distance_km * 100, 1)
                              if r.energy_used_kwh is not None and r.distance_km >= 1
                              else None),
            "sources": r.sources, "live_session_id": r.live_session_id,
            "trip_id": trips.get(r.live_session_id)} for r in rows],
        "next_before": next_before,
        "totals": {"count": count, "distance_km": round(km or 0, 1),
                   "energy_used_kwh": round(kwh or 0, 1),
                   "distance_km_with_energy": round(km_measured or 0, 1)},
    }


@router.get("/api/vehicles/{vehicle_id}/charges")
def charges(vehicle_id: int, limit: int = Query(30, ge=1, le=200),
            before: datetime | None = None, db: Session = Depends(get_db)):
    _vehicle(db, vehicle_id)
    rows, next_before = _page(db, models.VehicleCharge, vehicle_id, limit, before)
    C = models.VehicleCharge
    by_type = {kind or "unknown": {"count": n, "energy_kwh": round(kwh or 0, 1)}
               for kind, n, kwh in db.query(C.charge_type, func.count(C.id),
                                            func.sum(C.energy_kwh))
               .filter(C.vehicle_id == vehicle_id).group_by(C.charge_type).all()}
    return {
        "items": [{
            "id": r.id, "started_at": utc_iso(r.started_at),
            "ended_at": utc_iso(r.ended_at), "soc_start": r.soc_start,
            "soc_end": r.soc_end, "energy_kwh": r.energy_kwh,
            "max_power_kw": r.max_power_kw, "avg_power_kw": r.avg_power_kw,
            "charge_type": r.charge_type, "odometer_km": r.odometer_km,
            "sources": r.sources} for r in rows],
        "next_before": next_before,
        "totals": {"count": sum(t["count"] for t in by_type.values()),
                   "energy_kwh": round(sum(t["energy_kwh"] for t in by_type.values()), 1),
                   "by_type": by_type},
    }
