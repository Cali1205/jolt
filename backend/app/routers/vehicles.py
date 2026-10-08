import math
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from .. import deps, models
from ..timestamp import utc_iso
from ..database import get_db
from ..charging import curves

router = APIRouter(prefix="/api/fahrzeuge", tags=["fahrzeuge"],
                   dependencies=[Depends(deps.current_session)])


class ElectricityPrice(BaseModel):
    """Ein Preis für Säulen, deren Name das Muster enthält."""
    pattern: str = Field(max_length=80)
    eur_kwh: float = Field(ge=0, le=5, allow_inf_nan=False)


class VehicleInput(BaseModel):
    """Grenzen mit Luft nach oben: Sie fangen Tippfehler und kaputte Clients,
    nicht ungewöhnliche Fahrzeuge. Null oder Negatives im Fahrwiderstand ergab
    sonst Division durch null oder NaN im Verbrauchsmodell."""
    name: str = Field(min_length=1, max_length=120)
    battery_gross_kwh: float = Field(gt=0, le=1000, allow_inf_nan=False)
    battery_net_kwh: float = Field(gt=0, le=1000, allow_inf_nan=False)
    curb_mass_kg: float = Field(default=1800.0, gt=0, le=20000, allow_inf_nan=False)
    payload_kg: float = Field(default=150.0, ge=0, le=10000, allow_inf_nan=False)
    c_w: float = Field(default=0.28, gt=0, le=2, allow_inf_nan=False)
    frontal_area_m2: float = Field(default=2.30, gt=0, le=20, allow_inf_nan=False)
    c_rr: float = Field(default=0.010, ge=0, le=0.1, allow_inf_nan=False)
    # Höchstgeschwindigkeit in km/h; None = keine Grenze im Modell.
    max_speed_kmh: float | None = Field(default=None, ge=30, le=300)
    eta_drive: float = Field(default=0.88, gt=0, le=1)
    eta_regen: float = Field(default=0.70, ge=0, le=1)
    p_aux_w: float = Field(default=350.0, ge=0, le=20000, allow_inf_nan=False)
    heat_pump: bool = True
    reserve_soc: float = Field(default=10.0, ge=0, le=50)
    target_soc: float = Field(default=20.0, ge=0, le=100)
    max_charge_power_kw: float = Field(default=150.0, gt=0, le=1500,
                                       allow_inf_nan=False)
    connector_type: str = Field(default="CCS", max_length=40)
    # Namen oder Namensteile, die der Ladeplan bevorzugt - kein harter Filter.
    preferred_operators: list[Annotated[str, Field(max_length=80)]] = Field(
        default=[], max_length=50)
    # Was eine Kilowattstunde kostet. Am Fahrzeug, weil der Preis am Vertrag
    # hängt und nicht an der Säule - siehe laden/prices.py.
    electricity_price_eur_kwh: float = Field(default=0.59, ge=0, le=5)
    # [{"muster": "Ionity", "eur_kwh": 0.39}, ...]
    electricity_prices: list[ElectricityPrice] = Field(default=[], max_length=50)
    # [[soc, kw], ...] - leer heisst "Kurve unverändert lassen"
    charge_curve: list[list[float]] = Field(default=[], max_length=100)

    @field_validator("charge_curve")
    @classmethod
    def _curve_valid(cls, curve):
        for entry in curve:
            if len(entry) < 2:
                continue
            soc, kw = entry[0], entry[1]
            if not (math.isfinite(soc) and math.isfinite(kw)):
                raise ValueError("Ladekurve enthält keine endliche Zahl")
            if not 0 <= soc <= 100:
                raise ValueError("Ladestand der Ladekurve liegt ausserhalb 0-100 %")
            if not 0 <= kw <= 1500:
                raise ValueError("Ladeleistung der Ladekurve liegt ausserhalb 0-1500 kW")
        return curve


def _as_dict(vehicle: models.Vehicle) -> dict:
    return {"id": vehicle.id, "name": vehicle.name,
            "battery_gross_kwh": vehicle.battery_gross_kwh,
            "battery_net_kwh": vehicle.battery_net_kwh,
            "curb_mass_kg": vehicle.curb_mass_kg,
            "payload_kg": vehicle.payload_kg,
            "c_w": vehicle.c_w, "frontal_area_m2": vehicle.frontal_area_m2,
            "c_rr": vehicle.c_rr, "max_speed_kmh": vehicle.max_speed_kmh,
            "eta_drive": vehicle.eta_drive,
            "eta_regen": vehicle.eta_regen, "p_aux_w": vehicle.p_aux_w,
            "heat_pump": vehicle.heat_pump,
            "reserve_soc": vehicle.reserve_soc, "target_soc": vehicle.target_soc,
            "max_charge_power_kw": vehicle.max_charge_power_kw,
            "connector_type": vehicle.connector_type,
            "preferred_operators": vehicle.preferred_operators or [],
            "electricity_price_eur_kwh": vehicle.electricity_price_eur_kwh,
            "electricity_prices": vehicle.electricity_prices or [],
            "correction_factor": vehicle.correction_factor,
            # Was das Fahrzeug selbst ueber seinen Akku sagt, und wann.
            # Beides null, solange nie gemessen wurde.
            "measured_capacity_kwh": vehicle.measured_capacity_kwh,
            "capacity_measured_at": utc_iso(vehicle.capacity_measured_at),
            # Womit tatsaechlich gerechnet wird - gemessen, sonst Profil.
            "capacity_kwh": vehicle.capacity_kwh,
            # Nur ob eines eingerichtet ist, nicht welches. Das Token steht
            # genau einmal in einer Antwort - der, mit der es entsteht.
            "logger_active": bool(vehicle.logger_token),
            "charge_curve": [[p.soc_percent, p.kw] for p in vehicle.charge_curve]}


