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
STECKER_MUSTER = [
    (re.compile(r"ccs|combo|combined", re.I), "CCS"),
    (re.compile(r"chademo", re.I), "CHAdeMO"),
    (re.compile(r"typ\s*2|type\s*2|mennekes|ac steckdose", re.I), "Typ2"),
    (re.compile(r"schuko|typ\s*f", re.I), "Schuko"),
]


def _typ_vereinheitlichen(text: str) -> list[str]:
    treffer = [name for muster, name in STECKER_MUSTER if muster.search(text or "")]
    return sorted(set(treffer))


def _zahl(wert) -> float:
    """Zahl aus einem Feld, das Dezimalkomma enthalten kann.

    Die amtliche CSV benutzt durchgängig das deutsche Komma; ein naives
    float() liefert dort 0 und würde jede Leistungsangabe verschlucken.
    """
    if wert is None:
        return 0.0
    if isinstance(wert, (int, float)):
        return float(wert)
    text = str(wert).strip().replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return 0.0


def _stabile_id(*teile) -> str:
    """Eine reproduzierbare ID aus den Feldern bilden.

    Die CSV der Bundesnetzagentur hat keinen Primärschlüssel. Ohne eine aus
    dem Inhalt abgeleitete ID würde jeder Import dieselben Säulen erneut
    anlegen. Grundlage sind Betreiber, Adresse und die auf fünf Nachkomma-
    stellen (rund einen Meter) gerundeten Koordinaten - fein genug, um zwei
    Standorte zu trennen, grob genug, um Rundungsrauschen zu überstehen.
    """
    roh = "|".join(str(t).strip().lower() for t in teile)
    return hashlib.sha1(roh.encode("utf-8")).hexdigest()[:24]


def _laengen_kuerzen(felder: dict) -> dict:
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
    gekuerzt = {}
    for name, wert in felder.items():
        spalte = models.Ladepunkt.__table__.columns.get(name)
        laenge = getattr(getattr(spalte, "type", None), "length", None)
        if isinstance(wert, str) and laenge and len(wert) > laenge:
            log.info("Feld %s auf %d Zeichen gekürzt: %r", name, laenge, wert)
            wert = wert[:laenge]
        gekuerzt[name] = wert
    return gekuerzt


def _speichern(db, quelle: str, fremd_id: str, felder: dict) -> str:
    felder = _laengen_kuerzen(felder)
    vorhanden = (db.query(models.Ladepunkt)
                 .filter_by(quelle=quelle, fremd_id=fremd_id).one_or_none())
    if vorhanden:
        for schluessel, wert in felder.items():
            setattr(vorhanden, schluessel, wert)
        return "aktualisiert"
    db.add(models.Ladepunkt(quelle=quelle, fremd_id=fremd_id, **felder))
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

def _kopfzeile_finden(zeilen: list[list[str]]) -> int:
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
    for i, zeile in enumerate(zeilen[:60]):
        verbunden = " ".join(zeile).lower()
        if "breitengrad" in verbunden and "ngengrad" in verbunden:
            return i
    raise ValueError("Kopfzeile mit 'Breitengrad'/'Längengrad' nicht gefunden - "
                     "ist das wirklich das Ladesäulenregister?")


def _spalte(kopf: list[str], *begriffe: str) -> int | None:
    """Spaltenindex über Teilstrings suchen, nicht über exakte Namen.

    Die Bundesnetzagentur ändert Schreibweisen zwischen den Ausgaben (mal
    "Art der Ladeeinrichung" mit Tippfehler, mal ohne). Eine Suche über
    Teilstrings überlebt das, eine feste Namensliste nicht.
    """
    normiert = [(s or "").strip().lower() for s in kopf]
    for begriff in begriffe:
        for i, name in enumerate(normiert):
            if begriff.lower() in name:
                return i
    return None


