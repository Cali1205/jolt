"""The charging curve as the logbook measured it.

A logbook charge keeps the power over time, but the charge level only at its
start and end. The level in between is estimated from the energy delivered so
far: start + (kWh so far / kWh in total) x gain. The battery's charge level is
energy-based, so this follows the real one closely. Same estimate as the
`grafana_charge_curve` view in tools/grafana/setup.sql.

Many charges together give an envelope: per charge-level bin the 90th
percentile of the power. Not the maximum, which one spike would set, and not
the median, which every charger that could not deliver more pulls down. What
remains is what the car takes when the charger is not the limit and the
battery is warm.
"""
from datetime import datetime

from .curves import power_at

# Source preference for the power track, as in the Grafana view: the history
# export samples densely, the portal only reports now and then.
TRACK_SOURCES = ("vw_history", "vw_portal")
# A gap longer than this counts as this long, as in the logbook: a track that
# falls silent for an hour did not charge at its last power all along.
MAX_GAP_S = 600.0

BIN_PCT = 5.0
QUANTILE = 0.9
# A bin needs points from several charges, or one charge at a slow charger
# would be the curve there.
MIN_POINTS_PER_BIN = 5
MIN_CHARGES_PER_BIN = 2
# Below this the measured data covers too little and the profile applies.
MIN_CHARGES = 3
MIN_BINS_10_TO_80 = 8

Curve = list[tuple[float, float]]


def _track(details: dict | None) -> list | None:
    by_source = (details or {}).get("by_source") or {}
    for source in TRACK_SOURCES:
        records = by_source.get(source) or []
        if records and records[0].get("power_track") is not None:
            return records[0]["power_track"]
    return None


def charge_points(charge) -> Curve:
    """(estimated charge level, kW) for each point of one charge's power track."""
    soc_start, soc_end = charge.soc_start, charge.soc_end
    if soc_start is None or soc_end is None or soc_end <= soc_start:
        return []
    track = [(datetime.fromisoformat(t), float(kw))
             for t, kw in _track(charge.details) or [] if kw is not None]
    if not track:
        return []
    so_far = [0.0]
    for (t0, kw0), (t1, kw1) in zip(track, track[1:]):
        seconds = min((t1 - t0).total_seconds(), MAX_GAP_S)
        so_far.append(so_far[-1] + (kw0 + kw1) / 2 * seconds / 3600)
    total = so_far[-1]
    if total <= 0:
        return []
    gain = soc_end - soc_start
    return [(soc_start + kwh / total * gain, kw)
            for kwh, (_, kw) in zip(so_far, track)]


def _quantile(values: list[float], q: float) -> float:
    """Linear between the ranks, as Postgres' percentile_cont."""
    ordered = sorted(values)
    position = q * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def envelope(points_by_charge: list[Curve], bin_pct: float = BIN_PCT,
             quantile: float = QUANTILE,
             min_points: int = MIN_POINTS_PER_BIN,
             min_charges: int = MIN_CHARGES_PER_BIN) -> Curve | None:
    """Per charge-level bin the `quantile` of the power, or None when the
    charges cover too little to stand in for the profile curve.

    Bins are centred on multiples of `bin_pct`; a bin's point sits at its
    centre. Meant for DC charges only: an AC charge is limited by the wall
    box, not by the battery.
    """
    usable = [points for points in points_by_charge if points]
    if len(usable) < MIN_CHARGES:
        return None
    kws: dict[int, list[float]] = {}
    charges: dict[int, set[int]] = {}
    for n, points in enumerate(usable):
        for soc, kw in points:
            b = round(soc / bin_pct)
            kws.setdefault(b, []).append(kw)
            charges.setdefault(b, set()).add(n)
    curve = [(b * bin_pct, round(_quantile(kws[b], quantile), 1))
             for b in sorted(kws)
             if len(kws[b]) >= min_points and len(charges[b]) >= min_charges]
    if sum(1 for soc, _ in curve if 10 <= soc <= 80) < MIN_BINS_10_TO_80:
        return None
    return curve


def merge(measured: Curve, profile: Curve) -> Curve:
    """The measured bins, and the profile where nothing was measured.

    Measured values are not capped at the profile's maximum: they are what
    the car did. The vehicle's `max_charge_power_kw` still caps the planner,
    which passes it as `max_vehicle_kw`.
    """
    if not measured:
        return sorted(profile)
    low, high = measured[0][0], measured[-1][0]
    outside = [(soc, kw) for soc, kw in profile if soc < low or soc > high]
    pairs = sorted(list(measured) + outside)
    ends = profile or measured
    if pairs[0][0] > 0:
        pairs.insert(0, (0.0, power_at(ends, 0.0)))
    if pairs[-1][0] < 100:
        pairs.append((100.0, power_at(ends, 100.0)))
    return pairs
