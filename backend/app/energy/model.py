"""Das Verbrauchsmodell - der Kern von jolt.

Gerechnet wird je Streckensegment die Physik, nicht ein pauschaler Wert in
kWh/100 km. Der Grund steht ausführlich in konzept-routenplaner.md, kurz:

- Luftwiderstand geht mit v² ein. Der Unterschied zwischen 110 und 130 km/h
  ist keine 18 %, sondern 40 %. Ohne das lässt sich die einzige Frage, die
  man bei 12 % Restladung noch beeinflussen kann - "schaffe ich es, wenn ich
  langsamer fahre?" - nicht beantworten.
- Steigung schlägt bei 1,8 t härter zu als Roll- und Luftwiderstand zusammen,
  und die Rekuperation bergab holt nur rund 70 % zurück. Über einen Pass ist
  die Bilanz deshalb negativ, obwohl man wieder auf Ausgangshöhe ankommt.
- Die Nebenverbraucher hängen an der Zeit, nicht an der Strecke. Im Stau
  heizt das Auto weiter, ohne Kilometer zu machen - der Grund, warum
  Winterfahrten mit Stau jede Prognose reissen.

Alle Funktionen hier sind rein und ohne Datenbank- oder Netzzugriff, damit
tools/check_model.py sie direkt durchrechnen kann.
"""
import math
from dataclasses import dataclass, field

# Geometrie liegt unter allem und kennt nichts - siehe app/geo.py.
from ..geo import haversine_m, bearing_degree

G = 9.80665
R_AIR = 287.058          # spezifische Gaskonstante trockener Luft, J/(kg·K)
P0 = 101325.0             # Normdruck auf Meereshöhe, Pa