def aus_bnetza_csv(db, inhalt: bytes | str, land: str = "DE") -> dict:
    """Das Ladesäulenregister der Bundesnetzagentur einlesen.

    Die Datei wird als CSV mit Semikolon erwartet, so wie sie von der
    Ladesäulenkarte heruntergeladen wird.
    """
    if isinstance(inhalt, bytes):
        # Die Datei kam schon in beiden Kodierungen vor. utf-8-sig zuerst,
        # weil ein falsch dekodiertes Umlaut-Chaos sonst unbemerkt in die
        # Datenbank wandert.
        for kodierung in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = inhalt.decode(kodierung)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("Kodierung der CSV nicht erkannt.")
    else:
        text = inhalt

    zeilen = list(csv.reader(io.StringIO(text), delimiter=";"))
    kopf_index = _kopfzeile_finden(zeilen)
    kopf = zeilen[kopf_index]

    i_betreiber = _spalte(kopf, "betreiber")
    i_strasse = _spalte(kopf, "straße", "strasse")
    i_hausnr = _spalte(kopf, "hausnummer")
    i_plz = _spalte(kopf, "postleitzahl", "plz")
    i_ort = _spalte(kopf, "ort")
    i_lat = _spalte(kopf, "breitengrad")
    i_lon = _spalte(kopf, "längengrad", "laengengrad", "ngengrad")
    i_leistung = _spalte(kopf, "nennleistung")
    i_anzahl = _spalte(kopf, "anzahl der ladepunkte", "anzahl ladepunkte")
    i_stand = _spalte(kopf, "inbetriebnahme")

    if i_lat is None or i_lon is None:
        raise ValueError("Spalten für Koordinaten nicht gefunden.")

    # Die Stecker stehen in bis zu vier Blöcken: Steckertypen1 / P1 [kW] / ...
    stecker_spalten = []
    for nummer in range(1, 9):
        i_typ = _spalte(kopf, f"steckertypen{nummer}")
        i_kw = _spalte(kopf, f"p{nummer} [kw]", f"p{nummer}[kw]")
        if i_typ is not None:
            stecker_spalten.append((i_typ, i_kw))

    def feld(zeile, index):
        if index is None or index >= len(zeile):
            return ""
        return (zeile[index] or "").strip()

    zaehler = {"neu": 0, "aktualisiert": 0, "uebersprungen": 0}

    for zeile in zeilen[kopf_index + 1:]:
        if not zeile or len(zeile) < 3:
            continue
        lat, lon = _zahl(feld(zeile, i_lat)), _zahl(feld(zeile, i_lon))
        # Koordinate 0/0 liegt im Atlantik - solche Zeilen gibt es in der
        # Quelle, und ohne diese Prüfung tauchen sie in jedem Korridor auf,
        # der zufällig in die Nähe des Nullmeridians reicht.
        if not (-90 < lat < 90) or not (-180 < lon < 180) or (lat == 0 and lon == 0):
            zaehler["uebersprungen"] += 1
            continue

        anschluesse = []
        typen: list[str] = []
        for i_typ, i_kw in stecker_spalten:
            typ_text = feld(zeile, i_typ)
            if not typ_text:
                continue
            kw = _zahl(feld(zeile, i_kw))
            erkannt = _typ_vereinheitlichen(typ_text)
            typen.extend(erkannt)
            anschluesse.append({"typ": ", ".join(erkannt) or typ_text,
                                "kw": kw, "roh": typ_text})

        gesamtleistung = _zahl(feld(zeile, i_leistung))
        max_kw = max([a["kw"] for a in anschluesse] + [0.0])
        if max_kw <= 0:
            # Fehlt die Leistung je Stecker, ist die Nennleistung der
            # Ladeeinrichtung die beste verfügbare Näherung.
            max_kw = gesamtleistung

        betreiber = feld(zeile, i_betreiber)
        strasse = f"{feld(zeile, i_strasse)} {feld(zeile, i_hausnr)}".strip()
        fremd_id = _stabile_id(betreiber, strasse, feld(zeile, i_plz),
                               round(lat, 5), round(lon, 5))

        ergebnis = _speichern(db, "bnetza", fremd_id, {
            "name": f"{betreiber} {feld(zeile, i_ort)}".strip() or strasse,
            "betreiber": betreiber, "lat": lat, "lon": lon,
            "adresse": strasse, "plz": feld(zeile, i_plz),
            "ort": feld(zeile, i_ort), "land": land,
            "anschluesse": anschluesse, "max_kw": max_kw,
            "anzahl_punkte": int(_zahl(feld(zeile, i_anzahl)) or len(anschluesse) or 1),
            "steckertypen": ",".join(sorted(set(typen))),
            "stand": feld(zeile, i_stand)})
        zaehler[ergebnis] += 1

    db.commit()
    log.info("Bundesnetzagentur-Import: %s", zaehler)
    return zaehler


