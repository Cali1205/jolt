"""Den Ladeplan während der Fahrt neu rechnen - Stufe 3.

Der Optimierer aus Stufe 2 plant von einem Start mit einem Ladestand. Genau
das liegt unterwegs auch vor: Die aktuelle Position ist der Start, der
gemeldete Ladestand der Ladestand. Neu geplant wird deshalb nicht die ganze
Fahrt, sondern die **Reststrecke** - und zwar mit dem, was unterwegs gemessen
wurde statt mit dem, was vor der Abfahrt angenommen war.

Zwei Messwerte gehen dabei ein:

- Der **Verbrauchsfaktor** skaliert den Energiebedarf der Reststrecke. Wer
  bisher 20 % mehr gebraucht hat, wird die nächsten hundert Kilometer kaum
  plötzlich sparsam fahren: Tempo, Beladung und Wetter bleiben, was sie sind.
- Der **Zeitfaktor** skaliert die Fahrzeiten. Er ist nicht dasselbe: Im Stau
  steigt der Verbrauch je Kilometer um wenige Prozent, die Fahrzeit aber um
  ein Vielfaches. Ohne ihn wären alle Ankunftszeiten des neuen Plans falsch.

Beide sind die ehrlichste verfügbare Fortschreibung - keine Vorhersage, nur
die Annahme, dass es bleibt, wie es war. Genau deshalb wird der Plan laufend
nachgezogen und nicht einmal perfekt gerechnet.
"""
import logging

from .. import models
from ..energy import model, weather
from ..energy.model import VehicleValues, Environment
from ..geo import haversine_m
from ..charging import curves, optimizer, prices, availability
from ..routing import corridor

log = logging.getLogger("uvicorn.error")

# Vorgaben für die Kandidatensuche, wenn die Sitzung noch keine mitbringt.
# Sie entsprechen den Vorgaben der Oberfläche.
DEFAULTS = {"radius_km": 10.0, "min_kw": 50.0, "connector_type": "",
            "detour_limit_min": optimizer.DETOUR_LIMIT_MIN,
            "stop_fixed_cost_min": optimizer.STOP_FIXED_COST_MIN,
            "charge_park_bonus_min": optimizer.CHARGE_PARK_BONUS_MIN,
            "time_value_eur_h": optimizer.TIME_VALUE_EUR_H}


def read_parameter(plan: dict | None) -> dict:
    """Die Suchparameter aus einem gespeicherten Plan, sonst die Vorgaben.

    Damit wird unterwegs mit demselben Radius und derselben Mindestleistung
    gesucht wie beim Start - ein Plan, der sich mitten in der Fahrt auch noch
    die Auswahlkriterien ändert, wäre nicht mehr nachvollziehbar.
    """
    vals = dict(DEFAULTS)
    for keyname in vals:
        if plan and plan.get(keyname) is not None:
            vals[keyname] = plan[keyname]
    return vals


def rest_from(geometry: list, from_km: float) -> tuple[list, float]:
    """Die Route ab einem Kilometerstand, plus deren tatsächlichen Startwert.

    Zurückgegeben wird der *Stützpunkt* vor `ab_km` und sein Kilometerstand -
    nicht `ab_km` selbst. Nur so beziehen sich Geometrie und Profil hinterher
    auf denselben Nullpunkt; eine Verschiebung zwischen beiden wäre ein
    Versatz in jeder Etappenrechnung.
    """
    if len(geometry) < 2:
        return list(geometry), 0.0
    km = 0.0
    for i in range(1, len(geometry)):
        earlier = km
        km += haversine_m(geometry[i - 1][1], geometry[i - 1][0],
                          geometry[i][1], geometry[i][0]) / 1000.0
        if km >= from_km:
            return geometry[i - 1:], earlier
    # Schon am Ziel - die letzte Kante bleibt übrig, damit es überhaupt eine
    # Strecke gibt.
    return geometry[-2:], km


