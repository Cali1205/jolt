"""The logbook: drives and charges, stored once however many sources saw them.

Three sources can see the same drive: the VW history export, the portal's
live reports and jolt's own recording. None depends on another. A drive the
VW data shows is stored without the app having been running, a jolt
recording is stored without VW credentials, and when both saw it, it is one
row with both names in `sources`.

What a row says is derived, never accumulated. `details.by_source` keeps what
each source detected; the columns are computed from it with a fixed
preference per field. That makes storing idempotent (the same detection
again replaces itself and changes nothing) and independent of the order in
which the sources arrive. A detected drive belongs to a stored one when their
odometer ranges overlap, or, where one of them has no odometer, their times.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import and_, or_

from .. import models
from ..geo import haversine_m
from . import observations, sessions

log = logging.getLogger("uvicorn.error")

HISTORY = "vw_history"
PORTAL = "vw_portal"
JOLT = "jolt"

# Times, odometer and charge level: the export reports every kilometre, the
# portal only at ignition on and off, the dongle reads the same odometer.
TRACK_PREFERENCE = (HISTORY, PORTAL, JOLT)
# Energy: the dongle reads the battery's own counters; VW only has the
# charge level in whole percent and, from the portal, the energy content.
ENERGY_PREFERENCE = (JOLT, PORTAL, HISTORY)
CHARGE_PREFERENCE = (HISTORY, PORTAL)


def _naive(t: datetime) -> datetime:
    return t.astimezone(timezone.utc).replace(tzinfo=None) if t.tzinfo else t


def _iso(t: datetime) -> str:
    return _naive(t).isoformat()


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _track(track) -> list:
    return [[_iso(t), v] for t, v in track]


# ---------------------------------------------------------------------------
# Records: what one source says about one drive or charge, JSON-ready
# ---------------------------------------------------------------------------

def drive_record(d: sessions.Drive, live_session_id: int | None = None) -> dict:
    return {"started_at": _iso(d.started_at), "ended_at": _iso(d.ended_at),
            "odo_start": d.odo_start, "odo_end": d.odo_end,
            "distance_km": round(d.odo_end - d.odo_start, 1),
            "soc_start": d.soc_start, "soc_end": d.soc_end,
            "energy_used_kwh": d.energy_used_kwh,
            "km_track": _track(d.km_track), "soc_track": _track(d.soc_track),
            "live_session_id": live_session_id}


def charge_record(c: sessions.Charge) -> dict:
    return {"started_at": _iso(c.started_at), "ended_at": _iso(c.ended_at),
            "soc_start": c.soc_start, "soc_end": c.soc_end,
            "energy_kwh": c.energy_kwh, "max_power_kw": c.max_power_kw,
            "avg_power_kw": c.avg_power_kw, "charge_type": c.charge_type,
            "odometer_km": c.odometer_km,
            "power_track": _track(c.power_track)}


def _span(record: dict) -> tuple[datetime, datetime]:
    return _time(record["started_at"]), _time(record["ended_at"])


def _has_odometer(record: dict) -> bool:
    return record.get("odo_start") is not None and record.get("odo_end") is not None


def _overlaps(a: dict, b: dict) -> bool:
    """The same drive or charge? By the odometer when both have it - it is
    exact, where times of two sources can touch at a switch-off burst - and
    by time otherwise."""
    if _has_odometer(a) and _has_odometer(b):
        return a["odo_start"] < b["odo_end"] and b["odo_start"] < a["odo_end"]
    (a0, a1), (b0, b1) = _span(a), _span(b)
    return a0 < b1 and b0 < a1


def _combine(records: list[dict]) -> dict:
    """Several records of one source for one row (a source that split what
    another saw as one), read as one."""
    records = sorted(records, key=lambda r: r["started_at"])
    if len(records) == 1:
        return records[0]
    first, last = records[0], records[-1]

    def values(key):
        return [r[key] for r in records if r.get(key) is not None]

    combined = dict(first)
    combined.update(ended_at=max(r["ended_at"] for r in records),
                    soc_end=last.get("soc_end"))
    for key in ("distance_km", "energy_used_kwh", "energy_kwh"):
        if key in first:
            found = values(key)
            combined[key] = (round(sum(found), 2) if len(found) == len(records)
                             else None)
    for key, pick in (("odo_start", min), ("odo_end", max), ("max_power_kw", max)):
        if key in first:
            combined[key] = pick(values(key), default=None)
    for key in ("km_track", "soc_track", "power_track"):
        if key in first:
            combined[key] = [p for r in records for p in r.get(key) or []]
    if "avg_power_kw" in first:
        start, end = _span(combined)
        hours = (end - start).total_seconds() / 3600
        combined["avg_power_kw"] = (round(combined["energy_kwh"] / hours, 2)
                                    if hours > 0 and combined["energy_kwh"] else None)
    return combined


def _preferred(by_source: dict, order: tuple) -> dict:
    """The combined record of the first source in `order` that has one."""
    return next(_combine(by_source[s]) for s in order if by_source.get(s))


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def _same_distance(a: dict, b: dict) -> bool:
    return abs(a["distance_km"] - b["distance_km"]) <= max(1.0, 0.1 * b["distance_km"])


def _apply_drive(row: models.VehicleDrive) -> None:
    by_source = row.details["by_source"]
    main = _preferred(by_source, TRACK_PREFERENCE)
    row.started_at, row.ended_at = _span(main)
    row.odo_start, row.odo_end = main.get("odo_start"), main.get("odo_end")
    row.distance_km = main["distance_km"]
    row.soc_start, row.soc_end = main.get("soc_start"), main.get("soc_end")
    # Energy only from a source that saw the whole drive: a recording that
    # covers two drives, or half of one, has the wrong kilowatt hours for it.
    energy = None
    for source in ENERGY_PREFERENCE:
        if by_source.get(source):
            found = _combine(by_source[source])
            if found.get("energy_used_kwh") is not None and _same_distance(found, main):
                energy = found["energy_used_kwh"]
                break
    row.energy_used_kwh = energy
    jolt = by_source.get(JOLT) or []
    row.live_session_id = (max(jolt, key=lambda r: r["distance_km"])["live_session_id"]
                           if jolt else None)
    row.sources = [s for s in TRACK_PREFERENCE if by_source.get(s)]


def _apply_charge(row: models.VehicleCharge) -> None:
    main = _preferred(row.details["by_source"], CHARGE_PREFERENCE)
    row.started_at, row.ended_at = _span(main)
    for key in ("soc_start", "soc_end", "energy_kwh", "max_power_kw",
                "avg_power_kw", "charge_type", "odometer_km"):
        setattr(row, key, main.get(key))
    row.sources = [s for s in CHARGE_PREFERENCE if row.details["by_source"].get(s)]


def _snapshot(row) -> tuple:
    return tuple(getattr(row, c.name) for c in row.__table__.columns)


def _candidates(db, model, vehicle_id: int, record: dict):
    """Stored rows that `_overlaps` the record."""
    start, end = _span(record)
    by_time = and_(model.started_at < end, model.ended_at > start)
    if model is models.VehicleDrive:
        if _has_odometer(record):
            by_time = or_(
                and_(model.odo_start < record["odo_end"],
                     model.odo_end > record["odo_start"]),
                and_(by_time, model.odo_start.is_(None)))
    return (db.query(model).filter(model.vehicle_id == vehicle_id, by_time)
            .order_by(model.id).all())


def _merge(db, apply, row, others: list, source: str, record: dict) -> bool:
    """Put the record into the row (and the other rows with it); True when
    the row changed."""
    before = _snapshot(row), row.details
    by_source = {k: list(v) for k, v in row.details["by_source"].items()}
    for other in others:
        for name, items in other.details["by_source"].items():
            by_source.setdefault(name, []).extend(items)
        db.delete(other)
    # The source's earlier detection of the same drive gives way to this one.
    kept = [r for r in by_source.get(source, []) if not _overlaps(r, record)]
    by_source[source] = sorted(kept + [record], key=lambda r: r["started_at"])
    row.details = {**row.details, "by_source": by_source}
    apply(row)
    return bool(others) or (_snapshot(row), row.details) != before


def _counts() -> dict:
    return {"new": 0, "updated": 0, "unchanged": 0}


def _upsert(db, model, apply, vehicle_id: int, source: str,
            records: list[dict]) -> dict:
    counts = _counts()
    for record in records:
        rows = _candidates(db, model, vehicle_id, record)
        if not rows:
            row = model(vehicle_id=vehicle_id,
                        details={"by_source": {source: [record]}})
            apply(row)
            db.add(row)
            counts["new"] += 1
        elif source == JOLT:
            # A recording with a charging stop spans two drives the VW data
            # tells apart. It belongs to each of them and glues none together.
            changed = [_merge(db, apply, row, [], source, record) for row in rows]
            counts["updated" if any(changed) else "unchanged"] += 1
        else:
            changed = _merge(db, apply, rows[0], rows[1:], source, record)
            counts["updated" if changed else "unchanged"] += 1
        db.flush()
    return counts


def store_drives(db, vehicle_id: int, source: str, records: list[dict]) -> dict:
    return _upsert(db, models.VehicleDrive, _apply_drive, vehicle_id, source,
                   records)


def store_charges(db, vehicle_id: int, source: str, records: list[dict]) -> dict:
    return _upsert(db, models.VehicleCharge, _apply_charge, vehicle_id, source,
                   records)


def kwh_per_pct(vehicle: models.Vehicle | None) -> float | None:
    return vehicle.capacity_kwh / 100 if vehicle else None


# ---------------------------------------------------------------------------
# Source: jolt's own recordings
# ---------------------------------------------------------------------------

def _gps_km(points) -> float:
    km = 0.0
    for a, b in zip(points, points[1:]):
        km += haversine_m(a.lat, a.lon, b.lat, b.lon) / 1000
    return km


def session_record(session: models.LiveSession) -> dict | None:
    """A finished live session as one drive - the trip as its driver
    recorded it, even when it had a charging stop the VW data splits at.

    Not split by `sessions.detect_drives`: the dongle falls silent whenever
    its connection drops, so its gaps say nothing about the car standing.
    Without odometer readings a recording counts with its GPS distance; a
    planned trip without them is left out, since the simulator plays those
    back with made-up times.
    """
    points = list(session.points)
    if session.running or len(points) < 2:
        return None
    by_field: dict[str, list] = {}
    for o in observations.from_live_points(points):
        by_field.setdefault(o.field, []).append((o.t, o.value))
    km_track = sessions.thin(sessions.dedup(sorted(by_field.get("odometer_km", []))))
    soc_track = by_field.get("soc_pct", [])
    net = by_field.get("energy_net_kwh", [])
    if km_track:
        odo_start = min(v for _, v in km_track)
        odo_end = max(v for _, v in km_track)
        km = odo_end - odo_start
    else:
        trip = session.trip
        if trip is None or not trip.recording:
            return None
        odo_start = odo_end = None
        km = _gps_km(points)
    if km < sessions.MIN_DRIVE_KM:
        return None
    used = net[-1][1] - net[0][1] if len(net) > 1 else None
    return {"started_at": _iso(points[0].timestamp),
            "ended_at": _iso(points[-1].timestamp),
            "odo_start": odo_start, "odo_end": odo_end,
            "distance_km": round(km, 1),
            "soc_start": soc_track[0][1] if soc_track else None,
            "soc_end": soc_track[-1][1] if soc_track else None,
            "energy_used_kwh": round(used, 2) if used is not None and used >= 0 else None,
            "km_track": _track(km_track),
            "soc_track": _track(sessions.thin(sessions.dedup(soc_track))),
            "live_session_id": session.id}


def store_session(db, session: models.LiveSession) -> dict:
    record = session_record(session)
    if record is None:
        return _counts()
    return store_drives(db, session.trip.vehicle_id, JOLT, [record])


def store_all_sessions(db, vehicle_id: int | None = None) -> dict:
    """Every finished live session (of one vehicle) into the logbook."""
    query = (db.query(models.LiveSession)
             .join(models.Trip, models.LiveSession.trip_id == models.Trip.id)
             .filter(models.LiveSession.running.is_(False)))
    if vehicle_id is not None:
        query = query.filter(models.Trip.vehicle_id == vehicle_id)
    total = _counts()
    for session in query.order_by(models.LiveSession.id).all():
        for key, value in store_session(db, session).items():
            total[key] += value
    return total


def after_session_end(db, session: models.LiveSession) -> None:
    """Hook for the end of a live session; never lets ending fail."""
    try:
        with db.begin_nested():
            store_session(db, session)
    except Exception as failure:      # noqa: BLE001
        log.warning("Logbook: session %s not stored: %s", session.id, failure)
