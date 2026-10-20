#!/usr/bin/env python3
"""Checks the logbook: drives and charges found in VW data and jolt recordings.

All streams are synthetic, built in the shapes the real data has (the
history export, the portal's reports, the dongle's points). No real VIN, no
real export. Each detection threshold is changed once on purpose to show
that the cases notice it.

Without network, without Postgres:

    ./tools/check_vw_sessions.py
"""
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
from examine import Check, application_provide  # noqa: E402

application_provide("vw-sessions")

from app.cloud import observations, sessions  # noqa: E402

verify = Check()
UTC = timezone.utc
S = timedelta(seconds=1)
MIN = timedelta(minutes=1)


def T(day, h, m=0, s=0.0):
    return datetime(2026, 10, day, h, m, tzinfo=UTC) + timedelta(seconds=s)


def p(t, name, value):
    """One `Data` entry of the history export."""
    return {"key": f"k-{name}", "dataFieldName": name, "value": str(value),
            "timestampUtc": t.isoformat().replace("+00:00", "Z")}


def burst(t, odo, soc, plug="disconnected"):
    """What the car reports within seconds when it wakes up or switches off."""
    return [p(t, "mileage", odo), p(t + S, "currentSOCInPct", soc),
            p(t + 3 * S, "plugConnectionState", plug),
            p(t + 4 * S, "chargingState", "NOT_READY_FOR_CHARGING"),
            p(t + 4 * S, "chargePowerInKW", "0.0"),
            p(t + 4 * S, "chargeType", "INVALID")]


def drive(start, odo, km, soc_start, soc_end, first_km_after=75 * S,
          every=90 * S, switch_off=True):
    """A drive as the export has it: wake-up burst, one odometer point per
    kilometre, the charge level now and then, switch-off bursts. Returns the
    points and the time of the last kilometre."""
    points = burst(start, odo, soc_start)
    last = start
    for i in range(1, km + 1):
        last = start + first_km_after + (i - 1) * every
        points.append(p(last, "mileage", odo + i))
        if i % 2 == 0:
            soc = round(soc_start + (soc_end - soc_start) * i / km)
            points.append(p(last + 10 * S, "currentSOCInPct", soc))
    if switch_off:
        points += burst(last + 53 * S, odo + km, soc_end)
        points += burst(last + 85 * S, odo + km, soc_end)
    return points, last


def charging(start, minutes, kw, soc_start, kind="AC", spelling="upper",
             every=60 * S, kwh_per_pct=0.726, soc_from=None):
    """Power reports every `every`, the charge level rising with them, and a
    zero report half a minute after the last one."""
    state = "CHARGING" if spelling == "upper" else "charging"
    kind = kind if spelling == "upper" else kind.lower()
    points = [p(start - 30 * S, "plugConnectionState", "connected")]
    steps = int(timedelta(minutes=minutes) / every)
    base = soc_start if soc_from is None else soc_from
    for i in range(steps + 1):
        t = start + i * every
        kwh = kw * (i * every).total_seconds() / 3600
        points += [p(t, "chargingState", state), p(t, "chargePowerInKW", kw),
                   p(t, "chargeType", kind),
                   p(t, "currentSOCInPct", round(base + kwh / kwh_per_pct))]
    end = start + steps * every + 30 * S
    points += [p(end, "chargePowerInKW", "0.0"),
               p(end, "chargingState", "NOT_READY_FOR_CHARGING" if spelling == "upper"
                 else "notReadyForCharging")]
    return points, end


def obs(points):
    return observations.from_history(points)


def close(a, b, tolerance):
    return a is not None and b is not None and abs(a - b) <= tolerance


# ---------------------------------------------------------------------------
# Synthetic cases. Each returns (ok, claim, measured); `part_detection`
# prints them, the mutation part runs them again with a threshold changed.
# ---------------------------------------------------------------------------

def case_session_36():
    points, _ = drive(T(16, 15, 44, 21), 62020, 6, 74, 73,
                      every=timedelta(seconds=104.8))
    found = sessions.detect_drives(obs(points))
    d = found[0] if len(found) == 1 else None
    return [(d is not None and d.started_at == T(16, 15, 44, 21)
             and d.ended_at == T(16, 15, 55, 49),
             "Fahrt wie Sitzung 36: Start mit dem Aufwach-Burst, Ende mit dem "
             "letzten Abschalt-Burst",
             str(found and (found[0].started_at, found[0].ended_at))),
            (d is not None and (d.odo_start, d.odo_end, d.soc_start, d.soc_end)
             == (62020, 62026, 74, 73),
             "Kilometerstand 62020 -> 62026, Ladestand 74 -> 73",
             str(d and (d.odo_start, d.odo_end, d.soc_start, d.soc_end)))]


