"""The charge stop optimizer - section 4 of the concept in code.

The task: find the sequence of charge stops and charge amounts that
minimizes the **total travel time**, subject to the constraint that the SoC
never falls below the reserve and that the desired target SoC is reached at
the destination.

This is not a shortest path, but a shortest path with a continuous decision
variable per node: how much to charge. Hence the five steps from the concept:

1. **Candidates** - charge points in the corridor, filtered by detour time
   and thinned out per route section.
2. **Graph** - nodes are start, candidates, destination; an edge exists if
   the leg can be driven on a full battery.
3. **Pareto-Dijkstra** over the state `(node, arrival SoC)`. Each node keeps
   a Pareto front of labels `(cost, SoC)`: a label is dropped if another one
   is at the same time cheaper *and* has more charge there.
4. **Post-optimization** - with the stop sequence fixed, the charge swings
   move on a fine grid into the steep part of the charge curve.
5. **Alternative sites** - for each stop the best alternative stop that is
   still reachable without recharging.

Why not greedy? A greedy planner fails systematically in two places: before
long gaps without a fast charger, where one should have charged *more*
beforehand, and when choosing between a 50 kW and a 300 kW site twenty
kilometers later. Those are exactly the cases in which a planner pays off.

This module knows neither database nor network: it gets a fully computed
route profile and a list of charge options. That allows it to be run through
completely in tools/check_optimizer.py - just like the consumption model.
"""
import heapq
import math
from bisect import bisect_left
from dataclasses import dataclass, field

from .curves import power_at
from .availability import (CHARGE_PARK_BONUS_MIN, operator_bonus,
                             redundancy_bonus)

# Candidates with more detour are dropped: they almost never win the time
# back at the charger. Fifteen minutes of detour are fifteen minutes of
# charging time, and at a 150 kW charger that buys about 30 kWh.
#
# Used to be 10.0. On long routes with thinner corridor coverage (e.g.
# Besançon-Dijon-Chalon-Brive on the way to southwest France) every reachable
# candidate was 11-23 minutes off the route and dropped out of the planning
# entirely, although the leg could have been driven with a single extra
# detour of a good ten minutes.
DETOUR_LIMIT_MIN = 15.0

# Grid of the charge targets in the search. Five percentage points keep the
# graph small; step 4 recovers the quantization afterwards.
SOC_GRID = 5.0
# Grid of the post-optimization - five times finer, because there the stop
# sequence is already fixed and only the charge amounts are left to find.
SOC_GRID_FINE = 1.0

# What a stop costs before the first electron flows - and after the last one
# has flowed. Leaving the route and merging back is already in
# `detour_minutes`; here go parking, fetching the cable, unlocking, waiting
# for the handshake and afterwards the same in reverse.
#
# **Without this item the objective function was blind to the number of
# stops.** It counted charging time and detour, nothing else. A battery
# charges much faster at 10 % than at 60 %, so under this assumption it is
# always cheaper to spread the same energy over many short stops at a low
# state of charge instead of a few long ones. The optimizer did exactly
# that: on the Périgueux-Vichy trip seven stops of two to six minutes, each
# time down to 10-12 % and then a sip in the steepest part of the curve.
# Mathematically optimal, practically nonsense - nobody leaves the route
# seven times to charge for three times two minutes.
#
# Five minutes are deliberately not chosen tightly. Anyone who sets the item
# too small gets the fragmentation back in weakened form; anyone who sets it
# too large loses at most a sensible intermediate stop - and that is the
# more harmless error.
STOP_FIXED_COST_MIN = 5.0

# How much must at least be charged at a stop for it to be worthwhile.
#
# Since the fixed costs above are in the objective function, this is only a
# safety net and no longer the mechanism: a stop that is not worthwhile is
# now not chosen anyway because it costs five minutes. The limit stays
# regardless - it keeps stops out of the plan that just barely add up
# mathematically at a weak charger, and costs nothing.
MIN_CHARGE_SWING = 8.0

# Thinning out the candidates: the best N per route section. Without it,
# twenty equivalent sites would sit in the graph at a motorway junction and
# cost computing time without insight.
CANDIDATES_PER_SECTION = 3
SECTION_KM = 15.0
AT_MOST_CANDIDATES = 60

# The arrival SoC of the labels is **rounded down** to this grid. That keeps
# the Pareto front finite without ever claiming more charge than there is -
# rounding down is the pessimistic direction.
LABEL_GRID = 0.5

# What one hour of the driver's time is worth, in euros. This turns money
# into time, and the objective function stays a single quantity - Dijkstra
# needs that, and everything can still be read in minutes.
#
# 30 EUR/h means: one euro weighs two minutes. Whoever sets 60 buys time
# dearly and drives faster; whoever sets 10 accepts detours for cheap
# electricity. **Zero means "I don't care about cost"** - then jolt
# calculates purely on time, as before.
#
# Without this item the wish for a specific provider could only be expressed
# as a time credit - a preference disguised as minutes. The trade-off that
# is really at stake could not be formulated at all: charge longer, but
# cheaper.
TIME_VALUE_EUR_H = 30.0

