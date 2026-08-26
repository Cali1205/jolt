"""Eine gefahrene Strecke nachträglich zu einer Fahrt machen.

Der umgekehrte Weg zur Planung: Dort steht die Route vorher fest und die
Fahrt wird dagegen gehalten; hier wird gefahren, mitgeschrieben, und die
Route entsteht hinterher aus dem, was das Telefon aufgezeichnet hat.

**Wozu.** Der Korrekturfaktor eines Fahrzeugs lernt aus dem Vergleich von
Prognose und Wirklichkeit. Dafür eine Route planen zu müssen, ist für den
naheliegendsten Fall zu umständlich - eine bekannte kurze Strecke, immer
dieselbe, ein paarmal gefahren, ist die sauberste Messung überhaupt: kein
Ladestopp, gleiche Bedingungen, wiederholbar.

**Warum es überhaupt eine Rekonstruktion braucht.** Eine Verbrauchsmessung
ohne Höhenprofil ist nicht deutbar. Ob 22 kWh/100 km am Fahrstil lagen oder
an vierhundert Höhenmetern, lässt sich aus dem Verbrauch allein nicht
trennen - und wer es trotzdem in den Korrekturfaktor schreibt, bringt dem
Fahrzeug den Hügel bei, über den er zufällig gefahren ist.

Die Höhe kommt deshalb aus Kartendaten (openrouteservice), nicht aus dem
GPS: Dessen Höhenangabe streut um zehn bis zwanzig Meter, und wer solche
Differenzen aufsummiert, erhält für eine Fahrt durch die Ebene mehrere
hundert Meter Steigung. Für die *Position* ist GPS genau genug, für die
Höhe nicht. Die GPS-Höhe wird trotzdem mitgeschrieben - als Rückfall, wenn
kein Schlüssel vorliegt, und weil sie nichts kostet.

**Und das Tempo?** Das ist der Gewinn dieser Betriebsart: Es wird nicht
angenommen, sondern aus den Zeitstempeln der Messpunkte gerechnet. Eine
aufgezeichnete Fahrt kennt ihre Geschwindigkeit je Teilstück genau - die
geplante muss sie schätzen.
"""
import logging

from .. import models, routing
from ..energie import modell, wetter
from ..energie.modell import Fahrzeugwerte, Umgebung, haversine_m
from ..routing.korridor import punkt_auf_route

log = logging.getLogger("uvicorn.error")

# Höchstzahl der Stützpunkte für die Höhenabfrage. openrouteservice nimmt
# nicht beliebig viele, und feiner als rund alle hundert Meter bringt das
# Höhenprofil ohnehin nichts.
HOECHSTENS_STUETZPUNKTE = 1800

# Punkte, die enger beieinanderliegen, werden zusammengefasst. Ein stehendes
# Auto liefert sonst hunderte Punkte auf demselben Fleck, und die verzerren
# jede Geschwindigkeit, die daraus gerechnet wird.
MINDESTABSTAND_M = 25.0


def strecke_bauen(punkte: list) -> list:
    """Aus den Messpunkten eine Geometrie [[lon, lat], ...].

    Zusammengefasst wird alles, was enger als `MINDESTABSTAND_M` liegt: An
    einer Ampel oder an der Säule stehen sonst dutzende Punkte übereinander,
    aus denen sich eine Geschwindigkeit von null und ein Teilstück der Länge
    null ergäbe - beides bringt die Rechnung dahinter durcheinander.
    """
    gebaut: list = []
    letzter = None
    for punkt in punkte:
        if punkt.lat is None or punkt.lon is None:
            continue
        if letzter is not None:
            abstand = haversine_m(letzter.lat, letzter.lon, punkt.lat, punkt.lon)
            if abstand < MINDESTABSTAND_M:
                continue
        gebaut.append(punkt)
        letzter = punkt
    return gebaut