@dataclass
class VehicleValues:
    """Die Fahrzeugparameter, losgelöst vom ORM.

    Bewusst eine eigene Struktur: So lässt sich das Modell ohne Datenbank
    prüfen, und ein hypothetisches Fahrzeug ("was wäre mit Dachbox?") ist
    eine Kopie mit geändertem c_w statt eines DB-Eintrags.
    """
    mass_kg: float = 1950.0
    c_w: float = 0.28
    frontal_area_m2: float = 2.3
    c_rr: float = 0.010
    eta_drive: float = 0.88
    eta_regen: float = 0.70
    p_aux_w: float = 350.0
    heat_pump: bool = True
    battery_net_kwh: float = 60.0
    reserve_soc: float = 10.0
    correction_factor: float = 1.0
    # Ein Anhänger ist eine eigene Grösse und kein verbogener cw-Wert des
    # Autos: Er bringt Masse (steckt schon in `masse_kg`) und eine eigene
    # Luftwiderstandsfläche mit, c_w mal A in m². Sie wird zu der des Autos
    # **addiert**; dahinter fährt der Anhänger im Windschatten, deshalb ist
    # sie kleiner als seine Stirnfläche allein.
    cwa_extra_m2: float = 0.0
    # Harte Obergrenze in m/s, None = keine. Der Tempo-Regler skaliert nur
    # die Annahme des Routings und kennt keine Grenze: bei 130 % rechnete das
    # Modell mit 165 km/h, die kein Serienfahrzeug fährt - und ein Gespann
    # darf in Deutschland 100.
    speed_max_ms: float | None = None

    @classmethod
    def from_trip(cls, trip) -> "VehicleValues":
        """Die Werte des Fahrzeugs, angepasst an *diese* Fahrt.

        Zwei Dinge gehören der Fahrt und nicht dem Auto: die Zuladung und
        das, was aussen dranhängt. Beides hier zusammenzuführen ist der
        einzige Weg, der sicherstellt, dass Planung, Umplanung und
        Aufzeichnung mit denselben Werten rechnen - vorher stand
        `aus_modell(fahrzeug)` an fünf Stellen, und ein Träger, der an einer
        davon fehlt, ergibt einen Plan, der unterwegs nicht mehr aufgeht.
        """
        vals = cls.from_model(trip.vehicle)
        if getattr(trip, "payload_kg", None) is not None:
            vals.mass_kg = trip.vehicle.curb_mass_kg + trip.payload_kg
        factor = getattr(trip, "air_drag_factor", None) or 1.0
        # Der Zuschlag geht auf den Beiwert und nicht auf die Stirnfläche -
        # rechnerisch dasselbe, weil beide sich multiplizieren, aber die
        # Stirnfläche ist eine Abmessung des Autos und ändert sich nicht,
        # wenn hinten Räder hängen.
        vals.c_w = vals.c_w * factor

        trailer_kg = getattr(trip, "trailer_kg", None)
        if trailer_kg:
            vals.mass_kg += trailer_kg
            vals.cwa_extra_m2 = getattr(trip, "trailer_cwa_m2", None) or 0.0
        # Die Grenze der Fahrt und die des Fahrzeugs: Es gilt die kleinere.
        limits = [g for g in (getattr(trip, "speed_max_kmh", None),
                               vals.speed_max_ms and vals.speed_max_ms * 3.6)
                   if g]
        vals.speed_max_ms = min(limits) / 3.6 if limits else None
        return vals

    @classmethod
    def from_model(cls, vehicle) -> "VehicleValues":
        return cls(mass_kg=vehicle.mass_kg, c_w=vehicle.c_w,
                   frontal_area_m2=vehicle.frontal_area_m2, c_rr=vehicle.c_rr,
                   eta_drive=vehicle.eta_drive, eta_regen=vehicle.eta_regen,
                   p_aux_w=vehicle.p_aux_w, heat_pump=vehicle.heat_pump,
                   # Die gemessene Kapazitaet, wenn es sie gibt - siehe
                   # `models.Fahrzeug.kapazitaet_kwh`. Damit zieht die ganze
                   # Kette mit: Verbrauchsmodell, Ladeplan, Prognose.
                   battery_net_kwh=getattr(vehicle, "capacity_kwh",
                                          vehicle.battery_net_kwh),
                   reserve_soc=vehicle.reserve_soc,
                   correction_factor=vehicle.correction_factor,
                   speed_max_ms=(getattr(vehicle, "max_speed_kmh", None) or 0)
                   / 3.6 or None)


@dataclass
class Environment:
    """Wetter an einem Punkt der Strecke."""
    temp_c: float = 15.0
    wind_speed_ms: float = 0.0
    # Meteorologisch: die Richtung, aus der der Wind kommt (0 = Nord).
    wind_direction_degree: float = 0.0


@dataclass
class ProfilePoint:
    km: float
    elevation_m: float
    speed_kmh: float
    kwh_cumulative: float
    soc: float
    minutes_cumulative: float
    # Position mitgeführt, damit das gespeicherte Profil für sich allein
    # auswertbar ist: Die Live-Nachführung und die Reserve-Marke brauchen zu
    # einem Kilometerstand eine Koordinate, ohne die Geometrie erneut
    # abzulaufen - und ohne sich darauf zu verlassen, dass beide Listen
    # dieselbe Länge haben.
    lat: float = 0.0
    lon: float = 0.0

    def as_dict(self) -> dict:
        return {"km": self.km, "soc": self.soc, "kwh": self.kwh_cumulative,
                "elevation": self.elevation_m, "speed_kmh": self.speed_kmh,
                "mins": self.minutes_cumulative, "lat": self.lat, "lon": self.lon}


