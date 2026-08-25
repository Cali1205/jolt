"""Die Live-Nachführung: Ist gegen Soll, während gefahren wird.

Der Grund für das ganze Projekt. Ein Plan, der bei Abfahrt gerechnet wurde,
ist nach achtzig Kilometern falsch - Tempo, Temperatur, Wind und Stau
addieren sich in dieselbe Richtung. Wer das merkt, braucht keinen Puffer von
zwanzig Prozent; wer es nicht merkt, steht mit vier Prozent an einer belegten
Säule.

Was hier passiert: Zu jedem Messpunkt wird bestimmt, wo auf der Route er
liegt, was der Plan an dieser Stelle vorhergesagt hatte, und wie weit die
Wirklichkeit davon abweicht. Daraus entstehen zwei laufende Faktoren - einer
für den Verbrauch, einer für die Zeit - und aus ihnen die Frage, ob der
Ladeplan noch stimmt. Tut er das nicht, wird er neu gerechnet
(`live/umplanung.py`).

Neu geplant wird bewusst nicht bei jeder Messung, sondern nur, wenn einer der
Auslöser aus Abschnitt 2.3 des Konzepts greift. Ein Plan, der sich alle
dreissig Sekunden ändert, ist kein Plan.
"""
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .. import models
from ..laden import verfuegbarkeit
from ..routing.korridor import punkt_auf_route
from . import umplanung

log = logging.getLogger("uvicorn.error")

# Schwellen für die Neuplanung, eins zu eins aus Abschnitt 2.3 des Konzepts.
# Bewusst Schwellen und keine Neuberechnung bei jeder Messung: Wer gerade
# beschlossen hat, in 40 km Pause zu machen, soll das nicht dreimal umwerfen
# müssen. Eine Änderung muss etwas bedeuten.
SCHWELLE_SOC_PP = 5.0            # Prozentpunkte Abweichung
SCHWELLE_ABWEG_M = 500.0         # Abstand zur Route
SCHWELLE_ABWEG_S = 60.0          # ... und wie lange er anhalten muss
SCHWELLE_ANKUNFT_MIN = 10.0      # Verschiebung der Ankunftszeit

# Wie weit gefahren sein muss, bevor derselbe nicht-dringende Auslöser erneut
# eine Neuplanung anstösst. Ohne diese Sperre rechnete jede Messung neu,
# solange die Abweichung besteht - und das ist der Normalfall, nicht die
# Ausnahme.
NEUPLANUNG_ABSTAND_KM = 10.0

# Über wie viele Kilometer die Faktoren gemittelt werden. Zu kurz, und eine
# einzelne Ampelphase verbiegt sie; zu lang, und der Wetterumschwung hinter
# dem Pass kommt zu spät an.
FENSTER_KM = 25.0
# Vorher ist die SoC-Anzeige (meist 1 % Auflösung) zu grob für eine Aussage.
MINDESTSTRECKE_KM = 5.0

# Ab welchem Anstieg des Ladestands zwischen zwei Messpunkten geladen wurde -
# und nicht nur rekuperiert oder gerundet. Die Anzeige löst je nach Fahrzeug
# in halben oder ganzen Prozentpunkten auf; darunter ist alles Rauschen.
LADEN_SOC_PP = 0.5


@dataclass
class Zustand:
    km_auf_route: float
    abstand_zur_route_m: float
    ist_soc: float
    soll_soc: float | None
    abweichung_pp: float | None
    verbrauchsfaktor: float
    zeitfaktor: float
    rest_km: float
    prognose_soc_am_ziel: float | None
    reserve_bei_km: float | None
    ankunft_verschiebung_min: float | None
    naechster_stopp: dict | None
    neuplanung_noetig: bool
    grund: str
    # Wird nur gesetzt, wenn tatsächlich neu geplant wurde.
    plan: dict | None = None
    plan_geaendert: bool = False
    aenderung: str = ""
    dringend: bool = field(default=False, repr=False)


# ---------------------------------------------------------------------------
# Nachschlagen im Profil
# ---------------------------------------------------------------------------