def _ausduennen(punkte: list, hoechstens: int) -> list:
    if len(punkte) <= hoechstens:
        return punkte
    schritt = len(punkte) / hoechstens
    gewaehlt = [punkte[int(i * schritt)] for i in range(hoechstens)]
    # Der letzte Punkt muss dabei sein - sonst endet die rekonstruierte
    # Strecke vor dem Ziel und die Bilanz stimmt nicht.
    if gewaehlt[-1] is not punkte[-1]:
        gewaehlt.append(punkte[-1])
    return gewaehlt


def hoehen_ergaenzen(geometrie: list, gps_hoehen: list | None = None) -> list:
    """[[lon, lat], ...] zu [[lon, lat, hoehe], ...] machen.

    Erste Wahl sind Kartendaten. Fällt die Abfrage aus, gilt die GPS-Höhe,
    und wenn auch die fehlt, wird flach gerechnet. Der Reihe nach ist das
    absteigend genau, aber jede Stufe ist besser als keine Fahrt.

    Eine flach gerechnete Strecke ist ausdrücklich **kein** Beinbruch für
    die Kalibrierung, solange Start und Ziel gleich hoch liegen - über eine
    geschlossene Runde hebt sich die Höhe ohnehin auf. Für eine Fahrt ins
    Gebirge taugt sie nicht, und das steht dann auch im Log.
    """
    try:
        mit_hoehe = routing.provider().hoehen(geometrie)
        if mit_hoehe:
            return mit_hoehe
    except Exception as fehler:      # noqa: BLE001
        log.warning("Höhenabfrage fehlgeschlagen: %s", fehler)

    if gps_hoehen and len(gps_hoehen) == len(geometrie):
        log.info("Höhen aus dem GPS - ungenauer als Kartendaten.")
        return [[lon, lat, hoehe if hoehe is not None else 0.0]
                for (lon, lat), hoehe in zip(geometrie, gps_hoehen)]

    log.warning("Keine Höhendaten - die Strecke wird flach gerechnet.")
    return [[lon, lat, 0.0] for lon, lat in geometrie]


def tempo_je_teilstueck(punkte: list) -> list:
    """Gefahrene Geschwindigkeit in m/s je Teilstück, aus den Zeitstempeln.

    Der eigentliche Vorzug einer Aufzeichnung: Die geplante Fahrt muss das
    Tempo annehmen, die gefahrene weiss es. Zeitsprünge und Standzeiten
    ergeben absurde Werte, deshalb die Schranken - unter 2 m/s rechnet das
    Modell ohnehin mit seinem eigenen Mindestwert, über 70 m/s (252 km/h)
    war es kein Auto, sondern eine kaputte Uhr.
    """
    tempi = []
    for vorher, nachher in zip(punkte, punkte[1:]):
        strecke = haversine_m(vorher.lat, vorher.lon, nachher.lat, nachher.lon)
        dauer = 0.0
        if vorher.zeit and nachher.zeit:
            dauer = (nachher.zeit - vorher.zeit).total_seconds()
        tempi.append(min(70.0, max(2.0, strecke / dauer)) if dauer > 0 else 25.0)
    return tempi


def umgebung_bestimmen(punkte: list, geometrie: list):
    """Das Wetter der Fahrt - gemessen, wenn es gemessen wurde.

    Ein Logger am OBD2-Anschluss liefert die Aussentemperatur des Fahrzeugs.
    Die ist jeder Vorhersage überlegen: Sie stammt von der Strecke, zur
    richtigen Zeit, und sie ist der grösste Einzelposten der Kälte. Nur wenn
    keine mitkam, wird nachgefragt - und dann liefert der Wetterdienst das
    Wetter von *jetzt*, nicht das von der Fahrt.
    """
    gemessen = [p.aussentemp_c for p in punkte if p.aussentemp_c is not None]
    if gemessen:
        mittel = sum(gemessen) / len(gemessen)
        log.info("Aufzeichnung: gemessene Aussentemperatur %.1f °C", mittel)
        return lambda lat, lon: Umgebung(temp_c=mittel), mittel

    hole = wetter.entlang_route(geometrie)
    mittel = wetter.mittelwert(geometrie).temp_c
    return hole, mittel