@dataclass
class Profile:
    points: list[ProfilePoint] = field(default_factory=list)
    kwh_total: float = 0.0
    distance_km: float = 0.0
    mins: float = 0.0
    # Fahrzeit ohne die Tempo-Obergrenze. Das Verhältnis `minuten` zu diesem
    # Wert ist, um wieviel die Grenze die Fahrt verlängert - und damit der
    # Faktor, mit dem sich die Zeit des Routings entsprechend strecken lässt.
    minutes_without_cap: float = 0.0
    # Kilometerstand, an dem der SoC die Reserve erreicht. None = Ziel wird
    # erreicht, ohne die Reserve anzugreifen.
    reserve_at_km: float | None = None
    soc_at_target: float = 0.0
    consumption_kwh_100km: float = 0.0


def air_density(temp_c: float, elevation_m: float) -> float:
    """Luftdichte in kg/m³ aus Temperatur und Höhe.

    Beide Effekte sind gross genug, um sie nicht zu ignorieren: Bei -5 °C ist
    die Luft rund 9 % dichter als bei 20 °C - der Luftwiderstand steigt um
    denselben Anteil. Auf 1500 m Höhe ist sie 15 % dünner.
    """
    # Unter dem Meeresspiegel gilt die barometrische Formel weiter - Höhen
    # unter null wegzuschneiden hiesse, die dichtere Luft in den Niederlanden
    # zu verschenken. Begrenzt wird nur gegen unsinnige Werte aus kaputten
    # Höhendaten, die sonst eine Wurzel aus einer negativen Zahl erzeugen.
    elevation = max(-500.0, min(elevation_m, 9000.0))
    pressure = P0 * (1.0 - 2.25577e-5 * elevation) ** 5.25588
    return pressure / (R_AIR * (273.15 + temp_c))


def hvac_power_w(temp_c: float, heat_pump: bool = True) -> float:
    """Leistung für Heizung bzw. Klimaanlage.

    Eine Näherung, kein Modell der Kabine: Der tatsächliche Bedarf hängt an
    Sonne, Insassen und daran, wie oft die Türen aufgehen. Die Grössenordnung
    stimmt aber, und sie ist die zweitgrösste Fehlerquelle nach dem Tempo.

    Der Wert wird über die Kalibrierung an das eigene Auto herangeführt -
    hier steht nur der Startpunkt.
    """
    if temp_c >= 25.0:
        # Klimaanlage: deutlich sparsamer als Heizen, weil die Wärmepumpe
        # ohnehin in dieser Richtung arbeitet.
        return min(2500.0, 400.0 + (temp_c - 25.0) * 160.0)
    if temp_c >= 18.0:
        return 150.0        # nur Gebläse
    heating_demand = (18.0 - temp_c) * 300.0
    if heat_pump:
        heating_demand *= 0.5
    return min(6000.0, 150.0 + heating_demand)


def headwind_ms(bearing: float, environment: Environment) -> float:
    """Gegenwindkomponente in m/s (negativ = Rückenwind).

    Der Faktor 0,7 rechnet die in 10 m Höhe gemessene Windgeschwindigkeit auf
    Fahrzeughöhe herunter - dort ist es durch Bodenreibung, Bewuchs und
    Leitplanken spürbar ruhiger. Ohne diese Korrektur überschätzt das Modell
    den Wind systematisch.
    """
    if environment.wind_speed_ms <= 0:
        return 0.0
    difference = math.radians(environment.wind_direction_degree - bearing)
    return 0.7 * environment.wind_speed_ms * math.cos(difference)