# ---------------------------------------------------------------------------
# Open Charge Map
# ---------------------------------------------------------------------------

def _ocm_kopf(api_key: str) -> dict:
    return {
        # Kein compact=true: Das lässt OCM AddressInfo.Country und
        # OperatorInfo als blosse IDs statt als Objekte liefern - genau die
        # Felder, die unten für "land" und "betreiber" gebraucht werden. Ohne
        # diesen Parameter kommen die vollen Objekte.
        "output": "json", "key": api_key, "verbose": "false"}


def _ocm_eintrag_verarbeiten(db, eintrag: dict, min_kw: float) -> str:
    """Ein einzelner OCM-Datensatz: parsen, filtern, speichern.

    Gemeinsam für den Länder- und den Strecken-Import, damit beide dieselbe
    Feldzuordnung verwenden und nicht auseinanderlaufen können.
    """
    adresse = eintrag.get("AddressInfo") or {}
    lat, lon = adresse.get("Latitude"), adresse.get("Longitude")
    if lat is None or lon is None:
        return "uebersprungen"

    anschluesse, typen = [], []
    for verbindung in (eintrag.get("Connections") or []):
        typ_text = ((verbindung.get("ConnectionType") or {}).get("Title")
                    or str(verbindung.get("ConnectionTypeID") or ""))
        erkannt = _typ_vereinheitlichen(typ_text)
        typen.extend(erkannt)
        anschluesse.append({"typ": ", ".join(erkannt) or typ_text,
                            "kw": float(verbindung.get("PowerKW") or 0.0),
                            "anzahl": verbindung.get("Quantity") or 1,
                            "roh": typ_text})

    max_kw = max([a["kw"] for a in anschluesse] + [0.0])
    if max_kw < min_kw:
        return "uebersprungen"

    # Was in Worten dasteht - und bisher weggeworfen wurde. Genau hier
    # steht, warum ein Ladepunkt für eine konkrete Fahrt nichts taugt:
    # "nur für Hotelgäste", "hinter Schranke", "Kabel zu kurz".
    nutzung = eintrag.get("UsageType") or {}
    zustand = eintrag.get("StatusType") or {}
    hinweise = {
        "kosten": eintrag.get("UsageCost") or "",
        "allgemein": eintrag.get("GeneralComments") or "",
        "zugang": eintrag.get("AccessComments") or "",
        "geprueft_am": (eintrag.get("DateLastVerified") or "")[:10],
    }
    # Leere Felder gar nicht erst aufheben - sonst steht in fast jedem
    # Datensatz ein Objekt aus vier leeren Zeichenketten.
    hinweise = {k: v for k, v in hinweise.items() if v}

    return _speichern(db, "ocm", str(eintrag.get("ID")), {
        "name": adresse.get("Title") or "",
        "betreiber": (eintrag.get("OperatorInfo") or {}).get("Title") or "",
        "lat": float(lat), "lon": float(lon),
        "adresse": adresse.get("AddressLine1") or "",
        "plz": adresse.get("Postcode") or "",
        "ort": adresse.get("Town") or "",
        "land": ((adresse.get("Country") or {}).get("ISOCode") or "")[:2],
        "anschluesse": anschluesse, "max_kw": max_kw,
        "anzahl_punkte": eintrag.get("NumberOfPoints") or len(anschluesse) or 1,
        "steckertypen": ",".join(sorted(set(typen))),
        "stand": (eintrag.get("DateLastStatusUpdate") or "")[:10],
        # `IsOperational` fehlt bei OCM häufig. Dann bleibt es None -
        # "unbekannt" und nicht "kaputt".
        "betriebsbereit": zustand.get("IsOperational"),
        "zugang": (nutzung.get("Title") or "")[:60] or None,
        "mitgliedschaft_noetig": nutzung.get("IsMembershipRequired"),
        "hinweise": hinweise or None})


