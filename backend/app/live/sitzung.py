"""Die Live-Nachführung: Ist gegen Soll, während gefahren wird.

Der Grund für das ganze Projekt. Ein Plan, der bei Abfahrt gerechnet wurde,
ist nach achtzig Kilometern falsch - Tempo, Temperatur, Wind und Stau
addieren sich in dieselbe Richtung. Wer das merkt, braucht keinen Puffer von
zwanzig Prozent; wer es nicht merkt, steht mit vier Prozent an einer belegten
Säule.

Was hier passiert: Zu jedem Messpunkt wird bestimmt, wo auf der Route er
liegt, was der Plan an dieser Stelle vorhergesagt hatte, und wie weit die
Wirklichkeit davon abweicht. Daraus entsteht ein laufender Verbrauchsfaktor,
mit dem der Rest der Strecke neu gerechnet wird.

Die eigentliche Umplanung der Ladestopps ist Stufe 3 (siehe
konzept-routenplaner.md). Was hier bereits vollständig ist: die Messung, der
Faktor, und die Erkennung, *dass* neu geplant werden müsste - inklusive des
Grundes.
"""
import logging
from dataclasses import asdict, dataclass

from .. import models
from ..routing.korridor import punkt_auf_route

log = logging.getLogger("uvicorn.error")

# Schwellen für die Neuplanung. Bewusst Schwellen und keine Neuberechnung bei
# jeder Messung: Ein Plan, der sich alle dreissig Sekunden ändert, ist kein
# Plan. Wer gerade beschlossen hat, in 40 km Pause zu machen, soll das nicht
# dreimal umwerfen müssen. Eine Änderung muss etwas bedeuten.
SCHWELLE_SOC_PP = 5.0            # Prozentpunkte Abweichung
SCHWELLE_ABWEG_M = 500.0         # Abstand zur Route
SCHWELLE_ANKUNFT_MIN = 10.0      # Verschiebung der Ankunftszeit

# Über wie viele Kilometer der Verbrauchsfaktor gemittelt wird. Zu kurz, und
# eine einzelne Ampelphase verbiegt ihn; zu lang, und der Wetterumschwung
# hinter dem Pass kommt zu spät an.
FENSTER_KM = 25.0
# Vorher ist die SoC-Anzeige (meist 1 % Auflösung) zu grob für eine Aussage.
MINDESTSTRECKE_KM = 5.0


@dataclass
class Zustand:
    km_auf_route: float
    abstand_zur_route_m: float
    ist_soc: float
    soll_soc: float | None
    abweichung_pp: float | None
    verbrauchsfaktor: float
    rest_km: float
    prognose_soc_am_ziel: float | None
    reserve_bei_km: float | None
    neuplanung_noetig: bool
    grund: str


def soll_soc_bei(energieprofil: list, km: float) -> float | None:
    """Den geplanten Ladestand an einem Kilometerstand nachschlagen.

    Zwischen zwei Profilpunkten wird linear interpoliert - die liegen rund
    250 m auseinander, da ist die Gerade genau genug.
    """
    if not energieprofil:
        return None
    if km <= energieprofil[0].get("km", 0.0):
        return energieprofil[0].get("soc")
    for vorher, nachher in zip(energieprofil, energieprofil[1:]):
        km1, km2 = vorher.get("km", 0.0), nachher.get("km", 0.0)
        if km1 <= km <= km2:
            if km2 <= km1:
                return vorher.get("soc")
            anteil = (km - km1) / (km2 - km1)
            s1, s2 = vorher.get("soc", 0.0), nachher.get("soc", 0.0)
            return round(s1 + (s2 - s1) * anteil, 2)
    return energieprofil[-1].get("soc")


def _verbrauchsfaktor(punkte: list) -> float | None:
    """Ist-Verbrauch geteilt durch Soll-Verbrauch über das gleitende Fenster.

    Gerechnet wird über SoC-Differenzen und nicht über absolute Werte: Ein
    Tacho, der grundsätzlich zwei Prozent zu hoch anzeigt, verfälscht die
    Differenz nicht - den absoluten Vergleich aber schon.
    """
    brauchbar = [p for p in punkte
                 if p.km_auf_route is not None and p.soll_soc is not None]
    if len(brauchbar) < 2:
        return None

    letzter = brauchbar[-1]
    fenster = [p for p in brauchbar
               if (letzter.km_auf_route - p.km_auf_route) <= FENSTER_KM]
    if len(fenster) < 2:
        fenster = brauchbar[-2:]

    erster = fenster[0]
    strecke = letzter.km_auf_route - erster.km_auf_route
    if strecke < MINDESTSTRECKE_KM:
        return None

    ist_verbrauch = erster.soc - letzter.soc
    soll_verbrauch = (erster.soll_soc or 0.0) - (letzter.soll_soc or 0.0)
    if soll_verbrauch <= 0.5:
        return None

    faktor = ist_verbrauch / soll_verbrauch
    # Ausreisser abfangen: Wer zwischendurch geladen hat, produziert einen
    # negativen "Verbrauch". Das ist kein Modellfehler, sondern ein Ladestopp.
    if not 0.4 <= faktor <= 2.5:
        return None
    return round(faktor, 3)


