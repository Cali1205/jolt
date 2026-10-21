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


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


# What the lists can be narrowed by, one entry per query parameter: the
# condition it adds. The same conditions apply to the page and to its totals,
# so the sums above a filtered list are the sums of that list.
COMMON_FILTERS = {
    "since": lambda M, v: M.started_at >= _utc(v),
    "until": lambda M, v: M.started_at < _utc(v),
}
DRIVE_FILTERS = {
    **COMMON_FILTERS,
    "min_km": lambda M, v: M.distance_km >= v,
    "max_km": lambda M, v: M.distance_km < v,
    # A recording is what brings the map; the VW data has no position.
    "recorded": lambda M, v: (M.live_session_id.isnot(None) if v
                              else M.live_session_id.is_(None)),
}
CHARGE_FILTERS = {
    **COMMON_FILTERS,
    "type": lambda M, v: M.charge_type == v,
    "min_kwh": lambda M, v: M.energy_kwh >= v,
}


def _conditions(model, table: dict, given: dict) -> list:
    return [table[name](model, value) for name, value in given.items()
            if value is not None]


def _page(db, model, vehicle_id: int, limit: int, before: datetime | None,
          conditions: list):
    query = db.query(model).filter(model.vehicle_id == vehicle_id, *conditions)
    if before is not None:
        query = query.filter(model.started_at < _utc(before))
    rows = query.order_by(model.started_at.desc()).limit(limit + 1).all()
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, utc_iso(rows[-1].started_at) if more else None


@router.get("/api/vehicles/{vehicle_id}/drives")
def drives(vehicle_id: int, limit: int = Query(30, ge=1, le=200),
           before: datetime | None = None, since: datetime | None = None,
           until: datetime | None = None,
           min_km: float | None = Query(None, ge=0),
           max_km: float | None = Query(None, gt=0),
           recorded: bool | None = None, db: Session = Depends(get_db)):
    _vehicle(db, vehicle_id)
    D = models.VehicleDrive
    conditions = _conditions(D, DRIVE_FILTERS, {
        "since": since, "until": until, "min_km": min_km, "max_km": max_km,
        "recorded": recorded})
    rows, next_before = _page(db, D, vehicle_id, limit, before, conditions)
    sessions = [r.live_session_id for r in rows if r.live_session_id]
    trips = dict(db.query(models.LiveSession.id, models.LiveSession.trip_id)
                 .filter(models.LiveSession.id.in_(sessions)).all()) if sessions else {}
    count, km, kwh, km_measured = (
        db.query(func.count(D.id), func.sum(D.distance_km), func.sum(D.energy_used_kwh),
                 func.sum(case((D.energy_used_kwh.isnot(None), D.distance_km))))
        .filter(D.vehicle_id == vehicle_id, *conditions).one())
    return {
        "items": [{
            "id": r.id, "started_at": utc_iso(r.started_at),
            "start_estimated": r.start_estimated,
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
            before: datetime | None = None, since: datetime | None = None,
            until: datetime | None = None,
            type: str | None = Query(None, pattern="^(AC|DC)$"),
            min_kwh: float | None = Query(None, ge=0),
            db: Session = Depends(get_db)):
    _vehicle(db, vehicle_id)
    C = models.VehicleCharge
    conditions = _conditions(C, CHARGE_FILTERS, {
        "since": since, "until": until, "type": type, "min_kwh": min_kwh})
    rows, next_before = _page(db, C, vehicle_id, limit, before, conditions)
    by_type = {kind or "unknown": {"count": n, "energy_kwh": round(kwh or 0, 1)}
               for kind, n, kwh in db.query(C.charge_type, func.count(C.id),
                                            func.sum(C.energy_kwh))
               .filter(C.vehicle_id == vehicle_id, *conditions)
               .group_by(C.charge_type).all()}
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