def case_stops():
    a, last = drive(T(5, 7), 1000, 10, 80, 77)
    b, _ = drive(last + 7 * MIN, 1010, 10, 77, 74)
    seven = sessions.detect_drives(obs(a + b))
    a, last = drive(T(5, 7), 1000, 10, 80, 77)
    b, _ = drive(last + 15 * MIN, 1010, 10, 77, 74)
    fifteen = sessions.detect_drives(obs(a + b))
    return [(len(seven) == 1 and seven[0].odo_end - seven[0].odo_start == 20,
             "7 Minuten Halt mit Abschalten: eine Fahrt über 20 km",
             str([(d.odo_start, d.odo_end) for d in seven])),
            (len(fifteen) == 2 and fifteen[1].started_at == last + 15 * MIN,
             "15 Minuten Halt: zwei Fahrten, die zweite beginnt mit ihrem "
             "Aufwach-Burst",
             str([(d.odo_start, d.odo_end, d.started_at) for d in fifteen]))]


def case_into_dc():
    a, last = drive(T(6, 10), 2000, 10, 30, 25, switch_off=False)
    plug = last + 40 * S
    c, end = charging(last + 70 * S, 5, 100.0, 25, kind="DC", every=20 * S)
    c[0] = p(plug, "plugConnectionState", "connected")
    b, _ = drive(end + 30 * S, 2010, 5, 33, 32)
    found = sessions.detect_drives(obs(a + c + b))
    charges = sessions.detect_charges(obs(a + c + b))
    ch = charges[0] if len(charges) == 1 else None
    return [(len(found) == 2 and found[0].ended_at <= plug
             and found[1].started_at >= end,
             "Fahrt direkt an den Schnelllader: Laden trennt die Fahrten, auch "
             "nach weniger als 10 Minuten",
             str([(d.started_at, d.ended_at) for d in found])),
            (ch is not None and ch.charge_type == "DC" and ch.max_power_kw == 100
             and close(ch.energy_kwh, 100 * (5 / 60 + 15 / 3600), 0.1)
             and (ch.soc_start, ch.odometer_km) == (25, 2010),
             "DC-Ladung: Typ, Spitzenleistung, Energie als Integral, "
             "Kilometerstand",
             str(ch))]


def case_overnight():
    first, _ = charging(T(7, 22), 60, 11.0, 10)
    later, end = charging(T(8, 1), 120, 11.0, 0, soc_from=10 + 33 / 0.726)
    points = first[:-2] + later[1:]
    with_capacity = sessions.detect_charges(obs(points), kwh_per_pct=0.726)
    without = sessions.detect_charges(obs(points))
    expected_full = 11 * 5 + 11 * 15 / 3600
    stalled, _ = charging(T(9, 10), 30, 11.0, 60)
    resumed, _ = charging(T(9, 12, 30), 30, 11.0, 60, soc_from=60 + 5.5 / 0.726)
    flat = sessions.detect_charges(obs(stalled[:-2] + resumed[1:]), kwh_per_pct=0.726)
    capped = 11 * 0.5 * 2 + 11 * 10 / 60
    return [(len(with_capacity) == 1
             and close(with_capacity[0].energy_kwh, expected_full, 1.0),
             "AC über Nacht mit 2 h Datenlücke: die Lücke zählt, was der "
             "gestiegene Ladestand hergibt",
             str(with_capacity and with_capacity[0].energy_kwh)),
            (len(without) == 1
             and close(without[0].energy_kwh, expected_full - 22 + 11 / 6, 0.5),
             "ohne bekannte Kapazität zählt die Lücke höchstens 10 Minuten",
             str(without and without[0].energy_kwh)),
            (len(flat) == 1 and close(flat[0].energy_kwh, capped, 0.5),
             "Lücke ohne Anstieg des Ladestands: höchstens 10 Minuten",
             str(flat and flat[0].energy_kwh))]