def remaining_profile(energy_profile: list, from_km: float, consumption_factor: float = 1.0,
               time_factor: float = 1.0) -> optimizer.RouteProfile:
    """Das Streckenprofil der Reststrecke, auf null gesetzt und skaliert."""
    rest = [e for e in energy_profile if (e.get("km") or 0.0) >= from_km]
    if len(rest) < 2:
        rest = energy_profile[-2:] if len(energy_profile) >= 2 else energy_profile
    if len(rest) < 2:
        return optimizer.RouteProfile(km=[], kwh=[], mins=[])

    km0 = rest[0].get("km") or 0.0
    kwh0 = rest[0].get("kwh") or 0.0
    min0 = rest[0].get("mins") or 0.0
    return optimizer.RouteProfile(
        km=[(e.get("km") or 0.0) - km0 for e in rest],
        kwh=[((e.get("kwh") or 0.0) - kwh0) * consumption_factor for e in rest],
        mins=[((e.get("mins") or 0.0) - min0) * time_factor for e in rest])


def environment_on_the_road(trip: models.Trip, points: list):
    """Das Wetter für die Reststrecke - jetzt, nicht bei der Abfahrt.

    Auf achthundert Kilometern liegen zwischen Start und Ziel im Winter
    regelmässig zehn Grad und ein anderer Wind, und die Vorhersage von heute
    früh ist am Nachmittag nicht mehr die von heute früh. Die Abfrage kostet
    einen Aufruf je Umplanung, und umgeplant wird höchstens alle zehn
    Kilometer.

    Fällt sie aus, gilt die Temperatur **dieser Fahrt** und nicht die
    Standardvorgabe von 15 °C: Eine bei -5 °C gerechnete Fahrt auf 15 °C
    zurückzusetzen verlöre die Heizlast und machte die Reststrecke auf dem
    Papier billiger, als sie ist - der Fehler zeigte in genau die Richtung,
    in der er jemanden stehen lässt.
    """
    fallback = Environment()
    if trip.outside_temp_c is not None:
        fallback = Environment(temp_c=trip.outside_temp_c)
    try:
        return weather.along_route(points, preset=fallback)
    except Exception as failure:      # noqa: BLE001
        log.warning("Wetter unterwegs nicht abrufbar: %s", failure)
        return lambda lat, lon: fallback


def remaining_profile_physics(trip: models.Trip, rest: list, speed_factor: float,
                      environment_for) -> optimizer.RouteProfile | None:
    """Die Reststrecke mit dem **gemessenen** Tempo neu durchrechnen.

    Der Unterschied zu `restprofil` ist der zwischen Skalieren und Rechnen.
    Skalieren nimmt das Ergebnis der Planung und multipliziert es; das ist
    richtig, solange man einen gemessenen Verbrauch hat, den man
    fortschreiben will. Wer aber nur weiss, dass er schneller fährt als
    angenommen, kann daraus keinen Energiefaktor machen: Der Luftwiderstand
    geht mit v², der Rollwiderstand nahezu linear, die Nebenverbraucher gar
    nicht mit dem Tempo, sondern mit der Zeit - und die *sinkt*, wenn man
    schneller fährt. Ein pauschaler Aufschlag träfe keinen dieser drei.

    Deshalb wird hier das Modell erneut über die Reststrecke gefahren, mit
    demselben Höhenprofil und denselben Streckengeschwindigkeiten wie bei der
    Planung, nur um den gemessenen Faktor verschoben. Das geht, weil das
    gespeicherte Energieprofil Position, Höhe und Tempo je Stützstelle
    mitführt - es ist für sich allein auswertbar und braucht die
    Routing-Antwort nicht mehr.

    Gibt None zurück, wenn das Profil dafür nicht genug hergibt (Fahrten aus
    der Zeit vor diesen Feldern). Der Aufrufer fällt dann aufs Skalieren
    zurück - eine schlechtere Rechnung ist besser als keine.
    """
    if len(rest) < 2:
        return None
    points, speed_ms = [], []
    for i, entry in enumerate(rest):
        lat, lon = entry.get("lat"), entry.get("lon")
        if lat is None or lon is None:
            return None
        points.append([lon, lat, entry.get("elevation") or 0.0])
        # `tempo_kmh` an einer Stützstelle ist die Geschwindigkeit des
        # Teilstücks, das *dort endet* - siehe modell.profil_rechnen. Für das
        # Teilstück i (von i nach i+1) steht sie also am Punkt i+1.
        if i > 0:
            velocity = entry.get("speed_kmh")
            if not velocity:
                return None
            speed_ms.append(velocity / 3.6)

    fresh = model.compute_profile(
        VehicleValues.from_trip(trip), points, speed_ms,
        # Der Ladestand ist für das Streckenprofil ohne Belang: Gebraucht
        # werden nur die kumulierten kWh und Minuten, und der Energiebedarf
        # einer Etappe hängt nicht davon ab, wie voll der Akku ist.
        start_soc=100.0, environment_for=environment_for, speed_factor=speed_factor)
    if len(fresh.points) < 2:
        return None
    return optimizer.RouteProfile(
        km=[p.km for p in fresh.points],
        kwh=[p.kwh_cumulative for p in fresh.points],
        mins=[p.minutes_cumulative for p in fresh.points])


