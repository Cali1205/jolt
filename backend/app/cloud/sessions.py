"""Drives and charges, found in a stream of observations.

The input is a time-ordered list of `observations.Observation`, from the VW
history export, from the portal's live reports or from jolt's own dongle.
The output is a list of `Drive` and a list of `Charge`. Pure: no database,
no clock.

How the car reports, and what follows from it
---------------------------------------------

The odometer is the backbone of a drive. In the history export the car
reports it once per kilometre while it moves, with the time it passed the
kilometre; while parked it does not change. So a drive is a run of odometer
increases. The portal's live reports are coarser: one report when the
ignition goes on (often without the odometer), and about three within a
minute when it goes off, the first of which may still carry a reading from
mid-drive. Taking the largest reading of a run as the end makes both shapes
give the same drive.

Where a drive splits. A red light or a five-minute stop at the bakery is part
of the drive; ten minutes without a new kilometre is not (`STOP_SPLIT`).
Charging or plugging in ends a drive at once, however short the stop: the
time at the charger belongs to the charge, not to the drive.

Where a drive starts. When the car wakes up it reports a burst of values
within a few seconds, the odometer usually among them. The start is the
earliest observation within `BURST` before the last thing the car said
before its first new kilometre, but never before the last time it still
reported the old odometer reading. A window and not a chain of reports: a
car being preheated reports every minute for half an hour while it stands
in the drive. When the car said nothing between parking and the first new
kilometre, as with the portal's reports, the last report before it is the
best lower bound there is.

A kilometre that arrives after ten minutes of silence in a source that
reports while the car moves (`dense`: the export, the dongle) was not driven
then: the car woke up and reported a reading it had not sent before going to
sleep. It is booked at the moment the car fell silent, which makes it part of
the drive that ended there, not the one that is just starting.

Where a drive ends. At the last new kilometre, extended to the switch-off
burst if it comes within `BURST` after it. A drive of less than one
kilometre (`MIN_DRIVE_KM`) is manoeuvring, not a drive.

Charges. A charge is a run of reports with charge power above zero. The
charge state alone is not trusted: in the export it says "charging" for
reports with zero power, and the power is what moves energy. A pause shorter
than `CHARGE_PAUSE` (the car waits for the grid, the wallbox balances load,
someone plugs in again after the charger aborted) stays inside the charge;
driving ends it. The plug state does not: the export reports "disconnected"
in the middle of a charge at 107 kW. The energy is the
integral of the power over time (trapezoids). A gap in the data counts for at
most `POWER_GAP_CAP`, so an hour without reports is not booked as an hour at
the last power. When the charge level rose across the gap, the car kept
charging (the export has such hours: 41 % before, 50 % after, 4 kW on both
sides); the gap then counts for what that rise holds, at most for the whole
gap at the mean power. A charge that moved less than 0.1 kWh and did not
raise the charge level by a point was noise.
"""
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from .observations import Observation

STOP_SPLIT = timedelta(minutes=10)
BURST = timedelta(minutes=5)
MIN_DRIVE_KM = 1.0
CHARGE_PAUSE = timedelta(minutes=15)
POWER_GAP_CAP = timedelta(minutes=10)
MIN_CHARGE_KWH = 0.1
MIN_CHARGE_SOC_RISE = 1.0
TRACK_POINTS = 300

Track = list[tuple[datetime, float]]


@dataclass(frozen=True)
class Drive:
    started_at: datetime
    ended_at: datetime
    odo_start: float
    odo_end: float
    soc_start: float | None
    soc_end: float | None
    energy_used_kwh: float | None
    km_track: Track
    soc_track: Track


@dataclass(frozen=True)
class Charge:
    started_at: datetime
    ended_at: datetime
    soc_start: float | None
    soc_end: float | None
    energy_kwh: float
    max_power_kw: float | None       # None from a source without power reports
    avg_power_kw: float | None
    charge_type: str | None
    odometer_km: float | None
    power_track: Track


def thin(track: Track, at_most: int = TRACK_POINTS) -> Track:
    """Every n-th point, keeping the first and the last."""
    if len(track) <= at_most:
        return list(track)
    step = (len(track) - 1) / (at_most - 1)
    return [track[round(i * step)] for i in range(at_most)]


def _ordered(obs) -> list[Observation]:
    return sorted(obs, key=lambda o: (o.t, o.field, str(o.value)))


class _Stream:
    """The ordered observations, with the lookups detection needs."""

    def __init__(self, obs):
        self.all = _ordered(obs)
        self.times = [o.t for o in self.all]
        self.by_field: dict[str, list[Observation]] = {}
        for o in self.all:
            self.by_field.setdefault(o.field, []).append(o)
        self.field_times = {name: [o.t for o in items]
                            for name, items in self.by_field.items()}

    def field(self, name: str) -> list[Observation]:
        return self.by_field.get(name, [])

    def last_at_or_before(self, name: str, t: datetime) -> Observation | None:
        i = bisect_right(self.field_times.get(name, []), t)
        return self.field(name)[i - 1] if i else None

    def last_before(self, name: str, t: datetime) -> Observation | None:
        i = bisect_left(self.field_times.get(name, []), t)
        return self.field(name)[i - 1] if i else None

    def between(self, name: str, start: datetime, end: datetime) -> list[Observation]:
        times = self.field_times.get(name, [])
        return self.field(name)[bisect_left(times, start):bisect_right(times, end)]


