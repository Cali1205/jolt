"""Look things up in the calculated energy profile.

The profile is a list of support points along the route, one roughly every
250 m: `{"km", "soc", "kwh", "hoehe", "speed_kmh", "minuten", "lat",
"lon"}`. Asking for it is one of the most common operations in jolt - and it
occurred three times in two different meanings, which is the actual reason
for this module.

The two meanings really are different, and mixing them up costs accuracy at
exactly the places where it matters:

`value_at` **interpolates** between the support points. That is right for
everything that is compared or computed - the planned charge level at the
reported position, for example. At 250 m spacing the straight line in between
is accurate enough, and the jump from one support point to the next would be
a visible error for a charge level with two decimal places.

`entry_at` returns the **support point itself**, unblended. That is right
when a coherent row is needed - position, elevation and charge level of the
*same* point. Interpolating between two support points would yield a
coordinate that lies on no route.
"""


def value_at(profile: list, km: float, field: str) -> float | None:
    """A single value at a kilometre mark, linearly interpolated."""
    if not profile:
        return None
    if km <= (profile[0].get("km") or 0.0):
        return profile[0].get(field)
    for earlier, after in zip(profile, profile[1:]):
        km1, km2 = earlier.get("km", 0.0), after.get("km", 0.0)
        if km1 <= km <= km2:
            if km2 <= km1:
                return earlier.get(field)
            share = (km - km1) / (km2 - km1)
            w1, w2 = earlier.get(field, 0.0), after.get(field, 0.0)
            return round(w1 + (w2 - w1) * share, 2)
    return profile[-1].get(field)


def soc_at(profile: list, km: float) -> float | None:
    """The planned charge level at a kilometre mark."""
    return value_at(profile, km, "soc")


def minutes_at(profile: list, km: float) -> float | None:
    """The planned **driving time** up to a kilometre mark.

    Explicitly without charging time - that is in the charging plan, never in
    the profile. Anyone who mixes this up holds the wall clock against a pure
    driving time and, after the first charging stop, sees a delay equal to
    the charging duration. See `live/session.py`.
    """
    return value_at(profile, km, "mins")


def entry_at(profile: list, km: float) -> dict:
    """The support point at a kilometre mark - the first one from `km`.

    An empty profile yields an empty object instead of an error: callers pick
    individual fields from it with `.get`, and a missing profile is no reason
    to crash.
    """
    if not profile:
        return {}
    if km <= (profile[0].get("km") or 0.0):
        return profile[0]
    for earlier, after in zip(profile, profile[1:]):
        if (earlier.get("km") or 0.0) <= km <= (after.get("km") or 0.0):
            return after
    return profile[-1]
