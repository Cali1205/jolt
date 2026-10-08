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
from ..energy import model, weather
from ..energy.model import VehicleValues, Environment
from ..geo import haversine_m
from ..routing.corridor import point_on_route

log = logging.getLogger("uvicorn.error")

# Höchstzahl der Stützpunkte für die Höhenabfrage. openrouteservice nimmt
# nicht beliebig viele, und feiner als rund alle hundert Meter bringt das
# Höhenprofil ohnehin nichts.
AT_MOST_SUPPORT_POINTS = 1800

# Punkte, die enger beieinanderliegen, werden zusammengefasst. Ein stehendes
# Auto liefert sonst hunderte Punkte auf demselben Fleck, und die verzerren
# jede Geschwindigkeit, die daraus gerechnet wird.
MIN_DISTANCE_M = 25.0

# Fensterbreite, über die GPS-Höhen gemittelt werden. Fünfhundert Meter sind
# ein Kompromiss: schmal genug, dass eine echte Autobahnsteigung stehen
# bleibt (die zieht sich über Kilometer), und breit genug, dass vom
# hochfrequenten Rauschen wenig übrig ist.
SMOOTHING_M = 500.0


def distance_build(points: list) -> list:
    """Aus den Messpunkten eine Geometrie [[lon, lat], ...].

    Zusammengefasst wird alles, was enger als `MINDESTABSTAND_M` liegt: An
    einer Ampel oder an der Säule stehen sonst dutzende Punkte übereinander,
    aus denen sich eine Geschwindigkeit von null und ein Teilstück der Länge
    null ergäbe - beides bringt die Rechnung dahinter durcheinander.
    """
    built: list = []
    last = None
    for point in points:
        if point.lat is None or point.lon is None:
            continue
        if last is not None:
            spacing = haversine_m(last.lat, last.lon, point.lat, point.lon)
            if spacing < MIN_DISTANCE_M:
                continue
        built.append(point)
        last = point
    return built


def _thin_out(points: list, at_most: int) -> list:
    if len(points) <= at_most:
        return points
    step = len(points) / at_most
    chosen = [points[int(i * step)] for i in range(at_most)]
    # Der letzte Punkt muss dabei sein - sonst endet die rekonstruierte
    # Strecke vor dem Ziel und die Bilanz stimmt nicht.
    if chosen[-1] is not points[-1]:
        chosen.append(points[-1])
    return chosen


def gps_elevations_smooth(geometry: list, elevations: list) -> list:
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
    if len(geometry) != len(elevations) or len(geometry) < 3:
        return list(elevations)

    # Laufende Strecke entlang der Route - einmal gerechnet, danach ist das
    # Fenster ein Schieben zweier Ränder.
    odometer_km = [0.0]
    for (lon1, lat1), (lon2, lat2) in zip(geometry, geometry[1:]):
        odometer_km.append(odometer_km[-1] + haversine_m(lat1, lon1, lat2, lon2))

    half = SMOOTHING_M / 2.0
    smoothed = []
    left_side = right = 0
    for i, middle in enumerate(odometer_km):
        while odometer_km[left_side] < middle - half:
            left_side += 1
        while right + 1 < len(odometer_km) and odometer_km[right + 1] <= middle + half:
            right += 1
        timeframe = [elevations[j] for j in range(left_side, right + 1)
                   if elevations[j] is not None]
        smoothed.append(sum(timeframe) / len(timeframe) if timeframe
                          else (elevations[i] or 0.0))
    return smoothed