def _examine_curve(pairs: list[list[float]]) -> None:
    """Jeder Ladestand darf nur einmal vorkommen - die Tabelle hat dafür eine
    Unique-Constraint (fahrzeug_id, soc_prozent). Ohne diese Prüfung landet ein
    doppelter Ladestand nicht als verständliche Fehlermeldung beim Nutzer,
    sondern als nackter Datenbankfehler und HTTP 500.
    """
    seen: set[float] = set()
    for entry in pairs:
        if len(entry) < 2:
            continue
        soc = float(entry[0])
        if soc in seen:
            raise HTTPException(
                400, f"Ladestand {soc:g} % kommt in der Ladekurve mehrfach vor - "
                     "jeder Ladestand darf nur eine Ladeleistung haben.")
        seen.add(soc)


def _set_curve(db: Session, vehicle: models.Vehicle,
                  pairs: list[list[float]]) -> None:
    for old in list(vehicle.charge_curve):
        db.delete(old)
    vehicle.charge_curve.clear()
    # Ohne dieses Flush schreibt SQLAlchemy im selben Flush zuerst die neuen
    # Zeilen und erst danach die DELETEs der alten - beim Bearbeiten eines
    # Fahrzeugs, das dieselben Ladestände behält (der Normalfall: nur die
    # kW-Werte ändern sich), verletzt das dann dieselbe Unique-Constraint wie
    # ein echtes Duplikat, nur unsichtbar für _kurve_pruefen(). Das Flush
    # zwingt die Löschungen zuerst in die Datenbank.
    db.flush()
    for entry in pairs:
        if len(entry) < 2:
            continue
        vehicle.charge_curve.append(models.ChargeCurvePoint(
            soc_percent=float(entry[0]), kw=float(entry[1])))


@router.get("/vorlagen")
def templates():
    """Startwerte, damit niemand c_w-Wert und Ladekurve von Hand raten muss.

    Ausdrücklich Näherungen und keine Herstellerangaben - sie werden über den
    Korrekturfaktor an das eigene Auto herangeführt.
    """
    return [{**v, "charge_curve": [list(p) for p in v["charge_curve"]]}
            for v in curves.TEMPLATES]


@router.get("")
def lst(db: Session = Depends(get_db)):
    return [_as_dict(f) for f in db.query(models.Vehicle)
            .order_by(models.Vehicle.id).all()]


@router.post("")
def create(user_input: VehicleInput, db: Session = Depends(get_db)):
    if user_input.battery_net_kwh > user_input.battery_gross_kwh:
        raise HTTPException(400, "Netto-Kapazität kann nicht über brutto liegen.")
    _examine_curve(user_input.charge_curve)
    fields = user_input.model_dump(exclude={"charge_curve"})
    vehicle = models.Vehicle(**fields)
    db.add(vehicle)
    db.flush()
    _set_curve(db, vehicle, user_input.charge_curve or
                  [list(p) for p in curves.TEMPLATES[0]["charge_curve"]])
    db.commit()
    return _as_dict(vehicle)


@router.put("/{vehicle_id}")
def change(vehicle_id: int, user_input: VehicleInput,
            db: Session = Depends(get_db)):
    vehicle = db.get(models.Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(404, "Fahrzeug nicht gefunden.")
    if user_input.battery_net_kwh > user_input.battery_gross_kwh:
        raise HTTPException(400, "Netto-Kapazität kann nicht über brutto liegen.")
    if user_input.charge_curve:
        _examine_curve(user_input.charge_curve)
    for keyname, val in user_input.model_dump(exclude={"charge_curve"}).items():
        setattr(vehicle, keyname, val)
    if user_input.charge_curve:
        _set_curve(db, vehicle, user_input.charge_curve)
    db.commit()
    return _as_dict(vehicle)


@router.post("/{vehicle_id}/logger-token")
def renew_logger_token(vehicle_id: int, db: Session = Depends(get_db)):
    """Ein neues Logger-Token erzeugen - und damit das alte entwerten.

    Das Token ist der Schlüssel, mit dem ein Gerät im Auto Messpunkte melden
    darf (`POST /api/live/melden`). Es steht **nur in dieser einen Antwort**;
    danach ist es aus der Oberfläche nicht mehr abzurufen. Wer es verliert,
    erzeugt ein neues - das kostet nichts ausser dem Neueintragen im Logger,
    und es hält die Gewohnheit aufrecht, ein Geheimnis nicht in jeder
    Listenantwort mitzuschleppen.
    """
    vehicle = db.get(models.Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(404, "Fahrzeug nicht gefunden.")
    vehicle.logger_token = secrets.token_urlsafe(32)
    db.commit()
    return {"logger_token": vehicle.logger_token,
            "hint": "Dieses Token wird nur einmal angezeigt."}


@router.delete("/{vehicle_id}/logger-token")
def delete_logger_token(vehicle_id: int, db: Session = Depends(get_db)):
    """Den Logger abmelden. Danach wird von ihm nichts mehr angenommen."""
    vehicle = db.get(models.Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(404, "Fahrzeug nicht gefunden.")
    vehicle.logger_token = None
    db.commit()
    return {"ok": True}


@router.delete("/{vehicle_id}")
def remove(vehicle_id: int, db: Session = Depends(get_db)):
    vehicle = db.get(models.Vehicle, vehicle_id)
    if not vehicle:
        raise HTTPException(404, "Fahrzeug nicht gefunden.")
    if db.query(models.Trip).filter_by(vehicle_id=vehicle_id).first():
        raise HTTPException(409, "Zu diesem Fahrzeug gibt es noch Fahrten.")
    db.delete(vehicle)
    db.commit()
    return {"ok": True}