_EPS = 1e-9


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

@dataclass
class ChargeOption:
    """A possible stop - everything the optimizer needs to know about it.

    Deliberately not an ORM object: the optimizer should be testable without
    a database, and a hypothetical site ("what if a 300 kW charger stood
    here?") is then one line of code instead of a DB entry.
    """
    id: int
    km_on_route: float
    detour_minutes: float
    max_kw: float
    point_count: int = 1
    name: str = ""
    operator: str = ""
    city: str = ""
    lat: float = 0.0
    lon: float = 0.0
    # Reported as occupied: dropped from the planning, but stays as a record.
    locked: bool = False

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "operator": self.operator,
                "city": self.city, "lat": self.lat, "lon": self.lon,
                "max_kw": self.max_kw, "point_count": self.point_count,
                "km_on_route": round(self.km_on_route, 1),
                "detour_minutes": round(self.detour_minutes, 1)}


@dataclass
class RouteProfile:
    """Cumulative energy and time over the odometer.

    The decisive point: the energy demand of a leg does **not** depend on the
    state of charge - an EV does not get heavier when charging. So a single
    run of the consumption model suffices, and the optimizer reads the demand
    of each leg as the difference of two cumulative values, instead of
    recomputing the profile for every variant.
    """
    km: list[float]
    kwh: list[float]
    mins: list[float]

    @classmethod
    def from_profile(cls, profile) -> "RouteProfile":
        return cls(km=[p.km for p in profile.points],
                   kwh=[p.kwh_cumulative for p in profile.points],
                   mins=[p.minutes_cumulative for p in profile.points])

    @classmethod
    def from_dicts(cls, entries: list[dict]) -> "RouteProfile":
        """From the stored `Trip.energy_profile`."""
        return cls(km=[float(e.get("km", 0.0)) for e in entries],
                   kwh=[float(e.get("kwh", 0.0)) for e in entries],
                   mins=[float(e.get("mins", 0.0)) for e in entries])

    @property
    def total_km(self) -> float:
        return self.km[-1] if self.km else 0.0

    @property
    def total_minutes(self) -> float:
        return self.mins[-1] if self.mins else 0.0

    def val(self, vals: list[float], km: float) -> float:
        """Linearly interpolated - the support points are about 250 m apart."""
        if not self.km:
            return 0.0
        if km <= self.km[0]:
            return vals[0]
        if km >= self.km[-1]:
            return vals[-1]
        i = bisect_left(self.km, km)
        k0, k1 = self.km[i - 1], self.km[i]
        w0, w1 = vals[i - 1], vals[i]
        if k1 - k0 <= _EPS:
            return w1
        return w0 + (w1 - w0) * (km - k0) / (k1 - k0)


# ---------------------------------------------------------------------------
# Ergebnis
# ---------------------------------------------------------------------------

@dataclass
class Stop:
    option: ChargeOption
    arrival_soc: float
    departure_soc: float
    charge_time_minutes: float
    detour_minutes: float
    kwh_charged: float
    # Minutes since departure, including all previous stops and detours.
    arrival_minute: float
    departure_minute: float
    # What this charge costs. After the fields without a default value,
    # because a dataclass requires that - and with a default, so that
    # callers that know no prices keep working unchanged.
    cost_eur: float = 0.0
    # The site one could still reach without recharging if everything here is
    # occupied. None = there is none.
    detour_alt: dict | None = None

    def as_dict(self) -> dict:
        return {**self.option.as_dict(),
                "arrival_soc": round(self.arrival_soc, 1),
                "departure_soc": round(self.departure_soc, 1),
                "charge_time_minutes": round(self.charge_time_minutes, 1),
                "cost_eur": round(self.cost_eur, 2),
                "detour_minutes": round(self.detour_minutes, 1),
                "kwh_charged": round(self.kwh_charged, 1),
                "arrival_minute": round(self.arrival_minute),
                "departure_minute": round(self.departure_minute),
                "detour_alt": self.detour_alt}


@dataclass
class ChargePlan:
    feasible: bool = False
    reason: str = ""
    stops: list[Stop] = field(default_factory=list)
    drive_time_minutes: float = 0.0
    charge_time_minutes: float = 0.0
    detour_time_minutes: float = 0.0
    # Parking, cable, unlocking - once per stop. A separate item, so that the
    # balance keeps showing what the mere *number* of stops costs.
    holding_cost_minutes: float = 0.0
    # What the charges cost. Not a time item - it stands beside the time,
    # because it is the second quantity by which a plan is judged.
    cost_eur: float = 0.0
    total_minutes: float = 0.0
    soc_at_target: float = 0.0
    checked_candidates: int = 0

    def as_dict(self) -> dict:
        return {"feasible": self.feasible, "reason": self.reason,
                "stop_count": len(self.stops),
                "stops": [s.as_dict() for s in self.stops],
                "drive_time_minutes": round(self.drive_time_minutes),
                "charge_time_minutes": round(self.charge_time_minutes),
                "detour_time_minutes": round(self.detour_time_minutes),
                "holding_cost_minutes": round(self.holding_cost_minutes),
                "cost_eur": round(self.cost_eur, 2),
                "total_minutes": round(self.total_minutes),
                "soc_at_target": round(self.soc_at_target, 1),
                "checked_candidates": self.checked_candidates}


