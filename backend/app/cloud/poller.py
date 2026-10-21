"""Fetch the manufacturer cloud's snapshot in the background.

Started by main.py only when a source is configured. Each round asks the
cloud for what is new and stores the snapshot unless that report is already
there. A failing round (wrong password, portal down, VW changed the login)
must neither stop the task nor fill the log: the next try waits twice as
long each time, up to two hours, and the log gets one line when the state
changes, not one per round.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import models, signals
from ..timestamp import utc_iso
from . import Config, SourceError, active, logbook, mask
from .snapshot import Snapshot

log = logging.getLogger("uvicorn.error")

MAX_DELAY_S = 2 * 3600
# After this long without a new report the car is parked and unchanged;
# asking every quarter hour then only tries the portal's patience (it
# answers 429). Nothing is lost by asking less often: every report the car
# made in between is still listed and is fetched one by one.
QUIET_AFTER_S = 2 * 3600
QUIET_DELAY_S = 3600


class SetupError(Exception):
    """The configuration does not say which car or vehicle is meant."""


@dataclass
class Status:
    name: str
    vehicle_id: int | None = None
    last_ok: datetime | None = None
    last_error: str | None = None
    failures: int = 0


# One per configured source, for /api/cloud/status.
STATUS: dict[str, Status] = {}


def store(db, vehicle_id: int, source: str, snap: Snapshot,
          now: datetime | None = None) -> bool:
    """Keep the snapshot unless this report is already stored."""
    captured_at = snap.captured_at.astimezone(timezone.utc).replace(tzinfo=None)
    known = (db.query(models.VehicleSnapshot.id)
             .filter_by(vehicle_id=vehicle_id, source=source,
                        captured_at=captured_at)
             .first())
    if known:
        return False
    db.add(models.VehicleSnapshot(vehicle_id=vehicle_id, source=source,
                                  captured_at=captured_at,
                                  received_at=now or datetime.utcnow(),
                                  values=dict(snap.values),
                                  raw=dict(snap.raw) or None))
    db.commit()
    return True


def delay_s(interval_min: int, failures: int,
            last_new: datetime | None = None,
            now: datetime | None = None) -> float:
    """Back off after failures; otherwise the interval while the car has
    been reporting lately, and once an hour while it is quiet."""
    base = interval_min * 60
    if failures:
        return min(base * 2 ** failures, MAX_DELAY_S)
    recent = (last_new is not None and now is not None
              and (now - last_new).total_seconds() < QUIET_AFTER_S)
    return base if recent else max(base, QUIET_DELAY_S)


def record(status: Status, error: str | None, now: datetime) -> str | None:
    """Book the round; return a log line only when the state changed."""
    if error is None:
        line = None
        if status.failures:
            line = f"{status.name}: working again"
        elif status.last_ok is None:
            line = f"{status.name}: first snapshot fetched"
        status.last_ok, status.last_error, status.failures = now, None, 0
        return line
    changed = status.failures == 0 or error != status.last_error
    status.failures += 1
    status.last_error = error
    return f"{status.name}: {error}" if changed else None


def _one_vin(vins: list[str], env: str) -> str:
    if len(vins) == 1:
        return vins[0]
    if not vins:
        raise SetupError("no vehicle linked to the account")
    raise SetupError(f"the account has {len(vins)} vehicles "
                     f"({', '.join(mask(v) for v in vins)}) - set {env}_VIN")


def _vehicle_id(db, configured: str | None, env: str) -> int:
    if configured is None:
        ids = [row[0] for row in db.query(models.Vehicle.id).all()]
        if len(ids) != 1:
            raise SetupError(f"jolt has {len(ids)} vehicles - set {env}_VEHICLE")
        return ids[0]
    try:
        vehicle_id = int(configured)
    except ValueError:
        raise SetupError(f"{env}_VEHICLE must be a jolt vehicle id") from None
    if db.get(models.Vehicle, vehicle_id) is None:
        raise SetupError(f"{env}_VEHICLE={vehicle_id}: no such vehicle in jolt")
    return vehicle_id


def remember_vin(db, vehicle_id: int, vin: str) -> None:
    """The cloud knows which car it reports on; the app's dongle check needs
    that VIN on the vehicle. Fills only an empty field, and only when no
    other vehicle has it."""
    vehicle = db.get(models.Vehicle, vehicle_id)
    if vehicle is None or vehicle.vin or not vin or len(vin) != 17:
        return
    if db.query(models.Vehicle.id).filter(models.Vehicle.vin == vin).first():
        log.warning("cloud: VIN %s belongs to another vehicle, not stored", mask(vin))
        return
    vehicle.vin = vin
    db.commit()
    log.info("cloud: VIN %s stored on vehicle %s", mask(vin), vehicle_id)


class Poll:
    """One source: its client, the car it means, and its status."""

    def __init__(self, config: Config, db_factory):
        self.config = config
        self.db_factory = db_factory
        self.client = None
        self.vin: str | None = None
        self.last_new: datetime | None = None
        self.logbook_error: str | None = None
        # The whole trip list once per start; the upsert makes a repeat harmless.
        self.trips_fetched = False
        self.trips_error: str | None = None
        self.status = STATUS[config.name] = Status(config.name)

    def tick(self) -> int:
        """One round, blocking: how many new snapshots were stored."""
        db = self.db_factory()
        try:
            if self.status.vehicle_id is None:
                self.status.vehicle_id = _vehicle_id(db, self.config.vehicle,
                                                     self.config.env)
            if self.client is None:
                self.client = self.config.connect(self.status.vehicle_id)
            if self.vin is None:
                self.vin = self.config.vin or _one_vin(self.client.vehicles(),
                                                       self.config.env)
                remember_vin(db, self.status.vehicle_id, self.vin)
            stored = sum(store(db, self.status.vehicle_id, self.config.name, snap)
                         for snap in self.client.updates(self.vin))
            if stored:
                self.last_new = datetime.utcnow()
            self.update_trips(db)
            self.update_logbook(db)
            return stored
        finally:
            db.close()

    def update_trips(self, db) -> None:
        """The car's own trip list, for a client that has one (Stellantis):
        every page on the first round, then the last pages while the car has
        been reporting lately - a parked car makes no trips. Stored under
        `<source>_trips`; a failure does not count as a failed fetch."""
        drives = getattr(self.client, "drives", None)
        if drives is None:
            return
        active = (self.last_new is not None
                  and (datetime.utcnow() - self.last_new).total_seconds() < QUIET_AFTER_S)
        if self.trips_fetched and not active:
            return
        try:
            records = drives(self.vin, everything=not self.trips_fetched)
            logbook.store_drives(db, self.status.vehicle_id, self.config.name + "_trips",
                                 records)
            db.commit()
            self.trips_fetched = True
            error = None
        except Exception as failure:      # noqa: BLE001
            db.rollback()
            error = _describe(failure)
        if error and error != self.trips_error:
            log.warning("%s: trips: %s", self.config.name, error)
        self.trips_error = error

    def update_logbook(self, db) -> None:
        """Drives and charges from the stored reports. Every round, not only
        when something new came: a drive is stored only half an hour after
        its end, and a parked car may send nothing new for hours. A failure
        here must not count as a failed fetch."""
        try:
            logbook.store_from_snapshots(
                db, self.status.vehicle_id, self.config.name,
                powered="charge_power_kw" in self.config.provides,
                stop_split=(timedelta(minutes=2 * self.config.interval_min)
                            if self.config.sampled else None))
            error = None
        except Exception as failure:      # noqa: BLE001
            db.rollback()
            error = _describe(failure)
        if error and error != self.logbook_error:
            log.warning("%s: logbook: %s", self.config.name, error)
        self.logbook_error = error


def _describe(failure: Exception) -> str:
    if isinstance(failure, (SourceError, SetupError)):
        return str(failure)
    return f"{type(failure).__name__}: {failure}"[:200]


async def run(poll: Poll, sleep=asyncio.sleep) -> None:
    while True:
        try:
            await asyncio.to_thread(poll.tick)
            error = None
        except Exception as failure:      # noqa: BLE001
            error = _describe(failure)
        line = record(poll.status, error, datetime.utcnow())
        if line:
            (log.warning if error else log.info)(line)
        await sleep(delay_s(poll.config.interval_min, poll.status.failures,
                            poll.last_new, datetime.utcnow()))


async def loop(db_factory, configs: list[Config] | None = None) -> None:
    """The background task: one round per source and interval."""
    polls = [Poll(config, db_factory)
             for config in (active() if configs is None else configs)]
    for poll in polls:
        log.info("%s: fetching every %d min", poll.config.name,
                 poll.config.interval_min)
    await asyncio.gather(*(run(poll) for poll in polls))


def status_list() -> list[dict]:
    rows = []
    for config in active():
        status = STATUS.get(config.name) or Status(config.name)
        rows.append({"name": status.name,
                     "label": signals.SOURCE_LABELS.get(status.name, status.name),
                     "vehicle_id": status.vehicle_id,
                     "last_ok": utc_iso(status.last_ok),
                     "last_error": status.last_error,
                     "failures": status.failures})
    return rows
