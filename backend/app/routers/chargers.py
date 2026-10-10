import logging
import os
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import deps, models
from ..database import get_db
from ..charging import chargers_import, availability
from ..routing import corridor

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chargers", tags=["chargers"],
                   dependencies=[Depends(deps.current_session)])


@router.get("/stock")
def stock(db: Session = Depends(get_db)):
    """What has been imported at all? The first question when a corridor
    stays empty - usually it is not the search but the empty table."""
    per_source = (db.query(models.ChargePoint.source, func.count(models.ChargePoint.id))
                 .group_by(models.ChargePoint.source).all())
    return {"total": sum(count for _, count in per_source),
            "per_source": {source: count for source, count in per_source}}


@router.get("/along/{trip_id}")
def along_trip(trip_id: int, radius_km: float = Query(8.0, gt=0, le=50),
                  min_kw: float = Query(50.0, ge=0),
                  connector_type: str = "", db: Session = Depends(get_db)):
    """Charge points in the corridor around a planned trip.

    Sorted by progress along the route, not by distance from the user - on
    the road, "how far until I get there" is the only ordering that
    matters.
    """
    trip = db.get(models.Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Fahrt nicht gefunden.")

    kind = connector_type or trip.vehicle.connector_type
    candidates = corridor.seek(db, trip.geometry or [], radius_km=radius_km,
                                 min_kw=min_kw, connector_type=kind)

    result = []
    for candidate in candidates:
        state = availability.REPORTS.state(candidate.charge_point)
        result.append({
            **candidate.as_dict(),
            "occupied_reported": state.source == "meldung",
            "availability": state.source,
            # As long as nobody knows what is free, the number of charge
            # points is the only reliable indication of the risk of
            # arriving at an occupied charger.
            "redundancy_bonus_min": availability.redundancy_bonus(
                candidate.charge_point.point_count or 1),
            # Access restrictions and free text from the source. They are
            # passed through raw because they are not evaluated yet - but a
            # hint like "nur für Hotelgäste" (hotel guests only) changes the
            # choice, and having it without showing it would be the worst
            # of all options.
            "access": candidate.charge_point.access,
            "membership_required": candidate.charge_point.membership_required,
            "hints": candidate.charge_point.hints or {}})

    return {"count": len(result), "connector_type": kind, "min_kw": min_kw,
            "radius_km": radius_km, "candidates": result}


@router.post("/{charge_point_id}/occupied")
def report_occupied(charge_point_id: int, db: Session = Depends(get_db)):
    """"Everything is full here" - the only availability information that
    is really reliable. Valid for half an hour, then it expires."""
    if not db.get(models.ChargePoint, charge_point_id):
        raise HTTPException(404, "Ladepunkt nicht gefunden.")
    availability.REPORTS.report(charge_point_id)
    return {"ok": True, "valid_minutes": availability.REPORT_VALID_S // 60}


@router.delete("/{charge_point_id}/occupied")
def occupied_revert(charge_point_id: int, db: Session = Depends(get_db)):
    if not db.get(models.ChargePoint, charge_point_id):
        raise HTTPException(404, "Ladepunkt nicht gefunden.")
    availability.REPORTS.release(charge_point_id)
    return {"ok": True}


@router.post("/import/ocm")
def import_ocm(countries: str = "DE", max_results: int = Query(2000, ge=1, le=20000),
               min_kw: float = Query(0.0, ge=0),  db: Session = Depends(get_db)):
    """Trigger the Open Charge Map import.

    The Bundesnetzagentur file is deliberately not downloaded here: it is
    over 50 MB and would block the request for minutes. That is what
    tools/import_bnetza.py is for.
    """
    api_key = os.environ.get("OCM_API_KEY", "")
    if not api_key:
        raise HTTPException(400, "Kein OCM_API_KEY gesetzt.")
    codes = [c.strip().upper() for c in countries.split(",") if c.strip()]
    if not codes or len(codes) > 10 or not all(re.fullmatch(r"[A-Z]{2}", c) for c in codes):
        raise HTTPException(422, "countries: 1 bis 10 Laendercodes aus zwei Buchstaben.")
    try:
        return chargers_import.from_ocm(db, api_key, countries=codes,
                                      max_results=max_results, min_kw=min_kw)
    except Exception as failure:      # noqa: BLE001
        # Never pass the exception text on: for HTTP errors `requests` puts
        # the whole URL in it, and the OCM key is a query parameter.
        log.warning("Open Charge Map import failed: %s", type(failure).__name__)
        raise HTTPException(502, "Import fehlgeschlagen (Details im Serverlog).") from failure