def aus_ocm(db, api_key: str, laender: list[str] | None = None,
            max_ergebnisse: int = 5000, min_kw: float = 0.0) -> dict:
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

    zaehler = {"neu": 0, "aktualisiert": 0, "uebersprungen": 0}
    block = 500

    for land in (laender or ["DE"]):
        geholt = 0
        while geholt < max_ergebnisse:
            antwort = requests.get(OCM_API, timeout=TIMEOUT, params={
                **_ocm_kopf(api_key), "countrycode": land,
                "maxresults": min(block, max_ergebnisse - geholt),
                "offset": geholt})
            antwort.raise_for_status()
            eintraege = antwort.json()
            if not eintraege:
                break

            for eintrag in eintraege:
                zaehler[_ocm_eintrag_verarbeiten(db, eintrag, min_kw)] += 1

            geholt += len(eintraege)
            db.commit()
            if len(eintraege) < block:
                break

    log.info("Open-Charge-Map-Import: %s", zaehler)
    return zaehler


# Wie weit zwei Anker entlang der Route auseinanderliegen dürfen, als
# Vielfaches des Umkreis-Radius. 1,6 statt 2,0, damit sich benachbarte Kreise
# spürbar überlappen - sonst blieben an den Nahtstellen Lücken, weil ein Kreis
# schmaler ist als der Abstand zwischen zwei Punkten auf der Route suggeriert.
_ANKER_FAKTOR = 1.6


def aus_ocm_route(db, api_key: str, punkte: list[tuple[float, float]],
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
    if len(punkte) < 2:
        raise ValueError("Zu wenige Streckenpunkte für eine Umkreissuche.")

    from ..routing.korridor import _anker, _kilometrierung

    km_liste = _kilometrierung(punkte)
    anker = _anker(punkte, km_liste, radius_km * _ANKER_FAKTOR)

    zaehler = {"neu": 0, "aktualisiert": 0, "uebersprungen": 0}
    gesehen: set[str] = set()

    for lat, lon, _km, _idx in anker:
        antwort = requests.get(OCM_API, timeout=TIMEOUT, params={
            **_ocm_kopf(api_key), "latitude": lat, "longitude": lon,
            "distance": radius_km, "distanceunit": "KM", "maxresults": 500})
        antwort.raise_for_status()
        eintraege = antwort.json()

        for eintrag in eintraege:
            # Überlappende Kreise sehen denselben Standort mehrfach - hier
            # gezählt statt db.commit() je Anker, damit ein Standort in der
            # Statistik nicht als "mehrfach neu" auftaucht.
            fremd_id = str(eintrag.get("ID"))
            if fremd_id in gesehen:
                continue
            gesehen.add(fremd_id)
            zaehler[_ocm_eintrag_verarbeiten(db, eintrag, min_kw)] += 1

        db.commit()

    log.info("Open-Charge-Map-Streckenimport: %s Anker, %s",
             len(anker), zaehler)
    return zaehler

    log.info("Open-Charge-Map-Import: %s", zaehler)
    return zaehler