def _profilwert(energieprofil: list, km: float, feld: str) -> float | None:
    """Einen Planwert an einem Kilometerstand nachschlagen.

    Zwischen zwei Profilpunkten wird linear interpoliert - die liegen rund
    250 m auseinander, da ist die Gerade genau genug.
    """
    if not energieprofil:
        return None
    if km <= (energieprofil[0].get("km") or 0.0):
        return energieprofil[0].get(feld)
    for vorher, nachher in zip(energieprofil, energieprofil[1:]):
        km1, km2 = vorher.get("km", 0.0), nachher.get("km", 0.0)
        if km1 <= km <= km2:
            if km2 <= km1:
                return vorher.get(feld)
            anteil = (km - km1) / (km2 - km1)
            w1, w2 = vorher.get(feld, 0.0), nachher.get(feld, 0.0)
            return round(w1 + (w2 - w1) * anteil, 2)
    return energieprofil[-1].get(feld)


def soll_soc_bei(energieprofil: list, km: float) -> float | None:
    return _profilwert(energieprofil, km, "soc")


def soll_minuten_bei(energieprofil: list, km: float) -> float | None:
    return _profilwert(energieprofil, km, "minuten")


# ---------------------------------------------------------------------------
# Die beiden laufenden Faktoren
# ---------------------------------------------------------------------------

def _fenster(punkte: list) -> list:
    """Die Messpunkte der letzten `FENSTER_KM`, mindestens aber zwei."""
    brauchbar = [p for p in punkte
                 if p.km_auf_route is not None and p.soll_soc is not None]
    if len(brauchbar) < 2:
        return []
    letzter = brauchbar[-1]
    fenster = [p for p in brauchbar
               if (letzter.km_auf_route - p.km_auf_route) <= FENSTER_KM]
    return fenster if len(fenster) >= 2 else brauchbar[-2:]


def _ladepausen_minuten(punkte: list, energieprofil: list) -> float:
    """Wie viel der verstrichenen Zeit auf Ladepausen entfiel.

    Nötig, weil das Energieprofil ausschliesslich **Fahrzeit** führt: Die
    Ladezeit steht im Plan, nie im Profil. Wer die Wanduhr ungefiltert gegen
    das Profil hält, sieht deshalb nach dem ersten Ladestopp eine Verspätung
    in Höhe der Ladedauer - und zwar dauerhaft, denn sie wird nie wieder
    aufgeholt. Damit stünde der Auslöser "Ankunft verschiebt sich" für den
    Rest der Fahrt über seiner Schwelle und meldete alle zehn Kilometer
    dieselbe Verspätung. Eine Meldung, die immer kommt, schaltet man ab.

    Erkannt wird die Pause am steigenden Ladestand: Beim Fahren fällt er,
    beim Laden steigt er. Gezählt wird aber nicht die ganze Zeitspanne,
    sondern nur der Teil, der über der Fahrzeit für die dabei zurückgelegte
    Strecke liegt. Das erledigt zwei Fälle auf einmal - Rekuperation auf
    langer Talfahrt hebt den Ladestand zwar auch, kostet aber keine
    zusätzliche Zeit; und es ist gleichgültig, ob der Logger während des
    Ladens weitergesendet hat oder erst hinterher wieder aufgewacht ist.

    Nicht abgezogen wird eine Pause ohne Ladung - Mittagessen, Stau, Stau vor
    der Baustelle. Die verschiebt die Ankunft wirklich, und genau das soll
    der Auslöser sehen.
    """
    gesamt = 0.0
    for vorher, nachher in zip(punkte, punkte[1:]):
        if (nachher.soc - vorher.soc) < LADEN_SOC_PP:
            continue
        if not vorher.zeit or not nachher.zeit:
            continue
        verstrichen = (nachher.zeit - vorher.zeit).total_seconds() / 60.0
        von = soll_minuten_bei(energieprofil, vorher.km_auf_route or 0.0)
        bis = soll_minuten_bei(energieprofil, nachher.km_auf_route or 0.0)
        gefahren = 0.0 if von is None or bis is None else max(0.0, bis - von)
        gesamt += max(0.0, verstrichen - gefahren)
    return gesamt