def case_pauses_and_spellings():
    a, end = charging(T(10, 14), 30, 7.0, 50)
    b, _ = charging(end + 8 * MIN, 20, 7.0, 0, spelling="lower",
                    soc_from=50 + 3.5 / 0.726)
    short = sessions.detect_charges(obs(a + b))
    a, end = charging(T(11, 14), 30, 7.0, 50)
    b, _ = charging(end + 20 * MIN, 20, 7.0, 0, spelling="lower",
                    soc_from=50 + 3.5 / 0.726)
    long = sessions.detect_charges(obs(a + b))
    blip = [p(T(12, 16), "chargePowerInKW", 1.0), p(T(12, 16, 0, 30), "chargePowerInKW", 1.0),
            p(T(12, 16, 1), "chargePowerInKW", 0.0), p(T(12, 15), "currentSOCInPct", 70),
            p(T(12, 16, 2), "currentSOCInPct", 70)]
    noise = sessions.detect_charges(obs(blip))
    return [(len(short) == 1, "8 Minuten Pause: ein Ladevorgang", str(len(short))),
            (len(long) == 2 and [c.charge_type for c in long] == ["AC", "AC"],
             "20 Minuten Pause: zwei; AC in beiden Schreibweisen",
             str([(c.started_at, c.charge_type) for c in long])),
            (noise == [], "1 kW für eine halbe Minute ohne Anstieg ist Rauschen",
             str(noise))]


def case_manoeuvre():
    t = T(13, 9)
    small = [observations.Observation(t, "odometer_km", 100.0),
             observations.Observation(t + MIN, "odometer_km", 100.6)]
    real = small + [observations.Observation(t + 2 * MIN, "odometer_km", 101.2)]
    return [(sessions.detect_drives(small) == [],
             "0,6 km sind Rangieren, keine Fahrt", ""),
            (len(sessions.detect_drives(real)) == 1, "1,2 km sind eine Fahrt", "")]


def portal_rows():
    """The portal's reports around one short drive, as jolt stores them:
    `values` is the merged state, `raw` what the report itself carried."""
    def row(t, raw, odo, soc):
        return SimpleNamespace(
            captured_at=t.replace(tzinfo=None),
            values={"odometer_km": odo, "soc_pct": soc, "charge_power_kw": 0,
                    "charge_state": "CHARGE_STATE_NOT_READY_FOR_CHARGING"},
            raw={k: [name, value, None] for k, (name, value) in raw.items()})

    state = ("charging_state_report.current_charge_state",
             "CHARGE_STATE_NOT_READY_FOR_CHARGING")
    return [
        row(T(20, 4, 39, 33), {"a": ("mileage.value", "62067"),
                               "b": ("battery_level_HV.value", "56.0"), "c": state},
            62067, 56),
        row(T(20, 5, 44, 24), {"c": state, "d": ("report_type",
                                                 "REPORT_TYPE_CONFIGURATIONS")},
            62067, 56),
        row(T(20, 5, 59, 42), {"a": ("mileage.value", "62071"),
                               "e": ("battery_state_report.soc", "53"),
                               "f": ("update_reason", "UPDATE_REASON_CLAMP15_OFF")},
            62067, 53),
        row(T(20, 6, 0, 24), {"a": ("mileage.value", "62071"),
                              "g": ("mileage.value", "62074"),
                              "b": ("battery_level_HV.value", "53.0")},
            62074, 53),
        row(T(20, 6, 0, 41), {"a": ("mileage.value", "62071"),
                              "g": ("mileage.value", "62074"),
                              "b": ("battery_level_HV.value", "53.0")},
            62074, 53),
    ]


def case_portal():
    rows = portal_rows()
    found = sessions.detect_drives(observations.from_snapshots(rows), dense=False)
    d = found[0] if len(found) == 1 else None
    shuffled = rows[:]
    random.Random(7).shuffle(shuffled)
    again = sessions.detect_drives(observations.from_snapshots(shuffled), dense=False)
    return [(d is not None and d.started_at == T(20, 5, 44, 24)
             and d.ended_at == T(20, 6, 0, 41),
             "Portal: Start mit dem Bericht beim Einschalten (ohne Kilometerstand), "
             "Ende mit dem letzten Abschaltbericht",
             str(d and (d.started_at, d.ended_at))),
            (d is not None and (d.odo_start, d.odo_end, d.soc_start, d.soc_end)
             == (62067, 62074, 56, 53),
             "Portal: der erste Abschaltbericht mit altem Kilometerstand stört nicht, "
             "Ende 62074; Ladestand 56 -> 53",
             str(d and (d.odo_start, d.odo_end, d.soc_start, d.soc_end))),
            (again == found, "Portal: Berichte in anderer Reihenfolge, dieselbe Fahrt",
             str(again))]