def _charging(o: Observation) -> bool:
    return o.field == "charge_power_kw" and o.value > 0


def _blocks_drive(o: Observation) -> bool:
    return _charging(o) or (o.field == "plugged" and o.value is True)


def dedup(track: Track) -> Track:
    out: Track = []
    for t, v in track:
        if not out or out[-1][1] != v:
            out.append((t, v))
    return out


# ---------------------------------------------------------------------------
# Drives
# ---------------------------------------------------------------------------

@dataclass
class _Step:
    t: datetime
    value: float
    before: float            # the odometer before this step
    before_seen: datetime    # when the car last reported `before`


def _steps(stream: _Stream, dense: bool) -> list[_Step]:
    steps = []
    top = seen = None
    for o in stream.field("odometer_km"):
        if top is None:
            top, seen = o.value, o.t
        elif o.value > top:
            t = o.t
            if dense:
                i = bisect_left(stream.times, o.t) - 1
                if i >= 0 and o.t - stream.times[i] >= STOP_SPLIT:
                    t = stream.times[i]
            steps.append(_Step(t, o.value, top, seen))
            top, seen = o.value, o.t
        elif o.value == top:
            seen = o.t
    return steps


def _runs(stream: _Stream, steps: list[_Step],
          stop_split: timedelta) -> list[list[_Step]]:
    blockers = [o.t for o in stream.all if _blocks_drive(o)]
    runs: list[list[_Step]] = []
    for step in steps:
        if runs:
            previous = runs[-1][-1]
            i = bisect_right(blockers, previous.t)
            blocked = i < len(blockers) and blockers[i] < step.t
            if step.t - previous.t < stop_split and not blocked:
                runs[-1].append(step)
                continue
        runs.append([step])
    return runs


def _drive_start(stream: _Stream, run: list[_Step]) -> datetime:
    first = run[0]
    i = bisect_left(stream.times, first.t) - 1
    if i < 0:
        return first.t
    last_word = stream.times[i]
    start = last_word
    while i > 0:
        o = stream.all[i - 1]
        if last_word - o.t > BURST or _charging(o):
            break
        i -= 1
        start = o.t
    return max(start, first.before_seen)


def _drive_end(stream: _Stream, run: list[_Step]) -> datetime:
    last = run[-1]
    end = last.t
    for i in range(bisect_right(stream.times, last.t), len(stream.all)):
        o = stream.all[i]
        if o.t - last.t > BURST or _blocks_drive(o):
            break
        if o.field == "odometer_km" and o.value > last.value:
            break
        end = o.t
    return end


def _energy_used(stream: _Stream, start: datetime, first_step: datetime,
                 end: datetime) -> float | None:
    """From the dongle's lifetime counter if there is one, else from the
    battery's energy content."""
    for name, sign in (("energy_net_kwh", 1), ("energy_kwh", -1)):
        before = (stream.last_before(name, first_step)
                  or next(iter(stream.between(name, start, end)), None))
        after = stream.last_at_or_before(name, end)
        if before is None or after is None or after.t <= before.t:
            continue
        used = sign * (after.value - before.value)
        if used >= 0:
            return round(used, 2)
    return None


def detect_drives(obs, dense: bool = True,
                  stop_split: timedelta | None = None) -> list[Drive]:
    """`dense` for sources that report while the car moves (the history
    export, the dongle); False for the portal's live reports. `stop_split`
    replaces `STOP_SPLIT` for a source that is sampled less often than that
    (Stellantis, asked every quarter hour): two samples a poll apart are
    one drive."""
    stream = _Stream(obs)
    drives = []
    for run in _runs(stream, _steps(stream, dense), stop_split or STOP_SPLIT):
        odo_start, odo_end = run[0].before, max(s.value for s in run)
        if odo_end - odo_start < MIN_DRIVE_KM:
            continue
        start = _drive_start(stream, run)
        if drives:
            start = max(start, drives[-1].ended_at)
        end = _drive_end(stream, run)
        # Strictly before: the report that brings the first new kilometre
        # may already carry the charge level at the end of the drive.
        soc_before = (stream.last_before("soc_pct", run[0].t)
                      or next(iter(stream.between("soc_pct", start, end)), None))
        soc_after = stream.last_at_or_before("soc_pct", end)
        km_track = [(start, odo_start)] + [(s.t, s.value) for s in run]
        soc_track = dedup([(o.t, o.value)
                            for o in stream.between("soc_pct", start, end)])
        drives.append(Drive(
            started_at=start, ended_at=end,
            odo_start=odo_start, odo_end=odo_end,
            soc_start=soc_before.value if soc_before else None,
            soc_end=soc_after.value if soc_after else None,
            energy_used_kwh=_energy_used(stream, start, run[0].t, end),
            km_track=thin(km_track), soc_track=thin(soc_track)))
    return drives


