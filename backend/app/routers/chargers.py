import os

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import deps, models
from ..database import get_db
from ..charging import chargers_import, availability
from ..routing import corridor

router = APIRouter(prefix="/api/saeulen", tags=["ladesäulen"],
                   dependencies=[Depends(deps.current_session)])


@router.get("/bestand")
def stock(db: Session = Depends(get_db)):
    """Was ist überhaupt importiert? Die erste Frage, wenn ein Korridor leer
    bleibt - meist ist es nicht die Suche, sondern die leere Tabelle."""
    per_source = (db.query(models.ChargePoint.source, func.count(models.ChargePoint.id))
                 .group_by(models.ChargePoint.source).all())
    return {"total": sum(count for _, count in per_source),
            "per_source": {source: count for source, count in per_source}}


@router.get("/entlang/{trip_id}")
def along_trip(trip_id: int, radius_km: float = Query(8.0, gt=0, le=50),
                  min_kw: float = Query(50.0, ge=0),
                  connector_type: str = "", db: Session = Depends(get_db)):
    """Ladepunkte im Korridor um eine geplante Fahrt.

    Sortiert nach Fortschritt entlang der Route, nicht nach Entfernung zum
    Nutzer - unterwegs ist "wie weit noch bis dahin" die einzige Ordnung,
    die zählt.
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
            # Solange niemand weiss, was frei ist, ist die Anzahl der
            # Ladepunkte die einzige belastbare Aussage über das Risiko,
            # vor einer belegten Säule zu stehen.
            "redundancy_bonus_min": availability.redundancy_bonus(
                candidate.charge_point.point_count or 1),
            # Zugangsbeschränkungen und Freitext der Quelle. Sie stehen hier
            # roh, weil sie noch nicht ausgewertet werden - aber ein Hinweis
            # wie "nur für Hotelgäste" ändert die Wahl, und ihn zu haben und
            # nicht zu zeigen wäre die schlechteste aller Möglichkeiten.
            "access": candidate.charge_point.access,
            "membership_required": candidate.charge_point.membership_required,
            "hints": candidate.charge_point.hints or {}})

    return {"count": len(result), "connector_type": kind, "min_kw": min_kw,
            "radius_km": radius_km, "candidates": result}


@router.post("/{charge_point_id}/belegt")
def report_occupied(charge_point_id: int, db: Session = Depends(get_db)):
    """"Hier ist alles voll" - die einzige Verfügbarkeitsinformation, die
    wirklich stimmt. Gilt eine halbe Stunde, danach verfällt sie."""
    if not db.get(models.ChargePoint, charge_point_id):
        raise HTTPException(404, "Ladepunkt nicht gefunden.")
    availability.REPORTS.report(charge_point_id)
    return {"ok": True, "valid_minutes": availability.REPORT_VALID_S // 60}


@router.delete("/{charge_point_id}/belegt")
def occupied_revert(charge_point_id: int):
    availability.REPORTS.release(charge_point_id)
    return {"ok": True}


@router.post("/import/ocm")
def import_ocm(countries: str = "DE", max_results: int = Query(2000, le=20000),
               min_kw: float = 0.0, db: Session = Depends(get_db)):
    """Open-Charge-Map-Import anstossen.

    Die Bundesnetzagentur-Datei wird bewusst nicht hier heruntergeladen: Sie
    ist über 50 MB gross und würde die Anfrage minutenlang blockieren. Dafür
    gibt es tools/import_bnetza.py.
    """
    keyname = os.environ.get("OCM_API_KEY", "")
    if not keyname:
        raise HTTPException(400, "Kein OCM_API_KEY gesetzt.")
    try:
        return chargers_import.from_ocm(db, keyname,
                                      countries=[l.strip() for l in countries.split(",")],
                                      max_results=max_results, min_kw=min_kw)
    except Exception as failure:      # noqa: BLE001
        raise HTTPException(502, f"Import fehlgeschlagen: {failure}") from failure