CASES = [case_session_36, case_stops, case_into_dc, case_overnight,
         case_pauses_and_spellings, case_manoeuvre, case_portal]


def run_cases() -> list:
    return [result for case in CASES for result in case()]


def part_detection():
    verify.section("Fahrten und Ladevorgänge erkennen")
    for ok, claim, measured in run_cases():
        verify(ok, claim, measured)
    spellings = ["CHARGING", "charging", "NOT_READY_FOR_CHARGING", "notReadyForCharging",
                 "readyForCharging", "ERROR", "error",
                 "CHARGE_PURPOSE_REACHED_AND_NOT_CONVERSATION_CHARGING",
                 "chargePurposeReachedAndNotConservationCharging",
                 "CHARGE_PURPOSE_REACHED_AND_CONVERSATION",
                 "chargePurposeReachedAndConservation",
                 "CHARGE_STATE_NOT_READY_FOR_CHARGING"]
    verify([observations.charge_state(s) for s in spellings]
           == ["charging", "charging", "not_ready", "not_ready", "ready", "error",
               "error", "purpose_reached", "purpose_reached", "conservation",
               "conservation", "not_ready"],
           "beide Schreibweisen des Ladezustands (und die des Portals) ergeben "
           "denselben Wert")
    broken = [p(T(1, 1), "mileage", 5), {"dataFieldName": "mileage", "value": "6",
                                          "timestampUtc": "0001-01-01T00:00:00.000Z"},
              {"dataFieldName": "mileage", "value": "7", "timestampUtc": "N/A"},
              {"dataFieldName": "mileage", "value": "8"},
              p(T(1, 2), "chargeType", "INVALID"), p(T(1, 2), "unknownField", 1)]
    verify(len(obs(broken)) == 1,
           "Punkte ohne gültige Zeit, INVALID und unbekannte Felder fallen weg",
           str(obs(broken)))


MUTATIONS = [("STOP_SPLIT", timedelta(minutes=5)), ("STOP_SPLIT", timedelta(minutes=20)),
             ("BURST", timedelta(seconds=10)), ("MIN_DRIVE_KM", 0.5),
             ("CHARGE_PAUSE", timedelta(minutes=5)),
             ("CHARGE_PAUSE", timedelta(minutes=30)),
             ("POWER_GAP_CAP", timedelta(hours=3)), ("MIN_CHARGE_KWH", 0.001)]


def part_mutations():
    verify.section("Jede Schwelle ist geprüft")
    for name, value in MUTATIONS:
        original = getattr(sessions, name)
        setattr(sessions, name, value)
        try:
            failed = [claim for ok, claim, _ in run_cases() if not ok]
        except Exception as failure:      # noqa: BLE001
            failed = [repr(failure)]
        finally:
            setattr(sessions, name, original)
        verify(failed, f"{name} = {value} fällt auf", "kein Fall schlägt fehl")


COLUMNS = ("started_at", "ended_at", "odo_start", "odo_end", "distance_km",
           "soc_start", "soc_end", "energy_used_kwh", "sources", "live_session_id")


def columns(row) -> dict:
    return {c: getattr(row, c) for c in COLUMNS} | {"details": row.details}


def new_vehicle(db) -> int:
    from app import models
    vehicle = models.Vehicle(name="Prüfauto", battery_gross_kwh=77.0,
                             battery_net_kwh=72.6)
    db.add(vehicle)
    db.flush()
    return vehicle.id


def drives_of(db, vehicle_id):
    from app import models
    return (db.query(models.VehicleDrive).filter_by(vehicle_id=vehicle_id)
            .order_by(models.VehicleDrive.started_at).all())


def history_records():
    from app.cloud import logbook
    a, last = drive(T(5, 7), 1000, 10, 80, 77)
    b, _ = drive(last + 15 * MIN, 1010, 10, 77, 74)
    c, _ = drive(T(16, 15, 44, 21), 62020, 6, 74, 73, every=timedelta(seconds=104.8))
    charge, _ = charging(T(17, 22), 60, 11.0, 30)
    # Two streams: the odometer of the second car-week does not continue
    # the first, and one stream would read the jump as a drive.
    found = sessions.detect_drives(obs(a + b)) + sessions.detect_drives(obs(c))
    return ([logbook.drive_record(d) for d in found],
            [logbook.charge_record(x)
             for x in sessions.detect_charges(obs(charge), 0.726)])