def segment_wh(fz: VehicleValues, distance_m: float, elevation_delta_m: float,
               speed_ms: float, environment: Environment, elevation_m: float,
               bearing: float = 0.0) -> tuple[float, float]:
    """Energiebedarf eines Segments in Wh und seine Dauer in Sekunden.

    Rückgabe kann negativ sein: eine lange Abfahrt speist mehr zurück, als
    die Nebenverbraucher in dieser Zeit ziehen.
    """
    if distance_m <= 0 or speed_ms <= 0:
        return 0.0, 0.0

    duration_s = distance_m / speed_ms
    distance_3d = math.hypot(distance_m, elevation_delta_m)
    sin_theta = elevation_delta_m / distance_3d if distance_3d else 0.0
    cos_theta = distance_m / distance_3d if distance_3d else 1.0

    rho = air_density(environment.temp_c, elevation_m)
    v_air = speed_ms + headwind_ms(bearing, environment)
    # Quadrat mit Vorzeichen: starker Rückenwind schiebt, er bremst nicht.
    cwa = fz.c_w * fz.frontal_area_m2 + fz.cwa_extra_m2
    f_air = 0.5 * rho * cwa * v_air * abs(v_air)
    f_roll = fz.c_rr * fz.mass_kg * G * cos_theta
    f_climb = fz.mass_kg * G * sin_theta

    f_total = f_roll + f_air + f_climb
    work_j = f_total * distance_3d

    if work_j >= 0:
        rad_j = work_j / fz.eta_drive
    else:
        # Bergab: nur der rekuperierte Anteil kommt zurück. Was darüber
        # hinausginge, verbrennt die Reibungsbremse - deshalb keine
        # vollständige Rückgewinnung.
        rad_j = work_j * fz.eta_regen

    aux_j = (fz.p_aux_w + hvac_power_w(environment.temp_c, fz.heat_pump)) * duration_s
    wh = (rad_j + aux_j) / 3600.0
    return wh * fz.correction_factor, duration_s


def compute_profile(fz: VehicleValues, points: list, speed_ms: list,
                   start_soc: float, environment_for=None,
                   speed_factor: float = 1.0,
                   distance_factor: float = 1.0,
                   speed_cap: bool = True) -> Profile:
    """Das Energieprofil über die gesamte Route.

    `punkte`      : [[lon, lat, hoehe], ...] aus dem Routing
    `tempo_ms`    : Geschwindigkeit je Teilstück, Länge len(punkte) - 1
    `umgebung_fuer`: Funktion (lat, lon) -> Umgebung. None = Standardwetter.
    `tempo_faktor`: 1.1 heisst "zehn Prozent schneller als das Routing annimmt".
                    Genau der Regler, mit dem man unterwegs einen Ladestopp
                    einsparen kann - er wirkt über v² überproportional.
    `strecke_faktor`: Streckt jedes Teilstück. Gebraucht für **Aufzeichnungen**,
                    deren Stützpunkte weit auseinanderliegen: Zwischen zwei
                    GPS-Meldungen im Abstand von dreissig Sekunden liegen bei
                    Landstrassentempo vierhundert Meter, und die Luftlinie
                    dazwischen schneidet jede Kurve ab. Der Kilometerstand des
                    Fahrzeugs weiss es besser; `live/recording.py` bildet
                    daraus den Faktor. Er wirkt auf Roll- und Luftwiderstand
                    wie auf die Steigung - eine längere Strecke bei gleichem
                    Höhenunterschied ist eine flachere Steigung, und genau so
                    war sie auch gefahren.
    `tempo_deckel`: Wendet `fz.tempo_max_ms` an. Aus für **Aufzeichnungen**:
                    Was gefahren wurde, wird nicht nachträglich auf die
                    Grenze zurechtgestutzt - sonst stimmte der gemessene
                    Verbrauch nicht mehr zur gemessenen Strecke.
    """
    std_default = Environment()
    fetch_environment = environment_for or (lambda lat, lon: std_default)

    capacity_wh = fz.battery_net_kwh * 1000.0
    soc = start_soc
    kwh_cum = 0.0
    meter_cum = 0.0
    seconds_cum = 0.0
    seconds_without_cap = 0.0
    reserve_at_km = None

    result = Profile()
    if not points:
        return result

    hoehe0 = points[0][2] if len(points[0]) > 2 else 0.0
    result.points.append(ProfilePoint(0.0, hoehe0, 0.0, 0.0, soc, 0.0,
                                       lat=points[0][1], lon=points[0][0]))

    for i in range(len(points) - 1):
        lon1, lat1 = points[i][0], points[i][1]
        lon2, lat2 = points[i + 1][0], points[i + 1][1]
        h1 = points[i][2] if len(points[i]) > 2 else 0.0
        h2 = points[i + 1][2] if len(points[i + 1]) > 2 else 0.0

        distance = haversine_m(lat1, lon1, lat2, lon2) * distance_factor
        if distance <= 0:
            continue

        v = (speed_ms[i] if i < len(speed_ms) else 25.0) * speed_factor
        v = max(2.0, v)
        v_free = v
        if speed_cap and fz.speed_max_ms:
            v = max(2.0, min(v, fz.speed_max_ms))
        environment = fetch_environment(lat1, lon1)
        bearing = bearing_degree(lat1, lon1, lat2, lon2)

        wh, duration = segment_wh(fz, distance, h2 - h1, v, environment,
                               (h1 + h2) / 2.0, bearing)

        kwh_cum += wh / 1000.0
        meter_cum += distance
        seconds_cum += duration
        seconds_without_cap += distance / v_free
        soc = start_soc - (kwh_cum * 1000.0 / capacity_wh) * 100.0

        if reserve_at_km is None and soc <= fz.reserve_soc:
            reserve_at_km = meter_cum / 1000.0

        result.points.append(ProfilePoint(
            km=round(meter_cum / 1000.0, 3), elevation_m=h2,
            speed_kmh=round(v * 3.6, 1), kwh_cumulative=round(kwh_cum, 3),
            soc=round(soc, 2), minutes_cumulative=round(seconds_cum / 60.0, 1),
            lat=lat2, lon=lon2))

    result.kwh_total = round(kwh_cum, 3)
    result.distance_km = round(meter_cum / 1000.0, 2)
    result.mins = round(seconds_cum / 60.0, 1)
    result.minutes_without_cap = round(seconds_without_cap / 60.0, 1)
    result.reserve_at_km = round(reserve_at_km, 2) if reserve_at_km else None
    result.soc_at_target = round(soc, 2)
    if meter_cum > 0:
        result.consumption_kwh_100km = round(kwh_cum / (meter_cum / 1000.0) * 100.0, 2)
    return result


