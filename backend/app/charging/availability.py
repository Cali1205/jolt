"""Is the charger free? - and an honest way of dealing with not knowing.

Real occupancy data for public chargers is not freely available in Germany.
Whoever has it has it through OCPI contracts with operators or through
commercial aggregators. For a private project that is out of reach for now.

Instead of faking availability, jolt does three things:

1. This interface exists so that an OCPI integration later is an adapter
   and not a rebuild.
2. As long as there is no data, **redundancy** counts: a site with eight
   charge points is preferable to one with two, even if it costs two minutes
   of detour. That is the best available approximation of "something is
   probably free there".
3. The user can report that a site is occupied. That applies to the current
   trip - and is the only information that is really correct.
"""
import time
from dataclasses import dataclass
from typing import Protocol

# How long an "occupied" report stays valid. Half an hour is how long a fast
# charging session typically takes - after that the statement is worthless
# and would only exclude a usable site permanently.
REPORT_VALID_S = 30 * 60


@dataclass
class State:
    free: int | None          # None = unbekannt
    total: int
    source: str               # "unbekannt" | "meldung" | "ocpi"
    as_of_s: float = 0.0


class AvailabilitySource(Protocol):
    def state(self, charge_point) -> State:
        ...


class Unknown:
    """The default: no data, only the number of charge points."""

    def state(self, charge_point) -> State:
        return State(free=None, total=charge_point.point_count or 1,
                       source="unbekannt")


class Reports:
    """User reports, kept in memory.

    Deliberately not in the database: the statement is worthless after thirty
    minutes, and something that expires faster than a restart takes does not
    belong in permanent storage.
    """

    def __init__(self, onward: AvailabilitySource | None = None):
        self._occupied: dict[int, float] = {}
        self._next = onward or Unknown()

    def report(self, charge_point_id: int) -> None:
        self._occupied[charge_point_id] = time.time()

    def release(self, charge_point_id: int) -> None:
        self._occupied.pop(charge_point_id, None)

    def actual_reported(self, charge_point_id: int) -> bool:
        since = self._occupied.get(charge_point_id)
        if since is None:
            return False
        if time.time() - since > REPORT_VALID_S:
            del self._occupied[charge_point_id]
            return False
        return True

    def state(self, charge_point) -> State:
        if self.actual_reported(charge_point.id):
            return State(free=0, total=charge_point.point_count or 1,
                           source="meldung",
                           as_of_s=time.time() - self._occupied[charge_point.id])
        return self._next.state(charge_point)


# From how many charge points a site counts as "large" and gets the full
# credit. Beyond that nothing grows any more - the jump from 30 to 40
# chargers no longer changes the question of whether something is free.
#
# It used to be around 15, and that was too early: in the data of a France
# route, sites with 15, 17, 20, 28 and 30 charge points all got exactly the
# same credit. Size thus stopped counting exactly where the interesting
# charging parks begin.
LARGE_PARK = 30
CHARGE_PARK_BONUS_MIN = 4.0


def redundancy_bonus(point_count: int, at_most: float = CHARGE_PARK_BONUS_MIN
                    ) -> float:
    """Time credit in minutes for a site with many charge points.

    As long as nobody knows what is free, the number of charge points is the
    only reliable indication of the risk of facing an occupied charger. A
    site with many points is worth a small detour - and, since the credit no
    longer depends on the detour, also a preference over a smaller one right
    next to it.

    The logarithm, because the jump from 2 to 4 charge points means much
    more than the one from 20 to 22. The full credit applies from
    `LARGE_PARK` points.

    `at_most` is adjustable because it is a preference and not a constant of
    nature: anyone to whom a large charging park means little sets it to
    zero, and then time alone decides.
    """
    import math
    if at_most <= 0:
        return 0.0
    share = math.log(max(1, point_count)) / math.log(LARGE_PARK)
    return round(min(at_most, at_most * share), 2)


# Time credit for a preferred provider - of the same order of magnitude as
# the redundancy bonus, so that neither effect systematically outweighs the
# other.
OPERATOR_BONUS_MIN = 4.0


def operator_bonus(operator: str, preferred: list[str] | None) -> float:
    """Time credit in minutes if the operator is on the preferred list.

    Not a hard filter but, like the redundancy bonus, only a weight in the
    stop choice: a preferred provider makes a stop more attractive, never
    free - when called, the bonus offsets only the detour, never the charging
    time (see optimizer.py).

    The comparison is a lowercase substring match: "EnBW" in the list
    matches "EnBW mobility+" in the record, without needing to know the exact
    provider wording.
    """
    if not preferred or not operator:
        return 0.0
    operator_small = operator.strip().lower()
    for entry in preferred:
        if entry and entry.strip().lower() in operator_small:
            return OPERATOR_BONUS_MIN
    return 0.0


# One instance per process. The state is deliberately process-local - jolt
# runs as one container for one household.
REPORTS = Reports()