def _verbrauchsfaktor(punkte: list) -> float | None:
    """Ist-Verbrauch geteilt durch Soll-Verbrauch über das gleitende Fenster.

    Gerechnet wird über SoC-Differenzen und nicht über absolute Werte: Ein
    Tacho, der grundsätzlich zwei Prozent zu hoch anzeigt, verfälscht die
    Differenz nicht - den absoluten Vergleich aber schon.
    """
    fenster = _fenster(punkte)
    if not fenster:
        return None

    erster, letzter = fenster[0], fenster[-1]
    if letzter.km_auf_route - erster.km_auf_route < MINDESTSTRECKE_KM:
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


def _zeitfaktor(punkte: list, energieprofil: list) -> float | None:
    """Ist-Fahrzeit geteilt durch Soll-Fahrzeit über dasselbe Fenster.

    Der eigene Faktor ist nötig, weil der Verbrauch einen Stau nicht sieht:
    Wer steht, verbraucht je Kilometer sogar etwas mehr, aber die Ankunftszeit
    verschiebt sich um ein Vielfaches davon. Ohne diese Zahl wäre der Auslöser
    "Ankunftszeit verschiebt sich" nicht zu haben - und jede Ankunftszeit im
    umgeplanten Ladeplan wäre die aus dem alten Plan.
    """
    fenster = _fenster(punkte)
    if not fenster:
        return None

    erster, letzter = fenster[0], fenster[-1]
    if letzter.km_auf_route - erster.km_auf_route < MINDESTSTRECKE_KM:
        return None
    if not erster.zeit or not letzter.zeit:
        return None

    # Die Ladezeit gehört nicht in den Zeitfaktor: Wer eine halbe Stunde an
    # der Säule stand, hat keinen Stau. Ohne den Abzug wäre der Faktor nach
    # jedem Ladestopp so gross, dass ihn die Schranke unten verwirft - und
    # damit für die nächsten FENSTER_KM eingefroren, also genau auf der
    # Strecke blind, auf der er wieder gebraucht wird.
    ist_minuten = ((letzter.zeit - erster.zeit).total_seconds() / 60.0
                   - _ladepausen_minuten(fenster, energieprofil))
    soll_ende = soll_minuten_bei(energieprofil, letzter.km_auf_route)
    soll_start = soll_minuten_bei(energieprofil, erster.km_auf_route)
    if soll_ende is None or soll_start is None:
        return None
    soll_minuten = soll_ende - soll_start
    if soll_minuten <= 0.5 or ist_minuten <= 0:
        return None

    faktor = ist_minuten / soll_minuten
    # Dieselbe Ausreisserschranke wie beim Verbrauch - jetzt nur noch gegen
    # das, was der Ladepausen-Abzug nicht erklärt.
    if not 0.4 <= faktor <= 3.0:
        return None
    return round(faktor, 3)


# ---------------------------------------------------------------------------
# Messpunkt herein
# ---------------------------------------------------------------------------

def messpunkt_aufnehmen(db, sitzung: models.LiveSitzung, lat: float, lon: float,
                        soc: float, tempo_kmh: float | None = None,
                        aussentemp_c: float | None = None,
                        zeit: datetime | None = None) -> Zustand:
    """Einen Messpunkt einsortieren und den neuen Zustand zurückgeben.

    `zeit` überschreibt den Zeitstempel. Gebraucht wird das vom Simulator: Er
    spielt Stunden in Sekunden ab, und mit echten Uhrzeiten wäre der
    Zeitfaktor dort sinnlos - also genau die Grösse, die den Stau abbildet.
    """
    fahrt = sitzung.fahrt
    geometrie = fahrt.geometrie or []
    profil = fahrt.energieprofil or []

    km, abstand = punkt_auf_route(geometrie, lat, lon)
    soll = soll_soc_bei(profil, km)

    punkt = models.LivePunkt(lat=lat, lon=lon, soc=soc, tempo_kmh=tempo_kmh,
                             aussentemp_c=aussentemp_c, km_auf_route=km,
                             soll_soc=soll, zeit=zeit or datetime.utcnow())
    # Über die Beziehung anhängen und nicht über db.add(): Sonst steht der
    # Punkt zweimal in der geladenen Sammlung - einmal durch das Anhängen,
    # einmal durch die Kaskade - und die Faktoren rechnen mit einem Duplikat.
    sitzung.punkte.append(punkt)
    db.flush()

    faktor = _verbrauchsfaktor(sitzung.punkte)
    if faktor is not None:
        sitzung.verbrauchsfaktor = faktor
    zfaktor = _zeitfaktor(sitzung.punkte, profil)
    if zfaktor is not None:
        sitzung.zeitfaktor = zfaktor

    # Abweg braucht Dauer, nicht nur Abstand: Eine ungenaue Messung unter
    # einer Brücke ist kein Verlassen der Route.
    if abstand > SCHWELLE_ABWEG_M:
        if sitzung.abweg_seit is None:
            sitzung.abweg_seit = punkt.zeit
    else:
        sitzung.abweg_seit = None

    zustand = _zustand_bilden(sitzung, punkt, abstand)

    if zustand.neuplanung_noetig and _darf_neu_planen(sitzung, km, zustand):
        _umplanen(db, sitzung, zustand, km, soc)

    sitzung.hinweis = zustand.grund
    db.commit()
    return zustand


