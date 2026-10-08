"""Import charger master data.

Two sources with different purposes:

- **Bundesnetzagentur** (CSV): official and complete for Germany, because
  reporting is required by law. No key needed. This is the basis.
- **Open Charge Map** (API): worldwide, crowd-maintained, with a free key.
  For trips across the border and as a supplement.

Both imports are **idempotent**: they write via `(source, foreign_id)` and
can be repeated any number of times. That is not a nicety but the
prerequisite for putting the import into a cron job without doubling the
table.
"""
import csv
import hashlib
import io
import logging
import re

import requests

from .. import models

log = logging.getLogger("uvicorn.error")

OCM_API = "https://api.openchargemap.io/v3/poi"
TIMEOUT = 60

# Map the sources' connector type names onto the terms jolt filters by.
# Without this normalization the corridor search would return different hits
# depending on the import source.
CONNECTOR_PATTERN = [
    (re.compile(r"ccs|combo|combined", re.I), "CCS"),
    (re.compile(r"chademo", re.I), "CHAdeMO"),
    (re.compile(r"typ\s*2|type\s*2|mennekes|ac steckdose", re.I), "Typ2"),
    (re.compile(r"schuko|typ\s*f", re.I), "Schuko"),
]


def _type_unify(text: str) -> list[str]:
    hit = [name for pattern, name in CONNECTOR_PATTERN if pattern.search(text or "")]
    return sorted(set(hit))


def _number(val) -> float:
    """Number from a field that may contain a decimal comma.

    The official CSV uses the German comma throughout; a naive float()
    returns 0 there and would swallow every power figure.
    """
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    text = str(val).strip().replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return 0.0


