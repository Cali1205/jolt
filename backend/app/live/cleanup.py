"""End forgotten trips on their own.

A live session ends when someone taps "Fahrt beenden" (end trip). That is the
action one is most likely to forget at the destination - you arrive, get out,
and the phone is the last thing you think of.

For a planned trip that is only half as bad: the samples are in the
database, the plan had been calculated anyway. For a **recording** it is a
total loss. Route, elevation profile and energy profile are only built from
the samples when the trip ends; until then the trip is a shell with empty
geometry. Whoever forgets to end it has recorded in vain - and only notices
when they want to look it up.

That is why jolt ends by itself whatever has been silent for a while. The
deadlines are deliberately generous, because the two errors are not equally
expensive:

* **Ended too early** means the trip is cut in half. The rest of the route is
  lost, and it cannot be repeated.
* **Ended too late** means the trip is booked an hour too long. The route is
  right, the energy profile is right, only the timestamp at the end is
  generous - and that hardly matters for learning, because neither distance
  nor consumption is added during that hour.

So too late is clearly cheaper than too early, and the deadlines are set
accordingly.

The most dangerous case is the **charging pause**. It can last an hour, the
phone lies in the car meanwhile or is locked, and afterwards the trip
continues. If it is cleaned up in the meantime, the second half of the trip
is lost. That is why `charge_phases.charges_at_end` checks whether the state
of charge has *risen* at the end of the samples - then the last thing jolt
saw was a charging process, and the deadline is extended significantly once
more.

The measure is the **last sample**, not the start: a long trip is no reason
to end it, a long silence is.
"""
import asyncio
import logging
from datetime import datetime, timedelta

from .. import models
from ..energy import calibration, charge_phases
from ..geo import haversine_m
from . import recording

log = logging.getLogger("uvicorn.error")

# How long a session may stay silent before it counts as ended.
# Three hours cover a charging stop, lunch and a dead zone together.
QUIET_MINUTES = 180

# And if charging was the last thing seen, twice as long again. A charging
# pause with a meal can easily take two hours, and then the trip continues -
# exactly the trip that must not be cut up.
CHARGE_PAUSE_MINUTES = 360

# How far back to look for a rising state of charge, and by how much it must
# have risen. Higher than the threshold in `energy.charge_phases`, because a
# different question is asked here: not "is this section charging", but "was
# there enough at the end to assume a charging pause". One percentage point is
# more than the noise of the SoC measurement and less than any charging
# process.
CHARGE_WINDOW_MINUTES = 25
CHARGE_SWING_PERCENT = 1.0

# A session without a single sample is a false start: someone tapped
# "record" and changed their mind, or the connection never came about. It does
# not need 90 minutes of leniency.
FALSE_START_MINUTES = 20

# How often to check. More often would not be needed - the deadlines are
# hours.
TICK_SECONDS = 5 * 60


# If the car stands at the end for longer than this, it had been forgotten:
# you get out, walk away and only end the trip later. Everything after the
# last drive is discarded. Ten minutes is more than any traffic light, any
# level crossing and any traffic jam that is still moving.
AS_OF_DISCARD_MINUTES = 10

# From this mean speed between two samples on, the car counts as driving.
# Walking gets you to 5 km/h, and GPS noise while stationary is below that;
# whoever walks away with the phone in hand is not driving.
DRIVE_KMH = 12.0


def as_of_at_end_cut_off(db, session) -> dict | None:
    """Discard the time after the last drive if it was long enough.

    The use case: getting out and forgetting to end the trip. The phone then
    lies in a pocket for hours, the samples from the car park and the walk
    home are attached to the route, and when learning, each of those minutes
    counts as standing consumption.

    Driving is judged by the distance between two samples (distance over
    time), not by `speed_kmh`: that is regularly missing on iOS. Only the
    **end** is shortened. A charging pause in the middle of the trip stays as
    it is - after all, the trip continues afterwards.
    """
    points = list(session.points)
    if len(points) < 3:
        return None

    latest_trip = None
    for ahead_of, afterwards in zip(points, points[1:]):
        dt = (afterwards.timestamp - ahead_of.timestamp).total_seconds()
        if dt <= 0:
            continue
        kmh = haversine_m(ahead_of.lat, ahead_of.lon, afterwards.lat, afterwards.lon) / dt * 3.6
        if kmh >= DRIVE_KMH or (afterwards.speed_kmh or 0) >= DRIVE_KMH:
            latest_trip = afterwards
    # Never drove: that is a different story (false start), not a remainder.
    if latest_trip is None:
        return None

    as_of = points[-1].timestamp - latest_trip.timestamp
    if as_of < timedelta(minutes=AS_OF_DISCARD_MINUTES):
        return None

    path = [p for p in points if p.timestamp > latest_trip.timestamp]
    for p in path:
        session.points.remove(p)
        db.delete(p)
    session.ended_at = latest_trip.timestamp
    db.flush()
    result = {"discarded_points": len(path),
                "discarded_minutes": round(as_of.total_seconds() / 60)}
    log.info("Session %s: discarded %s min of standing time at the end (%s samples).",
             session.id, result["discarded_minutes"], len(path))
    return result