# ---------------------------------------------------------------------------
# Charges
# ---------------------------------------------------------------------------

def _soc_rise(stream: _Stream, t0: datetime, t1: datetime) -> float:
    before = stream.last_at_or_before("soc_pct", t0)
    after = stream.last_at_or_before("soc_pct", t1)
    if before is None or after is None:
        return 0.0
    return max(0.0, after.value - before.value)


def _integrate(stream: _Stream, samples: Track,
               kwh_per_pct: float | None) -> float:
    kwh = 0.0
    for (t0, p0), (t1, p1) in zip(samples, samples[1:]):
        hours = (t1 - t0).total_seconds() / 3600
        mean = (p0 + p1) / 2
        if t1 - t0 <= POWER_GAP_CAP:
            kwh += hours * mean
            continue
        capped = POWER_GAP_CAP.total_seconds() / 3600 * mean
        by_soc = _soc_rise(stream, t0, t1) * (kwh_per_pct or 0.0)
        kwh += max(capped, min(hours * mean, by_soc))
    return kwh


def _level(o: Observation, powered: bool) -> float | None:
    """How much a report says the car charges: its power, or, from a source
    that reports no power (Stellantis), 1 for the state "charging" and 0 for
    any other state. None for reports that say nothing about it."""
    if powered:
        return o.value if o.field == "charge_power_kw" else None
    if o.field == "charge_state":
        return 1.0 if o.value == "charging" else 0.0
    return None


def _charge_runs(stream: _Stream, powered: bool) -> list[list[Observation]]:
    """Charging reports grouped into charges: each group starts with a
    positive report and runs up to the zero report that ended it, if there
    was one."""
    runs: list[list[Observation]] = []
    current: list[Observation] | None = None
    last_positive: Observation | None = None
    odometer = None
    for o in stream.all:
        if o.field == "odometer_km":
            moved = odometer is not None and o.value > odometer
            odometer = o.value if odometer is None else max(odometer, o.value)
            if moved:
                current = last_positive = None
            continue
        level = _level(o, powered)
        if level is None:
            continue
        if level > 0:
            if current is None:
                if (last_positive is not None and runs
                        and o.t - last_positive.t < CHARGE_PAUSE):
                    current = runs[-1]
                else:
                    current = []
                    runs.append(current)
            current.append(o)
            last_positive = o
        elif current is not None:
            current.append(o)
            current = None
    return runs


def detect_charges(obs, kwh_per_pct: float | None = None,
                   powered: bool = True) -> list[Charge]:
    """`kwh_per_pct`, the battery's usable capacity over 100, lets a gap in
    the data count for what the charge level says went in.

    `powered` False is for a source that reports no charge power
    (Stellantis): it is read from its charge state instead. Its reports are a
    quarter of an hour apart, so the charge ends at the first report that no
    longer says "charging", the one that carries the final charge level; the
    energy is the charge level's rise, and there is no power track. VW's
    charge state is not read that way: it says "charging" at zero power."""
    stream = _Stream(obs)
    charges = []
    for run in _charge_runs(stream, powered):
        positive = [o for o in run if _level(o, powered) > 0]
        samples = [(o.t, float(_level(o, powered))) for o in run]
        if (powered and samples[-1][1] == 0
                and run[-1].t - positive[-1].t > POWER_GAP_CAP):
            samples.pop()
        start, end = samples[0][0], samples[-1][0]
        soc_before = (stream.last_at_or_before("soc_pct", start)
                      or next(iter(stream.between("soc_pct", start, end)), None))
        soc_after = stream.last_at_or_before("soc_pct", end + BURST)
        soc_start = soc_before.value if soc_before else None
        soc_end = soc_after.value if soc_after else None
        rise = (soc_end - soc_start) if soc_start is not None and soc_end is not None else 0
        if powered:
            energy = _integrate(stream, samples, kwh_per_pct)
        else:
            energy = max(0.0, rise) * (kwh_per_pct or 0.0)
            samples = []
        if energy < MIN_CHARGE_KWH and rise < MIN_CHARGE_SOC_RISE:
            continue
        kinds = Counter(o.value for o in stream.between("charge_type", start, end))
        hours = (end - start).total_seconds() / 3600
        odometer = stream.last_at_or_before("odometer_km", end)
        charges.append(Charge(
            started_at=start, ended_at=end,
            soc_start=soc_start, soc_end=soc_end,
            energy_kwh=round(energy, 2),
            max_power_kw=max((p for _, p in samples), default=None),
            avg_power_kw=round(energy / hours, 2) if hours > 0 else None,
            charge_type=kinds.most_common(1)[0][0] if kinds else None,
            odometer_km=odometer.value if odometer else None,
            power_track=thin(samples)))
    return charges