def complete_elevations(geometry: list, gps_elevations: list | None = None
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
        with_elevation = routing.provider().elevations(geometry)
        if with_elevation:
            return with_elevation, "karte"
    except Exception as failure:      # noqa: BLE001
        log.warning("Höhenabfrage fehlgeschlagen: %s", failure)

    if gps_elevations and len(gps_elevations) == len(geometry) \
            and any(h is not None for h in gps_elevations):
        smoothed = gps_elevations_smooth(geometry, gps_elevations)
        log.info("Höhen aus dem GPS, über %.0f m geglättet - ungenauer als "
                 "Kartendaten.", SMOOTHING_M)
        return ([[lon, lat, elevation] for (lon, lat), elevation
                 in zip(geometry, smoothed)], "gps")

    log.warning("Keine Höhendaten - die Strecke wird flach gerechnet.")
    return [[lon, lat, 0.0] for lon, lat in geometry], "flach"


# Ab welcher Fahrstrecke der Kilometerstand des Fahrzeugs die Strecke
# bestimmen darf. Er loest in ganzen Kilometern auf: Auf einer Fahrt von vier
# Kilometern ist das ein Viertel Unsicherheit, auf hundert ein Prozent.
ODOMETER_MIN_DISTANCE_KM = 5.0

# Wie weit Kilometerstand und GPS-Strecke auseinanderliegen duerfen, bevor
# der Kilometerstand als unglaubwuerdig gilt. Unter 1.0 waere die GPS-Spur
# laenger als die gefahrene Strecke - das kann nur Rauschen sein. Ueber 3.0
# stimmt etwas anderes nicht (ein Ableseformat, ein Fahrzeugwechsel), und
# eine Strecke zu verdreifachen ist zu folgenreich, um es zu raten.
ODOMETER_LIMITS = (1.0, 3.0)


def odometer_factor(points: list, gps_km: float) -> tuple[float, dict]:
    """Um wie viel die GPS-Spur zu kurz ist - laut Kilometerstand des Autos.

    **Warum das noetig ist.** Die Strecke einer Aufzeichnung entsteht aus den
    Messpunkten, und die kommen alle dreissig Sekunden. Bei Landstrassentempo
    liegen dazwischen vierhundert Meter, und die Luftlinie schneidet jede
    Kurve ab. Bei einer Funkloch-Luecke fehlt gleich ein ganzes Stueck. Beides
    macht die Strecke zu kurz - und weil der gemessene Verbrauch in
    Kilowattstunden **pro hundert Kilometer** gerechnet wird, wandert der
    Fehler direkt in den Korrekturfaktor des Fahrzeugs.

    Das Auto weiss es genauer. Sein Kilometerstand zaehlt Radumdrehungen und
    kennt weder Kurven noch Funkloecher.

    Zurueckgegeben wird ein Faktor auf die **ganze** Strecke, nicht je
    Teilstueck: Der Zaehler loest in ganzen Kilometern auf, und zwischen zwei
    Messpunkten im Abstand von vierhundert Metern springt er um null oder
    eins. Fuer das einzelne Teilstueck ist er damit unbrauchbar, fuer die
    Summe ueber eine Fahrt genau richtig.
    """
    as_of = [(p.raw_values or {}).get("odometer_km") for p in points]
    as_of = [k for k in as_of if isinstance(k, (int, float))]
    if len(as_of) < 2:
        return 1.0, {"reason": "weniger als zwei Ablesungen"}

    driven = as_of[-1] - as_of[0]
    if driven < ODOMETER_MIN_DISTANCE_KM:
        return 1.0, {"reason": f"nur {driven:g} km laut Zaehler - zu kurz "
                              f"fuer eine Aufloesung von einem Kilometer",
                     "odometer_km": driven}
    if gps_km <= 0:
        return 1.0, {"reason": "keine GPS-Strecke zum Vergleichen"}

    factor = driven / gps_km
    if not ODOMETER_LIMITS[0] <= factor <= ODOMETER_LIMITS[1]:
        log.warning("Kilometerstand verworfen: %.0f km laut Zaehler gegen "
                    "%.1f km aus dem GPS (Faktor %.2f).", driven, gps_km,
                    factor)
        return 1.0, {"reason": f"Faktor {factor:.2f} ausserhalb der Grenzen",
                     "odometer_km": driven, "gps_km": round(gps_km, 1)}
    return factor, {"odometer_km": driven, "gps_km": round(gps_km, 1),
                    "factor": round(factor, 3)}


def speed_per_segment(points: list, distance_factor: float = 1.0) -> list:
    """Gefahrene Geschwindigkeit in m/s je Teilstück, aus den Zeitstempeln.

    `strecke_faktor` gehört hier genauso hinein wie ins Energieprofil: Wer
    die Strecke streckt, ohne das Tempo mitzuziehen, lässt das Modell zu
    langsam fahren - und über v² sagt es dann deutlich zu wenig Verbrauch
    voraus.

    Der eigentliche Vorzug einer Aufzeichnung: Die geplante Fahrt muss das
    Tempo annehmen, die gefahrene weiss es. Zeitsprünge und Standzeiten
    ergeben absurde Werte, deshalb die Schranken - unter 2 m/s rechnet das
    Modell ohnehin mit seinem eigenen Mindestwert, über 70 m/s (252 km/h)
    war es kein Auto, sondern eine kaputte Uhr.
    """
    speeds = []
    for earlier, after in zip(points, points[1:]):
        distance = haversine_m(earlier.lat, earlier.lon,
                              after.lat, after.lon) * distance_factor
        duration = 0.0
        if earlier.timestamp and after.timestamp:
            duration = (after.timestamp - earlier.timestamp).total_seconds()
        speeds.append(min(70.0, max(2.0, distance / duration)) if duration > 0 else 25.0)
    return speeds


def determine_environment(points: list, geometry: list):
    """Das Wetter der Fahrt - gemessen, wenn es gemessen wurde.

    Ein Logger am OBD2-Anschluss liefert die Aussentemperatur des Fahrzeugs.
    Die ist jeder Vorhersage überlegen: Sie stammt von der Strecke, zur
    richtigen Zeit, und sie ist der grösste Einzelposten der Kälte. Nur wenn
    keine mitkam, wird nachgefragt - und dann liefert der Wetterdienst das
    Wetter von *jetzt*, nicht das von der Fahrt.
    """
    measured = [p.outside_temp_c for p in points if p.outside_temp_c is not None]
    if measured:
        avg = sum(measured) / len(measured)
        log.info("Aufzeichnung: gemessene Aussentemperatur %.1f °C", avg)
        return lambda lat, lon: Environment(temp_c=avg), avg

    fetch = weather.along_route(geometry)
    avg = weather.mean(geometry).temp_c
    return fetch, avg


def complete(db, trip: models.Trip, session: models.LiveSession) -> dict:
    """Aus den Messpunkten einer Sitzung Geometrie und Energieprofil bauen.

    Danach ist die Aufzeichnung eine Fahrt wie jede andere: Sie hat eine
    Strecke, ein Höhenprofil und eine Prognose, gegen die sich der gemessene
    Verbrauch halten lässt. Erst dadurch kann `energie/calibration.py`
    überhaupt etwas lernen - es vergleicht `soll_soc` mit `soc`, und beides
    steht erst jetzt fest.
    """
    raw = distance_build(list(session.points))
    if len(raw) < 2:
        return {"ok": False, "reason": "Zu wenige Messpunkte für eine Strecke."}

    chosen = _thin_out(raw, AT_MOST_SUPPORT_POINTS)
    flat = [[p.lon, p.lat] for p in chosen]
    gps_elevations = [(p.raw_values or {}).get("elevation_m") for p in chosen]
    geometry, elevations_source = complete_elevations(flat, gps_elevations)

    fetch_environment, avg_temp = determine_environment(chosen, flat)

    # Was das GPS hergibt - und was das Auto dazu sagt.
    gps_km = sum(haversine_m(a.lat, a.lon, b.lat, b.lon)
                 for a, b in zip(chosen, chosen[1:])) / 1000.0
    factor, odo = odometer_factor(list(session.points), gps_km)
    if factor != 1.0:
        log.info("Strecke nach Kilometerstand gestreckt: %.1f km aus dem GPS "
                 "-> %g km laut Zaehler (Faktor %.3f).",
                 gps_km, odo.get("odometer_km"), factor)

    profile = model.compute_profile(
        VehicleValues.from_trip(trip), geometry,
        speed_per_segment(chosen, factor),
        start_soc=chosen[0].soc if chosen[0].soc is not None else 100.0,
        environment_for=fetch_environment, distance_factor=factor,
        speed_cap=False)
    if len(profile.points) < 2:
        return {"ok": False, "reason": "Aus der Strecke entstand kein Profil."}

    trip.geometry = geometry
    trip.energy_profile = [p.as_dict() for p in profile.points]
    trip.distance_m = profile.distance_km * 1000.0
    trip.drive_time_s = profile.mins * 60.0
    trip.outside_temp_c = round(avg_temp, 1)
    trip.start_lat, trip.start_lon = chosen[0].lat, chosen[0].lon
    trip.target_lat, trip.target_lon = chosen[-1].lat, chosen[-1].lon
    # Beim Anlegen stand hier "unterwegs" - das Ziel war da noch unbekannt.
    # Jetzt ist es bekannt, nur hat es keinen Namen: jolt kann Orte suchen,
    # aber nicht umgekehrt aus einer Koordinate einen Ortsnamen machen. Das
    # Platzhalterwort stehen zu lassen war die schlechteste der Möglichkeiten
    # - in der Fahrtenliste stand danach dauerhaft "Aufzeichnung →
    # unterwegs", also eine Behauptung über eine Fahrt, die längst zu Ende
    # ist. Leer heisst hier ehrlich "kein Ortsname"; die Liste zeigt dann
    # den Namen der Aufzeichnung allein.
    if (trip.target_text or "") == "unterwegs":
        trip.target_text = ""
    if chosen[0].soc is not None:
        trip.start_soc = chosen[0].soc

    # Die Messpunkte tragen bisher weder Kilometerstand noch Sollwert - beim
    # Eintreffen gab es ja keine Strecke, auf die man sie hätte legen können.
    # Ohne das findet die Kalibrierung nichts Verwertbares.
    for point in session.points:
        km, _ = point_on_route(geometry, point.lat, point.lon)
        point.km_on_route = km
        point.plan_soc = _plan_at(trip.energy_profile, km)

    db.flush()
    return {"ok": True, "distance_km": round(profile.distance_km, 1),
            "drive_time_minutes": round(profile.mins),
            "consumption_kwh": round(profile.kwh_total, 2),
            "outside_temp_c": trip.outside_temp_c,
            "elevations": elevations_source,
            # Woher die Strecke stammt - eine aus dem Kilometerstand
            # korrigierte ist etwas anderes als eine reine GPS-Spur, und man
            # soll es der Fahrt ansehen.
            "distance_source": "kilometerstand" if factor != 1.0 else "gps",
            "odometer": odo}


def _plan_at(energy_profile: list, km: float):
    # Bewusst hier und nicht über energie/profile.py: Das Profil ist gerade
    # erst entstanden und liegt als Liste von dicts vor, nicht am Fahrt-Objekt.
    from ..energy.profile import soc_at
    return soc_at(energy_profile, km)