def end_and_learn(db, session) -> dict:
    """Bring a session to a proper end: build the route, then learn.

    The order is not a matter of taste. The calibration compares `plan_soc`
    with `soc` at the samples, and the target value only comes into being
    when the route is built - the other way round it learns against nothing
    but zeros.

    This function lives here and not in the router, because there are
    **three** ways in which a session ends: the button on the phone, the
    cleanup further down, and starting a new recording that replaces the old
    one. Three copies of the same procedure inevitably drift apart, and on the
    third path it was last missing altogether.
    """
    result: dict = {"recording": None, "learned": None,
                      "not_learned": None}
    trip = session.trip

    # Shorten first: route and learning should never see the end of the trip,
    # only the trip itself.
    result["as_of_discarded"] = as_of_at_end_cut_off(db, session)

    if trip is not None and trip.recording and not trip.geometry:
        try:
            result["recording"] = recording.complete(
                db, trip, session)
        except Exception as failure:      # noqa: BLE001
            # The samples stay; a failed reconstruction must not take them
            # along.
            log.warning("Recording %s could not be completed: %s",
                        trip.id, failure)
            result["recording"] = {"ok": False, "reason": str(failure)}

    vehicle = trip.vehicle if trip else None

    # A trip with a bike rack or roof box teaches nothing about the
    # *vehicle*: the measured extra consumption then contains two unknowns,
    # and two numbers cannot be determined from one measurement.
    surcharge = (trip.air_drag_factor or 1.0) if trip else 1.0
    # The same applies with a trailer: mass and air resistance of the combination
    # are contained in the measured consumption and do not belong in the vehicle.
    if vehicle and trip.trailer_kg:
        result["not_learned"] = (
            f"Fahrt mit Anhänger ({trip.trailer_kg:g} kg) - daraus "
            f"lässt sich der Faktor des Fahrzeugs nicht bestimmen.")
        vehicle = None
    elif vehicle and abs(surcharge - 1.0) > 0.001:
        result["not_learned"] = (
            f"Fahrt mit Luftwiderstands-Zuschlag ×{surcharge:g} - daraus "
            f"lässt sich der Faktor des Fahrzeugs nicht bestimmen.")
        vehicle = None

    if vehicle:
        # The measured capacity here, too: the learned factor is the quotient
        # of measured and predicted energy, and the measured one comes from
        # percent times capacity.
        raw = calibration.from_live_session(session, vehicle.capacity_kwh)
        if raw is not None:
            earlier = vehicle.correction_factor
            vehicle.correction_factor = calibration.carry_on(earlier, raw)
            result["learned"] = {"raw_factor": round(raw, 3),
                                   "earlier": round(earlier, 3),
                                   "after": vehicle.correction_factor}
            log.info("Calibration %s: %.3f -> %.3f (raw %.3f)",
                     vehicle.name, earlier, vehicle.correction_factor, raw)
    return result


def end_orphaned(db) -> list[dict]:
    """End all sessions that have been silent for too long.

    Returns what was ended - for the log, and so that a check run can look
    something up.
    """
    now = datetime.utcnow()
    ended_at = []
    for session in (db.query(models.LiveSession).filter_by(running=True)
                    .with_for_update(skip_locked=True).all()):
        points = session.points
        tail = points[-1].timestamp if points else session.started_at
        if not points:
            mins = FALSE_START_MINUTES
        elif charge_phases.charges_at_end(points, CHARGE_WINDOW_MINUTES,
                                      CHARGE_SWING_PERCENT):
            mins = CHARGE_PAUSE_MINUTES
        else:
            mins = QUIET_MINUTES
        if tail is None or now - tail < timedelta(minutes=mins):
            continue

        session.running = False
        session.ended_at = now
        result = {"session_id": session.id, "points": len(points),
                    "deadline_minutes": mins,
                    "quiet_minutes": round((now - tail).total_seconds() / 60)}

        # Learning happens here, too - a forgotten trip is no worse a
        # measurement than a properly ended one.
        # A session on which learning fails must not hold up the others -
        # otherwise every round tries the same one first and never gets any
        # further. It is ended; what failed is in the log.
        try:
            with db.begin_nested():
                result.update(end_and_learn(db, session))
        except Exception as failure:      # noqa: BLE001
            log.warning("Orphaned session %s could not be completed: %s",
                        session.id, failure)
            result["failure"] = str(failure)

        ended_at.append(result)
        log.info("Orphaned session %s ended after %s min of silence: %s",
                 session.id, result["quiet_minutes"], result)

    if ended_at:
        db.commit()
    return ended_at


async def loop(db_factory) -> None:
    """The background task. Fired up when the application starts.

    Each pass gets its own database session: the task runs for the whole
    lifetime of the process, and a permanently open connection is exactly the
    one that dies at the first network hiccup.
    """
    while True:
        await asyncio.sleep(TICK_SECONDS)
        db = db_factory()
        try:
            end_orphaned(db)
        except Exception as failure:      # noqa: BLE001
            # A failed round must not end the task - otherwise cleanup stops
            # for good at the first error, and nobody notices.
            log.warning("Cleanup failed: %s", failure)
        finally:
            db.close()
