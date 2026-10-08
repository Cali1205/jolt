"""Split a series of measurements into driving and charging sections.

**Why this module exists.** The question "did the car charge between these
two measurement points?" was answered independently in five places - with
three constants in three modules, two different thresholds (0.5 and 1.0
percentage points) and two different forms (pairwise against the predecessor,
or against the minimum of a time window). None of the five knew about the
others.

That was not a matter of taste but the source of several bugs. A charging
stop is a special case for almost every evaluation of a measurement series,
and anyone who does not know about it silently calculates wrong:

* The calibration took start minus end. Percentage points recharged were
  missing from that difference - a trip at 29 kWh/100 km turned into a
  learned value of 12, and because it stayed within the plausibility limits,
  nobody noticed.
* The deviation compared the charge level against a profile that knows no
  charging stops, and then reported three-digit percentage points.

Both were fixed by writing the detection **once more** in each place. The
next module to evaluate measurement points would have gone the same way.
That is why it now lives in one place, here.

**Why in `energy` and not in `live`.** `energy.calibration` is one of the
users, and `energy` sits below `live` - the other way round would create a
cycle. Content-wise it fits: it is about interpreting a measurement series,
just like the calibration next to it.

**What a measurement point must be.** Nothing more than an object with `soc`,
`timestamp` and `km_on_route`. Deliberately not an ORM type: `energy` does not
know the database, and that should stay so - only for that reason can the
check scripts run without the application.
"""
from dataclasses import dataclass
from datetime import timedelta

# From what rise onwards a section counts as charging. Below that it is
# regeneration or the noise of the SoC measurement (usually 0.5 % resolution),
# and both belong to driving. **The one threshold**, previously three.
CHARGING_PP = 0.5


@dataclass(frozen=True)
class Section:
    """The stretch between two consecutive measurement points.

    `soc_pp` is positive when energy was consumed and negative when charging -
    i.e. in the direction of "what did it cost", not "how did the charge level
    change". That is the viewpoint of all users.
    """
    begin: object
    past: object
    charges: bool
    soc_pp: float
    km: float

    @property
    def charged_pp(self) -> float:
        """How much was recharged in this section, otherwise zero."""
        return -self.soc_pp if self.charges else 0.0

    @property
    def mins(self) -> float | None:
        if not getattr(self.begin, "timestamp", None) or not getattr(self.past, "timestamp", None):
            return None
        return (self.past.timestamp - self.begin.timestamp).total_seconds() / 60.0


def sections(points) -> list[Section]:
    """Split the series pairwise, each section with a charging flag.

    Only points with a **reported** charge level count. At a point without a
    charge level no rise can be read, and an extrapolated value would be
    particularly harmful here: it comes from the same model that the users of
    this function are about to check.

    The time between two reports is not lost as a result - it ends up in a
    larger jump, and whoever needs it finds it via `Section.mins`.
    """
    with_soc = [p for p in points if p.soc is not None]
    result = []
    for earlier, after in zip(with_soc, with_soc[1:]):
        soc_pp = earlier.soc - after.soc
        result.append(Section(
            begin=earlier, past=after,
            charges=soc_pp <= -CHARGING_PP,
            soc_pp=soc_pp,
            km=((after.km_on_route or 0.0) - (earlier.km_on_route or 0.0))))
    return result


def charged_pp(points, until_point=None) -> float:
    """How many percentage points were recharged in total.

    `until_point` limits it to the start of the series up to that point -
    needed for the deviation during the trip, which must know what has been
    charged **so far**.
    """
    total = 0.0
    for section in sections(points):
        total += section.charged_pp
        if until_point is not None and section.past is until_point:
            break
    return total


def consumption(points) -> tuple[float, float]:
    """(consumed percentage points, kilometres driven in the process).

    Charging sections are left out - they say nothing about consumption, and
    their distance must not count either, otherwise it would be in the
    denominator without the matching consumption in the numerator.
    """
    pp = km = 0.0
    for section in sections(points):
        if section.charges:
            continue
        pp += section.soc_pp
        km += section.km
    return pp, km


def charge_pauses_minutes(points, driven_minutes) -> float:
    """How much of the elapsed time was spent on charging pauses.

    `driven_minutes(from_km, to_km)` returns the **driving time** for a
    stretch of road; it is subtracted because a downhill run also raises the
    charge level but costs no extra time. What remains is the standing time.

    This is needed because the energy profile carries driving time only: the
    charging time is in the plan, never in the profile. Anyone who holds the
    wall clock against it unfiltered sees a delay equal to the charging
    duration after the first charging stop - permanently, since it is never
    made up.
    """
    total = 0.0
    for section in sections(points):
        if not section.charges:
            continue
        elapsed = section.mins
        if elapsed is None:
            continue
        driven = driven_minutes(section.begin.km_on_route or 0.0,
                                     section.past.km_on_route or 0.0)
        total += max(0.0, elapsed - (driven or 0.0))
    return total


def charges_at_end(points, timeframe_minutes: float,
                  min_swing_pp: float) -> bool:
    """Did the end of the series look like a charging process?

    A different question from `sections`, hence its own threshold: here it is
    not about whether a single section charges, but whether **enough** was
    recharged at the end to assume a charging pause. The caller decides how
    much is enough - for cleaning up a forgotten trip the threshold may be
    higher than for a balance.
    """
    with_soc = [p for p in points if p.soc is not None and p.timestamp is not None]
    if len(with_soc) < 2:
        return False
    end = with_soc[-1].timestamp
    timeframe = [p for p in with_soc
               if end - p.timestamp <= timedelta(minutes=timeframe_minutes)]
    return charged_pp(timeframe) >= min_swing_pp
