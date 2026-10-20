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
from datetime import datetime, timezone

from .. import models
from ..timestamp import utc_iso
from . import Config, active
from .snapshot import Snapshot
from .vw_portal import PortalError, mask

log = logging.getLogger("uvicorn.error")

MAX_DELAY_S = 2 * 3600


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
                                  values=dict(snap.values)))
    db.commit()
    return True


def delay_s(interval_min: int, failures: int) -> float:
    return min(interval_min * 60 * 2 ** failures, MAX_DELAY_S)


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


def _one_vin(vins: list[str]) -> str:
    if len(vins) == 1:
        return vins[0]
    if not vins:
        raise SetupError("no vehicle linked in the portal")
    raise SetupError(f"the account has {len(vins)} vehicles "
                     f"({', '.join(mask(v) for v in vins)}) - set VW_PORTAL_VIN")


def _vehicle_id(db, configured: str | None) -> int:
    if configured is None:
        ids = [row[0] for row in db.query(models.Vehicle.id).all()]
        if len(ids) != 1:
            raise SetupError(f"jolt has {len(ids)} vehicles - set VW_PORTAL_VEHICLE")
        return ids[0]
    try:
        vehicle_id = int(configured)
    except ValueError:
        raise SetupError("VW_PORTAL_VEHICLE must be a jolt vehicle id") from None
    if db.get(models.Vehicle, vehicle_id) is None:
        raise SetupError(f"VW_PORTAL_VEHICLE={vehicle_id}: no such vehicle in jolt")
    return vehicle_id


class Poll:
    """One source: its client, the car it means, and its status."""

    def __init__(self, config: Config, db_factory):
        self.config = config
        self.db_factory = db_factory
        self.client = None
        self.vin: str | None = None
        self.status = STATUS[config.name] = Status(config.name)

    def tick(self) -> bool:
        """One round, blocking. True when a new snapshot was stored."""
        db = self.db_factory()
        try:
            if self.status.vehicle_id is None:
                self.status.vehicle_id = _vehicle_id(db, self.config.vehicle)
            if self.client is None:
                self.client = self.config.connect()
            if self.vin is None:
                self.vin = self.config.vin or _one_vin(self.client.vehicles())
            snap = self.client.latest(self.vin)
            return snap is not None and store(db, self.status.vehicle_id,
                                              self.config.name, snap)
        finally:
            db.close()


def _describe(failure: Exception) -> str:
    if isinstance(failure, (PortalError, SetupError)):
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
        await sleep(delay_s(poll.config.interval_min, poll.status.failures))


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
        rows.append({"name": status.name, "vehicle_id": status.vehicle_id,
                     "last_ok": utc_iso(status.last_ok),
                     "last_error": status.last_error,
                     "failures": status.failures})
    return rows