def part_storage():
    verify.section("Speichern: einmal, egal wie oft und in welcher Reihenfolge")
    from app import models
    from app.cloud import logbook
    from app.database import SessionLocal, migrate
    migrate()
    db = SessionLocal()
    drives, charges = history_records()
    try:
        vid = new_vehicle(db)
        first = logbook.store_drives(db, vid, logbook.HISTORY, drives)
        again = logbook.store_drives(db, vid, logbook.HISTORY, drives)
        verify(first == {"new": 3, "updated": 0, "unchanged": 0}
               and again == {"new": 0, "updated": 0, "unchanged": 3}
               and len(drives_of(db, vid)) == 3,
               "derselbe Export zweimal: drei Fahrten, beim zweiten Mal unverändert",
               f"{first} {again}")
        first = logbook.store_charges(db, vid, logbook.HISTORY, charges)
        again = logbook.store_charges(db, vid, logbook.HISTORY, charges)
        verify(first["new"] == 1 and again == {"new": 0, "updated": 0, "unchanged": 1}
               and db.query(models.VehicleCharge).filter_by(vehicle_id=vid).count() == 1,
               "Ladevorgänge ebenso", f"{first} {again}")

        history = drives[2]
        portal = dict(history, started_at=_iso(T(16, 15, 44, 30)),
                      ended_at=_iso(T(16, 15, 56, 10)), soc_end=72,
                      energy_used_kwh=1.2, km_track=[], soc_track=[])
        logbook.store_drives(db, vid, logbook.PORTAL, [portal])
        rows = drives_of(db, vid)
        merged = rows[-1]
        verify(len(rows) == 3 and merged.sources == ["vw_history", "vw_portal"]
               and merged.started_at == T(16, 15, 44, 21).replace(tzinfo=None)
               and merged.soc_end == 73 and merged.energy_used_kwh == 1.2,
               "dieselbe Fahrt aus dem Portal: eine Zeile; Zeiten und Ladestand aus "
               "dem Export, Energie aus dem Portal",
               str(columns(merged)))

        other = new_vehicle(db)
        logbook.store_drives(db, other, logbook.PORTAL, [portal])
        logbook.store_drives(db, other, logbook.HISTORY, [history])
        verify(columns(drives_of(db, other)[0]) == columns(merged),
               "umgekehrte Reihenfolge (erst Portal, dann Export): dieselbe Zeile")

        whole = dict(portal, odo_start=1000.0, odo_end=1010.0, distance_km=10.0,
                     started_at=_iso(T(18, 8)), ended_at=_iso(T(18, 8, 20)))
        halves = [dict(whole, odo_end=1005.0, distance_km=5.0,
                       ended_at=_iso(T(18, 8, 10)), energy_used_kwh=0.6),
                  dict(whole, odo_start=1005.0, distance_km=5.0,
                       started_at=_iso(T(18, 8, 10, 30)), energy_used_kwh=0.7)]
        split = new_vehicle(db)
        logbook.store_drives(db, split, logbook.PORTAL, halves)
        logbook.store_drives(db, split, logbook.HISTORY, [dict(whole, energy_used_kwh=None)])
        logbook.store_drives(db, split, logbook.PORTAL, halves)
        rows = drives_of(db, split)
        verify(len(rows) == 1 and rows[0].distance_km == 10
               and rows[0].energy_used_kwh == 1.3,
               "zwei Portal-Hälften und eine Export-Fahrt: eine Zeile, die Energie der "
               "Hälften zusammen; erneutes Speichern ändert daran nichts",
               str([columns(r) for r in rows]))

        partial = dict(portal, odo_start=62067.0, odo_end=62071.0, distance_km=4.0,
                       started_at=_iso(T(20, 5, 44, 24)), ended_at=_iso(T(20, 5, 59, 42)))
        full = dict(partial, odo_end=62074.0, distance_km=7.0,
                    ended_at=_iso(T(20, 6, 0, 41)))
        late, direct = new_vehicle(db), new_vehicle(db)
        logbook.store_drives(db, late, logbook.PORTAL, [partial])
        logbook.store_drives(db, late, logbook.PORTAL, [full])
        logbook.store_drives(db, direct, logbook.PORTAL, [full])
        verify([columns(r) for r in drives_of(db, late)]
               == [columns(r) for r in drives_of(db, direct)],
               "eine spätere, vollständigere Erkennung ersetzt die frühere")
        db.commit()
    finally:
        db.close()


