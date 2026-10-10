"""Live endpoints: measurement points in, state out.

The POST endpoint is deliberately kept so simple that an OBD2 logger or an
Apple Shortcut can use it without a library - a JSON object with position
and state of charge, nothing more is needed. This is the interface where the
real vehicle data will dock on later.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import (APIRouter, Depends, HTTPException, Query, Request,
                     WebSocket, WebSocketDisconnect)
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from .. import deps, models, push, security
from ..timestamp import utc_iso
from ..database import SessionLocal, get_db
from ..live import cleanup, channel, sources, simulator, replanning
from ..live import session as live_session

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api/live", tags=["live"])


# The dongle's measurement has just under twenty values; four times that is headroom.
RAW_VALUES_MAX_KEY = 80
RAW_VALUES_MAX_BYTES = 8000


class Sample(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    # Without a state of charge this is a pure position report - the normal
    # case as long as the car does not supply its state of charge itself.
    # The phone delivers the position every second, the state of charge is
    # occasionally added by hand. Without these points there would be
    # neither a time factor nor an arrival forecast on the road.
    soc: float | None = Field(default=None, ge=0, le=100)
    # Limits no car exceeds: a value from a broken logger (unit mixed up,
    # overflow) would otherwise flow unchecked into the calibration and
    # distort the learning factor.
    speed_kmh: float | None = Field(default=None, ge=0, le=500,
                                    allow_inf_nan=False)
    outside_temp_c: float | None = Field(default=None, ge=-80, le=70,
                                       allow_inf_nan=False)
    # Everything else the source delivers - only kept, not used in
    # calculations. See models.LivePoint.rohwerte.
    raw_values: dict | None = None

    @field_validator("raw_values")
    @classmethod
    def _limit_raw_values(cls, value):
        """It is stored per point as JSON - so it has to stay small.

        A batch has up to 500 points; without a limit a single request could
        fill the database and memory.
        """
        if value is None:
            return value
        if len(value) > RAW_VALUES_MAX_KEY:
            raise ValueError(f"höchstens {RAW_VALUES_MAX_KEY} Rohwerte je Punkt")
        if len(json.dumps(value, default=str)) > RAW_VALUES_MAX_BYTES:
            raise ValueError(f"Rohwerte höchstens {RAW_VALUES_MAX_BYTES} Zeichen")
        return value

    # Time of the **measurement**. If missing, the time of receipt applies.
    # It is set by a device that submits a dead-zone buffer later - otherwise
    # all the late-submitted points would fall on the same second, and the
    # time factor would be nonsense. With time zone (`Z`) or without; without
    # one, UTC applies.
    timestamp: datetime | None = None


class SampleBatch(BaseModel):
    points: list[Sample] = Field(min_length=1, max_length=500)


# How far a timestamp may deviate from the present. Forward only a clock
# error of the phone, backward a long trip without network.
TIME_AHEAD = timedelta(minutes=5)
TIME_BACK = timedelta(hours=48)


def _examine_time(timestamp: datetime | None) -> datetime | None:
    """Bring a device timestamp to naive UTC - or reject it.

    The database holds naive UTC times (`datetime.utcnow`). A stamp with a
    time zone is converted instead of truncated, otherwise `+02:00` would be
    two hours off. A stamp from the future or from the year 1970 is a clock
    error and would wreck the order of the points.
    """
    if timestamp is None:
        return None
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
    now = datetime.utcnow()
    if timestamp > now + TIME_AHEAD or timestamp < now - TIME_BACK:
        raise HTTPException(422, "Der Zeitstempel der Messung liegt zu weit "
                                 "von der Gegenwart entfernt.")
    return timestamp


class LoggerReport(BaseModel):
    """A report from a device in the car.

    Apart from `token` and `format`, nothing is deliberately fixed here: the
    remaining fields belong to the respective format and are validated by its
    translator in `live/quellen/`. Describing them here a second time would
    mean updating every format change in two places - and the validation
    would then lie with Pydantic instead of the module that really knows the
    format.
    """
    model_config = ConfigDict(extra="allow")

    token: str = Field(min_length=1, max_length=64)
    format: str = "jolt"


def _fetch_session(db: Session, session_id: int,
                   lock: bool = False) -> models.LiveSession:
    """`lock` holds the row until the commit, so that two requests that end
    the same session cannot both pass the `running` check."""
    if lock:
        session = (db.query(models.LiveSession).filter_by(id=session_id)
                   .with_for_update().one_or_none())
    else:
        session = db.get(models.LiveSession, session_id)
    if not session:
        raise HTTPException(404, "Live-Sitzung nicht gefunden.")
    return session


def _fetch_active_session(db: Session, session_id: int) -> models.LiveSession:
    session = _fetch_session(db, session_id)
    if not session.running:
        raise HTTPException(409, "Diese Live-Sitzung ist beendet.")
    return session


def _vehicle_to_token(db: Session, token: str):
    return (db.query(models.Vehicle)
            .filter(models.Vehicle.logger_token == token).one_or_none())


def _active_session_to_vehicle(db: Session, vehicle_id: int):
    return (db.query(models.LiveSession)
            .join(models.Trip, models.LiveSession.trip_id == models.Trip.id)
            .filter(models.Trip.vehicle_id == vehicle_id,
                    models.LiveSession.running.is_(True))
            .order_by(models.LiveSession.id.desc())
            .first())


async def _process_point(db: Session, session: models.LiveSession,
                             point: sources.RawPoint,
                             new_plan: bool = True) -> dict:
    """File a measurement point and notify everyone concerned.

    `new_plan=False` (re-plan) means: late-submitted point, not the
    present. Then there is neither re-planning nor sending to the viewers - a
    state from ten minutes ago would make the display jump back.
    """
    # In the thread pool: `record_sample` (record sample) calculates
    # and writes synchronously, and in an `async def` that would block the
    # event loop - a batch of 500 points made every other request and every
    # WebSocket wait.
    state = await run_in_threadpool(
        live_session.record_sample,
        db, session, point.lat, point.lon, point.soc,
        point.speed_kmh, point.outside_temp_c, timestamp=point.timestamp,
        raw_values=point.raw_values, new_plan=new_plan)

    message = {"kind": "zustand", "simulated": False,
                 **live_session.state_as_dict(state)}
    if not new_plan:
        return message
    await channel.send(session.id, message)
    # A changed plan is the only reason to disturb someone at the wheel -
    # and the only one that also has to reach a dark phone. In the
    # background, because the response to a moving car must not wait for a
    # push service.
    if state.plan_changed:
        push.send_background(SessionLocal, "jolt – Ladeplan geändert",
                                state.change)
    return message


@router.post("/start/{trip_id}", dependencies=[Depends(deps.current_session)])
def launch(trip_id: int, radius_km: float = Query(10.0, gt=0, le=50),
            min_kw: float = Query(50.0, ge=0), connector_type: str = Query("", max_length=40),
            detour_limit_min: float = Query(replanning.DEFAULTS["detour_limit_min"],
                                            gt=0, le=60),
            stop_fixed_cost_min: float = Query(
                replanning.DEFAULTS["stop_fixed_cost_min"], ge=0, le=30),
            charge_park_bonus_min: float = Query(
                replanning.DEFAULTS["charge_park_bonus_min"], ge=0, le=15),
            time_value_eur_h: float = Query(
                replanning.DEFAULTS["time_value_eur_h"], ge=0, le=200),
            db: Session = Depends(get_db)):
    trip = db.get(models.Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Fahrt nicht gefunden.")

    # A second running session for the same trip would only be confusing:
    # two consumption factors for the same route, and nobody knows which one
    # applies. The old one is therefore ended.
    for old in db.query(models.LiveSession).filter_by(trip_id=trip_id,
                                                      running=True).all():
        old.running = False
        old.ended_at = datetime.utcnow()
        try:
            with db.begin_nested():
                cleanup.end_and_learn(db, old)
        except Exception as failure:      # noqa: BLE001
            # Same consideration as in `start_recording`: the new start must
            # not fail because of the old session.
            log.warning("Could not finish previous session %s: %s",
                        old.id, failure)

    session = models.LiveSession(trip_id=trip_id)

    # The plan at departure. It is the reference point for everything else:
    # without it one could not say on the road *that* something has changed -
    # only that something differs from what the energy profile expected. And
    # the search parameters stay the same for the whole trip.
    parameter = {"radius_km": radius_km, "min_kw": min_kw,
                 "connector_type": connector_type, "detour_limit_min": detour_limit_min,
                 "stop_fixed_cost_min": stop_fixed_cost_min,
                 "charge_park_bonus_min": charge_park_bonus_min,
                 "time_value_eur_h": time_value_eur_h}
    if trip.energy_profile:
        try:
            session.plan = replanning.schedule(db, trip, 0.0, trip.start_soc,
                                            parameter)
        except Exception as failure:      # noqa: BLE001
            # Without a start plan the trip runs anyway - the tracking then
            # works against the energy profile, as in stage 1.
            log.warning("Start plan failed: %s", failure)

    db.add(session)
    db.commit()
    return {"session_id": session.id, "trip_id": trip_id,
            "plan": session.plan}


@router.post("/{session_id}/point", dependencies=[Depends(deps.current_session)])
async def report_point(session_id: int, sample: Sample,
                       db: Session = Depends(get_db)):
    """File a measurement point.

    Only with login (`X-Token`). This used to say the session ID was the key
    - a sequential number anyone could guess, which allowed reading position
    and state of charge and injecting false measurement points. A device in
    the car without login uses `/report` with its vehicle's logger token.
    """
    session = await run_in_threadpool(_fetch_active_session, db, session_id)
    return await _process_point(db, session, sources.RawPoint(
        lat=sample.lat, lon=sample.lon, soc=sample.soc,
        speed_kmh=sample.speed_kmh, outside_temp_c=sample.outside_temp_c,
        timestamp=_examine_time(sample.timestamp), raw_values=sample.raw_values))


@router.post("/{session_id}/points", dependencies=[Depends(deps.current_session)])
async def report_points(session_id: int, batch: SampleBatch,
                        db: Session = Depends(get_db)):
    """Several measurement points at once - the way for a dead-zone buffer.

    The points are sorted by measurement time and recorded in order; the
    order in the request does not matter. Re-planning happens only for the
    **last** one (see `record_sample`), and only it is sent as state
    to the viewers. The response is the state after the last point - the
    same shape as for `/point`, so the UI treats both alike.

    All or nothing: an invalid timestamp rejects the whole batch before
    anything has been written.
    """
    session = await run_in_threadpool(_fetch_active_session, db, session_id)
    now = datetime.utcnow()
    checked = [(_examine_time(p.timestamp) or now, i, p)
                for i, p in enumerate(batch.points)]
    checked.sort(key=lambda t: (t[0], t[1]))
    message = None
    for nr, (timestamp, _, p) in enumerate(checked):
        message = await _process_point(db, session, sources.RawPoint(
            lat=p.lat, lon=p.lon, soc=p.soc, speed_kmh=p.speed_kmh,
            outside_temp_c=p.outside_temp_c, timestamp=timestamp, raw_values=p.raw_values),
            new_plan=nr == len(checked) - 1)
    return message


class RecordingStart(BaseModel):
    vehicle_id: int
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    soc: float | None = Field(default=None, ge=0, le=100)
    name: str = Field(default="", max_length=120)


@router.post("/recording", dependencies=[Depends(deps.current_session)])
def start_recording(start: RecordingStart,
                         db: Session = Depends(get_db)):
    """Record a trip without planning it beforehand.

    The way for the case where planning is not worthwhile: a known short
    route, driven a few times to learn the vehicle's consumption. Route,
    elevation profile and forecast only arise when ending, from the
    measurement points (`live/recording.py`).

    The start is wherever the device currently is - the destination is still
    unknown at this point and is initially set equal to it. Both are
    corrected when finishing, from the first and last measurement point.
    """
    vehicle = db.get(models.Vehicle, start.vehicle_id)
    if not vehicle:
        raise HTTPException(404, "Fahrzeug nicht gefunden.")

    # A session of the same vehicle that is still running gives way - but it
    # is **finished** and not silently dropped.
    #
    # This used to be only `alt.laeuft = False`. For a recording that meant
    # total loss: route and energy profile only arise when finishing, from
    # the measurement points, and with `laeuft = False` (running = False) the
    # cleanup never sees it again either. Whoever noticed a forgotten trip by
    # starting a new one thereby deleted exactly the one they wanted to save.
    for old in (db.query(models.LiveSession)
                .join(models.Trip, models.LiveSession.trip_id == models.Trip.id)
                .filter(models.Trip.vehicle_id == vehicle.id,
                        models.LiveSession.running.is_(True)).all()):
        old.running = False
        old.ended_at = datetime.utcnow()
        try:
            with db.begin_nested():
                cleanup.end_and_learn(db, old)
        except Exception as failure:      # noqa: BLE001
            # The new trip must not fail because of this - someone is sitting
            # in the car and wants to drive off.
            log.warning("Could not finish previous session %s: %s",
                        old.id, failure)

    label = (start.name or "").strip() or "Aufzeichnung"
    trip = models.Trip(
        vehicle_id=vehicle.id, recording=True,
        start_text=label, target_text="unterwegs",
        start_lat=start.lat, start_lon=start.lon,
        target_lat=start.lat, target_lon=start.lon,
        start_soc=start.soc if start.soc is not None else 100.0,
        geometry=[], energy_profile=[])
    db.add(trip)
    db.flush()

    session = models.LiveSession(trip_id=trip.id)
    db.add(session)
    db.commit()

    # The starting state of charge is a measurement - the first of the trip.
    # Recorded as a measurement point, the recording has a state of charge
    # from the first second: the live display shows it, and the
    # reconstruction when finishing starts at what the car reported, not at
    # 100 %.
    #
    # Until now it was only stored on the trip. A recording has no profile
    # from which a state of charge could be estimated - without a
    # measurement the display stayed empty until the car answered for the
    # first time.
    if start.soc is not None:
        try:
            live_session.record_sample(db, session, start.lat, start.lon,
                                             soc=start.soc)
        except Exception as failure:      # noqa: BLE001
            # The trip must not fail because of this - someone is sitting in
            # the car.
            log.warning("Start point of recording %s not recorded: %s",
                        session.id, failure)
            db.rollback()
    return {"session_id": session.id, "trip_id": trip.id,
            "recording": True}


@router.post("/report")
async def report_logger(report: LoggerReport, request: Request,
                        db: Session = Depends(get_db)):
    """Report a measurement point without knowing the session ID.

    The way for a device permanently installed in the car: an OBD2 dongle, a
    shortcut, a script on a single-board computer. It identifies itself with
    the **vehicle's** logger token - a secret that stays - and the backend
    finds the running live session of this vehicle itself. That changes with
    every trip, and an installed device has no way of learning about it.

    If no trip is running, that is **not an error**: the car is simply
    parked outside, and the logger sends anyway. It therefore gets 200 with
    `aufgenommen: false` (recorded: false) and not 404 - an unattended device
    that runs into error responses starts logging errors or switching itself
    off, and neither helps anyone.

    The format of the measurement values is given by `format`; translation
    happens in `live/quellen/`. If omitted, jolt's own format applies.
    """
    # The token first: whoever has none should not get the translation and
    # thus computing time. And whoever sends a wrong one too often is
    # throttled - the path is exempt from the general limit.
    if security.report_locked(request):
        raise HTTPException(429, "Zu viele ungültige Logger-Token. "
                                 "Später erneut versuchen.")
    vehicle = await run_in_threadpool(_vehicle_to_token, db, report.token)
    if not vehicle:
        # A wrong token is an error - otherwise one could not tell whether
        # the logger is set up wrongly or whether there is just no trip
        # running at the moment.
        security.count_report_error(request)
        raise HTTPException(401, "Logger-Token unbekannt.")

    try:
        translator = sources.find(report.format)
        point = translator.normalize(
            report.model_dump(exclude={"token", "format"}))
    except sources.SourcesError as failure:
        # 400 and not 422: the sentence from the translator says what the
        # logger sends wrongly, and it should reach whoever is setting it up
        # unfiltered.
        raise HTTPException(400, str(failure))

    session = await run_in_threadpool(_active_session_to_vehicle, db,
                                      vehicle.id)
    if not session:
        return {"recorded": False, "vehicle": vehicle.name,
                "reason": "Zu diesem Fahrzeug läuft gerade keine Fahrt."}

    message = await _process_point(db, session, point)
    return {"recorded": True, "session_id": session.id, **message}


@router.get("/{session_id}", dependencies=[Depends(deps.current_session)])
def read_state(session_id: int, db: Session = Depends(get_db)):
    session = _fetch_session(db, session_id)
    last = session.points[-1] if session.points else None
    return {"session_id": session.id, "trip_id": session.trip_id,
            "running": session.running, "hint": session.hint,
            "consumption_factor": round(session.consumption_factor, 3),
            "time_factor": round(session.time_factor, 3),
            # The currently valid plan, so that a device that reconnects does
            # not have to wait for the next measurement point.
            "plan": session.plan,
            "viewer": channel.viewer(session_id),
            "points": len(session.points),
            "last": None if not last else {
                "lat": last.lat, "lon": last.lon, "soc": last.soc,
                "km_on_route": last.km_on_route,
                "plan_soc": last.plan_soc,
                "timestamp": utc_iso(last.timestamp)}}


@router.get("/{session_id}/points", dependencies=[Depends(deps.current_session)])
def read_points(session_id: int, db: Session = Depends(get_db)):
    """The measurement points of a session - for a device that newly joins.

    The state alone is not enough for that: it only knows the *last* point.
    Whoever reloaded the page used to get an empty track, an empty bar chart
    and a state-of-charge curve that started at zero - the trip continued but
    looked like a new one. That is exactly what led, on 2 September, to a
    running trip being considered lost and a new one being planned.

    From `rohwerte` (raw values) only the four numbers are included from
    which the UI back-calculates the consumption. The full set would be
    seventeen times as large per point, and on a long trip with a few
    thousand points nobody downloads that over mobile data.
    """
    session = _fetch_session(db, session_id)
    samples = []
    for point in session.points:
        raw = point.raw_values if isinstance(point.raw_values, dict) else {}
        samples.append({
            "timestamp": utc_iso(point.timestamp),
            "lat": point.lat, "lon": point.lon,
            "soc": point.soc, "km_on_route": point.km_on_route,
            "odometer_km": raw.get("odometer_km"),
            "discharge_kwh": raw.get("discharge_kwh"),
            "charged_kwh": raw.get("charged_kwh"),
            "soc_raw": raw.get("soc_raw"),
        })
    return {"session_id": session.id, "points": samples}


@router.post("/{session_id}/end", dependencies=[Depends(deps.current_session)])
def finish(session_id: int, db: Session = Depends(get_db)):
    """Finish the trip - and learn from it.

    The session's consumption factor applies only to this one trip; it dies
    with it. What should remain is the insight behind it: if the model
    systematically calculates too optimistically, the *next* plan should
    already know that instead of learning it again after eighty kilometers.
    That is exactly why the vehicle carries a correction factor, and exactly
    here it is updated - damped, so that a single trip with a roof box does
    not bend it permanently.
    """
    session = _fetch_session(db, session_id, lock=True)
    # Ending twice (retry after a dead zone, double tap, or the cleanup was
    # faster) must not learn twice: the vehicle's correction factor would be
    # updated twice from the same trip.
    if not session.running:
        return {"ok": True, "already_ended_at": True, "recording": None,
                "as_of_discarded": None, "not_learned": None,
                "consumption_factor": round(session.consumption_factor, 3),
                "learned": None}
    session.running = False
    session.ended_at = datetime.utcnow()

    # A recording only becomes a trip here: route, elevation profile and
    # forecast arise from the measurement points. This has to happen
    # **before** the calibration - it compares target and actual at the
    # points, and the target value is only there afterwards.
    # Build the route, then learn - the procedure lives in
    # `live/aufraeumen.end_and_learn` (cleanup, end_and_learn), because
    # three paths need it.
    result = cleanup.end_and_learn(db, session)
    built = result["recording"]
    learned = result["learned"]
    not_learned = result["not_learned"]

    db.commit()
    return {"ok": True, "recording": built,
            "as_of_discarded": result.get("as_of_discarded"),
            "not_learned": not_learned,
            "consumption_factor": round(session.consumption_factor, 3),
            # None means "this trip was not usable" - too short, or the
            # factor was outside the plausibility limits.
            "learned": learned}


def _session_exists(session_id: int) -> bool:
    db = SessionLocal()
    try:
        return db.get(models.LiveSession, session_id) is not None
    finally:
        db.close()


# Background tasks: the event loop holds only a weak reference. Without a
# strong one of our own, the garbage collector can clean up a running
# simulation.
_tasks: set = set()
_simulations: dict = {}


def _task_hold(task):
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


@router.post("/{session_id}/simulate",
             dependencies=[Depends(deps.current_session)])
async def simulate(session_id: int,
                     extra_consumption: float = Query(1.0, ge=0.5, le=2.0),
                     tick_s: float = Query(0.5, ge=0.05, le=10.0),
                     time_factor: float = Query(1.0, ge=0.5, le=3.0),
                     db: Session = Depends(get_db)):
    """Replay the planned trip, with adjustable extra consumption.

    With 1.0 the simulation follows the plan exactly, with 1.2 it consumes
    twenty percent more - then the tracking has to kick in and bring the
    reserve forward. This is the acid test of the live function.

    `zeitfaktor` (time factor) simulates congestion: 1.4 means "forty percent
    longer on the road". Consumption barely notices, arrival time very much
    so - and that lets one test the trigger that consumption alone never
    fires.
    """
    def examine():
        session = _fetch_active_session(db, session_id)
        if not (session.trip.energy_profile or []):
            raise HTTPException(409, "Zur Fahrt gibt es kein Energieprofil.")

    await run_in_threadpool(examine)

    if session_id in _simulations:
        raise HTTPException(409, "Für diese Fahrt läuft schon eine Simulation.")
    _simulations[session_id] = _task_hold(asyncio.create_task(
        simulator.replay(SessionLocal, session_id, extra_consumption, tick_s,
                            time_factor=time_factor)))
    _simulations[session_id].add_done_callback(
        lambda _t, s=session_id: _simulations.pop(s, None))
    return {"started_at": True, "extra_consumption": extra_consumption,
            "tick_s": tick_s, "time_factor": time_factor}


@router.websocket("/{session_id}/ws")
async def live_channel(websocket: WebSocket, session_id: int):
    """The state of a trip, live.

    A browser cannot set headers on a WebSocket, and a token in the URL
    would appear in every proxy log. So the client sends the token as the
    **first message** (`{"token": "..."}`); only then is it added to the
    distributor and gets to see anything. Without a password this does not
    apply, the response `{"typ": "bereit"}` (kind: ready) comes anyway.
    """
    await websocket.accept()
    if deps.password_set():
        try:
            first_item = await asyncio.wait_for(websocket.receive_text(), timeout=10)
            token = json.loads(first_item).get("token", "")
            valid = isinstance(token, str) and await run_in_threadpool(
                deps.token_valid, token)
        except (asyncio.TimeoutError, ValueError, AttributeError,
                WebSocketDisconnect):
            valid = False
        if not valid:
            try:
                await websocket.close(code=4401)
            except Exception:      # noqa: BLE001
                pass
            return
    present = await run_in_threadpool(_session_exists, session_id)
    if not present or not await channel.sign_in(session_id, websocket):
        try:
            await websocket.close(code=4404 if not present else 4429)
        except Exception:      # noqa: BLE001
            pass
        return
    try:
        await websocket.send_json({"kind": "bereit"})
        while True:
            # Nothing is expected; receiving only keeps the connection open
            # and reports its termination.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as failure:      # noqa: BLE001
        log.debug("Live WebSocket ended: %s", failure)
    finally:
        await channel.sign_out(session_id, websocket)