def _umplanen(db, sitzung: models.LiveSitzung, zustand: Zustand, km: float,
              soc: float) -> None:
    try:
        neu = umplanung.planen(
            db, sitzung.fahrt, km, soc,
            umplanung.parameter_lesen(sitzung.plan),
            sitzung.verbrauchsfaktor, sitzung.zeitfaktor)
    except Exception as fehler:      # noqa: BLE001
        # Eine gescheiterte Umplanung darf die Fahrt nicht beenden: Die
        # Messung läuft weiter, und der alte Plan ist immer noch besser als
        # gar keiner.
        log.warning("Umplanung fehlgeschlagen: %s", fehler)
        return

    if not umplanung.stopps_gleich(sitzung.plan, neu):
        zustand.plan_geaendert = True
        zustand.aenderung = umplanung.aenderung_beschreiben(sitzung.plan, neu)
    sitzung.plan = neu
    zustand.plan = neu


def _darf_neu_planen(sitzung: models.LiveSitzung, km: float,
                     zustand: Zustand) -> bool:
    """Sperre gegen einen Plan, der sich im Minutentakt ändert.

    Dringende Gründe - die Säule ist belegt, die Reserve reicht nicht - gehen
    immer durch. Alles andere erst wieder nach `NEUPLANUNG_ABSTAND_KM`: Die
    Abweichung besteht ja weiter, sonst hätte der Auslöser nicht gegriffen.
    Ohne die Sperre rechnete jede einzelne Messung neu.
    """
    if sitzung.plan is None or zustand.dringend:
        return True
    letzter_stand = (sitzung.plan or {}).get("stand_km")
    if letzter_stand is None:
        return True
    return (km - letzter_stand) >= NEUPLANUNG_ABSTAND_KM


# ---------------------------------------------------------------------------
# Zustand und Auslöser
# ---------------------------------------------------------------------------

def _zustand_bilden(sitzung: models.LiveSitzung, punkt: models.LivePunkt,
                    abstand_m: float) -> Zustand:
    fahrt = sitzung.fahrt
    fahrzeug = fahrt.fahrzeug
    profil = fahrt.energieprofil or []
    gesamt_km = profil[-1]["km"] if profil else 0.0
    km = punkt.km_auf_route or 0.0
    rest_km = max(0.0, gesamt_km - km)

    abweichung = None
    if punkt.soll_soc is not None:
        abweichung = round(punkt.soc - punkt.soll_soc, 2)

    prognose = _prognose_am_ziel(profil, punkt, sitzung.verbrauchsfaktor)
    reserve_bei = _reserve_bei(profil, punkt, sitzung.verbrauchsfaktor,
                               fahrzeug.reserve_soc)
    verschiebung = _ankunft_verschiebung(sitzung, profil, punkt, gesamt_km)
    naechster, ankunft_soc = _naechster_stopp(sitzung, profil, punkt)

    noetig, grund, dringend = _neuplanung_pruefen(
        fahrzeug=fahrzeug, abweichung=abweichung, abstand_m=abstand_m,
        abweg_seit=sitzung.abweg_seit, jetzt=punkt.zeit, prognose=prognose,
        reserve_bei=reserve_bei, gesamt_km=gesamt_km,
        verschiebung=verschiebung, naechster=naechster,
        ankunft_soc=ankunft_soc)

    return Zustand(
        km_auf_route=round(km, 2), abstand_zur_route_m=round(abstand_m),
        ist_soc=round(punkt.soc, 2), soll_soc=punkt.soll_soc,
        abweichung_pp=abweichung,
        verbrauchsfaktor=round(sitzung.verbrauchsfaktor, 3),
        zeitfaktor=round(sitzung.zeitfaktor, 3),
        rest_km=round(rest_km, 1), prognose_soc_am_ziel=prognose,
        reserve_bei_km=reserve_bei, ankunft_verschiebung_min=verschiebung,
        naechster_stopp=naechster, neuplanung_noetig=noetig, grund=grund,
        dringend=dringend)


