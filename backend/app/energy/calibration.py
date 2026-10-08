"""Learn a vehicle's correction factor from real trips.

The physics in model.py needs the drag coefficient (c_w), frontal area,
rolling resistance and efficiencies. Nobody knows these exactly, and they
change with tyres, roof box, load and battery age. Instead of making the user
guess, jolt measures: forecast kWh against kWh actually consumed.

The result is a single number per vehicle. Deliberately no back-calculation to
the individual parameters - a deviation of 8 % does not tell whether the c_w
value or the efficiency was off, and an over-determined model would fit
itself to noise.
"""
import logging

from . import charge_phases

# Below this distance the SoC measurement error (usually 1 % resolution) is
# larger than what is supposed to be measured.
MIN_DISTANCE_KM = 30.0
# How far a single trip may shift the factor. Without the damping, one trip
# with an unnoticed roof box would permanently distort the factor.
SMOOTHING = 0.25
# A factor outside these limits means measurement error, not the vehicle.
LOWER_LIMIT, UPPER_LIMIT = 0.6, 1.8

log = logging.getLogger("uvicorn.error")


def factor_from_trip(forecast_kwh: float, actual_kwh: float,
                     distance_km: float) -> float | None:
    """The raw factor of a single trip, or None if not usable."""
    if distance_km < MIN_DISTANCE_KM or forecast_kwh <= 0:
        return None
    factor = actual_kwh / forecast_kwh
    if not LOWER_LIMIT <= factor <= UPPER_LIMIT:
        log.info("Calibration discarded: factor %.2f outside the limits.",
                 factor)
        return None
    return factor


def carry_on(so_far: float, new_raw_factor: float) -> float:
    """Carry the stored factor forward with a new measurement."""
    return round(so_far * (1 - SMOOTHING) + new_raw_factor * SMOOTHING, 4)


def from_live_session(session, battery_net_kwh: float) -> float | None:
    """Raw factor from a completed live session.

    The actual SoC loss is compared with what the profile had predicted at
    the same place. Both are already stored on the measurement points -
    `plan_soc` is written along when a point arrives, precisely so that
    nothing has to be recalculated here.

    Only **reported** charge levels count. Points without a charge level do
    carry position and time, but their charge level is extrapolated from the
    very model that is to be checked here - including them would mean
    measuring the model against itself and reliably getting a factor of 1.0.

    **Calculated section by section, and charging sections drop out.** This
    used to be `first.soc - last.soc`, i.e. the charge level at the start
    minus the one at the end - and any charging in between fell by the
    wayside. Someone who recharges 40 percentage points on the way sees a loss
    at the end that is too small by those 40 points; the learned factor comes
    out too low accordingly, and because it stays within the plausibility
    limits, nobody notices. On every trip with a charging stop the vehicle
    learns that it is more economical than it is - and that is exactly the
    operating mode this is all about.

    Measured on a test run: 103 km with one charging pause gave a raw factor
    of 0.695 instead of the ~1.8 actually driven.
    """
    points = [p for p in session.points if p.plan_soc is not None
              and p.km_on_route is not None and p.soc is not None]
    if len(points) < 2:
        return None

    actual_pp, distance_km = charge_phases.consumption(points)
    plan_pp = sum(a.begin.plan_soc - a.past.plan_soc
                  for a in charge_phases.sections(points) if not a.charges)

    return factor_from_trip(plan_pp / 100.0 * battery_net_kwh,
                            actual_pp / 100.0 * battery_net_kwh, distance_km)
