"""Play back a trip without driving.

Without the simulator, the live chain could only be verified once a car, a
data supplier and a real long-distance drive come together. That would leave
exactly the part untested that this project is about.

The simulator walks along the planned route and reports charge levels that
deviate from the plan by `extra_consumption`. With 1.0 it follows the plan
exactly, with 1.2 it uses twenty percent more - and that is precisely when the
tracking has to kick in and bring the reserve forward. That is the acid test.

`time_factor` does the same with the clock: 1.4 means "forty percent longer
on the road than planned", i.e. a traffic jam. For that, every sample carries
a **simulated** time. Without it the time factor could not be tested here -
the simulator plays hours back in seconds, and measured against the real
clock every trip would be absurdly fast.
"""
import asyncio
import logging
from datetime import datetime, timedelta

from .. import models, push
from ..energy.profile import entry_at
from . import channel, session as live_session

log = logging.getLogger("uvicorn.error")

STEP_KM = 5.0


def steps(trip: models.Trip, extra_consumption: float = 1.0,
             step_km: float = STEP_KM,
             time_factor: float = 1.0) -> list[dict]:
    """The samples of a simulated trip.

    Pure function without a database and without waiting - so that it can be
    worked through directly in a check script.
    """
    profile = trip.energy_profile or []
    if not profile:
        return []

    total_km = profile[-1].get("km") or 0.0
    points = []
    km = 0.0
    while km <= total_km:
        entry = entry_at(profile, km)
        consumed = trip.start_soc - (entry.get("soc") or trip.start_soc)
        soc = trip.start_soc - consumed * extra_consumption
        points.append({
            "lat": entry.get("lat"), "lon": entry.get("lon"),
            "soc": round(max(0.0, soc), 2),
            "speed_kmh": entry.get("speed_kmh"),
            "outside_temp_c": trip.outside_temp_c,
            "km": round(km, 1),
            # Minutes since departure, as they would have passed *in the car*.
            "mins": round((entry.get("mins") or 0.0) * time_factor, 3)})
        # At zero it is over. A simulated car that keeps driving on an empty
        # battery while dutifully reporting 0 % would hide exactly the case
        # the simulation is meant to expose: that it should have charged
        # earlier. Whoever uses more gets less far - and that has to be
        # visible in the number of samples.
        if soc <= 0:
            break
        km += step_km
    return points



async def replay(db_factory, session_id: int, extra_consumption: float = 1.0,
                    tick_s: float = 1.0, step_km: float = STEP_KM,
                    time_factor: float = 1.0) -> None:
    """The simulation as a background task.

    Each step gets its own database session: the task runs for minutes, and a
    connection held open the whole time would be exactly the one that dies at
    the first network hiccup.
    """
    db = db_factory()
    try:
        session = db.get(models.LiveSession, session_id)
        if not session:
            return
        points = steps(session.trip, extra_consumption, step_km, time_factor)
    finally:
        db.close()

    # The zero point of the simulated clock. The samples carry their time
    # relative to it, so that the tracking sees a plausible trip and not six
    # hundred kilometres in four seconds.
    onset = datetime.utcnow()

    for sample in points:
        db = db_factory()
        try:
            session = db.get(models.LiveSession, session_id)
            if not session or not session.running:
                return
            state = live_session.record_sample(
                db, session, sample["lat"], sample["lon"], sample["soc"],
                sample.get("speed_kmh"), sample.get("outside_temp_c"),
                timestamp=onset + timedelta(minutes=sample.get("mins") or 0.0))
        except Exception as failure:      # noqa: BLE001
            log.warning("Simulation aborted: %s", failure)
            return
        finally:
            db.close()

        await channel.send(session_id, {"kind": "zustand", "simulated": True,
                                        **live_session.state_as_dict(state)})
        # The simulation notifies, too - otherwise the chain could never be
        # played through all the way to the phone without actually driving.
        if state.plan_changed:
            push.send_background(db_factory, "jolt – Ladeplan geändert",
                                    state.change)
        await asyncio.sleep(tick_s)

    db = db_factory()
    try:
        session = db.get(models.LiveSession, session_id)
        if session:
            session.running = False
            db.commit()
    finally:
        db.close()
    await channel.send(session_id, {"kind": "ende", "simulated": True})