def _prognose_am_ziel(profil: list, punkt, verbrauchsfaktor: float):
    """Der Rest der Strecke mit dem gemessenen Faktor hochgerechnet."""
    if not profil:
        return None
    rest_soll = (punkt.soll_soc or punkt.soc) - (profil[-1].get("soc") or 0.0)
    return round(punkt.soc - rest_soll * verbrauchsfaktor, 2)


def _reserve_bei(profil: list, punkt, verbrauchsfaktor: float,
                 reserve_soc: float):
    """Wo die Reserve erreicht wird, wenn es so weitergeht wie bisher."""
    for eintrag in profil:
        if (eintrag.get("km") or 0.0) < (punkt.km_auf_route or 0.0):
            continue
        verbraucht = (punkt.soll_soc or punkt.soc) - (eintrag.get("soc") or 0.0)
        if punkt.soc - verbraucht * verbrauchsfaktor <= reserve_soc:
            return round(eintrag.get("km") or 0.0, 1)
    return None


def _ankunft_verschiebung(sitzung, profil: list, punkt, gesamt_km: float):
    """Um wie viele Minuten sich die Ankunft verschiebt - Stau inbegriffen.

    Zwei Anteile: was bereits verloren ist, und was der Zeitfaktor auf der
    Reststrecke noch kosten wird. Nur zusammen ergeben sie die Zahl, die
    interessiert.

    Die bereits verbrachte Ladezeit zählt nicht als Verspätung - sie stand so
    im Plan. Siehe `_ladepausen_minuten`.
    """
    punkte = [p for p in sitzung.punkte if p.km_auf_route is not None]
    if len(punkte) < 2 or not profil:
        return None

    erster = punkte[0]
    if not erster.zeit or not punkt.zeit:
        return None
    ist_minuten = (punkt.zeit - erster.zeit).total_seconds() / 60.0
    soll_jetzt = soll_minuten_bei(profil, punkt.km_auf_route or 0.0)
    soll_start = soll_minuten_bei(profil, erster.km_auf_route or 0.0)
    soll_ziel = soll_minuten_bei(profil, gesamt_km)
    if soll_jetzt is None or soll_start is None or soll_ziel is None:
        return None

    bisher = (ist_minuten - (soll_jetzt - soll_start)
              - _ladepausen_minuten(punkte, profil))
    rest = max(0.0, soll_ziel - soll_jetzt) * (sitzung.zeitfaktor - 1.0)
    return round(bisher + rest, 1)


def _naechster_stopp(sitzung, profil: list, punkt):
    """Der nächste geplante Ladestopp und der dort erwartete Ladestand.

    Der erwartete Wert wird mit dem gemessenen Verbrauchsfaktor hochgerechnet
    und gegen den Plan gehalten. Genau das ist der Auslöser aus dem Konzept:
    nicht die Abweichung hier, sondern die am nächsten Stopp - dort wird sie
    zum Problem.
    """
    stopps = ((sitzung.plan or {}).get("stopps") or [])
    km = punkt.km_auf_route or 0.0
    naechster = next((s for s in stopps
                      if (s.get("km_auf_route") or 0.0) > km + 0.5), None)
    if naechster is None:
        return None, None

    ziel_km = naechster.get("km_auf_route") or 0.0
    soll_dort = soll_soc_bei(profil, ziel_km)
    if soll_dort is None or punkt.soll_soc is None:
        return dict(naechster), None

    verbraucht = punkt.soll_soc - soll_dort
    hochgerechnet = round(punkt.soc - verbraucht * sitzung.verbrauchsfaktor, 2)
    beschreibung = {"id": naechster.get("id"), "name": naechster.get("name"),
                    "km_auf_route": ziel_km,
                    "geplant_soc": naechster.get("ankunft_soc"),
                    "erwartet_soc": hochgerechnet}
    return beschreibung, hochgerechnet