def abschliessen(db, fahrt: models.Fahrt, sitzung: models.LiveSitzung) -> dict:
    """Aus den Messpunkten einer Sitzung Geometrie und Energieprofil bauen.

    Danach ist die Aufzeichnung eine Fahrt wie jede andere: Sie hat eine
    Strecke, ein Höhenprofil und eine Prognose, gegen die sich der gemessene
    Verbrauch halten lässt. Erst dadurch kann `energie/kalibrierung.py`
    überhaupt etwas lernen - es vergleicht `soll_soc` mit `soc`, und beides
    steht erst jetzt fest.
    """
    roh = strecke_bauen(list(sitzung.punkte))
    if len(roh) < 2:
        return {"ok": False, "grund": "Zu wenige Messpunkte für eine Strecke."}

    gewaehlt = _ausduennen(roh, HOECHSTENS_STUETZPUNKTE)
    flach = [[p.lon, p.lat] for p in gewaehlt]
    gps_hoehen = [(p.rohwerte or {}).get("hoehe_m") for p in gewaehlt]
    geometrie = hoehen_ergaenzen(flach, gps_hoehen)

    fahrzeug = fahrt.fahrzeug
    hole_umgebung, mittel_temp = umgebung_bestimmen(gewaehlt, flach)

    profil = modell.profil_rechnen(
        Fahrzeugwerte.aus_modell(fahrzeug), geometrie,
        tempo_je_teilstueck(gewaehlt),
        start_soc=gewaehlt[0].soc if gewaehlt[0].soc is not None else 100.0,
        umgebung_fuer=hole_umgebung)
    if len(profil.punkte) < 2:
        return {"ok": False, "grund": "Aus der Strecke entstand kein Profil."}

    fahrt.geometrie = geometrie
    fahrt.energieprofil = [p.als_dict() for p in profil.punkte]
    fahrt.strecke_m = profil.strecke_km * 1000.0
    fahrt.fahrzeit_s = profil.minuten * 60.0
    fahrt.aussentemp_c = round(mittel_temp, 1)
    fahrt.start_lat, fahrt.start_lon = gewaehlt[0].lat, gewaehlt[0].lon
    fahrt.ziel_lat, fahrt.ziel_lon = gewaehlt[-1].lat, gewaehlt[-1].lon
    if gewaehlt[0].soc is not None:
        fahrt.start_soc = gewaehlt[0].soc

    # Die Messpunkte tragen bisher weder Kilometerstand noch Sollwert - beim
    # Eintreffen gab es ja keine Strecke, auf die man sie hätte legen können.
    # Ohne das findet die Kalibrierung nichts Verwertbares.
    for punkt in sitzung.punkte:
        km, _ = punkt_auf_route(geometrie, punkt.lat, punkt.lon)
        punkt.km_auf_route = km
        punkt.soll_soc = _soll_bei(fahrt.energieprofil, km)

    db.flush()
    return {"ok": True, "strecke_km": round(profil.strecke_km, 1),
            "fahrzeit_minuten": round(profil.minuten),
            "verbrauch_kwh": round(profil.kwh_gesamt, 2),
            "aussentemp_c": fahrt.aussentemp_c,
            "hoehen": "karte" if any(p[2] for p in geometrie) else "flach"}


def _soll_bei(energieprofil: list, km: float):
    # Bewusst hier und nicht über energie/profil.py: Das Profil ist gerade
    # erst entstanden und liegt als Liste von dicts vor, nicht am Fahrt-Objekt.
    from ..energie.profil import soc_bei
    return soc_bei(energieprofil, km)
