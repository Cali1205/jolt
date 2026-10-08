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
    """A price for chargers whose name contains the pattern."""
    pattern: str = Field(max_length=80)
    eur_kwh: float = Field(ge=0, le=5, allow_inf_nan=False)


class VehicleInput(BaseModel):
    """Limits with plenty of headroom: they catch typos and broken clients,
    not unusual vehicles. Zero or negative values in the driving resistance
    otherwise caused division by zero or NaN in the consumption model."""
    name: str = Field(min_length=1, max_length=120)
    battery_gross_kwh: float = Field(gt=0, le=1000, allow_inf_nan=False)
    battery_net_kwh: float = Field(gt=0, le=1000, allow_inf_nan=False)
    curb_mass_kg: float = Field(default=1800.0, gt=0, le=20000, allow_inf_nan=False)
    payload_kg: float = Field(default=150.0, ge=0, le=10000, allow_inf_nan=False)
    c_w: float = Field(default=0.28, gt=0, le=2, allow_inf_nan=False)
    frontal_area_m2: float = Field(default=2.30, gt=0, le=20, allow_inf_nan=False)
    c_rr: float = Field(default=0.010, ge=0, le=0.1, allow_inf_nan=False)
    # Top speed in km/h; None = no limit in the model.
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
    # Names or name fragments the charging plan prefers - not a hard filter.
    preferred_operators: list[Annotated[str, Field(max_length=80)]] = Field(
        default=[], max_length=50)
    # What one kilowatt hour costs. Stored on the vehicle because the price
    # depends on the contract, not on the charger - see laden/prices.py.
    electricity_price_eur_kwh: float = Field(default=0.59, ge=0, le=5)
    # [{"muster": "Ionity", "eur_kwh": 0.39}, ...]  ("muster" = pattern)
    electricity_prices: list[ElectricityPrice] = Field(default=[], max_length=50)
    # [[soc, kw], ...] - empty means "leave the curve unchanged"
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
            # What the vehicle itself reports about its battery, and when.
            # Both null as long as it has never been measured.
            "measured_capacity_kwh": vehicle.measured_capacity_kwh,
            "capacity_measured_at": utc_iso(vehicle.capacity_measured_at),
            # What is actually used for calculation - measured, otherwise profile.
            "capacity_kwh": vehicle.capacity_kwh,
            # Only whether one is set up, not which one. The token appears
            # in exactly one response - the one that creates it.
            "logger_active": bool(vehicle.logger_token),
            "charge_curve": [[p.soc_percent, p.kw] for p in vehicle.charge_curve]}


def _examine_curve(pairs: list[list[float]]) -> None:
    """Each state of charge may occur only once - the table has a unique
    constraint for that (vehicle_id, soc_percent). Without this check a
    duplicate state of charge does not reach the user as an understandable
    error message but as a bare database error and HTTP 500.
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
    # Without this flush SQLAlchemy writes the new rows first and only then
    # the DELETEs of the old ones in the same flush - when editing a vehicle
    # that keeps the same states of charge (the normal case: only the kW
    # values change), that violates the same unique constraint as a real
    # duplicate, just invisible to _examine_curve(). The flush forces the
    # deletions into the database first.
    db.flush()
    for entry in pairs:
        if len(entry) < 2:
            continue
        vehicle.charge_curve.append(models.ChargeCurvePoint(
            soc_percent=float(entry[0]), kw=float(entry[1])))


@router.get("/vorlagen")
def templates():
    """Starting values so nobody has to guess the c_w value and charging
    curve by hand.

    Explicitly approximations and not manufacturer data - they are adapted to
    the actual car via the correction factor.
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
    """Create a new logger token - and thereby invalidate the old one.

    The token is the key that lets a device in the car report measurement
    points (`POST /api/live/melden`). It appears **only in this one
    response**; afterwards it cannot be retrieved from the UI. Whoever loses
    it creates a new one - that costs nothing except re-entering it in the
    logger, and it keeps up the habit of not dragging a secret along in
    every list response.
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
    """Deregister the logger. Nothing is accepted from it afterwards."""
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