# ---------------------------------------------------------------------------
# Charge time as a table
# ---------------------------------------------------------------------------

class _ChargeTimeTable:
    """Cumulative charge time from 0 % to x %, for a fixed charger power.

    The trick that makes the search affordable: the charge time from a to b
    is `T(b) - T(a)`, because the integral over the charge curve is additive.
    Instead of integrating again in each of the tens of thousands of edge
    evaluations, a table is built once per occurring charger power.
    """

    def __init__(self, curve, battery_net_kwh: float, max_charger_kw: float,
                 max_vehicle_kw: float, temperature_factor: float,
                 step: float = 0.5):
        self.step = step
        self.vals: list[float] = [0.0]
        amount_sum = 0.0
        soc = 0.0
        while soc < 100.0 - _EPS:
            kw = power_at(curve, soc + step / 2.0, max_charger_kw,
                              max_vehicle_kw, temperature_factor)
            if kw <= 0.1:
                # From here on the car accepts nothing more - everything above
                # is unreachable, not "takes long".
                amount_sum = math.inf
            elif amount_sum != math.inf:
                amount_sum += (battery_net_kwh * step / 100.0) / kw * 60.0
            self.vals.append(amount_sum)
            soc += step

    def _until(self, soc: float) -> float:
        if soc <= 0.0:
            return 0.0
        if soc >= 100.0:
            return self.vals[-1]
        pos = soc / self.step
        i = int(pos)
        if i + 1 >= len(self.vals):
            return self.vals[-1]
        a, b = self.vals[i], self.vals[i + 1]
        if a == math.inf or b == math.inf:
            return math.inf
        return a + (b - a) * (pos - i)

    def timestamp(self, from_soc: float, until_soc: float) -> float:
        if until_soc <= from_soc + _EPS:
            return 0.0
        end = self._until(until_soc)
        if end == math.inf:
            return math.inf
        return max(0.0, end - self._until(from_soc))


# ---------------------------------------------------------------------------
# Die Planung
# The planning

def schedule(profile: RouteProfile, options: list[ChargeOption], fz,
           curve: list[tuple[float, float]], start_soc: float,
           target_soc: float = 20.0, max_vehicle_kw: float = 1e9,
           temperature_factor: float = 1.0,
           detour_limit_min: float = DETOUR_LIMIT_MIN,
           preferred_operators: list[str] | None = None,
           stop_fixed_cost_min: float = STOP_FIXED_COST_MIN,
           charge_park_bonus_min: float = CHARGE_PARK_BONUS_MIN,
           price_for=None,
           time_value_eur_h: float = TIME_VALUE_EUR_H,
           km_offset: float = 0.0) -> ChargePlan:
    """The time-optimal sequence of charge stops.

    `fz` are the vehicle values from the consumption model (`battery_net_kwh`
    and `reserve_soc` are needed). `curve` are the support points of the
    charge curve as `(SoC, kW)`. `preferred_operators` favors matching sites
    when choosing stops, but does not exclude others - see
    availability.operator_bonus().
    """
    plan = ChargePlan()
    if not profile.km or len(profile.km) < 2 or fz.battery_net_kwh <= 0:
        plan.reason = "Kein brauchbares Streckenprofil."
        return plan

    total_km = profile.total_km
    target_soc = max(0.0, min(100.0, target_soc))

    filtered = _thin_out_candidates(options, total_km, detour_limit_min,
                                       preferred_operators)
    plan.checked_candidates = len(filtered)

    graph = _Graph(profile, filtered, fz, curve, max_vehicle_kw,
                   temperature_factor, preferred_operators,
                   stop_fixed_cost_min, charge_park_bonus_min,
                   price_for, time_value_eur_h)

    # Step 3: Pareto-Dijkstra.
    path = graph.seek(start_soc, target_soc)
    if path is None:
        plan.reason = graph.describe_gap(start_soc, filtered,
                                              km_offset)
        return plan
    stops, departures_coarse = path

    # Step 4: post-optimization on a fine grid with a fixed stop sequence. It
    # can only improve on the search - if it finds nothing, the search result
    # continues to apply unchanged.
    departures = graph.reoptimize(stops, start_soc, target_soc) or departures_coarse

    # Step 5 and assembly.
    return graph.plan_build(plan, stops, departures, start_soc)