def _iso(t):
    return t.astimezone(UTC).replace(tzinfo=None).isoformat()


def recorded(db, vehicle_id, start, odo, km, soc, recording=True,
             odometer=True, running=False):
    """A live session as the app and the dongle leave it: a point every 30 s,
    about 1 km per 1.5 minutes, energy counters rising 0.18 kWh per km."""
    from app import models
    trip = models.Trip(vehicle_id=vehicle_id, recording=recording,
                       start_lat=48.1, start_lon=11.5, target_lat=48.1,
                       target_lon=11.5, start_soc=soc, geometry=[],
                       energy_profile=[])
    db.add(trip)
    db.flush()
    session = models.LiveSession(trip_id=trip.id, running=running,
                                 started_at=start.replace(tzinfo=None))
    db.add(session)
    db.flush()
    for i in range(km * 3 + 1):
        done = i / 3
        raw = {"discharge_kwh": 9000 + 0.18 * done, "charged_kwh": 4000.0}
        if odometer:
            raw["odometer_km"] = odo + int(done)
        db.add(models.LivePoint(
            session_id=session.id, lat=48.1 + 0.009 * done, lon=11.5,
            timestamp=(start + i * 30 * S).replace(tzinfo=None),
            soc=round(soc - done * 0.25, 1), raw_values=raw))
    db.flush()
    db.refresh(session)
    return session


def part_jolt():
    verify.section("jolt-Aufzeichnungen im Fahrtenbuch")
    import logging
    from app import models
    from app.cloud import logbook
    from app.database import SessionLocal
    from app.live import cleanup
    drives, _ = history_records()
    db = SessionLocal()
    try:
        vid = new_vehicle(db)
        session = recorded(db, vid, T(16, 15, 44, 19), 62020, 6, 73.9,
                           recording=False, running=True)
        session.running = False
        cleanup.end_and_learn(db, session)
        rows = drives_of(db, vid)
        verify(len(rows) == 1 and rows[0].sources == ["jolt"]
               and rows[0].live_session_id == session.id
               and (rows[0].odo_start, rows[0].odo_end) == (62020, 62026)
               and rows[0].energy_used_kwh == 1.08,
               "eine beendete Fahrt steht sofort im Fahrtenbuch, auch ohne VW-Daten",
               str(rows and columns(rows[0])))
        logbook.store_drives(db, vid, logbook.HISTORY, [drives[2]])
        rows = drives_of(db, vid)
        jolt_first = columns(rows[0])
        verify(len(rows) == 1 and rows[0].sources == ["vw_history", "jolt"]
               and rows[0].started_at == T(16, 15, 44, 21).replace(tzinfo=None)
               and rows[0].live_session_id == session.id
               and rows[0].energy_used_kwh == 1.08,
               "danach der VW-Export: eine Zeile, Zeiten aus VW, Energie aus jolt, "
               "Verweis auf die Aufzeichnung", str(rows and columns(rows[0])))
        for row in rows:
            db.delete(row)
        db.flush()
        logbook.store_drives(db, vid, logbook.HISTORY, [drives[2]])
        logbook.store_session(db, session)
        verify([columns(r) for r in drives_of(db, vid)] == [jolt_first],
               "umgekehrte Reihenfolge: dieselbe Zeile")
        again = logbook.store_all_sessions(db, vid)
        verify(again == {"new": 0, "updated": 0, "unchanged": 1},
               "alle Aufzeichnungen noch einmal: nichts ändert sich", str(again))

        two = new_vehicle(db)
        logbook.store_drives(db, two, logbook.HISTORY, drives[:2])
        long = recorded(db, two, T(5, 6, 59), 1000, 20, 80)
        logbook.store_session(db, long)
        rows = drives_of(db, two)
        verify(len(rows) == 2 and all(r.live_session_id == long.id for r in rows)
               and all(r.energy_used_kwh is None for r in rows),
               "eine Aufzeichnung über zwei VW-Fahrten: beide verweisen auf sie, keine "
               "wird verklebt, keine bekommt die Energie der ganzen Aufzeichnung",
               str([columns(r) for r in rows]))

        three = new_vehicle(db)
        planned = recorded(db, three, T(14, 9), 0, 5, 60, recording=False,
                           odometer=False)
        phone = recorded(db, three, T(14, 12), 0, 5, 60, odometer=False)
        logbook.store_session(db, planned)
        logbook.store_session(db, phone)
        rows = drives_of(db, three)
        verify(len(rows) == 1 and rows[0].live_session_id == phone.id
               and rows[0].odo_start is None and close(rows[0].distance_km, 5.0, 0.1),
               "ohne Kilometerstand: eine Aufzeichnung zählt mit der GPS-Strecke, "
               "eine geplante Fahrt (der Simulator) gar nicht",
               str([columns(r) for r in rows]))

        broken = recorded(db, three, T(15, 9), 500, 3, 60, running=True)
        broken.running = False
        lines = []
        handler = logging.Handler()
        handler.emit = lambda record: lines.append(record.getMessage())
        logger = logging.getLogger("uvicorn.error")
        logger.addHandler(handler)
        original = logbook.store_session
        logbook.store_session = lambda db, s: 1 / 0
        try:
            result = cleanup.end_and_learn(db, broken)
            ended = True
        except Exception:      # noqa: BLE001
            ended = False
        finally:
            logbook.store_session = original
            logger.removeHandler(handler)
        verify(ended and "recording" in result and not broken.running
               and any("Logbook" in line for line in lines),
               "ein Fehler im Fahrtenbuch hält das Beenden nicht auf", str(lines))
        db.commit()
    finally:
        db.close()


