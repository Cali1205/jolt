"""Which charging curve the planner uses: the measured one or the profile's.

The profile curve is a template or what someone typed in. Once the logbook
has enough DC charges, the measured envelope (charging/measured.py) is the
better guess, and the profile only fills what was never measured. The
vehicle's `charge_curve_source` can pin the profile: "auto" uses the measured
curve when there is one, "profile" never does.
"""
from .. import models
from ..timestamp import utc_iso
from . import curves, measured

# vehicle id -> (fingerprint of its DC charges, envelope, charges used, latest)
_cache: dict[int, tuple] = {}


def _measured(db, vehicle_id: int) -> tuple:
    """The envelope of the vehicle's DC charges, recomputed only when one of
    them was added, removed or rewritten (the logbook merges a later source
    into an existing row, which changes its columns but not its id)."""
    columns = (models.VehicleCharge.id, models.VehicleCharge.ended_at,
               models.VehicleCharge.energy_kwh, models.VehicleCharge.soc_start,
               models.VehicleCharge.soc_end, models.VehicleCharge.sources)
    rows = (db.query(*columns)
            .filter(models.VehicleCharge.vehicle_id == vehicle_id,
                    models.VehicleCharge.charge_type == "DC")
            .order_by(models.VehicleCharge.id).all())
    fingerprint = tuple((r.id, r.ended_at, r.energy_kwh, r.soc_start, r.soc_end,
                         tuple(r.sources or ())) for r in rows)
    hit = _cache.get(vehicle_id)
    if hit and hit[0] == fingerprint:
        return hit[1:]
    charges = (db.query(models.VehicleCharge)
               .filter(models.VehicleCharge.id.in_([r.id for r in rows]))
               .all()) if rows else []
    points = [p for p in (measured.charge_points(c) for c in charges) if p]
    latest = max((r.ended_at for r in rows), default=None)
    result = (measured.envelope(points), len(points), utc_iso(latest))
    _cache[vehicle_id] = (fingerprint, *result)
    return result


def effective_curve(db, vehicle: models.Vehicle) -> tuple[measured.Curve, dict]:
    """The curve to plan with, and where it comes from.

    The measured envelope is the warm-battery case (90th percentile), so the
    planner's temperature factor applies on top of it just as on the profile.
    """
    profile = curves.as_pairs(vehicle.charge_curve)
    envelope, charges, updated = _measured(db, vehicle.id)
    setting = vehicle.charge_curve_source or "auto"
    use_measured = envelope is not None and setting == "auto"
    curve = measured.merge(envelope, profile) if use_measured else profile
    return curve, {"source": "measured" if use_measured else "profile",
                   "setting": setting, "charges": charges, "updated": updated,
                   "bins": len(envelope or []), "measured": envelope or [],
                   "profile": profile}