def _thin_out_candidates(options: list[ChargeOption], total_km: float,
                           detour_limit_min: float,
                           preferred_operators: list[str] | None = None
                           ) -> list[ChargeOption]:
    """Step 1: filter and keep the best per route section.

    Filtering is done by detour time, not by straight-line distance: eight
    kilometers beside the motorway are irrelevant if the exit is right there,
    and fatal if not.
    """
    usable = [o for o in options
                 if not o.locked
                 and o.detour_minutes <= detour_limit_min + _EPS
                 and 0.0 < o.km_on_route < total_km]
    if not usable:
        return []

    usable.sort(key=lambda o: o.km_on_route)

    # Choose the section width so that the upper limit is kept - on a trip of
    # 900 km, 15 km sections would otherwise not suffice.
    sections_max = max(1.0, AT_MOST_CANDIDATES / CANDIDATES_PER_SECTION)
    section_width_km = max(SECTION_KM, total_km / sections_max)

    keep: list[ChargeOption] = []
    for _, group in _group(usable, section_width_km):
        # Sort key in the order in which it counts on the road: power first
        # (it determines the standing time), then redundancy (the risk of
        # facing an occupied charger), then the detour.
        group.sort(key=lambda o: (-o.max_kw, -(o.point_count or 1),
                                   o.detour_minutes))
        selection = group[:CANDIDATES_PER_SECTION]
        # A preferred provider must not fail merely because of this sorting -
        # otherwise the operator bonus from _successor() never gets to see it
        # in a densely occupied section, however clear the preference was.
        # The weakest slot is replaced, not appended to: the upper limit per
        # section stays in place.
        if preferred_operators and not any(
                operator_bonus(o.operator, preferred_operators) > 0
                for o in selection):
            preferred = next(
                (o for o in group[CANDIDATES_PER_SECTION:]
                 if operator_bonus(o.operator, preferred_operators) > 0),
                None)
            if preferred:
                selection = selection[:-1] + [preferred]
        keep.extend(selection)

    keep.sort(key=lambda o: o.km_on_route)
    return keep[:AT_MOST_CANDIDATES]