def messpunkt_aufnehmen(db, sitzung: models.LiveSitzung, lat: float, lon: float,
                        soc: float, tempo_kmh: float | None = None,
                        aussentemp_c: float | None = None) -> Zustand:
    """Einen Messpunkt einsortieren und den neuen Zustand zurückgeben."""
    fahrt = sitzung.fahrt
    geometrie = fahrt.geometrie or []
    profil = fahrt.energieprofil or []

    km, abstand = punkt_auf_route(geometrie, lat, lon)
    soll = soll_soc_bei(profil, km)

    punkt = models.LivePunkt(lat=lat, lon=lon, soc=soc, tempo_kmh=tempo_kmh,
                             aussentemp_c=aussentemp_c, km_auf_route=km,
                             soll_soc=soll)
    # Über die Beziehung anhängen und nicht über db.add(): Sonst steht der
    # Punkt zweimal in der geladenen Sammlung - einmal durch das Anhängen,
    # einmal durch die Kaskade - und der Verbrauchsfaktor rechnet mit einem
    # Duplikat.
    sitzung.punkte.append(punkt)
    db.flush()

    faktor = _verbrauchsfaktor(sitzung.punkte)
    if faktor is not None:
        sitzung.verbrauchsfaktor = faktor

    zustand = _zustand_bilden(sitzung, punkt, abstand)
    sitzung.hinweis = zustand.grund
    db.commit()
    return zustand


def _zustand_bilden(sitzung: models.LiveSitzung, punkt: models.LivePunkt,
                    abstand_m: float) -> Zustand:
    fahrt = sitzung.fahrt
    fahrzeug = fahrt.fahrzeug
    profil = fahrt.energieprofil or []
    gesamt_km = profil[-1]["km"] if profil else 0.0
    rest_km = max(0.0, gesamt_km - (punkt.km_auf_route or 0.0))

    abweichung = None
    if punkt.soll_soc is not None:
        abweichung = round(punkt.soc - punkt.soll_soc, 2)

    # Der Rest der Strecke mit dem gemessenen Faktor neu hochgerechnet.
    prognose = None
    reserve_bei = None
    if profil:
        rest_soll = (punkt.soll_soc or punkt.soc) - (profil[-1].get("soc") or 0.0)
        prognose = round(punkt.soc - rest_soll * sitzung.verbrauchsfaktor, 2)

        for eintrag in profil:
            if (eintrag.get("km") or 0.0) < (punkt.km_auf_route or 0.0):
                continue
            verbraucht = ((punkt.soll_soc or punkt.soc) - (eintrag.get("soc") or 0.0))
            hochgerechnet = punkt.soc - verbraucht * sitzung.verbrauchsfaktor
            if hochgerechnet <= fahrzeug.reserve_soc:
                reserve_bei = round(eintrag.get("km") or 0.0, 1)
                break

    noetig, grund = _neuplanung_pruefen(fahrzeug, abweichung, abstand_m,
                                        prognose, reserve_bei, gesamt_km)
    return Zustand(km_auf_route=round(punkt.km_auf_route or 0.0, 2),
                   abstand_zur_route_m=round(abstand_m),
                   ist_soc=round(punkt.soc, 2), soll_soc=punkt.soll_soc,
                   abweichung_pp=abweichung,
                   verbrauchsfaktor=round(sitzung.verbrauchsfaktor, 3),
                   rest_km=round(rest_km, 1), prognose_soc_am_ziel=prognose,
                   reserve_bei_km=reserve_bei, neuplanung_noetig=noetig,
                   grund=grund)


def _neuplanung_pruefen(fahrzeug, abweichung, abstand_m, prognose, reserve_bei,
                        gesamt_km) -> tuple[bool, str]:
    """Muss der Plan angefasst werden - und warum?

    Die Reihenfolge ist die der Dringlichkeit: Was die Fahrt unmöglich macht,
    steht vor dem, was sie nur unbequem macht.
    """
    if reserve_bei is not None and reserve_bei < gesamt_km:
        return True, (f"Reserve wird bei km {reserve_bei:.0f} erreicht - "
                      f"vorher laden.")
    if prognose is not None and prognose < fahrzeug.reserve_soc:
        return True, (f"Ankunft mit {prognose:.0f} % prognostiziert, "
                      f"unter der Reserve von {fahrzeug.reserve_soc:.0f} %.")
    if abstand_m > SCHWELLE_ABWEG_M:
        return True, f"{abstand_m:.0f} m neben der geplanten Route."
    if abweichung is not None and abs(abweichung) >= SCHWELLE_SOC_PP:
        richtung = "unter" if abweichung < 0 else "über"
        return True, (f"{abs(abweichung):.0f} Prozentpunkte {richtung} Plan - "
                      f"Ladestopps neu rechnen.")
    if abweichung is not None and abs(abweichung) >= SCHWELLE_SOC_PP / 2:
        return False, f"{abweichung:+.0f} Prozentpunkte gegenüber Plan."
    return False, "im Plan"


def zustand_als_dict(zustand: Zustand) -> dict:
    return asdict(zustand)