def thin_out(points: list, speed_ms: list, min_distance_m: float = 250.0):
    """Stützpunkte zusammenfassen, ohne das Höhenprofil zu verlieren.

    Das Routing liefert auf einer Langstrecke fünfstellig viele Punkte - für
    Karte und Prognose ist das Rechenzeit ohne Erkenntnis. Zusammengefasst
    wird nur, solange die Höhe sich kaum ändert: Ein Punkt, der mehr als drei
    Meter vom letzten abweicht, bleibt in jedem Fall stehen. Sonst würde
    gerade das verschwinden, was den Verbrauch am stärksten treibt.
    """
    if len(points) < 3:
        return points, speed_ms

    new_points = [points[0]]
    new_speed: list[float] = []
    distance_since = 0.0
    speed_sum = 0.0
    weight = 0.0
    latest_elevation = points[0][2] if len(points[0]) > 2 else 0.0

    for i in range(len(points) - 1):
        lon1, lat1 = points[i][0], points[i][1]
        lon2, lat2 = points[i + 1][0], points[i + 1][1]
        d = haversine_m(lat1, lon1, lat2, lon2)
        v = speed_ms[i] if i < len(speed_ms) else 25.0
        distance_since += d
        speed_sum += v * d
        weight += d
        elevation = points[i + 1][2] if len(points[i + 1]) > 2 else 0.0

        last = (i + 1) == len(points) - 1
        if (distance_since >= min_distance_m or abs(elevation - latest_elevation) > 3.0
                or last):
            new_points.append(points[i + 1])
            new_speed.append(speed_sum / weight if weight else v)
            distance_since = 0.0
            speed_sum = 0.0
            weight = 0.0
            latest_elevation = elevation

    return new_points, new_speed
