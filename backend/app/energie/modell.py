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
tools/check_modell.py sie direkt durchrechnen kann.
"""
import math
from dataclasses import dataclass, field

# Geometrie liegt unter allem und kennt nichts - siehe app/geo.py.
from ..geo import haversine_m, peilung_grad

G = 9.80665
R_LUFT = 287.058          # spezifische Gaskonstante trockener Luft, J/(kg·K)
P0 = 101325.0             # Normdruck auf Meereshöhe, Pa


@dataclass
class Fahrzeugwerte:
    """Die Fahrzeugparameter, losgelöst vom ORM.

    Bewusst eine eigene Struktur: So lässt sich das Modell ohne Datenbank
    prüfen, und ein hypothetisches Fahrzeug ("was wäre mit Dachbox?") ist
    eine Kopie mit geändertem c_w statt eines DB-Eintrags.
    """
    masse_kg: float = 1950.0
    c_w: float = 0.28
    stirnflaeche_m2: float = 2.3
    c_rr: float = 0.010
    eta_antrieb: float = 0.88
    eta_rekup: float = 0.70
    p_neben_w: float = 350.0
    waermepumpe: bool = True
    akku_netto_kwh: float = 60.0
    reserve_soc: float = 10.0
    korrekturfaktor: float = 1.0

    @classmethod
    def aus_fahrt(cls, fahrt) -> "Fahrzeugwerte":
        """Die Werte des Fahrzeugs, angepasst an *diese* Fahrt.

        Zwei Dinge gehören der Fahrt und nicht dem Auto: die Zuladung und
        das, was aussen dranhängt. Beides hier zusammenzuführen ist der
        einzige Weg, der sicherstellt, dass Planung, Umplanung und
        Aufzeichnung mit denselben Werten rechnen - vorher stand
        `aus_modell(fahrzeug)` an fünf Stellen, und ein Träger, der an einer
        davon fehlt, ergibt einen Plan, der unterwegs nicht mehr aufgeht.
        """
        werte = cls.aus_modell(fahrt.fahrzeug)
        if getattr(fahrt, "zuladung_kg", None) is not None:
            werte.masse_kg = fahrt.fahrzeug.leermasse_kg + fahrt.zuladung_kg
        faktor = getattr(fahrt, "luftwiderstand_faktor", None) or 1.0
        # Der Zuschlag geht auf den Beiwert und nicht auf die Stirnfläche -
        # rechnerisch dasselbe, weil beide sich multiplizieren, aber die
        # Stirnfläche ist eine Abmessung des Autos und ändert sich nicht,
        # wenn hinten Räder hängen.
        werte.c_w = werte.c_w * faktor
        return werte

    @classmethod
    def aus_modell(cls, fahrzeug) -> "Fahrzeugwerte":
        return cls(masse_kg=fahrzeug.masse_kg, c_w=fahrzeug.c_w,
                   stirnflaeche_m2=fahrzeug.stirnflaeche_m2, c_rr=fahrzeug.c_rr,
                   eta_antrieb=fahrzeug.eta_antrieb, eta_rekup=fahrzeug.eta_rekup,
                   p_neben_w=fahrzeug.p_neben_w, waermepumpe=fahrzeug.waermepumpe,
                   akku_netto_kwh=fahrzeug.akku_netto_kwh,
                   reserve_soc=fahrzeug.reserve_soc,
                   korrekturfaktor=fahrzeug.korrekturfaktor)


@dataclass
class Umgebung:
    """Wetter an einem Punkt der Strecke."""
    temp_c: float = 15.0
    windgeschwindigkeit_ms: float = 0.0
    # Meteorologisch: die Richtung, aus der der Wind kommt (0 = Nord).
    windrichtung_grad: float = 0.0


@dataclass
class Profilpunkt:
    km: float
    hoehe_m: float
    tempo_kmh: float
    kwh_kumuliert: float
    soc: float
    minuten_kumuliert: float
    # Position mitgeführt, damit das gespeicherte Profil für sich allein
    # auswertbar ist: Die Live-Nachführung und die Reserve-Marke brauchen zu
    # einem Kilometerstand eine Koordinate, ohne die Geometrie erneut
    # abzulaufen - und ohne sich darauf zu verlassen, dass beide Listen
    # dieselbe Länge haben.
    lat: float = 0.0
    lon: float = 0.0

    def als_dict(self) -> dict:
        return {"km": self.km, "soc": self.soc, "kwh": self.kwh_kumuliert,
                "hoehe": self.hoehe_m, "tempo_kmh": self.tempo_kmh,
                "minuten": self.minuten_kumuliert, "lat": self.lat, "lon": self.lon}


@dataclass
class Profil:
    punkte: list[Profilpunkt] = field(default_factory=list)
    kwh_gesamt: float = 0.0
    strecke_km: float = 0.0
    minuten: float = 0.0
    # Kilometerstand, an dem der SoC die Reserve erreicht. None = Ziel wird
    # erreicht, ohne die Reserve anzugreifen.
    reserve_bei_km: float | None = None
    soc_am_ziel: float = 0.0
    verbrauch_kwh_100km: float = 0.0


def luftdichte(temp_c: float, hoehe_m: float) -> float:
    """Luftdichte in kg/m³ aus Temperatur und Höhe.

    Beide Effekte sind gross genug, um sie nicht zu ignorieren: Bei -5 °C ist
    die Luft rund 9 % dichter als bei 20 °C - der Luftwiderstand steigt um
    denselben Anteil. Auf 1500 m Höhe ist sie 15 % dünner.
    """
    # Unter dem Meeresspiegel gilt die barometrische Formel weiter - Höhen
    # unter null wegzuschneiden hiesse, die dichtere Luft in den Niederlanden
    # zu verschenken. Begrenzt wird nur gegen unsinnige Werte aus kaputten
    # Höhendaten, die sonst eine Wurzel aus einer negativen Zahl erzeugen.
    hoehe = max(-500.0, min(hoehe_m, 9000.0))
    druck = P0 * (1.0 - 2.25577e-5 * hoehe) ** 5.25588
    return druck / (R_LUFT * (273.15 + temp_c))


def hvac_leistung_w(temp_c: float, waermepumpe: bool = True) -> float:
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
    heizbedarf = (18.0 - temp_c) * 300.0
    if waermepumpe:
        heizbedarf *= 0.5
    return min(6000.0, 150.0 + heizbedarf)


def gegenwind_ms(peilung: float, umgebung: Umgebung) -> float:
    """Gegenwindkomponente in m/s (negativ = Rückenwind).

    Der Faktor 0,7 rechnet die in 10 m Höhe gemessene Windgeschwindigkeit auf
    Fahrzeughöhe herunter - dort ist es durch Bodenreibung, Bewuchs und
    Leitplanken spürbar ruhiger. Ohne diese Korrektur überschätzt das Modell
    den Wind systematisch.
    """
    if umgebung.windgeschwindigkeit_ms <= 0:
        return 0.0
    differenz = math.radians(umgebung.windrichtung_grad - peilung)
    return 0.7 * umgebung.windgeschwindigkeit_ms * math.cos(differenz)


def segment_wh(fz: Fahrzeugwerte, strecke_m: float, hoehe_delta_m: float,
               tempo_ms: float, umgebung: Umgebung, hoehe_m: float,
               peilung: float = 0.0) -> tuple[float, float]:
    """Energiebedarf eines Segments in Wh und seine Dauer in Sekunden.

    Rückgabe kann negativ sein: eine lange Abfahrt speist mehr zurück, als
    die Nebenverbraucher in dieser Zeit ziehen.
    """
    if strecke_m <= 0 or tempo_ms <= 0:
        return 0.0, 0.0

    dauer_s = strecke_m / tempo_ms
    strecke_3d = math.hypot(strecke_m, hoehe_delta_m)
    sin_theta = hoehe_delta_m / strecke_3d if strecke_3d else 0.0
    cos_theta = strecke_m / strecke_3d if strecke_3d else 1.0

    rho = luftdichte(umgebung.temp_c, hoehe_m)
    v_luft = tempo_ms + gegenwind_ms(peilung, umgebung)
    # Quadrat mit Vorzeichen: starker Rückenwind schiebt, er bremst nicht.
    f_luft = 0.5 * rho * fz.c_w * fz.stirnflaeche_m2 * v_luft * abs(v_luft)
    f_roll = fz.c_rr * fz.masse_kg * G * cos_theta
    f_steig = fz.masse_kg * G * sin_theta

    f_gesamt = f_roll + f_luft + f_steig
    arbeit_j = f_gesamt * strecke_3d

    if arbeit_j >= 0:
        rad_j = arbeit_j / fz.eta_antrieb
    else:
        # Bergab: nur der rekuperierte Anteil kommt zurück. Was darüber
        # hinausginge, verbrennt die Reibungsbremse - deshalb keine
        # vollständige Rückgewinnung.
        rad_j = arbeit_j * fz.eta_rekup

    neben_j = (fz.p_neben_w + hvac_leistung_w(umgebung.temp_c, fz.waermepumpe)) * dauer_s
    wh = (rad_j + neben_j) / 3600.0
    return wh * fz.korrekturfaktor, dauer_s


def profil_rechnen(fz: Fahrzeugwerte, punkte: list, tempo_ms: list,
                   start_soc: float, umgebung_fuer=None,
                   tempo_faktor: float = 1.0,
                   strecke_faktor: float = 1.0) -> Profil:
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
                    Fahrzeugs weiss es besser; `live/aufzeichnung.py` bildet
                    daraus den Faktor. Er wirkt auf Roll- und Luftwiderstand
                    wie auf die Steigung - eine längere Strecke bei gleichem
                    Höhenunterschied ist eine flachere Steigung, und genau so
                    war sie auch gefahren.
    """
    standard = Umgebung()
    hole_umgebung = umgebung_fuer or (lambda lat, lon: standard)

    kapazitaet_wh = fz.akku_netto_kwh * 1000.0
    soc = start_soc
    kwh_kum = 0.0
    meter_kum = 0.0
    sekunden_kum = 0.0
    reserve_bei_km = None

    ergebnis = Profil()
    if not punkte:
        return ergebnis

    hoehe0 = punkte[0][2] if len(punkte[0]) > 2 else 0.0
    ergebnis.punkte.append(Profilpunkt(0.0, hoehe0, 0.0, 0.0, soc, 0.0,
                                       lat=punkte[0][1], lon=punkte[0][0]))

    for i in range(len(punkte) - 1):
        lon1, lat1 = punkte[i][0], punkte[i][1]
        lon2, lat2 = punkte[i + 1][0], punkte[i + 1][1]
        h1 = punkte[i][2] if len(punkte[i]) > 2 else 0.0
        h2 = punkte[i + 1][2] if len(punkte[i + 1]) > 2 else 0.0

        strecke = haversine_m(lat1, lon1, lat2, lon2) * strecke_faktor
        if strecke <= 0:
            continue

        v = (tempo_ms[i] if i < len(tempo_ms) else 25.0) * tempo_faktor
        v = max(2.0, v)
        umgebung = hole_umgebung(lat1, lon1)
        peilung = peilung_grad(lat1, lon1, lat2, lon2)

        wh, dauer = segment_wh(fz, strecke, h2 - h1, v, umgebung,
                               (h1 + h2) / 2.0, peilung)

        kwh_kum += wh / 1000.0
        meter_kum += strecke
        sekunden_kum += dauer
        soc = start_soc - (kwh_kum * 1000.0 / kapazitaet_wh) * 100.0

        if reserve_bei_km is None and soc <= fz.reserve_soc:
            reserve_bei_km = meter_kum / 1000.0

        ergebnis.punkte.append(Profilpunkt(
            km=round(meter_kum / 1000.0, 3), hoehe_m=h2,
            tempo_kmh=round(v * 3.6, 1), kwh_kumuliert=round(kwh_kum, 3),
            soc=round(soc, 2), minuten_kumuliert=round(sekunden_kum / 60.0, 1),
            lat=lat2, lon=lon2))

    ergebnis.kwh_gesamt = round(kwh_kum, 3)
    ergebnis.strecke_km = round(meter_kum / 1000.0, 2)
    ergebnis.minuten = round(sekunden_kum / 60.0, 1)
    ergebnis.reserve_bei_km = round(reserve_bei_km, 2) if reserve_bei_km else None
    ergebnis.soc_am_ziel = round(soc, 2)
    if meter_kum > 0:
        ergebnis.verbrauch_kwh_100km = round(kwh_kum / (meter_kum / 1000.0) * 100.0, 2)
    return ergebnis