def _stable_id(*parts) -> str:
    """Build a reproducible ID from the fields.

    The Bundesnetzagentur CSV has no primary key. Without an ID derived from
    the content, every import would create the same chargers again. The basis
    is operator, address and the coordinates rounded to five decimal places
    (about one meter) - fine enough to tell two sites apart, coarse enough
    to survive rounding noise.
    """
    raw = "|".join(str(t).strip().lower() for t in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _lengths_shorten(fields: dict) -> dict:
    """Trim strings to the column width.

    Third-party data does not respect our columns. A single value that was
    too long used to make the **whole** import fail - thousands of records
    lost because of one. Exactly that has already happened: for sites with
    several postcodes (large shopping centers) OCM returns a semicolon list,
    and "33000;33100;33200;33300;33800" at Auchan Bordeaux Lac aborted the
    run. The column was then widened from 20 to 40 characters - which only
    postpones the same error, because the next site has six postcodes.

    Trimmed instead of skipped: a truncated postcode is a cosmetic flaw, a
    missing charge point on the route is not. The lengths come from the
    model so that the list cannot go stale next to the columns.
    """
    shortened = {}
    for name, val in fields.items():
        column = models.ChargePoint.__table__.columns.get(name)
        len_total = getattr(getattr(column, "type", None), "length", None)
        if isinstance(val, str) and len_total and len(val) > len_total:
            log.info("Field %s truncated to %d characters: %r", name, len_total, val)
            val = val[:len_total]
        shortened[name] = val
    return shortened


def _save(db, source: str, foreign_id: str, fields: dict) -> str:
    fields = _lengths_shorten(fields)
    present = (db.query(models.ChargePoint)
                 .filter_by(source=source, foreign_id=foreign_id).one_or_none())
    if present:
        for keyname, val in fields.items():
            setattr(present, keyname, val)
        return "aktualisiert"
    db.add(models.ChargePoint(source=source, foreign_id=foreign_id, **fields))
    # Without this flush the lookup above does not see a record that was just
    # added in the same, not yet committed batch (the session runs with
    # autoflush=False). If the same foreign_id occurs twice within one import
    # - e.g. when a multi-country query to Open Charge Map returns a site near
    # the border twice - the second lookup wrongly considers it new, and the
    # second INSERT violates the unique constraint (source, foreign_id).
    db.flush()
    return "neu"


# ---------------------------------------------------------------------------
# Bundesnetzagentur
# ---------------------------------------------------------------------------

def _header_row_find(rows: list[list[str]]) -> int:
    """Find the actual column header row.

    The official file starts with a preamble of title, date and notes - of
    varying length depending on the edition. Instead of skipping a fixed
    number of rows (which will be wrong at the next update), the row
    containing "Breitengrad" (latitude) is searched for.

    For the longitude ("Längengrad") only "ngengrad" is checked. The reason
    is the "ä": if the file is read with the wrong encoding, it reads
    "LÃ¤ngengrad" there - and a search for the correctly spelled word would
    fail. The abort would then come with the message "is this really the
    register?", although the problem lies somewhere else entirely. The word
    ending carries the same information and survives any encoding.
    """
    for i, row in enumerate(rows[:60]):
        linked = " ".join(row).lower()
        if "breitengrad" in linked and "ngengrad" in linked:
            return i
    raise ValueError("Kopfzeile mit 'Breitengrad'/'Längengrad' nicht gefunden - "
                     "ist das wirklich das Ladesäulenregister?")


def _column(header: list[str], *terms: str) -> int | None:
    """Find a column index by substring, not by exact name.

    The Bundesnetzagentur changes spellings between editions (sometimes
    "Art der Ladeeinrichung" with a typo, sometimes without). A substring
    search survives that, a fixed list of names does not.
    """
    normalized = [(s or "").strip().lower() for s in header]
    for term in terms:
        for i, name in enumerate(normalized):
            if term.lower() in name:
                return i
    return None


def from_bnetza_csv(db, contents: bytes | str, country: str = "DE") -> dict:
    """Read the Bundesnetzagentur charger register.

    The file is expected as a semicolon-separated CSV, as downloaded from
    the charger map.
    """
    if isinstance(contents, bytes):
        # The file has already appeared in both encodings. utf-8-sig first,
        # because otherwise wrongly decoded umlaut garbage silently ends up
        # in the database.
        for encoding in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = contents.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("Kodierung der CSV nicht erkannt.")
    else:
        text = contents

    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    header_index = _header_row_find(rows)
    header = rows[header_index]

    i_operator = _column(header, "betreiber")
    i_street = _column(header, "straße", "strasse")
    i_house_no = _column(header, "hausnummer")
    i_zip = _column(header, "postleitzahl", "plz")
    i_city = _column(header, "ort")
    i_lat = _column(header, "breitengrad")
    i_lon = _column(header, "längengrad", "laengengrad", "ngengrad")
    i_power = _column(header, "nennleistung")
    i_count = _column(header, "anzahl der ladepunkte", "anzahl ladepunkte")
    i_as_of = _column(header, "inbetriebnahme")

    if i_lat is None or i_lon is None:
        raise ValueError("Spalten für Koordinaten nicht gefunden.")

    # The connectors come in up to four blocks: Steckertypen1 / P1 [kW] / ...
    connector_columns = []
    for number in range(1, 9):
        i_type = _column(header, f"steckertypen{number}")
        i_kw = _column(header, f"p{number} [kw]", f"p{number}[kw]")
        if i_type is not None:
            connector_columns.append((i_type, i_kw))

    def field(row, index):
        if index is None or index >= len(row):
            return ""
        return (row[index] or "").strip()

    counter = {"neu": 0, "aktualisiert": 0, "skipped": 0}

    for row in rows[header_index + 1:]:
        if not row or len(row) < 3:
            continue
        lat, lon = _number(field(row, i_lat)), _number(field(row, i_lon))
        # Coordinate 0/0 lies in the Atlantic - such rows exist in the source,
        # and without this check they show up in every corridor that happens
        # to reach near the prime meridian.
        if not (-90 < lat < 90) or not (-180 < lon < 180) or (lat == 0 and lon == 0):
            counter["skipped"] += 1
            continue

        connectors = []
        types: list[str] = []
        for i_type, i_kw in connector_columns:
            type_text = field(row, i_type)
            if not type_text:
                continue
            kw = _number(field(row, i_kw))
            detected = _type_unify(type_text)
            types.extend(detected)
            connectors.append({"kind": ", ".join(detected) or type_text,
                                "kw": kw, "raw": type_text})

        total_power = _number(field(row, i_power))
        max_kw = max([a["kw"] for a in connectors] + [0.0])
        if max_kw <= 0:
            # If the power per connector is missing, the rated power of the
            # charging facility is the best available approximation.
            max_kw = total_power

        operator = field(row, i_operator)
        street = f"{field(row, i_street)} {field(row, i_house_no)}".strip()
        foreign_id = _stable_id(operator, street, field(row, i_zip),
                               round(lat, 5), round(lon, 5))

        result = _save(db, "bnetza", foreign_id, {
            "name": f"{operator} {field(row, i_city)}".strip() or street,
            "operator": operator, "lat": lat, "lon": lon,
            "address": street, "postcode": field(row, i_zip),
            "city": field(row, i_city), "country": country,
            "connectors": connectors, "max_kw": max_kw,
            "point_count": int(_number(field(row, i_count)) or len(connectors) or 1),
            "connector_types": ",".join(sorted(set(types))),
            "as_of": field(row, i_as_of)})
        counter[result] += 1

    db.commit()
    log.info("Bundesnetzagentur import: %s", counter)
    return counter


# ---------------------------------------------------------------------------
# Open Charge Map
# ---------------------------------------------------------------------------

def _ocm_header(api_key: str) -> dict:
    return {
        # No compact=true: it makes OCM return AddressInfo.Country and
        # OperatorInfo as bare IDs instead of objects - exactly the fields
        # needed below for "country" and "operator". Without this parameter
        # the full objects come back.
        "output": "json", "key": api_key, "verbose": "false"}


def _process_ocm_entry(db, entry: dict, min_kw: float) -> str:
    """A single OCM record: parse, filter, save.

    Shared by the country and the route import, so that both use the same
    field mapping and cannot drift apart.
    """
    address = entry.get("AddressInfo") or {}
    lat, lon = address.get("Latitude"), address.get("Longitude")
    if lat is None or lon is None:
        return "uebersprungen"

    connectors, types = [], []
    for connection in (entry.get("Connections") or []):
        type_text = ((connection.get("ConnectionType") or {}).get("Title")
                    or str(connection.get("ConnectionTypeID") or ""))
        detected = _type_unify(type_text)
        types.extend(detected)
        connectors.append({"kind": ", ".join(detected) or type_text,
                            "kw": float(connection.get("PowerKW") or 0.0),
                            "count": connection.get("Quantity") or 1,
                            "raw": type_text})

    max_kw = max([a["kw"] for a in connectors] + [0.0])
    if max_kw < min_kw:
        return "uebersprungen"

    # What is stated in words - and used to be thrown away. This is exactly
    # where it says why a charge point is useless for a specific trip:
    # "nur für Hotelgäste" (hotel guests only), "hinter Schranke" (behind a
    # barrier), "Kabel zu kurz" (cable too short).
    usage = entry.get("UsageType") or {}
    state = entry.get("StatusType") or {}
    hints = {
        "cost": entry.get("UsageCost") or "",
        "general": entry.get("GeneralComments") or "",
        "access": entry.get("AccessComments") or "",
        "checked_at": (entry.get("DateLastVerified") or "")[:10],
    }
    # Do not keep empty fields at all - otherwise almost every record would
    # carry an object made of four empty strings.
    hints = {k: v for k, v in hints.items() if v}

    return _save(db, "ocm", str(entry.get("ID")), {
        "name": address.get("Title") or "",
        "operator": (entry.get("OperatorInfo") or {}).get("Title") or "",
        "lat": float(lat), "lon": float(lon),
        "address": address.get("AddressLine1") or "",
        "postcode": address.get("Postcode") or "",
        "city": address.get("Town") or "",
        "country": ((address.get("Country") or {}).get("ISOCode") or "")[:2],
        "connectors": connectors, "max_kw": max_kw,
        "point_count": entry.get("NumberOfPoints") or len(connectors) or 1,
        "connector_types": ",".join(sorted(set(types))),
        "as_of": (entry.get("DateLastStatusUpdate") or "")[:10],
        # `IsOperational` is often missing in OCM. Then it stays None -
        # "unknown" and not "broken".
        "operational": state.get("IsOperational"),
        "access": (usage.get("Title") or "")[:60] or None,
        "membership_required": usage.get("IsMembershipRequired"),
        "hints": hints or None})


def from_ocm(db, api_key: str, countries: list[str] | None = None,
            max_results: int = 5000, min_kw: float = 0.0) -> dict:
    """Fetch charge points from Open Charge Map, country by country.

    One call per country, in blocks via `maxresults`/`offset` - a request
    for a whole country would otherwise run into a timeout. `max_results`
    applies per country, not to the sum of all.

    Deliberately not a single call with comma-separated country codes: OCM
    does accept `countrycode=AT,CH,FR`, but apparently ignores the
    restriction - the response ends up scattered across the whole world, not
    limited to the requested countries (observed in practice: a request for
    AT,CH,FR,IT,NL,BE also returned sites in Brazil, Japan and Kenya).
    Asking for each country individually is the only restriction the API
    reliably honors.

    Limitation of this function: with a very large number of hits (observed
    from about 5000 hits for one country) `offset` does not page on
    reliably - later pages return the same records again instead of new
    ones. For a large country like France, part of the charge points thus
    remains unreachable, no matter how high `max_results` is set. Anyone who
    specifically wants charge points along a route (and not a whole country)
    is better served by `from_ocm_route()` below - smaller radius queries,
    which OCM answers more reliably.
    """
    if not api_key:
        raise ValueError("Kein OCM_API_KEY gesetzt.")

    counter = {"neu": 0, "aktualisiert": 0, "skipped": 0}
    block = 500

    for country in (countries or ["DE"]):
        fetched = 0
        while fetched < max_results:
            response = requests.get(OCM_API, timeout=TIMEOUT, params={
                **_ocm_header(api_key), "countrycode": country,
                "maxresults": min(block, max_results - fetched),
                "offset": fetched})
            response.raise_for_status()
            entries = response.json()
            if not entries:
                break

            for entry in entries:
                counter[_process_ocm_entry(db, entry, min_kw)] += 1

            fetched += len(entries)
            db.commit()
            if len(entries) < block:
                break

    log.info("Open Charge Map import: %s", counter)
    return counter


# How far apart two anchors along the route may be, as a multiple of the
# search radius. 1.6 instead of 2.0, so that neighboring circles overlap
# noticeably - otherwise gaps would remain at the seams, because a circle is
# narrower than the distance between two points on the route suggests.
_ANCHOR_FACTOR = 1.6


def from_ocm_route(db, api_key: str, points: list[tuple[float, float]],
                  radius_km: float = 30.0, min_kw: float = 0.0) -> dict:
    """Fetch charge points from Open Charge Map along a route.

    Instead of a country filter with `offset` pagination (see `from_ocm()`,
    unreliable there with large result sets), a radius search at several
    points along the route - the same kind of query a user makes in the OCM
    map themselves, and which the API reliably limits to the requested
    radius.

    `points` is the route geometry in driving order, in the same format as
    `Trip.geometry`: `[[lon, lat, height_m], ...]` - GeoJSON convention,
    lon before lat. `_chainage()`/`_anchor()` from `routing.corridor` expect
    exactly this format, without conversion by the caller.
    """
    if not api_key:
        raise ValueError("Kein OCM_API_KEY gesetzt.")
    if len(points) < 2:
        raise ValueError("Zu wenige Streckenpunkte für eine Umkreissuche.")

    from ..routing.corridor import _anchor, _chainage

    km_list = _chainage(points)
    anchor = _anchor(points, km_list, radius_km * _ANCHOR_FACTOR)

    counter = {"neu": 0, "aktualisiert": 0, "skipped": 0}
    seen: set[str] = set()

    for lat, lon, _km, _idx in anchor:
        response = requests.get(OCM_API, timeout=TIMEOUT, params={
            **_ocm_header(api_key), "latitude": lat, "longitude": lon,
            "distance": radius_km, "distanceunit": "KM", "maxresults": 500})
        response.raise_for_status()
        entries = response.json()

        for entry in entries:
            # Overlapping circles see the same site several times - counted
            # here instead of db.commit() per anchor, so that a site does not
            # show up in the statistics as "new multiple times".
            foreign_id = str(entry.get("ID"))
            if foreign_id in seen:
                continue
            seen.add(foreign_id)
            counter[_process_ocm_entry(db, entry, min_kw)] += 1

        db.commit()

    log.info("Open Charge Map route import: %s anchors, %s",
             len(anchor), counter)
    return counter

    log.info("Open Charge Map import: %s", counter)
    return counter
