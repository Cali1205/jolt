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
from ..energie.modell import Fahrzeugwerte, Umgebung
from ..geo import haversine_m
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

# Fensterbreite, über die GPS-Höhen gemittelt werden. Fünfhundert Meter sind
# ein Kompromiss: schmal genug, dass eine echte Autobahnsteigung stehen
# bleibt (die zieht sich über Kilometer), und breit genug, dass vom
# hochfrequenten Rauschen wenig übrig ist.
GLAETTUNG_M = 500.0


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


def gps_hoehen_glaetten(geometrie: list, hoehen: list) -> list:
    """GPS-Höhen über ein Streckenfenster mitteln.

    **Warum das nötig ist.** Das Verbrauchsmodell interessiert sich nicht
    für Höhen, sondern für Höhen*unterschiede* zwischen aufeinanderfolgenden
    Punkten - und davon summiert es die positiven auf. Genau diese
    Gleichrichtung ist der Haken: Aus mittelwertfreiem Rauschen wird dabei
    ein systematischer Zuschlag (der Erwartungswert des positiven Anteils
    ist rund 0,4·σ), und der addiert sich linear über die Punkte auf, nicht
    mit der Wurzel. Eine 40-km-Fahrt hat bei 25 m Mindestabstand rund 1600
    Stützpunkte; schon bei σ = 5 m je Differenz kommen so kilometerweise
    erfundene Steigung zusammen. Bei 2,5 t sind 1000 m Steigung etwa 7 kWh -
    die Ebene sähe aus wie eine Alpenetappe, und das ginge ungebremst in
    den Korrekturfaktor.

    **Warum Mitteln hilft.** Der Fehler der GPS-Höhe hat zwei Anteile. Der
    langsam veränderliche (gleiche Satellitengeometrie über Minuten) ist
    der harmlosere: Er sieht aus wie ein langer Hügel, ist falsch, aber
    beschränkt. Der hochfrequente, von Messung zu Messung unabhängige Anteil
    ist der, der die Gleichrichtung füttert - und genau den nimmt ein
    gleitendes Mittel heraus. Die Glättung greift also da an, wo der Schaden
    entsteht.

    Gemittelt wird über die **Strecke**, nicht über eine Punktzahl: Die
    Messpunkte stehen im Stau dicht und auf der Autobahn weit auseinander,
    ein Fenster aus zwanzig Punkten wäre einmal 300 m und einmal 3 km breit.
    """
    if len(geometrie) != len(hoehen) or len(geometrie) < 3:
        return list(hoehen)

    # Laufende Strecke entlang der Route - einmal gerechnet, danach ist das
    # Fenster ein Schieben zweier Ränder.
    km_stand = [0.0]
    for (lon1, lat1), (lon2, lat2) in zip(geometrie, geometrie[1:]):
        km_stand.append(km_stand[-1] + haversine_m(lat1, lon1, lat2, lon2))

    halb = GLAETTUNG_M / 2.0
    geglaettet = []
    links = rechts = 0
    for i, mitte in enumerate(km_stand):
        while km_stand[links] < mitte - halb:
            links += 1
        while rechts + 1 < len(km_stand) and km_stand[rechts + 1] <= mitte + halb:
            rechts += 1
        fenster = [hoehen[j] for j in range(links, rechts + 1)
                   if hoehen[j] is not None]
        geglaettet.append(sum(fenster) / len(fenster) if fenster
                          else (hoehen[i] or 0.0))
    return geglaettet


def hoehen_ergaenzen(geometrie: list, gps_hoehen: list | None = None
                     ) -> tuple[list, str]:
    """[[lon, lat], ...] zu [[lon, lat, hoehe], ...] machen.

    Gibt die Geometrie **und die Quelle** zurück. Die Quelle ist keine
    Nebensache: Hier stand vorher nur die Geometrie, und der Aufrufer riet
    aus "irgendeine Höhe ist ungleich null" auf `karte`. Fiel die
    ORS-Abfrage aus - erschöpftes Kontingent, Netzhänger -, rutschte es
    still auf GPS und meldete trotzdem `karte`. Man konnte einer Fahrt
    hinterher nicht ansehen, ob ihre Höhen etwas taugen.

    Erste Wahl sind Kartendaten. Ihr Fehler ist zwar absolut ähnlich gross
    wie beim GPS, aber räumlich korreliert und **immer derselbe**: Dieselbe
    Strasse bekommt bei jeder Fahrt dasselbe Profil. Für den Zweck der
    Fahrtenansicht - Januar gegen Juni, leer gegen beladen - kürzt sich ein
    Fehler, der bei beiden Fahrten gleich ist, gerade heraus.

    Fällt die Abfrage aus, gilt die geglättete GPS-Höhe (roh ist sie
    unbrauchbar, siehe `gps_hoehen_glaetten`), und wenn auch die fehlt, wird
    flach gerechnet. Eine flach gerechnete Strecke ist ausdrücklich **kein**
    Beinbruch für die Kalibrierung, solange Start und Ziel gleich hoch
    liegen - über eine geschlossene Runde hebt sich die Höhe ohnehin auf.
    Für eine Fahrt ins Gebirge taugt sie nicht, und das steht dann auch im
    Log.
    """
    try:
        mit_hoehe = routing.provider().hoehen(geometrie)
        if mit_hoehe:
            return mit_hoehe, "karte"
    except Exception as fehler:      # noqa: BLE001
        log.warning("Höhenabfrage fehlgeschlagen: %s", fehler)

    if gps_hoehen and len(gps_hoehen) == len(geometrie) \
            and any(h is not None for h in gps_hoehen):
        geglaettet = gps_hoehen_glaetten(geometrie, gps_hoehen)
        log.info("Höhen aus dem GPS, über %.0f m geglättet - ungenauer als "
                 "Kartendaten.", GLAETTUNG_M)
        return ([[lon, lat, hoehe] for (lon, lat), hoehe
                 in zip(geometrie, geglaettet)], "gps")

    log.warning("Keine Höhendaten - die Strecke wird flach gerechnet.")
    return [[lon, lat, 0.0] for lon, lat in geometrie], "flach"


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
    geometrie, hoehen_quelle = hoehen_ergaenzen(flach, gps_hoehen)

    fahrzeug = fahrt.fahrzeug
    hole_umgebung, mittel_temp = umgebung_bestimmen(gewaehlt, flach)

    profil = modell.profil_rechnen(
        Fahrzeugwerte.aus_fahrt(fahrt), geometrie,
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
    # Beim Anlegen stand hier "unterwegs" - das Ziel war da noch unbekannt.
    # Jetzt ist es bekannt, nur hat es keinen Namen: jolt kann Orte suchen,
    # aber nicht umgekehrt aus einer Koordinate einen Ortsnamen machen. Das
    # Platzhalterwort stehen zu lassen war die schlechteste der Möglichkeiten
    # - in der Fahrtenliste stand danach dauerhaft "Aufzeichnung →
    # unterwegs", also eine Behauptung über eine Fahrt, die längst zu Ende
    # ist. Leer heisst hier ehrlich "kein Ortsname"; die Liste zeigt dann
    # den Namen der Aufzeichnung allein.
    if (fahrt.ziel_text or "") == "unterwegs":
        fahrt.ziel_text = ""
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
            "hoehen": hoehen_quelle}


def _soll_bei(energieprofil: list, km: float):
    # Bewusst hier und nicht über energie/profil.py: Das Profil ist gerade
    # erst entstanden und liegt als Liste von dicts vor, nicht am Fahrt-Objekt.
    from ..energie.profil import soc_bei
    return soc_bei(energieprofil, km)