def part_portal():
    verify.section("Portalberichte im Fahrtenbuch")
    import logging
    from app import cloud, models
    from app.cloud import logbook, poller
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        vid = new_vehicle(db)
        session = recorded(db, vid, T(20, 5, 44, 16), 62067, 7, 55.7)
        logbook.store_session(db, session)
        for row in portal_rows():
            db.add(models.VehicleSnapshot(vehicle_id=vid, source="vw_portal",
                                          captured_at=row.captured_at,
                                          received_at=row.captured_at,
                                          values=row.values, raw=row.raw))
        db.commit()
        early = logbook.store_from_snapshots(db, vid, now=datetime(2026, 10, 20, 6, 10))
        verify(early["drives"]["new"] + early["drives"]["updated"] == 0
               and drives_of(db, vid)[0].sources == ["jolt"],
               "zehn Minuten nach dem Abschalten wird noch nichts gespeichert: "
               "Abschaltberichte kommen spät", str(early))
        later = logbook.store_from_snapshots(db, vid, now=datetime(2026, 10, 20, 7, 0))
        rows = drives_of(db, vid)
        verify(len(rows) == 1 and rows[0].sources == ["vw_portal", "jolt"]
               and rows[0].started_at == datetime(2026, 10, 20, 5, 44, 24)
               and rows[0].ended_at == datetime(2026, 10, 20, 6, 0, 41)
               and rows[0].live_session_id == session.id,
               "nach einer halben Stunde: die Portal-Fahrt, mit der Aufzeichnung "
               "zusammengeführt", f"{later} {[columns(r) for r in rows]}")
        again = logbook.store_from_snapshots(db, vid, now=datetime(2026, 10, 20, 7, 15))
        verify(again["drives"] == {"new": 0, "updated": 0, "unchanged": 1},
               "die nächste Runde ändert nichts", str(again))
    finally:
        db.close()

    lines = []
    handler = logging.Handler()
    handler.emit = lambda record: lines.append(record.getMessage())
    logger = logging.getLogger("uvicorn.error")
    logger.addHandler(handler)
    original = logbook.store_from_snapshots
    logbook.store_from_snapshots = lambda db, vehicle_id: 1 / 0
    config = cloud.Config(name="vw_portal", vin=None, vehicle=str(vid),
                          interval_min=15, connect=lambda: None)
    poll = poller.Poll(config, SessionLocal)
    poll.status.vehicle_id = vid
    db = SessionLocal()
    try:
        poll.update_logbook(db)
        poll.update_logbook(db)
        survived = True
    except Exception:      # noqa: BLE001
        survived = False
    finally:
        db.close()
        logbook.store_from_snapshots = original
        logger.removeHandler(handler)
    verify(survived and len([x for x in lines if "logbook" in x]) == 1
           and poll.status.failures == 0,
           "ein Fehler im Fahrtenbuch stört den Abruf nicht und steht einmal im Log",
           str(lines))