def search_options(db, geometry: list, vehicle, parameter: dict
                    ) -> list[optimizer.ChargeOption]:
    """Die Ladeoptionen im Korridor der (Rest-)Route."""
    kind = parameter.get("connector_type") or vehicle.connector_type
    candidates = corridor.seek(db, geometry,
                                 radius_km=parameter["radius_km"],
                                 min_kw=parameter["min_kw"], connector_type=kind)
    options = []
    for candidate in candidates:
        lp = candidate.charge_point
        state = availability.REPORTS.state(lp)
        options.append(optimizer.ChargeOption(
            id=lp.id, km_on_route=candidate.km_on_route,
            detour_minutes=candidate.detour_minutes, max_kw=lp.max_kw or 0.0,
            point_count=lp.point_count or 1, name=lp.name or "",
            operator=lp.operator or "", city=lp.city or "",
            lat=lp.lat, lon=lp.lon,
            locked=state.source == "meldung"))
    return options


def schedule(db, trip: models.Trip, from_km: float, start_soc: float,
           parameter: dict, consumption_factor: float = 1.0,
           time_factor: float = 1.0, speed_factor: float | None = None) -> dict:
    """Ein Ladeplan für die Reststrecke ab `ab_km` mit `start_soc`.

    Die Kilometerstände im Ergebnis sind wieder auf die **ganze** Fahrt
    bezogen, nicht auf die Reststrecke: Unterwegs will man wissen, dass der
    Stopp bei km 412 liegt, und nicht bei km 87 der Reststrecke.

    `tempo_faktor` schaltet vom Skalieren aufs Neurechnen um: Statt das
    geplante Profil mit einem Energiefaktor zu multiplizieren, wird das
    Modell mit dem gemessenen Tempo und dem aktuellen Wetter erneut über die
    Reststrecke gefahren. Gedacht ist das für den Fall, dass noch niemand
    einen Ladestand gemeldet hat - dann gibt es keinen Verbrauchsfaktor, und
    die Alternative wäre, weiter mit dem Reglerwert von vor der Abfahrt zu
    rechnen.

    Beides zusammen wäre falsch: Ein gemessener Verbrauch enthält die Wirkung
    des Tempos bereits. Wer zusätzlich das Tempo einrechnet, zählt es
    doppelt. Deshalb ist es ein Entweder-oder, und der Aufrufer entscheidet.
    """
    vehicle = trip.vehicle
    geometry, km0 = rest_from(trip.geometry or [], from_km)

    profile = None
    basis = "verbrauch gemessen" if consumption_factor != 1.0 else "planung"
    if speed_factor is not None:
        rest = [e for e in (trip.energy_profile or [])
                if (e.get("km") or 0.0) >= km0]
        try:
            profile = remaining_profile_physics(trip, rest, speed_factor,
                                       environment_on_the_road(trip, geometry))
        except Exception as failure:      # noqa: BLE001
            # Eine gescheiterte Neurechnung darf die Umplanung nicht kosten -
            # das Skalieren darunter ist schlechter, aber es steht.
            log.warning("Neurechnung mit gemessenem Tempo fehlgeschlagen: %s",
                        failure)
        if profile is not None:
            basis = "tempo gemessen"

    if profile is None:
        profile = remaining_profile(trip.energy_profile or [], km0, consumption_factor,
                            time_factor)

    if not profile.km or len(profile.km) < 2:
        return {**parameter, "feasible": False, "reading_km": round(from_km, 1),
                "reason": "Zur Reststrecke gibt es kein Profil mehr.",
                "stop_count": 0, "stops": []}

    options = search_options(db, geometry, vehicle, parameter)
    plan = optimizer.schedule(
        profile, options, VehicleValues.from_trip(trip),
        curves.as_pairs(vehicle.charge_curve), start_soc=start_soc,
        target_soc=vehicle.target_soc,
        max_vehicle_kw=vehicle.max_charge_power_kw,
        temperature_factor=curves.temperature_factor(
            trip.outside_temp_c if trip.outside_temp_c is not None else 15.0),
        detour_limit_min=parameter["detour_limit_min"],
        stop_fixed_cost_min=parameter["stop_fixed_cost_min"],
        charge_park_bonus_min=parameter["charge_park_bonus_min"],
        price_for=prices.price_function(vehicle),
        time_value_eur_h=parameter["time_value_eur_h"],
        preferred_operators=vehicle.preferred_operators or None,
        # Damit die Begründung dieselben Kilometer nennt wie die Stopps
        # darunter - der Optimierer rechnet auf der Reststrecke ab null.
        km_offset=km0)

    result = plan.as_dict()
    for stop in result["stops"]:
        stop["km_on_route"] = round(stop["km_on_route"] + km0, 1)
        if stop.get("detour_alt"):
            stop["detour_alt"]["km_on_route"] = round(
                stop["detour_alt"]["km_on_route"] + km0, 1)
    return {**parameter, **result, "reading_km": round(km0, 1),
            # Worauf der Plan beruht - damit am Steuer und im Log
            # nachvollziehbar ist, ob hier eine Messung wirkt oder noch der
            # Regler von vor der Abfahrt.
            "basis": basis,
            "speed_factor": speed_factor}


