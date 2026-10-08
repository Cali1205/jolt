"""The boundary between jolt and whatever measures in the car.

Why a layer of its own before any source is even connected: an ELM327 dongle
does not read the state of charge of an MEB vehicle through the standardised
OBD2 PIDs - those are tailored to combustion engines - but through
manufacturer-specific UDS queries. That knowledge is bought in via existing
software instead of being rebuilt. It does mean, however, that the samples
arrive in *its* format and not in jolt's. Which format that will be depends on
phone, app and dongle and may change; that translation is needed is certain.

Hence the same separation as with `routing/provider.py`: translation is one
file per format, and the tracking behind it only sees `RawPoint`.

**Translation knows no network.** A normalizer gets an already parsed object
and returns a `RawPoint` - nothing more. Whether that object came from a POST
to jolt, from the response to a query at a foreign service, or from a file is
a question of transport and does not belong here. That is why
`check_sources.py` runs without network, without database and without
credentials - and why a new format can be built in from a recorded response
without sitting in the car.

Foreign data is broken until proven otherwise: missing fields, percent as a
fraction instead of percentage points, timestamps in seconds instead of
milliseconds, `null` in the middle of a record. A normalizer that does not
catch this merely relocates the error - it then ends up as a 500 in the log
or, worse, as silent nonsense in the energy profile. That is why each of them
raises `SourcesError` with a sentence saying what was missing.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class SourcesError(ValueError):
    """The message could not be used - with a reason in plain language."""


@dataclass
class RawPoint:
    """A sample after it has been translated from a foreign format.

    Deliberately the same fields as `LivePoint` - plus two that some sources
    deliver and jolt cannot collect itself:

    `timestamp` is the time of the **measurement**, not of arrival. A logger
    that delivers a dead-zone buffer after the fact sends five points at
    once; without this information they would all land on the same second,
    and the time factor would be nonsense.

    `charges` is the source's statement that charging is happening right now.
    `live/session.py` currently detects charging pauses from a rising state
    of charge, because it has nothing better available - a source that knows
    directly is the better authority. The field is carried along here even
    though nobody reads it yet: a translator that discards a field of the
    format is lossy without need, and the place where it will be needed is
    already settled.

    `soc` may be missing - then it is a pure position report, and
    `live/session.py` extrapolates the state of charge from the energy
    profile. For a *foreign* source it is mandatory nevertheless: a logger in
    the car that does not deliver the state of charge has missed its only
    purpose, and silently waving it through as a position reporter would make
    a broken setup look like a working one. That is therefore enforced in the
    translators, not here.
    """
    lat: float
    lon: float
    soc: float | None = None
    speed_kmh: float | None = None
    outside_temp_c: float | None = None
    timestamp: datetime | None = None
    charges: bool | None = None
    #: Whatever else the source delivered, unchanged. See LivePoint.
    raw_values: dict | None = None


class Source(Protocol):
    #: Short name under which the format is addressed ("jolt", "abrp", ...).
    name: str

    def normalize(self, records: dict) -> RawPoint:
        """Translate a message of this format into a `RawPoint`.

        Raises `SourcesError` if the message cannot be used. A half-filled
        `RawPoint` is not an option: position and state of charge are the
        minimum with which a point can be placed on the route and held
        against the profile.
        """
        ...


# ---------------------------------------------------------------------------
# Checks every translator needs
# ---------------------------------------------------------------------------

def num(records: dict, *names: str) -> float | None:
    """Get the first available numeric value under several field names.

    Several names, because the same format is called differently depending on
    version and sender - `ext_temp` and `extTemp`, for instance. A `None` or
    an empty string counts as "not delivered" and not as zero: an outside
    temperature of 0 °C and "no outside temperature" are two different
    statements, and confusing them means in winter not accounting for the
    heating.
    """
    for name in names:
        if name not in records:
            continue
        value = records[name]
        if value is None or value == "":
            continue
        try:
            read = float(value)
        except (TypeError, ValueError):
            raise SourcesError(f"Feld {name!r} ist keine Zahl: {value!r}")
        # NaN does not come out of JSON, but it does come out of
        # float("nan") - and it silently poisons every calculation behind it,
        # because every comparison with it yields False and no limit kicks in.
        if read != read:
            raise SourcesError(f"Feld {name!r} ist keine Zahl: {value!r}")
        return read
    return None


def required(records: dict, *names: str) -> float:
    value = num(records, *names)
    if value is None:
        raise SourcesError(f"Pflichtfeld fehlt: {' oder '.join(names)}")
    return value


def limits(value: float, bottom: float, upper: float, name: str) -> float:
    if not bottom <= value <= upper:
        raise SourcesError(
            f"{name} liegt ausserhalb des Möglichen: {value:g} "
            f"(erwartet {bottom:g} bis {upper:g})")
    return value


def formate() -> dict:
    """All known formats, by their short name.

    The import is inside the function and not at the top of the file, because
    the translators in turn import from this module - at the top that would be
    a circular import. One call per message is not worth worrying about:
    Python keeps the modules in memory after the first import.
    """
    from .abrp import AbrpFormat
    from .jolt import JoltFormat
    return {q.name: q for q in (JoltFormat(), AbrpFormat())}


def find(name: str) -> Source:
    """Get the translator for a format name.

    An unknown name is a setup error and not a broken message - that is why
    the text lists the formats that exist instead of merely saying that this
    one is not among them.
    """
    known = formate()
    source = known.get((name or "").strip().lower())
    if source is None:
        raise SourcesError(
            f"Unbekanntes Meldeformat {name!r}. Bekannt sind: "
            f"{', '.join(sorted(known))}.")
    return source


def truth(records: dict, *names: str) -> bool | None:
    """Read a yes/no field that may arrive as bool, number or text.

    Sources are remarkably inconsistent here: `true`, `1`, `"1"`, `"true"`
    and `"yes"` have all occurred. Whatever cannot be interpreted counts as
    "no statement" - a guessed charging detection would be worse than none.
    """
    for name in names:
        if name not in records or records[name] is None:
            continue
        value = records[name]
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "ja"):
            return True
        if text in ("0", "false", "no", "nein"):
            return False
    return None