def part_import():
    verify.section("Import des Verlaufs")
    import json
    import subprocess
    import tempfile
    import zipfile
    from app.database import SessionLocal
    a, last = drive(T(5, 7), 1000, 10, 80, 77)
    b, _ = drive(last + 15 * MIN, 1010, 10, 77, 74)
    c, _ = charging(T(6, 22), 60, 11.0, 30)
    document = {"vin": "WVWZZZSYNTHETIC01", "userId": "synthetic",
                "Data": a + b + c}
    folder = tempfile.mkdtemp(prefix="jolt-vw-export-")
    path = os.path.join(folder, "export.zip")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("WVWZZZSYNTHETIC01_export.json",
                         json.dumps(document, indent=1))
    db = SessionLocal()
    vid = new_vehicle(db)
    db.commit()
    db.close()

    def run(*extra):
        done = subprocess.run(
            [sys.executable, os.path.join(TOOLS, "import_vw_history.py"), path,
             "--vehicle", str(vid), *extra],
            capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1"})
        return done.returncode, done.stdout + done.stderr

    def count():
        db = SessionLocal()
        try:
            return len(drives_of(db, vid))
        finally:
            db.close()

    code, dry = run("--dry-run")
    verify(code == 0 and "Fahrten:       2, 20 km" in dry and count() == 0,
           "Probelauf aus der ZIP-Datei: zwei Fahrten erkannt, nichts gespeichert", dry)
    code, first = run()
    code2, second = run()
    verify(code == 0 and "(2 neu," in first and code2 == 0
           and "(0 neu, 0 aktualisiert, 2 unverändert)" in second and count() == 2,
           "zweimal importiert: beim zweiten Mal unverändert", second)
    verify("SYNTHETIC01" not in first and "…IC01" in first,
           "die Ausgabe zeigt die VIN nur gekürzt", first)


def part_api():
    verify.section("API")
    from fastapi.testclient import TestClient
    from app.cloud import logbook
    from app.database import SessionLocal
    from app.main import app
    drives, charges = history_records()
    db = SessionLocal()
    try:
        vid = new_vehicle(db)
        logbook.store_drives(db, vid, logbook.HISTORY, drives)
        logbook.store_charges(db, vid, logbook.HISTORY, charges)
        session = recorded(db, vid, T(16, 15, 44, 19), 62020, 6, 73.9)
        logbook.store_session(db, session)
        trip_id = session.trip_id
        db.commit()
    finally:
        db.close()
    with TestClient(app) as client:
        first = client.get(f"/api/vehicles/{vid}/drives?limit=2").json()
        rest = client.get(f"/api/vehicles/{vid}/drives",
                          params={"limit": 2, "before": first["next_before"]}).json()
        load = client.get(f"/api/vehicles/{vid}/charges").json()
        missing = client.get("/api/vehicles/99999/drives").status_code
    items = first["items"] + rest["items"]
    newest = items[0]
    verify(len(items) == 3 and rest["next_before"] is None
           and [i["started_at"] for i in items]
           == sorted([i["started_at"] for i in items], reverse=True),
           "Fahrten, neueste zuerst, seitenweise über `before`",
           str([i["started_at"] for i in items]))
    verify(newest["sources"] == ["vw_history", "jolt"] and newest["trip_id"] == trip_id
           and newest["kwh_per_100km"] == 18.0 and newest["started_at"].endswith("Z"),
           "eine Fahrt aus VW und jolt: Verweis auf die Aufzeichnung, Verbrauch, "
           "Zeiten in UTC mit Z", str(newest))
    verify(first["totals"] == {"count": 3, "distance_km": 26.0, "energy_used_kwh": 1.1,
                               "distance_km_with_energy": 6.0},
           "Summen über alle Fahrten", str(first["totals"]))
    verify(load["totals"]["count"] == 1 and load["items"][0]["charge_type"] == "AC"
           and load["totals"]["by_type"]["AC"]["count"] == 1,
           "Ladevorgänge mit Summen nach AC/DC", str(load["totals"]))
    verify(missing == 404, "unbekanntes Fahrzeug: 404", str(missing))


PARTS = [part_detection, part_mutations, part_storage, part_jolt, part_portal,
         part_import, part_api]


def main() -> int:
    for part in PARTS:
        part()
    return verify.balance(
        "Nicht geprüft: ein echter Export und echte Portalberichte - dafür "
        "tools/import_vw_history.py --dry-run mit dem eigenen Export.")


if __name__ == "__main__":
    sys.exit(main())