def stops_same(old: dict | None, fresh: dict | None) -> bool:
    """Beschreiben zwei Pläne dieselben Stopps?

    Verglichen werden Standort und Abfahrts-Ladestand, nicht die Ladezeit auf
    die Nachkommastelle: Dass sich eine Standzeit um vierzig Sekunden
    verschiebt, ist keine Änderung, über die jemand am Steuer unterrichtet
    werden will. Ein anderer Standort oder ein spürbar anderer Ladehub schon.
    """
    if old is None or fresh is None:
        return old is fresh
    if bool(old.get("feasible")) != bool(fresh.get("feasible")):
        return False

    a = old.get("stops") or []
    b = fresh.get("stops") or []
    if len(a) != len(b):
        return False
    for one, two in zip(a, b):
        if one.get("id") != two.get("id"):
            return False
        if abs((one.get("departure_soc") or 0) - (two.get("departure_soc") or 0)) > 3.0:
            return False
    return True


def describe_change(old: dict | None, fresh: dict) -> str:
    """Was hat sich geändert - in einem Satz, der am Steuer trägt.

    Kein Diff und keine Liste: Wer fährt, kann einen Satz hören oder im
    Vorbeischauen lesen. Alles Weitere steht in der Ansicht.
    """
    if not fresh.get("feasible"):
        return fresh.get("reason") or "Kein Ladeplan mehr möglich."

    new_stops = fresh.get("stops") or []
    old_stops = (old or {}).get("stops") or []
    if old is not None and not old.get("feasible"):
        return f"Wieder ein Plan möglich: {len(new_stops)} Ladestopp(s)."

    if not new_stops:
        return "Kein Ladestopp mehr nötig."

    first = new_stops[0]
    name = first.get("name") or first.get("operator") or "Ladepunkt"
    if len(new_stops) != len(old_stops):
        direction = "mehr" if len(new_stops) > len(old_stops) else "weniger"
        return (f"{len(new_stops)} Ladestopps statt {len(old_stops)} "
                f"({direction}) - nächster: {name} bei km "
                f"{first.get('km_on_route')}.")
    if old_stops and first.get("id") != old_stops[0].get("id"):
        return f"Nächster Ladestopp jetzt {name} bei km {first.get('km_on_route')}."
    return (f"{name}: laden bis {first.get('departure_soc')} % "
            f"({first.get('charge_time_minutes')} min).")
