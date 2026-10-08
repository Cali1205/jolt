"""Route rechnen: Strecke, Höhenprofil, Wetter, Energiebedarf, Ladestopps.

Der Endpunkt, der alles zusammenführt: die Strecke aus dem Routing, den
Energiebedarf aus dem Verbrauchsmodell und - über `/ladeplan` - die zeitoptimale
Folge von Ladestopps aus dem Optimierer.

`/route` rechnet dabei nicht eine, sondern mehrere Varianten: die schnellste
Strasse, auf Wunsch eine mautfreie, und bis zu vier Ausweichrouten über
Zwischenpunkte neben der Strecke (`routing/variants.py`).

Die Zwischenpunkte sind kein Selbstzweck. openrouteservice kennt Energie
nicht als Kantengewicht - "verbrauchsoptimal" kann man dort nicht bestellen,
das könnte nur ein eigener Routing-Layer (siehe konzept-routenplaner.md).
Und sein eingebautes `alternative_routes` lehnt jede Route über 100 km ab,
also genau die, bei denen eine Alternative etwas ändern würde. Bleibt: selbst
Kandidaten erzeugen und sie mit jolts eigenem Verbrauchsmodell bewerten.

Bewertet wird am **fertigen Ladeplan** und nicht an der Fahrzeit - siehe
`_varianten_bewerten`. Eine Route, die länger *und* langsamer ist als die
schnellste, wird schon vorher verworfen: Sie kann keinen Ladeplan haben, der
sie rettet, und das Rechnen kostet mehr als das Aussortieren.

Der Ladeplan hängt bewusst an einer bereits gerechneten Fahrt und nicht an der
Routenanfrage: Radius, Mindestleistung und Steckertyp will man durchprobieren,
ohne jedes Mal das Routing-Kontingent zu belasten.
"""
import logging
from datetime import datetime, timedelta, timezone

from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import deps, models, routing
from ..database import get_db
from ..energy import model, weather
from ..geo import haversine_m
from ..timestamp import utc_iso
# Nur noch für die Vorgabewerte der Regler - gerechnet wird über
# `umplanung.planen`, das den Optimierer selbst aufruft.
from ..charging import optimizer
from ..live import replanning
from ..routing import own, tomtom, variants
from ..routing.provider import RoutingError

# Von der ORS-"preference" auf die Bezeichnung, die der Mensch am Steuer
# liest. "empfohlen" statt "recommended", weil das Wort sonst niemand
# verwendet, der nicht selbst openrouteservice-Kunde ist.
LABEL = {"fastest": "schnellste", "shortest": "kürzeste",
          "recommended": "empfohlene"}
# Wie nah zwei Varianten in Strecke und Fahrzeit beieinanderliegen müssen, um
# als "dieselbe Route" zu gelten. Ein Kilometer und eine Minute Toleranz
# fangen Rundungsunterschiede zwischen den ORS-Antworten ab, ohne zwei
# tatsächlich verschiedene Strecken fälschlich zusammenzulegen.
SAME_KM = 1.0
SAME_MIN = 1.0

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api", tags=["route"],
                   dependencies=[Depends(deps.current_session)])