def _neuplanung_pruefen(*, fahrzeug, abweichung, abstand_m, abweg_seit, jetzt,
                        prognose, reserve_bei, gesamt_km, verschiebung,
                        naechster, ankunft_soc) -> tuple[bool, str, bool]:
    """Muss der Plan angefasst werden, warum - und eilt es?

    Die Reihenfolge ist die der Dringlichkeit: Was die Fahrt unmöglich macht,
    steht vor dem, was sie nur unbequem macht. `dringend` entscheidet, ob die
    Sperre gegen zu häufiges Umplanen übergangen wird.
    """
    # 1. Der nächste Ladepunkt ist belegt. Die einzige Verfügbarkeitsangabe,
    #    die wirklich stimmt - und sie macht den Plan sofort wertlos.
    if naechster and naechster.get("id") is not None:
        if verfuegbarkeit.MELDUNGEN.ist_gemeldet(naechster["id"]):
            name = naechster.get("name") or "Der nächste Ladepunkt"
            return True, f"{name} ist als belegt gemeldet - Ausweichen.", True

    # 2. Es reicht nicht bis zum Ziel.
    if reserve_bei is not None and reserve_bei < gesamt_km:
        return True, (f"Reserve wird bei km {reserve_bei:.0f} erreicht - "
                      f"vorher laden."), True
    if prognose is not None and prognose < fahrzeug.reserve_soc:
        return True, (f"Ankunft mit {prognose:.0f} % prognostiziert, "
                      f"unter der Reserve von {fahrzeug.reserve_soc:.0f} %."), True

    # 3. Abseits der Route - aber erst, wenn es anhält.
    if abweg_seit is not None and jetzt is not None:
        dauer = (jetzt - abweg_seit).total_seconds()
        if dauer >= SCHWELLE_ABWEG_S:
            return True, (f"Seit {dauer / 60:.0f} min mehr als "
                          f"{abstand_m:.0f} m neben der Route."), True

    # 4. Am nächsten Stopp kommt etwas anderes an als geplant.
    if naechster and ankunft_soc is not None:
        geplant = naechster.get("geplant_soc")
        if geplant is not None and abs(ankunft_soc - geplant) >= SCHWELLE_SOC_PP:
            name = naechster.get("name") or "nächster Stopp"
            return True, (f"Ankunft an {name} mit {ankunft_soc:.0f} % statt "
                          f"{geplant:.0f} % - Ladestopps neu rechnen."), False

    # 5. Ohne Plan bleibt die Abweichung hier die beste verfügbare Aussage.
    if not naechster and abweichung is not None and abs(abweichung) >= SCHWELLE_SOC_PP:
        richtung = "unter" if abweichung < 0 else "über"
        return True, (f"{abs(abweichung):.0f} Prozentpunkte {richtung} Plan - "
                      f"Ladestopps neu rechnen."), False

    # 6. Stau: Der Verbrauch merkt ihn kaum, die Ankunftszeit sehr wohl.
    if verschiebung is not None and abs(verschiebung) >= SCHWELLE_ANKUNFT_MIN:
        wort = "später" if verschiebung > 0 else "früher"
        return True, (f"Ankunft {abs(verschiebung):.0f} min {wort} als "
                      f"geplant."), False

    if abweichung is not None and abs(abweichung) >= SCHWELLE_SOC_PP / 2:
        return False, f"{abweichung:+.0f} Prozentpunkte gegenüber Plan.", False
    return False, "im Plan", False


def zustand_als_dict(zustand: Zustand) -> dict:
    daten = asdict(zustand)
    daten.pop("dringend", None)      # nur für die interne Sperre
    return daten
