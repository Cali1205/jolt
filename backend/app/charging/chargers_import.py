"""Ladesäulen-Stammdaten importieren.

Zwei Quellen mit unterschiedlichem Zweck:

- **Bundesnetzagentur** (CSV): amtlich und für Deutschland vollständig, weil
  die Meldung gesetzlich vorgeschrieben ist. Kein Schlüssel nötig. Das ist die
  Basis.
- **Open Charge Map** (API): weltweit, crowdgepflegt, mit freiem Schlüssel.
  Für Fahrten über die Grenze und als Ergänzung.

Beide Importe sind **idempotent**: Sie schreiben über `(quelle, fremd_id)` und
lassen sich beliebig oft wiederholen. Das ist keine Feinheit, sondern die
Voraussetzung dafür, dass man den Import in einen Cronjob hängen kann, ohne
die Tabelle zu verdoppeln.
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

# Die Steckertyp-Bezeichnungen der Quellen auf die drei Begriffe abbilden, mit
# denen jolt filtert. Ohne die Vereinheitlichung würde die Korridor-Suche je
# nach Importquelle andere Treffer liefern.
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
    """Zahl aus einem Feld, das Dezimalkomma enthalten kann.

    Die amtliche CSV benutzt durchgängig das deutsche Komma; ein naives
    float() liefert dort 0 und würde jede Leistungsangabe verschlucken.
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
    """Eine reproduzierbare ID aus den Feldern bilden.

    Die CSV der Bundesnetzagentur hat keinen Primärschlüssel. Ohne eine aus
    dem Inhalt abgeleitete ID würde jeder Import dieselben Säulen erneut
    anlegen. Grundlage sind Betreiber, Adresse und die auf fünf Nachkomma-
    stellen (rund einen Meter) gerundeten Koordinaten - fein genug, um zwei
    Standorte zu trennen, grob genug, um Rundungsrauschen zu überstehen.
    """
    raw = "|".join(str(t).strip().lower() for t in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _lengths_shorten(fields: dict) -> dict:
    """Zeichenketten auf die Spaltenbreite stutzen.

    Fremde Daten halten sich nicht an unsere Spalten. Ein einziger zu langer
    Wert liess bisher den **ganzen** Import auflaufen - tausende Datensaetze
    verloren wegen eines einzigen. Passiert ist genau das schon: OCM liefert
    fuer Standorte mit mehreren Postleitzahlen (grosse Einkaufszentren) eine
    Semikolon-Liste, und "33000;33100;33200;33300;33800" beim Auchan Bordeaux
    Lac hat den Lauf abgebrochen. Die Spalte wurde daraufhin von 20 auf 40
    Zeichen verbreitert - was denselben Fehler nur weiter hinausschiebt, denn
    der naechste Standort hat sechs Postleitzahlen.

    Gestutzt statt uebersprungen: Eine abgeschnittene Postleitzahl ist ein
    Schoenheitsfehler, ein fehlender Ladepunkt auf der Route nicht. Die
    Laengen kommen aus dem Modell, damit die Liste nicht neben den Spalten
    veraltet.
    """
    shortened = {}
    for name, val in fields.items():
        column = models.ChargePoint.__table__.columns.get(name)
        len_total = getattr(getattr(column, "type", None), "length", None)
        if isinstance(val, str) and len_total and len(val) > len_total:
            log.info("Feld %s auf %d Zeichen gekürzt: %r", name, len_total, val)
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
    # Ohne dieses Flush sieht die obige Suche einen soeben in derselben, noch
    # nicht committeten Charge hinzugefügten Datensatz nicht (Session läuft
    # mit autoflush=False). Kommt derselbe fremd_id innerhalb eines Imports
    # zweimal vor - z.B. wenn eine Mehrländer-Abfrage bei Open Charge Map
    # einen Standort nahe der Grenze doppelt liefert -, hält die zweite Suche
    # ihn fälschlich für neu, und der zweite INSERT verletzt die
    # Unique-Constraint (quelle, fremd_id).
    db.flush()
    return "neu"


# ---------------------------------------------------------------------------
# Bundesnetzagentur
# ---------------------------------------------------------------------------

def _header_row_find(rows: list[list[str]]) -> int:
    """Die eigentliche Spaltenüberschrift suchen.

    Die amtliche Datei beginnt mit einem Vorspann aus Titel, Stand und
    Erläuterungen - je nach Ausgabe unterschiedlich lang. Statt eine feste
    Zeilenzahl zu überspringen (die beim nächsten Update nicht mehr stimmt),
    wird die Zeile gesucht, in der "Breitengrad" steht.

    Beim Längengrad wird nur auf "ngengrad" geprüft. Der Grund ist das "ä":
    Wird die Datei mit der falschen Kodierung gelesen, steht dort "LÃ¤ngengrad"
    - und die Suche nach dem korrekt geschriebenen Wort schlüge fehl. Der
    Abbruch käme dann mit der Meldung "ist das wirklich das Register?", obwohl
    das Problem ganz woanders liegt. Das Wortende trägt dieselbe Information
    und übersteht jede Kodierung.
    """
    for i, row in enumerate(rows[:60]):
        linked = " ".join(row).lower()
        if "breitengrad" in linked and "ngengrad" in linked:
            return i
    raise ValueError("Kopfzeile mit 'Breitengrad'/'Längengrad' nicht gefunden - "
                     "ist das wirklich das Ladesäulenregister?")


def _column(header: list[str], *terms: str) -> int | None:
    """Spaltenindex über Teilstrings suchen, nicht über exakte Namen.

    Die Bundesnetzagentur ändert Schreibweisen zwischen den Ausgaben (mal
    "Art der Ladeeinrichung" mit Tippfehler, mal ohne). Eine Suche über
    Teilstrings überlebt das, eine feste Namensliste nicht.
    """
    normalized = [(s or "").strip().lower() for s in header]
    for term in terms:
        for i, name in enumerate(normalized):
            if term.lower() in name:
                return i
    return None


def from_bnetza_csv(db, contents: bytes | str, country: str = "DE") -> dict:
    """Das Ladesäulenregister der Bundesnetzagentur einlesen.

    Die Datei wird als CSV mit Semikolon erwartet, so wie sie von der
    Ladesäulenkarte heruntergeladen wird.
    """
    if isinstance(contents, bytes):
        # Die Datei kam schon in beiden Kodierungen vor. utf-8-sig zuerst,
        # weil ein falsch dekodiertes Umlaut-Chaos sonst unbemerkt in die
        # Datenbank wandert.
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

    # Die Stecker stehen in bis zu vier Blöcken: Steckertypen1 / P1 [kW] / ...
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
        # Koordinate 0/0 liegt im Atlantik - solche Zeilen gibt es in der
        # Quelle, und ohne diese Prüfung tauchen sie in jedem Korridor auf,
        # der zufällig in die Nähe des Nullmeridians reicht.
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
            # Fehlt die Leistung je Stecker, ist die Nennleistung der
            # Ladeeinrichtung die beste verfügbare Näherung.
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
    log.info("Bundesnetzagentur-Import: %s", counter)
    return counter


# ---------------------------------------------------------------------------
# Open Charge Map
# ---------------------------------------------------------------------------

def _ocm_header(api_key: str) -> dict:
    return {
        # Kein compact=true: Das lässt OCM AddressInfo.Country und
        # OperatorInfo als blosse IDs statt als Objekte liefern - genau die
        # Felder, die unten für "land" und "betreiber" gebraucht werden. Ohne
        # diesen Parameter kommen die vollen Objekte.
        "output": "json", "key": api_key, "verbose": "false"}


def _process_ocm_entry(db, entry: dict, min_kw: float) -> str:
    """Ein einzelner OCM-Datensatz: parsen, filtern, speichern.

    Gemeinsam für den Länder- und den Strecken-Import, damit beide dieselbe
    Feldzuordnung verwenden und nicht auseinanderlaufen können.
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

    # Was in Worten dasteht - und bisher weggeworfen wurde. Genau hier
    # steht, warum ein Ladepunkt für eine konkrete Fahrt nichts taugt:
    # "nur für Hotelgäste", "hinter Schranke", "Kabel zu kurz".
    usage = entry.get("UsageType") or {}
    state = entry.get("StatusType") or {}
    hints = {
        "cost": entry.get("UsageCost") or "",
        "general": entry.get("GeneralComments") or "",
        "access": entry.get("AccessComments") or "",
        "checked_at": (entry.get("DateLastVerified") or "")[:10],
    }
    # Leere Felder gar nicht erst aufheben - sonst steht in fast jedem
    # Datensatz ein Objekt aus vier leeren Zeichenketten.
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
        # `IsOperational` fehlt bei OCM häufig. Dann bleibt es None -
        # "unbekannt" und nicht "kaputt".
        "operational": state.get("IsOperational"),
        "access": (usage.get("Title") or "")[:60] or None,
        "membership_required": usage.get("IsMembershipRequired"),
        "hints": hints or None})


def from_ocm(db, api_key: str, countries: list[str] | None = None,
            max_results: int = 5000, min_kw: float = 0.0) -> dict:
    """Ladepunkte von Open Charge Map holen, ländergebunden.

    Ein Aufruf je Land, blockweise über `maxresults`/`offset` - eine Anfrage
    über ein ganzes Land liefe sonst in ein Zeitlimit. `max_ergebnisse` gilt
    pro Land, nicht für die Summe aller.

    Bewusst kein einziger Aufruf mit kommagetrennten Ländercodes: OCM nimmt
    `countrycode=AT,CH,FR` zwar entgegen, ignoriert die Einschränkung dabei
    aber offenbar - die Antwort landet querbeet über die ganze Welt verstreut,
    nicht auf die angefragten Länder begrenzt (beobachtet in der Praxis: eine
    Anfrage für AT,CH,FR,IT,NL,BE lieferte auch Standorte in Brasilien, Japan
    und Kenia). Je Land einzeln zu fragen ist die einzige Einschränkung, die
    die API zuverlässig einhält.

    Einschränkung dieser Funktion: `offset` blättert bei einer sehr grossen
    Trefferzahl (beobachtet ab ca. 5000 Treffern für ein Land) nicht
    zuverlässig weiter - spätere Seiten liefern dieselben Datensätze erneut
    statt neuer. Für ein grosses Land wie Frankreich bleibt so ein Teil der
    Ladepunkte unerreichbar, egal wie hoch `max_ergebnisse` steht. Wer gezielt
    Ladepunkte entlang einer Strecke will (und nicht ein ganzes Land), ist mit
    `aus_ocm_route()` unten besser bedient - kleinere Umkreis-Anfragen, die
    OCM zuverlässiger beantwortet.
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

    log.info("Open-Charge-Map-Import: %s", counter)
    return counter


# Wie weit zwei Anker entlang der Route auseinanderliegen dürfen, als
# Vielfaches des Umkreis-Radius. 1,6 statt 2,0, damit sich benachbarte Kreise
# spürbar überlappen - sonst blieben an den Nahtstellen Lücken, weil ein Kreis
# schmaler ist als der Abstand zwischen zwei Punkten auf der Route suggeriert.
_ANCHOR_FACTOR = 1.6


def from_ocm_route(db, api_key: str, points: list[tuple[float, float]],
                  radius_km: float = 30.0, min_kw: float = 0.0) -> dict:
    """Ladepunkte von Open Charge Map entlang einer Strecke holen.

    Statt eines Länderfilters mit `offset`-Pagination (siehe `aus_ocm()`,
    dort unzuverlässig bei grossen Treffermengen) eine Umkreissuche an mehreren
    Punkten entlang der Route - dieselbe Art Anfrage, mit der ein Nutzer in der
    OCM-Karte selbst sucht, und die die API zuverlässig auf den angefragten
    Umkreis begrenzt.

    `punkte` ist die Routen-Geometrie in Fahrtreihenfolge, im selben Format
    wie `Fahrt.geometrie`: `[[lon, lat, höhe_m], ...]` - GeoJSON-Konvention,
    lon vor lat. `_kilometrierung()`/`_anker()` aus `routing.korridor`
    erwarten genau dieses Format, ohne Umrechnung durch den Aufrufer.
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
            # Überlappende Kreise sehen denselben Standort mehrfach - hier
            # gezählt statt db.commit() je Anker, damit ein Standort in der
            # Statistik nicht als "mehrfach neu" auftaucht.
            foreign_id = str(entry.get("ID"))
            if foreign_id in seen:
                continue
            seen.add(foreign_id)
            counter[_process_ocm_entry(db, entry, min_kw)] += 1

        db.commit()

    log.info("Open-Charge-Map-Streckenimport: %s Anker, %s",
             len(anchor), counter)
    return counter

    log.info("Open-Charge-Map-Import: %s", counter)
    return counter