class Point(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    text: str = ""


class Routenanfrage(BaseModel):
    vehicle_id: int
    start: Point
    destination: Point
    start_soc: float = Field(default=80.0, ge=0, le=100)
    # 1.1 heisst "zehn Prozent schneller als das Routing annimmt". Der Regler
    # wirkt über v² überproportional - genau der Hebel, mit dem sich unterwegs
    # ein Ladestopp einsparen lässt.
    speed_factor: float = Field(default=1.0, ge=0.6, le=1.5)
    # Zuschlag auf den Luftwiderstand für Fahrradträger oder Dachbox.
    # 1.0 = nichts dran. Siehe models.Fahrt.luftwiderstand_faktor.
    air_drag_factor: float = Field(default=1.0, ge=1.0, le=2.0)
    consider_weather: bool = True
    # Eine zweite Route mitrechnen. Aus: Es bleibt bei der schnellsten.
    #
    # "shortest" fehlt hier bewusst und ganz. Auf der Strecke Le Gurp -
    # Montchanin liefert sie 554 km in 11,8 Stunden gegen 654 km in 6,3 -
    # hundert Kilometer weniger, gekauft mit fünfeinhalb Stunden. Das
    # entscheidet niemand so, und eine Auswahl, in der eine Möglichkeit
    # nie gewählt wird, macht die Auswahl nur unübersichtlich. Sie kostet
    # ausserdem ein Drittel des ORS-Tageskontingents.
    alternative: bool = False
    # Ausweichrouten über Zwischenpunkte mitrechnen (routing/variants.py).
    #
    # Vorgabe aus, und das ist ein Messergebnis und keine Vorsicht: Auf der
    # Teststrecke verlor jeder der zwölf durchgerechneten geometrischen
    # Kandidaten gegen die schnellste Route. Vier zusätzliche
    # Routing-Anfragen je Planung für einen Vorschlag, der zuverlässig
    # schlechter ist, wäre ein schlechtes Geschäft.
    #
    # An: für Versuche mit anderen Abgriffstellen und Versatzweiten. Die
    # Mechanik dahinter stimmt - sie wartet nur auf einen
    # Kandidatenlieferanten, der etwas taugt.
    examine_detours: bool = False
    # Gefahrene Strecken als zusätzliche Kandidaten (routing/own.py): Wer
    # eine Strecke schon gefahren ist, kennt einen Weg, den kein Kantengewicht
    # kennt. Es werden höchstens zwei Anfragen mehr gestellt, und nur, wenn zu
    # Start und Ziel überhaupt eine frühere Fahrt passt.
    own_trips: bool = True
    # TomTom als Berater (routing/tomtom.py): Vorschläge, die OpenRouteService
    # nicht liefert, und die Verkehrsverzögerung je Route. Ohne
    # TOMTOM_API_KEY geschieht nichts. Gespeichert wird davon nichts.
    tomtom: bool = True
    # Wann losgefahren wird. Leer heisst jetzt. Der Verkehr (TomTom, zeitabhängig
    # prognostiziert) und das Wetter (stündliche Vorhersage, je Stützpunkt für
    # die Stunde der Ankunft dort) gelten dann für diese Zeit. Mit Zeitzone; der
    # Browser schickt UTC.
    departure: datetime | None = None
    # Zuladung dieser einen Fahrt. None heisst "wie im Fahrzeugprofil" - der
    # Normalfall. Gesetzt wird sie, wenn dieselbe Fahrt einmal zu zweit und
    # einmal voll beladen geplant wird: Masse geht linear in Roll- und
    # Steigungswiderstand ein, auf einer Bergstrecke sind 600 kg Unterschied
    # deutlich mehr als Kosmetik.
    payload_kg: float | None = Field(default=None, ge=0, le=2000)
    # Anhänger dieser Fahrt: Masse und zusätzliche Luftwiderstandsfläche
    # (c_w mal A, in m²). Siehe models.Fahrt.anhaenger_kg.
    trailer_kg: float | None = Field(default=None, ge=0, le=3500)
    trailer_cwa_m2: float | None = Field(default=None, ge=0, le=5)
    # Harte Höchstgeschwindigkeit dieser Fahrt in km/h, etwa 100 für ein
    # Gespann. Wirkt zusätzlich zu der des Fahrzeugs; es gilt die kleinere.
    speed_max_kmh: float | None = Field(default=None, ge=30, le=250)


@router.get("/orte")
def search_places(text: str = Query(min_length=2), country: str = ""):
    try:
        hit = routing.provider().seek(text, country)
    except RoutingError as failure:
        raise HTTPException(502, str(failure)) from failure
    return {"demo": routing.is_demo(),
            "hit": [{"name": o.name, "lat": o.lat, "lon": o.lon}
                        for o in hit]}


# Eine Abfahrt, die höchstens so weit zurückliegt, ist "jetzt": Wer die Uhrzeit
# im Formular eintippt, braucht eine Weile.
DEPARTURE_TOLERANCE = timedelta(minutes=10)
# So weit im Voraus kennt TomTom Strassensperrungen und Baustellen; darüber
# hinaus gäbe es nur noch den üblichen Verkehr, und das Wetter reicht ohnehin
# nur 15 Tage.
DEPARTURE_MAX = timedelta(days=60)


def _examine_departure(request: Routenanfrage) -> datetime | None:
    """Die Abfahrt als Zeitpunkt mit Zeitzone - oder None für "jetzt".

    Vergangenheit und fernes Datum sind ein Tippfehler und werden abgelehnt,
    statt stillschweigend mit "jetzt" zu rechnen: Wer für Freitag plant und
    den Montag erwischt, soll es merken, bevor er auf die Zahlen vertraut.
    """
    departure = request.departure
    if departure is None:
        return None
    if departure.tzinfo is None:
        departure = departure.replace(tzinfo=timezone.utc)
    now_ts = datetime.now(timezone.utc)
    if departure < now_ts - DEPARTURE_TOLERANCE:
        raise HTTPException(422, "Die Abfahrt liegt in der Vergangenheit.")
    if departure > now_ts + DEPARTURE_MAX:
        raise HTTPException(422, "Die Abfahrt liegt mehr als 60 Tage in der "
                                 "Zukunft - so weit reicht keine Prognose.")
    if departure <= now_ts + DEPARTURE_TOLERANCE:
        return None
    return departure


@router.post("/route")
def compute_route(request: Routenanfrage, db: Session = Depends(get_db)):
    vehicle = db.get(models.Vehicle, request.vehicle_id)
    if not vehicle:
        raise HTTPException(404, "Fahrzeug nicht gefunden.")

    # Nur für diese Rechnung, nicht am Fahrzeug gespeichert: Zuladung und
    # Luftwiderstandszuschlag sind Eigenschaften der Fahrt, nicht des Autos.
    # `aus_fahrt` erwartet ein Fahrt-artiges Objekt; die Fahrt entsteht erst
    # in `_fahrten_speichern`, deshalb ein leichtgewichtiger Platzhalter.
    vals = model.VehicleValues.from_trip(SimpleNamespace(
        vehicle=vehicle, payload_kg=request.payload_kg,
        air_drag_factor=request.air_drag_factor,
        trailer_kg=request.trailer_kg,
        trailer_cwa_m2=request.trailer_cwa_m2,
        speed_max_kmh=request.speed_max_kmh))

    departure = _examine_departure(request)
    groups = _distances_collect(request, db, departure)
    candidates = _compute_candidates(request, vals, groups, departure)
    results = _save_trips(db, request, vehicle, candidates)

    # Vor der Bewertung: Der Verkehr gehört in die Rangfolge.
    _fetch_traffic(db, request, results, departure)
    _variants_rate(db, results, vehicle)
    return {"variants": results,
            "departure": departure.isoformat() if departure else None}


# So viele gefahrene Strecken werden höchstens als Kandidaten nachgefahren.
# Jede kostet eine Routing-Anfrage vom Tageskontingent, und mehr als die
# letzten zwei bringen selten etwas Neues.
MAX_OWN = 2


def _own_routes(db: Session, start: tuple, destination: tuple) -> list[dict]:
    """Wege nach früheren Fahrten, die zu Start und Ziel passen.

    Zuerst eine billige Abfrage nach dem Umschliessenden Rechteck je Sitzung
    (eine Zeile je Sitzung, nicht je Messpunkt); nur Sitzungen, deren
    Rechteck Start **und** Ziel einschliesst, werden überhaupt geladen. Die
    neuesten zuerst: Die Strasse, die man zuletzt gefahren ist, ist die, die
    es noch gibt.
    """
    straight_line_km = haversine_m(start[0], start[1], destination[0], destination[1]) / 1000.0
    if straight_line_km < own.RADIUS_MIN_KM:
        return []
    edge = own.radius_km(straight_line_km) + 5.0
    # Ein Grad Länge ist nördlich von 60° weniger als 55 km; mit 55 zu
    # rechnen macht das Rechteck eher zu gross als zu klein - und zu gross
    # kostet nur ein paar Zeilen mehr.
    edge_degree = edge / 55.0

    box = (db.query(models.LivePoint.session_id,
                       func.min(models.LivePoint.lat), func.max(models.LivePoint.lat),
                       func.min(models.LivePoint.lon), func.max(models.LivePoint.lon),
                       func.count(models.LivePoint.id))
              .group_by(models.LivePoint.session_id)
              .having(func.count(models.LivePoint.id) >= 20)
              .order_by(models.LivePoint.session_id.desc()).all())

    def inside(point, lat0, lat1, lon0, lon1) -> bool:
        return (lat0 - edge_degree <= point[0] <= lat1 + edge_degree
                and lon0 - edge_degree <= point[1] <= lon1 + edge_degree)

    routes: list[dict] = []
    for session_id, lat0, lat1, lon0, lon1, _ in box:
        if len(routes) >= MAX_OWN:
            break
        if not (inside(start, lat0, lat1, lon0, lon1)
                and inside(destination, lat0, lat1, lon0, lon1)):
            continue
        rows = (db.query(models.LivePoint.lat, models.LivePoint.lon,
                           models.LivePoint.speed_kmh)
                  .filter(models.LivePoint.session_id == session_id)
                  .order_by(models.LivePoint.timestamp).all())
        section = own.fitting_section(
            own.path_from_samples(rows), start, destination)
        if section is None:
            continue
        between = own.waypoints(section)
        if not between:
            continue
        session = db.get(models.LiveSession, session_id)
        date = session.started_at.strftime("%d.%m.%Y") if session else "?"
        label = f"meine Strecke vom {date}"
        if section.opposite:
            label += " (Gegenrichtung)"
        log.info("Eigene Strecke aus Sitzung %s: %d Zwischenpunkte, %.0f km "
                 "Pfad (Abstand Start %.1f km, Ziel %.1f km).", session_id,
                 len(between), section.length_km,
                 section.spacing_start_km, section.spacing_target_km)
        routes.append({"between": between, "toll_free": False,
                     "label": label})
    return routes


def _tomtom_routes(start: tuple, destination: tuple,
                 departure: datetime | None = None) -> list[dict]:
    """Wege nach den Vorschlägen von TomTom, die nicht überholt sind.

    TomTom liefert nur die Vorlage: Aus dem Vorschlag werden Zwischenpunkte
    gewählt, und das Routing von OpenRouteService fährt sie ab. Gespeichert
    wird dessen Strasse mit Höhe und Tempo, nicht die von TomTom - schon
    deshalb, weil das Verbrauchsmodell beides braucht, und weil TomToms
    Bedingungen das Speichern ihrer Ergebnisse nicht erlauben.

    Scheitert TomTom (Schlüssel, Kontingent, Netz), läuft die Planung ohne
    weiter. Ein Berater, der die Planung zum Absturz bringt, wäre schlechter
    als keiner.
    """
    if not tomtom.obtainable():
        return []
    try:
        suggestions = tomtom.alternativen(start, destination, departure=departure)
    except tomtom.TomTomError as failure:
        log.warning("TomTom: %s Die Planung läuft ohne.", failure)
        return []

    routes: list[dict] = []
    for nr, v in enumerate(tomtom.not_overtaken(suggestions), 1):
        section = own.Section(
            points=[(lat, lon, None) for lat, lon in v.points],
            opposite=False, spacing_start_km=0.0, spacing_target_km=0.0,
            length_km=v.distance_m / 1000.0)
        between = own.waypoints(section)
        if not between:
            continue
        # Keine Koordinaten ins Log: Es sind TomTom-Ergebnisse.
        log.info("TomTom-Vorschlag %d: %d Zwischenpunkte (%.0f km, %.0f min "
                 "bei TomTom).", nr, len(between), v.distance_m / 1000,
                 v.time_s / 60)
        routes.append({"between": between, "toll_free": False,
                     "label": f"TomTom-Vorschlag {nr}"})
    return routes


def _fetch_traffic(db: Session, request: Routenanfrage, results: list,
                   departure: datetime | None = None) -> None:
    """Die Verkehrsverzögerung je Route - als Zahl in der Antwort, sonst nirgends.

    Gefragt wird TomTom für den Weg, den jolt fährt, nicht für seinen eigenen:
    Aus der gespeicherten Geometrie werden Zwischenpunkte gewählt, die TomTom
    auf dieselbe Strasse zwingen. Die Verzögerung steht danach an der Variante
    und fliesst in `_varianten_bewerten` ein; in die Datenbank geht sie nicht.
    """
    if not (request.tomtom and tomtom.obtainable()):
        return
    start = (request.start.lat, request.start.lon)
    destination = (request.destination.lat, request.destination.lon)
    for variant in results:
        trip = db.get(models.Trip, variant["trip_id"])
        geometry = (trip.geometry or []) if trip else []
        if len(geometry) < 3:
            continue
        between = own.waypoints(own.Section(
            points=[(p[1], p[0], None) for p in geometry],
            opposite=False, spacing_start_km=0.0, spacing_target_km=0.0,
            length_km=(trip.distance_m or 0.0) / 1000.0))
        try:
            result = tomtom.traffic(start, destination, between, departure=departure)
        except tomtom.TomTomError as failure:
            # Derselbe Fehler träfe die übrigen Anfragen auch.
            log.warning("TomTom-Verkehr: %s Die Rangfolge gilt ohne.", failure)
            return
        if result is not None:
            variant["traffic_min"] = round(result.delay_s / 60.0, 1)
            variant["traffic_source"] = "TomTom"
            # Live oder zeitabhängig prognostiziert - die Oberfläche sagt es.
            variant["traffic_basis"] = "prognose" if departure else "live"


def _routes_plan(request: Routenanfrage, start: tuple, destination: tuple,
                 db: Session | None = None,
                 departure: datetime | None = None) -> list[dict]:
    """Welche Routing-Anfragen gestellt werden - jede kostet vom Tageskontingent.

    Die erste ist die schnellste Strasse und zugleich der Massstab; alles
    Weitere muss sich an ihr messen lassen.

    `recommended` und `shortest` stehen bewusst nicht dabei: Ersteres liefert
    auf Autobahnstrecken dieselbe Strasse wie `fastest`, letzteres eine, die
    niemand fährt (477 km in 10,7 Stunden gegen 598 km in 5,6). Beides
    gemessen, siehe routing/variants.py.
    """
    routes = [{"between": [], "toll_free": False, "label": LABEL["fastest"]}]
    if request.alternative:
        routes.append({"between": [], "toll_free": True, "label": "toll_free"})
    if request.examine_detours:
        routes += [{"between": [k["point"]], "toll_free": False,
                  "label": k["label"]}
                 for k in variants.alternative_points(start, destination)]
    if request.own_trips and db is not None:
        routes += _own_routes(db, start, destination)
    if request.tomtom:
        routes += _tomtom_routes(start, destination, departure)
    return routes


def _distances_collect(request: Routenanfrage, db: Session | None = None,
                      departure: datetime | None = None) -> list[dict]:
    """Schritt 1: die Wege abfragen und zusammenlegen, was dieselbe Strasse ist.

    Bewusst vor Wetter und Verbrauchsmodell - die sind der teure Teil, und im
    Demo-Modus wie oft auch in echt (kürzere Strecken haben meist nur einen
    sinnvollen Weg) landen mehrere Vorgaben ohnehin auf derselben Route.

    Rückgabe: je tatsächlich verschiedener Route ein Eintrag mit den
    Etiketten aller Wege, die auf sie führten, und der Strecke selbst.
    """
    vendor = routing.provider()
    start = (request.start.lat, request.start.lon)
    destination = (request.destination.lat, request.destination.lon)

    groups: list[dict] = []
    last_error: RoutingError | None = None
    basis = None
    for path in _routes_plan(request, start, destination, db, departure):
        try:
            distance = vendor.route(start, destination,
                                     intermediate_stops=path["between"] or None,
                                     preference="fastest",
                                     toll_free=path["toll_free"])
        except RoutingError as failure:
            # Ein Weg, der scheitert, darf die anderen nicht mitreissen - nur
            # wenn am Ende keiner übrig ist, ist die Anfrage gescheitert. Ein
            # Zwischenpunkt kann durchaus im Wasser oder im Sperrgebiet
            # landen; das ist kein Grund, die Route nicht zu liefern.
            last_error = failure
            continue
        if len(distance.points) < 2:
            continue

        if basis is None:
            basis = distance

        # Zusammenlegen vor Aussortieren: `ist_dominiert` zählt eine gleich
        # lange und gleich schnelle Route mit, und die würde sonst verworfen,
        # bevor ihr Etikett an der schon vorhandenen Route landet - die
        # mautfreie Route verschwände dann ohne Spur, obwohl sie genau
        # dieselbe Strasse ist.
        fitting = next((g for g in groups
                        if abs(g["distance"].distance_m - distance.distance_m)
                        <= SAME_KM * 1000
                        and abs(g["distance"].drive_time_s - distance.drive_time_s)
                        <= SAME_MIN * 60), None)
        if fitting:
            if path["label"] not in fitting["labels"]:
                fitting["labels"].append(path["label"])
            continue

        if distance is not basis and variants.actual_dominated(
                distance.distance_m, distance.drive_time_s,
                basis.distance_m, basis.drive_time_s):
            # Länger *und* langsamer als die schnellste Route: Der Kandidat
            # kann keinen Ladeplan haben, der ihn rettet. Hier auszusortieren
            # spart Wetterabfrage, Verbrauchsprofil und Ladeplanung - den
            # teuren Teil. Die Routing-Anfrage ist da schon bezahlt.
            log.info("Ausweichroute '%s' verworfen: %.0f km/%.0f min gegen "
                     "%.0f km/%.0f min der schnellsten.", path["label"],
                     distance.distance_m / 1000, distance.drive_time_s / 60,
                     basis.distance_m / 1000, basis.drive_time_s / 60)
            continue

        groups.append({"labels": [path["label"]], "distance": distance})

    if not groups:
        if last_error:
            raise HTTPException(502, str(last_error)) from last_error
        raise HTTPException(502, "Route enthält zu wenige Punkte.")
    return groups


def _cap_factor(profile) -> float:
    """Um wieviel die Tempo-Obergrenze die Fahrzeit streckt, mindestens 1."""
    if profile.minutes_without_cap > 0 and profile.mins > 0:
        return max(1.0, profile.mins / profile.minutes_without_cap)
    return 1.0


def _compute_candidates(request: Routenanfrage, vals, groups: list[dict],
                        departure: datetime | None = None) -> list[dict]:
    """Schritt 2: für jede tatsächlich unterschiedliche Route - und nur für
    die - Wetter und Verbrauchsmodell rechnen."""
    candidates: list[dict] = []
    for group in groups:
        distance = group["distance"]
        # Auf rund einen Punkt je 250 m ausdünnen. Auf einer Langstrecke
        # liefert das Routing fünfstellig viele Stützpunkte - für Karte und
        # Prognose ist das Rechenzeit ohne Erkenntnis. Höhensprünge bleiben
        # dabei erhalten.
        points, velocity = model.thin_out(distance.points, distance.speed_ms)

        if request.consider_weather:
            # Für die Abfahrtszeit, nicht für jetzt: Eine Fahrt morgen früh
            # soll nicht mit dem Wetter von heute Nachmittag gerechnet werden.
            environment_for = weather.along_route(
                points, departure=departure, duration_s=distance.drive_time_s)
            avg = weather.mean(points, departure=departure,
                                       duration_s=distance.drive_time_s)
        else:
            environment_for = None
            avg = model.Environment()

        profile = model.compute_profile(vals, points, velocity, request.start_soc,
                                       environment_for, request.speed_factor)
        candidates.append({
            "labels": group["labels"],
            "distance_km": distance.distance_m / 1000.0 if distance.distance_m
                else profile.distance_km,
            # Fahrzeit: die Zahl von openrouteservice ist die realistische
            # Grundlage - sie kennt Kreuzungen, Kreisel und Ortsdurchfahrten,
            # die das Verbrauchsmodell nicht kennt. Nur kennt **sie** den
            # Tempo-Regler nicht, und der verschiebt sie linear: Wer zehn
            # Prozent schneller faehrt, braucht ein Elftel weniger Zeit.
            #
            # Ohne diese Teilung stand die Fahrzeit unveraendert da, egal wo
            # der Regler stand - waehrend Verbrauch und Ladeplan darunter
            # sich sehr wohl aenderten. Zwei verschiedene Zeiten fuer
            # dieselbe Fahrt auf demselben Schirm.
            #
            # Mit Tempo-Obergrenze kommt ein dritter Posten dazu: Was die
            # Grenze abschneidet, kostet Zeit. `profil` kennt beide Zeiten,
            # mit und ohne Grenze; ihr Verhältnis streckt die Zeit des
            # Routings (1.0, solange keine Grenze greift).
            "drive_time_min": (distance.drive_time_s / 60.0 / request.speed_factor
                             * _cap_factor(profile))
                if distance.drive_time_s else profile.mins,
            "points": points, "profile": profile, "avg": avg})
    return candidates


def _save_trips(db: Session, request: Routenanfrage, vehicle,
                       candidates: list[dict]) -> list[dict]:
    """Schritt 3: je Kandidat eine Fahrt anlegen und die Antwort bauen."""
    results = []
    for candidate in candidates:
        trip = models.Trip(
            vehicle_id=vehicle.id,
            start_text=request.start.text, start_lat=request.start.lat,
            start_lon=request.start.lon, target_text=request.destination.text,
            target_lat=request.destination.lat, target_lon=request.destination.lon,
            start_soc=request.start_soc, speed_factor=request.speed_factor,
            outside_temp_c=candidate["avg"].temp_c,
            payload_kg=request.payload_kg,
            air_drag_factor=request.air_drag_factor,
            trailer_kg=request.trailer_kg,
            trailer_cwa_m2=request.trailer_cwa_m2,
            speed_max_kmh=request.speed_max_kmh,
            distance_m=candidate["distance_km"] * 1000,
            drive_time_s=candidate["drive_time_min"] * 60,
            geometry=candidate["points"],
            energy_profile=[p.as_dict() for p in candidate["profile"].points])
        db.add(trip)
        db.flush()      # braucht fahrt.id, ohne schon endgültig zu committen
        results.append({"trip_id": trip.id,
                           "labels": candidate["labels"],
                           **_response(trip, candidate["profile"],
                                      candidate["avg"], vehicle)})
    db.commit()
    return results


def _variants_rate(db, results: list, vehicle) -> None:
    """Die Varianten am **fertigen Ladeplan** messen, nicht an der Fahrzeit.

    Das ist die Frage, die für ein Elektroauto zählt und die sonst niemand
    beantwortet: Nicht "welche Strasse ist kürzer", sondern "wo bin ich
    früher, wenn das Laden mitzählt". Eine Route mit hundert Kilometern
    Umweg kann gewinnen, wenn an ihr die stärkeren Säulen stehen - und eine
    sparsame Landstrasse verliert, obwohl sie weniger Energie braucht.

    Vorher trug die energieärmste Variante das Etikett "sparsamste". Das war
    irreführend: Auf Le Gurp - Montchanin zeichnete es die Strecke aus, die
    mit 47 km/h Schnitt zwar 17 statt 27 kWh/100 km braucht, dafür aber
    fünfeinhalb Stunden länger unterwegs ist. Sparsam war sie, sinnvoll
    nicht.

    Gerechnet wird mit den Vorgabewerten für Radius und Mindestleistung -
    die Regler der Oberfläche gelten für den Ladeplan darunter. Das ist
    unschädlich, weil **alle** Varianten dieselbe Behandlung bekommen: Für
    einen Vergleich zählt der Massstab, nicht sein Nullpunkt.
    """
    if len(results) < 2:
        return
    parameter = dict(replanning.DEFAULTS)
    for variant in results:
        trip = db.get(models.Trip, variant["trip_id"])
        try:
            plan = replanning.schedule(db, trip, 0.0, trip.start_soc, parameter)
        except Exception as failure:      # noqa: BLE001
            # Ohne Plan bleibt die Variante wählbar - sie trägt dann nur
            # keine Bewertung. Eine Route zu verwerfen, weil ihr Ladeplan
            # nicht rechnet, wäre die falsche Reaktion.
            log.warning("Variante %s nicht planbar: %s", variant["trip_id"],
                        failure)
            continue
        if not plan.get("feasible"):
            variant["plan_feasible"] = False
            continue
        variant.update({
            "plan_feasible": True,
            "plan_stops": plan.get("stop_count"),
            "plan_total_minutes": plan.get("total_minutes"),
            "plan_cost_eur": plan.get("cost_eur")})

    rated = [v for v in results if v.get("plan_feasible")]
    # Der Verkehr gehört zur Zeit: Eine Route, die auf dem Papier zwei
    # Minuten schneller ist, aber zwölf im Stau steht, ist nicht die schnellste.
    def total(v: dict) -> float:
        return v["plan_total_minutes"] + (v.get("traffic_min") or 0.0)

    for v in rated:
        if v.get("traffic_min") is not None:
            v["plan_total_with_traffic_min"] = round(total(v))
    if rated:
        min(rated, key=total)["labels"].append("insgesamt schnellste")
        cheapest = min(rated, key=lambda v: v["plan_cost_eur"])
        if "insgesamt schnellste" not in cheapest["labels"]:
            cheapest["labels"].append("günstigste")

    # Reihenfolge fürs Auge: die insgesamt schnellste zuerst - sie ist die
    # Antwort auf die Frage, die jolt beantworten soll.
    results.sort(key=lambda v: "insgesamt schnellste" not in v["labels"])


@router.get("/fahrten/{trip_id}")
def read_trip(trip_id: int, db: Session = Depends(get_db)):
    """Eine gespeicherte Fahrt - in derselben Form wie eine frische Variante.

    Die abgeleiteten Werte (Verbrauch, Reserve-Punkt, "reicht es?") werden aus
    dem gespeicherten Energieprofil neu bestimmt statt mitgespeichert: Sie
    sind Funktionen des Profils, und zwei Quellen für dieselbe Zahl laufen
    auseinander. Die Form entspricht bewusst der von `/api/route`, damit die
    Oberfläche eine alte Fahrt mit demselben Code zeichnet wie eine neue.
    """
    trip = db.get(models.Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Fahrt nicht gefunden.")

    profile = trip.energy_profile or []
    reserve_soc = trip.vehicle.reserve_soc
    distance_km = round((trip.distance_m or 0) / 1000.0, 1)
    kwh_total = profile[-1].get("kwh") if profile else None

    reserve_at_km, reserve_point = None, None
    for entry in profile:
        if entry.get("soc") is not None and entry["soc"] <= reserve_soc:
            reserve_at_km = entry.get("km")
            reserve_point = {"km": entry.get("km"), "lat": entry.get("lat"),
                             "lon": entry.get("lon")}
            break

    return {"trip_id": trip.id, "demo": routing.is_demo(),
            "start": {"lat": trip.start_lat, "lon": trip.start_lon,
                      "text": trip.start_text},
            "destination": {"lat": trip.target_lat, "lon": trip.target_lon,
                     "text": trip.target_text},
            "vehicle": {"id": trip.vehicle.id, "name": trip.vehicle.name,
                         "reserve_soc": reserve_soc},
            "start_soc": trip.start_soc, "speed_factor": trip.speed_factor,
            "outside_temp_c": trip.outside_temp_c, "payload_kg": trip.payload_kg,
            "trailer_kg": trip.trailer_kg,
            "trailer_cwa_m2": trip.trailer_cwa_m2,
            "speed_max_kmh": trip.speed_max_kmh,
            "distance_km": distance_km,
            "drive_time_minutes": round((trip.drive_time_s or 0) / 60.0),
            "kwh_total": round(kwh_total, 3) if kwh_total is not None else None,
            "consumption_kwh_100km": (round(kwh_total / distance_km * 100.0, 2)
                                    if kwh_total and distance_km else None),
            "reserve_at_km": reserve_at_km,
            "reserve_point": reserve_point,
            "suffices": reserve_at_km is None,
            # Der Wind der damaligen Fahrt ist nicht gespeichert - nur die
            # Temperatur, mit der gerechnet wurde. Sie ist die Zahl, die in
            # der Oberfläche steht ("gerechnet bei 4 °C").
            "weather": {"temp_c": trip.outside_temp_c},
            "geometry": trip.geometry or [],
            "profile": _thin_out_profile(profile),
            "soc_at_target": profile[-1]["soc"] if profile else None}


@router.post("/fahrten/{trip_id}/ladeplan")
def compute_charge_plan(trip_id: int, radius_km: float = Query(8.0, gt=0, le=50),
                     min_kw: float = Query(50.0, ge=0),
                     connector_type: str = "",
                     detour_limit_min: float = Query(
                         optimizer.DETOUR_LIMIT_MIN, gt=0, le=60),
                     # Null ist erlaubt, aber die Folge steht im Text der
                     # Oberfläche: Ohne Fixkosten je Halt zersplittert der
                     # Plan in viele Kurzstopps. Wer das sehen will, soll es
                     # sehen können.
                     stop_fixed_cost_min: float = Query(
                         optimizer.STOP_FIXED_COST_MIN, ge=0, le=30),
                     # Was ein grosser Ladepark wert ist, in Minuten.
                     # Null heisst "nur die Zeit zählt".
                     charge_park_bonus_min: float = Query(
                         optimizer.CHARGE_PARK_BONUS_MIN, ge=0, le=15),
                     # Was eine Stunde wert ist. Null heisst "Kosten sind
                     # mir gleich" - dann wird rein auf Zeit optimiert.
                     time_value_eur_h: float = Query(
                         optimizer.TIME_VALUE_EUR_H, ge=0, le=200),
                     db: Session = Depends(get_db)):
    """Die zeitoptimale Folge von Ladestopps für eine gerechnete Fahrt.

    Gerechnet wird auf dem gespeicherten Energieprofil - der Bedarf einer
    Etappe hängt nicht vom Ladestand ab, deshalb genügt der eine Durchlauf des
    Verbrauchsmodells aus `/route`. Ein zweiter Aufruf mit anderem Radius
    kostet damit weder Routing- noch Wetterabfragen.

    **Über `umplanung.planen` und nicht am Optimierer vorbei.** Hier stand
    dieselbe Kette noch einmal ausgeschrieben: Kandidaten im Korridor suchen,
    sie in Ladeoptionen übersetzen, den Optimierer mit dreizehn Argumenten
    aufrufen. Der Block, der Kandidaten übersetzt, war byteweise derselbe wie
    in `live/replanning.py`, und der Aufruf musste zweimal gepflegt werden -
    beim zuletzt ergänzten `km_versatz` ist das prompt schiefgegangen, er
    stand nur in einer der beiden Fassungen.

    Dass es eine Doppelung war und keine Absicht, zeigte dieser Router selbst:
    Für die Bewertung der Routenvarianten rief er `umplanung.planen` schon
    vorher auf. Eine Planung ab km 0 mit dem Start-Ladestand ist derselbe
    Vorgang wie eine Umplanung unterwegs, nur ohne zurückgelegte Strecke.
    """
    trip = db.get(models.Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Fahrt nicht gefunden.")
    if not trip.energy_profile or len(trip.energy_profile) < 2:
        raise HTTPException(409, "Zu dieser Fahrt liegt kein Energieprofil vor.")

    plan = replanning.schedule(db, trip, 0.0, trip.start_soc, {
        "radius_km": radius_km, "min_kw": min_kw, "connector_type": connector_type,
        "detour_limit_min": detour_limit_min,
        "stop_fixed_cost_min": stop_fixed_cost_min,
        "charge_park_bonus_min": charge_park_bonus_min,
        "time_value_eur_h": time_value_eur_h})
    # `steckertyp` aufgelöst zurückgeben: Leer heisst "der des Fahrzeugs",
    # und die Oberfläche soll anzeigen können, wonach gesucht wurde.
    return {**plan, "trip_id": trip.id, "demo": routing.is_demo(),
            "connector_type": connector_type or trip.vehicle.connector_type}


@router.get("/fahrten")
def trips_list(db: Session = Depends(get_db), bound: int = Query(30, ge=1, le=200)):
    """Die zuletzt geplanten Fahrten.

    Bewusst mehr als Start und Ziel: Ohne Verbrauch, Aussentemperatur und
    Zuladung ist eine Liste vergangener Fahrten eine Liste von Namen. Erst
    mit diesen Zahlen wird sie zu dem, wofür man sie aufschlägt - dem
    Vergleich, warum dieselbe Strecke im Januar zwei Ladestopps brauchte und
    im Juni einen.
    """
    trips = (db.query(models.Trip).order_by(models.Trip.id.desc())
               .limit(bound).all())

    result = []
    for f in trips:
        profile = f.energy_profile or []
        kwh = profile[-1].get("kwh") if profile else None
        distance_km = round((f.distance_m or 0) / 1000.0, 1)
        result.append({
            "id": f.id, "start": f.start_text, "destination": f.target_text,
            "created_at": utc_iso(f.created_at),
            "distance_km": distance_km,
            "drive_time_minutes": round((f.drive_time_s or 0) / 60.0),
            "vehicle": f.vehicle.name,
            "start_soc": f.start_soc,
            "soc_at_target": profile[-1].get("soc") if profile else None,
            "kwh_total": round(kwh, 1) if kwh is not None else None,
            "consumption_kwh_100km": (round(kwh / distance_km * 100.0, 1)
                                    if kwh and distance_km else None),
            "outside_temp_c": f.outside_temp_c,
            "speed_factor": f.speed_factor,
            "payload_kg": f.payload_kg,
            # Für den Vergleich in der Historie: Eine Fahrt mit Träger ist
            # nicht mit einer ohne vergleichbar, und eine Aufzeichnung nicht
            # mit einem Entwurf. Beides muss man sehen können, sonst
            # vergleicht man Äpfel mit Birnen und wundert sich.
            "air_drag_factor": f.air_drag_factor,
            "trailer_kg": f.trailer_kg,
            "speed_max_kmh": f.speed_max_kmh,
            "recording": bool(f.recording),
            # Ob zu dieser Fahrt tatsächlich gefahren wurde - eine geplante
            # Fahrt ohne Live-Sitzung ist ein Entwurf, keine Erinnerung.
            "driven": bool(f.live_sessions)})
    return result


@router.delete("/fahrten/{trip_id}")
def delete_trip(trip_id: int, db: Session = Depends(get_db)):
    """Eine Fahrt aus der Historie entfernen.

    Jede Routenberechnung legt bis zu drei Fahrten an (eine je Variante) -
    ohne diesen Endpunkt wächst die Liste mit jedem Versuch, und die eine
    Fahrt, die man wiederfinden will, verschwindet zwischen Entwürfen.
    """
    trip = db.get(models.Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Fahrt nicht gefunden.")
    db.delete(trip)
    db.commit()
    return {"ok": True}


# ---------- intern ----------

def _thin_out_profile(profile: list, at_most: int = 400) -> list:
    """Für die Anzeige reicht ein Bruchteil der Punkte.

    Die Diagramme in der Oberfläche sind ein paar hundert Pixel breit - mehr
    Punkte als Pixel zu übertragen bringt nichts ausser Ladezeit.
    """
    if len(profile) <= at_most:
        return profile
    step = len(profile) / at_most
    selected = [profile[int(i * step)] for i in range(at_most)]
    selected.append(profile[-1])
    return selected


def _response(trip, profile, avg, vehicle) -> dict:
    reserve_point = None
    if profile.reserve_at_km is not None:
        for entry in profile.points:
            if entry.km >= profile.reserve_at_km:
                reserve_point = {"km": entry.km, "lat": entry.lat,
                                 "lon": entry.lon}
                break

    return {
        "demo": routing.is_demo(),
        "distance_km": profile.distance_km,
        "drive_time_minutes": round((trip.drive_time_s or 0) / 60.0),
        "kwh_total": profile.kwh_total,
        "consumption_kwh_100km": profile.consumption_kwh_100km,
        "soc_at_target": profile.soc_at_target,
        "reserve_at_km": profile.reserve_at_km,
        "reserve_point": reserve_point,
        "suffices": profile.reserve_at_km is None,
        "weather": {"temp_c": avg.temp_c,
                   "wind_ms": avg.wind_speed_ms},
        "vehicle": {"id": vehicle.id, "name": vehicle.name,
                     "reserve_soc": vehicle.reserve_soc,
                     "battery_net_kwh": vehicle.battery_net_kwh},
        "geometry": trip.geometry or [],
        "profile": _thin_out_profile(trip.energy_profile or []),
    }