def ausduennen(punkte: list, tempo_ms: list, mindestabstand_m: float = 250.0):
    """Stützpunkte zusammenfassen, ohne das Höhenprofil zu verlieren.

    Das Routing liefert auf einer Langstrecke fünfstellig viele Punkte - für
    Karte und Prognose ist das Rechenzeit ohne Erkenntnis. Zusammengefasst
    wird nur, solange die Höhe sich kaum ändert: Ein Punkt, der mehr als drei
    Meter vom letzten abweicht, bleibt in jedem Fall stehen. Sonst würde
    gerade das verschwinden, was den Verbrauch am stärksten treibt.
    """
    if len(punkte) < 3:
        return punkte, tempo_ms

    neue_punkte = [punkte[0]]
    neue_tempo: list[float] = []
    strecke_seit = 0.0
    tempo_summe = 0.0
    gewicht = 0.0
    letzte_hoehe = punkte[0][2] if len(punkte[0]) > 2 else 0.0

    for i in range(len(punkte) - 1):
        lon1, lat1 = punkte[i][0], punkte[i][1]
        lon2, lat2 = punkte[i + 1][0], punkte[i + 1][1]
        d = haversine_m(lat1, lon1, lat2, lon2)
        v = tempo_ms[i] if i < len(tempo_ms) else 25.0
        strecke_seit += d
        tempo_summe += v * d
        gewicht += d
        hoehe = punkte[i + 1][2] if len(punkte[i + 1]) > 2 else 0.0

        letzter = (i + 1) == len(punkte) - 1
        if (strecke_seit >= mindestabstand_m or abs(hoehe - letzte_hoehe) > 3.0
                or letzter):
            neue_punkte.append(punkte[i + 1])
            neue_tempo.append(tempo_summe / gewicht if gewicht else v)
            strecke_seit = 0.0
            tempo_summe = 0.0
            gewicht = 0.0
            letzte_hoehe = hoehe

    return neue_punkte, neue_tempo