def _group(options: list[ChargeOption], width_km: float):
    current: list[ChargeOption] = []
    key = None
    for o in options:
        k = int(o.km_on_route // width_km)
        if key is None or k == key:
            key = k
            current.append(o)
        else:
            yield key, current
            key, current = k, [o]
    if current:
        yield key, current


class _Graph:
    """Nodes, edges and the search on them.

    Node 0 is the start, 1..n are the candidates in route order, n+1 is the
    destination.
    """

    def __init__(self, profile: RouteProfile, options: list[ChargeOption], fz,
                 curve, max_vehicle_kw: float, temperature_factor: float,
                 preferred_operators: list[str] | None = None,
                 stop_fixed_cost_min: float = STOP_FIXED_COST_MIN,
                 charge_park_bonus_min: float = CHARGE_PARK_BONUS_MIN,
                 price_for=None, time_value_eur_h: float = TIME_VALUE_EUR_H):
        self.stop_fixed_cost_min = stop_fixed_cost_min
        self.charge_park_bonus_min = charge_park_bonus_min
        self.price_for = price_for or (lambda option: 0.0)
        # Conversion factor euro -> minutes. Zero at time value 0: then costs
        # are irrelevant and optimization is purely on time.
        self.cost_weight = (60.0 / time_value_eur_h) if time_value_eur_h > 0 else 0.0
        self.profile = profile
        self.options = options
        self.fz = fz
        self.curve = curve
        self.max_vehicle_kw = max_vehicle_kw
        self.temperature_factor = temperature_factor
        self.preferred_operators = preferred_operators or []
        self.reserve = fz.reserve_soc
        # Conversion kWh -> percentage points. From here on the optimizer
        # calculates exclusively in SoC; that saves a multiplication per
        # evaluation in the inner loop and makes the bounds readable.
        self.soc_per_kwh = 100.0 / fz.battery_net_kwh

        self.km = [0.0] + [o.km_on_route for o in options] + [profile.total_km]
        self.n = len(self.km)
        self.target_index = self.n - 1

        self.kwh = [profile.val(profile.kwh, k) for k in self.km]
        self.mins = [profile.val(profile.mins, k) for k in self.km]

        self._peak = self._compute_peaks()
        self._tables: dict[float, _ChargeTimeTable] = {}

    # ---------- Legs ----------

    def _compute_peaks(self) -> list[list[float]]:
        """The largest cumulative demand between two nodes, not just the
        demand at the end.

        Over a pass this is the difference between "works" and "gets stuck at
        the top": at the summit the consumption is highest, on the descent
        regeneration recovers part of it. Anyone who only checks the balance
        at the end of the leg plans a leg that is not feasible in the middle.
        """
        peak = [[0.0] * self.n for _ in range(self.n)]
        for i in range(self.n):
            cycle = self.kwh[i]
            p = bisect_left(self.profile.km, self.km[i])
            for j in range(i + 1, self.n):
                while p < len(self.profile.km) and self.profile.km[p] <= self.km[j]:
                    if self.profile.kwh[p] > cycle:
                        cycle = self.profile.kwh[p]
                    p += 1
                if self.kwh[j] > cycle:
                    cycle = self.kwh[j]
                peak[i][j] = cycle - self.kwh[i]
        return peak

    def net_soc(self, i: int, j: int) -> float:
        return (self.kwh[j] - self.kwh[i]) * self.soc_per_kwh

    def peak_soc(self, i: int, j: int) -> float:
        return self._peak[i][j] * self.soc_per_kwh

    def drive_time(self, i: int, j: int) -> float:
        return max(0.0, self.mins[j] - self.mins[i])

    def table(self, node: int) -> _ChargeTimeTable:
        kw = round(self.options[node - 1].max_kw, 1)
        if kw not in self._tables:
            self._tables[kw] = _ChargeTimeTable(
                self.curve, self.fz.battery_net_kwh, kw, self.max_vehicle_kw,
                self.temperature_factor)
        return self._tables[kw]

    def min_charge(self, i: int, j: int, target_soc: float) -> float:
        """With what percentage must one set off at node i to make it to j?

        Two conditions, and the stricter one applies: never below the reserve
        on the way (that is the peak), and the required state of charge at the
        end of the leg (reserve for an intermediate stop, the target SoC at
        the destination).
        """
        demand_at_end = target_soc if j == self.target_index else self.reserve
        return max(self.reserve + self.peak_soc(i, j),
                   demand_at_end + self.net_soc(i, j))

    def reachable_at_all(self, i: int, j: int) -> bool:
        """Is the leg drivable on a full battery? Monotonic in j."""
        return self.reserve + self.peak_soc(i, j) <= 100.0 + _EPS

    # ---------- Step 3: Pareto-Dijkstra ----------

    def seek(self, start_soc: float, target_soc: float):
        """Cost-optimal stop sequence, or None.

        The optimization is not on pure time but on time **minus redundancy
        bonus**: a site with eight charge points gets a good four minutes
        credited, because the risk of facing an occupied charger is smaller
        there. As long as nobody has real availability data, the number of
        charge points is the best available approximation. What is reported
        in the end is the real time, not the cost.
        """
        # labels[i] = (node, soc, cost, predecessor, departure_soc_at_predecessor)
        labels: list[tuple] = [(0, start_soc, 0.0, -1, start_soc)]
        haufen: list[tuple[float, int]] = [(0.0, 0)]

        # Dominance by already processed labels. Because Dijkstra works in
        # ascending cost, every label generated later is at least as expensive
        # as every one already taken out. A new label with no more charge than
        # a taken-out one is therefore dominated - and this check costs one
        # comparison instead of a pass through the whole front. `done[k]` is
        # the maximum of the charge levels with which node k has already been
        # processed.
        done = [-1.0] * self.n
        # In addition, per node and charge level the cheapest cost so far.
        # Together the two replace the linear Pareto front without wrongly
        # discarding any of its labels.
        top: list[dict[float, float]] = [{} for _ in range(self.n)]

        while haufen:
            cost, lid = heapq.heappop(haufen)
            node, soc, saved, _, _ = labels[lid]
            if cost > saved + _EPS or soc <= done[node]:
                continue
            done[node] = soc
            if node == self.target_index:
                # Dijkstra with non-negative edges: the first label at the
                # destination is the cheapest.
                return self._path_trace_back(labels, lid)

            for successor in self._successor(node, soc, cost, target_soc, done):
                target_node, new_soc, new_cost, departure = successor
                if new_soc <= done[target_node]:
                    continue
                if new_cost >= top[target_node].get(new_soc, math.inf) - _EPS:
                    continue
                top[target_node][new_soc] = new_cost
                labels.append((target_node, new_soc, new_cost, lid, departure))
                heapq.heappush(haufen, (new_cost, len(labels) - 1))

        return None

    def _successor(self, node: int, soc: float, cost: float,
                    target_soc: float, done: list[float]):
        """All labels that arise from this one label.

        First the legs, then the charge targets, then the cross product - and
        exactly in this order, because the charge time depends only on the
        charge target and not on where one drives afterwards. Integrating it
        once per charge target instead of once per charge target *and* leg is
        the difference between seconds and fractions of a second.
        """
        legs = []
        for j in range(node + 1, self.n):
            if not self.reachable_at_all(node, j):
                # The peak grows monotonically with j - what is too far here
                # stays too far for all following nodes.
                break
            min_target = self.min_charge(node, j, target_soc)
            if min_target > 100.0 + _EPS:
                continue
            legs.append((j, min_target, self.net_soc(node, j),
                            self.drive_time(node, j)))
        if not legs:
            return

        if node == 0:
            # No charging at the start - one sets off with what is there.
            departures = [(soc, 0.0)]
        else:
            option = self.options[node - 1]
            detour = option.detour_minutes
            table = self.table(node)
            bonus_raw = (redundancy_bonus(option.point_count or 1,
                                         self.charge_park_bonus_min)
                        + operator_bonus(option.operator,
                                          self.preferred_operators))

            lower_limit = soc + MIN_CHARGE_SWING
            # The grid **and** the exactly required charge levels of the legs:
            # the charge swing that just barely suffices is the fastest stop of
            # all and almost never lies on the grid.
            targets = set(_grid(lower_limit, SOC_GRID))
            targets.update(z for _, z, _, _ in legs if z >= lower_limit)
            departures = []
            for charge_target in sorted(targets):
                if charge_target > 100.0 + _EPS:
                    break
                charge_time = table.timestamp(soc, charge_target)
                if charge_time == math.inf:
                    break
                # The credit may offset the detour **and** the fixed costs of
                # the stop, but never the charging time. That keeps every edge
                # positive - Dijkstra needs that - and a stop costs at least as
                # much as the charging takes.
                #
                # Before, this said `min(bonus_raw, detour)`, and that was the
                # bug: a charging park **directly on the route** has no
                # detour, so it got no credit either. Exactly where the large
                # parks are - on the motorway - the preference therefore had
                # no effect at all. On a France route it was cut away
                # completely at six of seven Ionity sites.
                #
                # Including the fixed costs is not a concession but the right
                # reference: the credit says "this stop is less annoying than
                # another" - and what is annoying about a stop is the stopping,
                # not the charging.
                bonus = min(bonus_raw, detour + self.stop_fixed_cost_min)
                # The fixed costs of the stop go here and not with the charge
                # time: they occur once per stop, regardless of how much is
                # charged. That is exactly the difference that lets a few long
                # stops win against many short ones.
                # What this charge costs, in equivalent minutes. The energy
                # demand of a leg does not depend on the state of charge, but
                # the *cost* of a stop very much depends on the amount charged
                # - that is why this is here per charge target and not per
                # stop.
                kwh = (charge_target - soc) / 100.0 * self.fz.battery_net_kwh
                cost_min = (kwh * self.price_for(option)
                              * self.cost_weight)
                departures.append((charge_target, self.stop_fixed_cost_min
                                  + detour + charge_time - bonus + cost_min))
            if not departures:
                return

        vals = [a for a, _ in departures]
        for j, min_target, net, drive in legs:
            # Two bounds: enough charge for the leg, and more charge than the
            # target node has already processed. The second is only a quick
            # pre-filter - exact discarding happens in `seek`.
            barrier = max(min_target, done[j] + net + LABEL_GRID)
            for idx in range(bisect_left(vals, barrier - _EPS), len(vals)):
                departure, surcharge = departures[idx]
                yield (j, _round_down(departure - net),
                       cost + surcharge + drive, departure)

    @staticmethod
    def _path_trace_back(labels, lid) -> tuple[list[int], list[float]]:
        """Stop sequence and the departure SoCs chosen there.

        Each label remembers with what state of charge it left the
        *predecessor*. Read backwards, that gives exactly the charge amounts
        of the path found - without having to compute them a second time.
        """
        node: list[int] = []
        departures: list[float] = []
        label_id = lid
        while label_id >= 0:
            k, _soc, _cost, predecessor, departure = labels[label_id]
            node.append(k)
            departures.append(departure)
            label_id = predecessor
        node.reverse()
        departures.reverse()
        # node = [start, stop .., destination]; departures[i] belongs to
        # node[i-1], so the first two entries concern the start.
        return node[1:-1], departures[2:]

    # ---------- Step 4: Post-optimization ----------

    def reoptimize(self, stops: list[int], start_soc: float,
                       target_soc: float) -> list[float]:
        """Redistribute the charge amounts for a fixed stop sequence.

        The search in step 3 works on a 5-percent grid so that the graph stays
        small. Once the stop sequence is fixed, the problem is only
        one-dimensional - then the fine grid pays off. The effect is the one
        from section 2.4: charge swings move into the steep part of the curve,
        because there the same kilowatt hour costs less time.

        Returns: the departure SoCs per stop.
        """
        if not stops:
            return []

        node_sequence = [0] + stops + [self.target_index]
        cells = _fine_grid()

        # levels[t] maps the departure SoC at stop t to (charge time up to
        # here, departure SoC at the previous stop). The back-reference makes
        # the resolution at the end unambiguous.
        levels: list[dict[float, tuple[float, float | None]]] = []

        arrival = start_soc - self.net_soc(0, node_sequence[1])
        if arrival + _EPS < self.reserve:
            return []
        level = self._fill_level(node_sequence[1], node_sequence[2], target_soc,
                                    cells, [(arrival, 0.0, None)])
        if not level:
            return []
        levels.append(level)

        for t in range(1, len(stops)):
            node = node_sequence[t + 1]
            net = self.net_soc(node_sequence[t], node)
            peak = self.peak_soc(node_sequence[t], node)
            inputs = []
            for d_prior, (time_prior, _) in levels[-1].items():
                if d_prior - peak + _EPS < self.reserve:
                    continue
                inputs.append((d_prior - net, time_prior, d_prior))
            level = self._fill_level(node, node_sequence[t + 2], target_soc,
                                        cells, inputs)
            if not level:
                return []
            levels.append(level)

        # The condition "at least the target SoC at the destination" is
        # already contained in `min_charge` for the last leg - only the
        # minimum remains here.
        last_level = levels[-1]
        d = min(last_level, key=lambda dep_soc: last_level[dep_soc][0])

        departures = [0.0] * len(stops)
        for t in range(len(stops) - 1, -1, -1):
            departures[t] = d
            d = levels[t][d][1]
        return departures

    def _fill_level(self, node: int, next_node: int, target_soc: float,
                       cells: list[float],
                       inputs: list[tuple[float, float, float | None]]):
        """One DP level: all sensible departure SoCs at this stop.

        `inputs` are triples (arrival SoC, charge time up to here, departure
        SoC at the previous stop).
        """
        tab = self.table(node)
        min_target = self.min_charge(node, next_node, target_soc)
        level: dict[float, tuple[float, float | None]] = {}
        for arrival, time_prior, origin in inputs:
            if arrival + _EPS < self.reserve:
                continue
            # The minimum charge swing applies here too. Without it the
            # post-optimization grinds the charge swings down to the bare
            # minimum and produces stops of one minute - Dijkstra had
            # forbidden them, the DP reintroduced them. It stays feasible: the
            # path from step 3 already meets the bound, so there is always a
            # solution here.
            lower_limit = max(arrival + MIN_CHARGE_SWING, min_target)
            for d in cells:
                if d + _EPS < lower_limit:
                    continue
                timestamp = tab.timestamp(arrival, d)
                if timestamp == math.inf:
                    break
                # The costs must count here exactly as in the search. Without
                # them the post-optimization minimized pure time - and because
                # it runs *after* Dijkstra and overwrites its charge amounts,
                # it shifted energy from the cheap charger to the expensive
                # one as soon as that saved a few seconds. That silently undid
                # what step 3 had just achieved in cost optimization.
                cost_min = ((d - arrival) / 100.0 * self.fz.battery_net_kwh
                              * self.price_for(self.options[node - 1])
                              * self.cost_weight)
                total = time_prior + timestamp + cost_min
                present = level.get(d)
                if present is None or total < present[0] - _EPS:
                    level[d] = (total, origin)
        return level

    # ---------- Step 5 and assembly ----------

    def plan_build(self, plan: ChargePlan, stops: list[int],
                   departures: list[float], start_soc: float) -> ChargePlan:
        plan.feasible = True
        plan.drive_time_minutes = self.profile.total_minutes

        soc = start_soc
        clock = 0.0
        earlier = 0
        for pos, node in enumerate(stops):
            option = self.options[node - 1]
            clock += self.drive_time(earlier, node)
            arrival = soc - self.net_soc(earlier, node)
            clock += option.detour_minutes

            departure = departures[pos] if pos < len(departures) else arrival
            departure = max(departure, arrival)
            charge_time = self.table(node).timestamp(arrival, departure)
            if charge_time == math.inf:
                charge_time = 0.0
                departure = arrival

            # Arrival is when one is there - the fixed costs run afterwards,
            # otherwise the plan would claim an arrival that already includes
            # the parking.
            arrival_minute = clock
            clock += self.stop_fixed_cost_min + charge_time

            plan.stops.append(Stop(
                option=option, arrival_soc=arrival, departure_soc=departure,
                charge_time_minutes=charge_time, detour_minutes=option.detour_minutes,
                kwh_charged=(departure - arrival) / 100.0 * self.fz.battery_net_kwh,
                cost_eur=((departure - arrival) / 100.0 * self.fz.battery_net_kwh
                            * self.price_for(option)),
                arrival_minute=arrival_minute, departure_minute=clock,
                detour_alt=self._alternative(node, arrival)))

            plan.charge_time_minutes += charge_time
            plan.detour_time_minutes += option.detour_minutes
            plan.holding_cost_minutes += self.stop_fixed_cost_min
            plan.cost_eur += plan.stops[-1].cost_eur
            soc = departure
            earlier = node

        plan.soc_at_target = soc - self.net_soc(earlier, self.target_index)
        plan.total_minutes = (plan.drive_time_minutes + plan.charge_time_minutes
                               + plan.detour_time_minutes
                               + plan.holding_cost_minutes)
        return plan

    def _alternative(self, node: int, arrival_soc: float) -> dict | None:
        """Step 5: where could one still get to if everything here is occupied?

        The condition is "reachable without recharging" - that is, with
        exactly the state of charge with which one arrives here. Whoever
        stands in front of an occupied charger has no reserve for a search;
        they need a name and a distance, immediately.

        The alternative route may dip into the reserve, but not go through
        it: that is exactly what it is there for. The plan itself never
        touches it - it arrives everywhere with at least `reserve_soc` - and
        therefore an alternative site that has to leave the full reserve
        untouched would almost never be reachable. Half the reserve remains
        as the hard limit.

        If there is none, `None` is the right answer and not a gap in the
        plan: it means "if everything is occupied here, it gets tight" - and
        that is exactly the information one wants beforehand.
        """
        lower_limit = max(2.0, self.reserve / 2.0)
        best = None
        for j in range(node + 1, self.target_index):
            option = self.options[j - 1]
            if option.locked:
                continue
            rest = arrival_soc - self.peak_soc(node, j)
            if rest + _EPS < lower_limit:
                # The peak grows monotonically with j - what is no longer
                # enough from here will not be enough for anything further.
                break
            cost = (self.drive_time(node, j) + option.detour_minutes
                      - redundancy_bonus(option.point_count or 1))
            if best is None or cost < best[0]:
                best = (cost, option,
                          arrival_soc - self.net_soc(node, j))
        if best is None:
            return None
        _, option, rest_soc = best
        return {**option.as_dict(), "arrival_soc": round(rest_soc, 1)}

    # ---------- Diagnosis ----------

    def describe_gap(self, start_soc: float,
                           candidates: list[ChargeOption],
                           km_offset: float = 0.0) -> str:
        """Why did it not work? The answer is almost always a gap.

        A bare "not feasible" helps nobody. Anyone who knows that between
        km 210 and km 480 no reachable fast charger lies in the corridor can
        widen the radius, lower the minimum power or drive slower - and sees
        immediately that the software is not the problem, but possibly the
        still empty charge point table.

        `km_offset` converts the kilometers to the **whole** trip. When
        replanning on the way, the optimizer calculates on the remaining
        route, which starts at zero; the stops are converted back afterwards,
        but this sentence was not. It therefore named kilometers that do not
        exist on the route - in a test run, at km 137 the message read
        "zwischen km 0 und km 44" (between km 0 and km 44). Anyone who reads
        a kilometer figure at the wheel looks for it on their route, not on
        an imaginary one.
        """
        if not candidates:
            return ("Ohne Ladestopp nicht machbar, und im Korridor liegt kein "
                    "passender Ladepunkt. Sind die Ladesäulen importiert?")

        # Reachability regardless of time: from every reached node onward
        # with a full battery.
        reached = {0}
        farthest = 0
        opened = [0]
        while opened:
            i = opened.pop()
            soc = start_soc if i == 0 else 100.0
            for j in range(i + 1, self.n):
                if not self.reachable_at_all(i, j):
                    break
                if j in reached:
                    continue
                if soc - self.peak_soc(i, j) + _EPS < self.reserve:
                    continue
                reached.add(j)
                if j != self.target_index:
                    opened.append(j)
                    farthest = max(farthest, j)

        until_km = self.km[farthest] + km_offset
        next_km = None
        for j in range(farthest + 1, self.target_index):
            next_km = self.km[j] + km_offset
            break
        if next_km is None:
            return (f"Ab km {until_km:.0f} liegt kein weiterer Ladepunkt im "
                    f"Korridor - das Ziel ist von dort nicht erreichbar.")
        return (f"Zwischen km {until_km:.0f} und km {next_km:.0f} liegt kein "
                f"erreichbarer Ladepunkt. Grösserer Radius, geringere "
                f"Mindestleistung oder langsamer fahren.")


# ---------------------------------------------------------------------------
# Odds and ends
# ---------------------------------------------------------------------------

def _round_down(soc: float) -> float:
    """**Round down** the arrival SoC to the label grid.

    Rounding down instead of rounding is intentional: it keeps the Pareto
    front finite without ever claiming more charge than is actually there. A
    plan that miscalculates by half a percent too optimistically is worse
    than one that gives away half a percent.
    """
    return math.floor(max(0.0, soc) / LABEL_GRID) * LABEL_GRID


def _grid(lower_limit: float, step: float):
    """The charge targets from `lower_limit`: first the exactly required value, then the grid.

    The exact value has to be included - it is the fastest stop that just
    barely supports the next leg, and almost never lies on the grid.
    """
    lower_limit = max(0.0, min(100.0, lower_limit))
    yield lower_limit
    soc = math.ceil((lower_limit + _EPS) / step) * step
    while soc <= 100.0 + _EPS:
        yield min(100.0, soc)
        soc += step


def _fine_grid() -> list[float]:
    vals = [i * SOC_GRID_FINE for i in range(int(100.0 / SOC_GRID_FINE) + 1)]
    if vals[-1] < 100.0:
        vals.append(100.0)
    return vals
