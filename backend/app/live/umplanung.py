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
from ..energie import modell, wetter
from ..energie.modell import Fahrzeugwerte, Umgebung, haversine_m
from ..laden import kurven, optimierer, verfuegbarkeit
from ..routing import korridor

log = logging.getLogger("uvicorn.error")

# Vorgaben für die Kandidatensuche, wenn die Sitzung noch keine mitbringt.
# Sie entsprechen den Vorgaben der Oberfläche.
VORGABEN = {"radius_km": 10.0, "min_kw": 50.0, "steckertyp": "",
            "umweg_grenze_min": optimierer.UMWEG_GRENZE_MIN,
            "stopp_fixkosten_min": optimierer.STOPP_FIXKOSTEN_MIN}


def parameter_lesen(plan: dict | None) -> dict:
    """Die Suchparameter aus einem gespeicherten Plan, sonst die Vorgaben.

    Damit wird unterwegs mit demselben Radius und derselben Mindestleistung
    gesucht wie beim Start - ein Plan, der sich mitten in der Fahrt auch noch
    die Auswahlkriterien ändert, wäre nicht mehr nachvollziehbar.
    """
    werte = dict(VORGABEN)
    for schluessel in werte:
        if plan and plan.get(schluessel) is not None:
            werte[schluessel] = plan[schluessel]
    return werte


def rest_ab(geometrie: list, ab_km: float) -> tuple[list, float]:
    """Die Route ab einem Kilometerstand, plus deren tatsächlichen Startwert.

    Zurückgegeben wird der *Stützpunkt* vor `ab_km` und sein Kilometerstand -
    nicht `ab_km` selbst. Nur so beziehen sich Geometrie und Profil hinterher
    auf denselben Nullpunkt; eine Verschiebung zwischen beiden wäre ein
    Versatz in jeder Etappenrechnung.
    """
    if len(geometrie) < 2:
        return list(geometrie), 0.0
    km = 0.0
    for i in range(1, len(geometrie)):
        vorher = km
        km += haversine_m(geometrie[i - 1][1], geometrie[i - 1][0],
                          geometrie[i][1], geometrie[i][0]) / 1000.0
        if km >= ab_km:
            return geometrie[i - 1:], vorher
    # Schon am Ziel - die letzte Kante bleibt übrig, damit es überhaupt eine
    # Strecke gibt.
    return geometrie[-2:], km


def restprofil(energieprofil: list, ab_km: float, verbrauchsfaktor: float = 1.0,
               zeitfaktor: float = 1.0) -> optimierer.Streckenprofil:
    """Das Streckenprofil der Reststrecke, auf null gesetzt und skaliert."""
    rest = [e for e in energieprofil if (e.get("km") or 0.0) >= ab_km]
    if len(rest) < 2:
        rest = energieprofil[-2:] if len(energieprofil) >= 2 else energieprofil
    if len(rest) < 2:
        return optimierer.Streckenprofil(km=[], kwh=[], minuten=[])

    km0 = rest[0].get("km") or 0.0
    kwh0 = rest[0].get("kwh") or 0.0
    min0 = rest[0].get("minuten") or 0.0
    return optimierer.Streckenprofil(
        km=[(e.get("km") or 0.0) - km0 for e in rest],
        kwh=[((e.get("kwh") or 0.0) - kwh0) * verbrauchsfaktor for e in rest],
        minuten=[((e.get("minuten") or 0.0) - min0) * zeitfaktor for e in rest])


def umgebung_unterwegs(fahrt: models.Fahrt, punkte: list):
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
    ersatz = Umgebung()
    if fahrt.aussentemp_c is not None:
        ersatz = Umgebung(temp_c=fahrt.aussentemp_c)
    try:
        return wetter.entlang_route(punkte, vorgabe=ersatz)
    except Exception as fehler:      # noqa: BLE001
        log.warning("Wetter unterwegs nicht abrufbar: %s", fehler)
        return lambda lat, lon: ersatz


def restprofil_physik(fahrt: models.Fahrt, rest: list, tempo_faktor: float,
                      umgebung_fuer) -> optimierer.Streckenprofil | None:
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
    punkte, tempo_ms = [], []
    for i, eintrag in enumerate(rest):
        lat, lon = eintrag.get("lat"), eintrag.get("lon")
        if lat is None or lon is None:
            return None
        punkte.append([lon, lat, eintrag.get("hoehe") or 0.0])
        # `tempo_kmh` an einer Stützstelle ist die Geschwindigkeit des
        # Teilstücks, das *dort endet* - siehe modell.profil_rechnen. Für das
        # Teilstück i (von i nach i+1) steht sie also am Punkt i+1.
        if i > 0:
            tempo = eintrag.get("tempo_kmh")
            if not tempo:
                return None
            tempo_ms.append(tempo / 3.6)

    neu = modell.profil_rechnen(
        Fahrzeugwerte.aus_fahrt(fahrt), punkte, tempo_ms,
        # Der Ladestand ist für das Streckenprofil ohne Belang: Gebraucht
        # werden nur die kumulierten kWh und Minuten, und der Energiebedarf
        # einer Etappe hängt nicht davon ab, wie voll der Akku ist.
        start_soc=100.0, umgebung_fuer=umgebung_fuer, tempo_faktor=tempo_faktor)
    if len(neu.punkte) < 2:
        return None
    return optimierer.Streckenprofil(
        km=[p.km for p in neu.punkte],
        kwh=[p.kwh_kumuliert for p in neu.punkte],
        minuten=[p.minuten_kumuliert for p in neu.punkte])


def optionen_suchen(db, geometrie: list, fahrzeug, parameter: dict
                    ) -> list[optimierer.Ladeoption]:
    """Die Ladeoptionen im Korridor der (Rest-)Route."""
    typ = parameter.get("steckertyp") or fahrzeug.steckertyp
    kandidaten = korridor.suchen(db, geometrie,
                                 radius_km=parameter["radius_km"],
                                 min_kw=parameter["min_kw"], steckertyp=typ)
    optionen = []
    for kandidat in kandidaten:
        lp = kandidat.ladepunkt
        zustand = verfuegbarkeit.MELDUNGEN.zustand(lp)
        optionen.append(optimierer.Ladeoption(
            id=lp.id, km_auf_route=kandidat.km_auf_route,
            umweg_minuten=kandidat.umweg_minuten, max_kw=lp.max_kw or 0.0,
            anzahl_punkte=lp.anzahl_punkte or 1, name=lp.name or "",
            betreiber=lp.betreiber or "", ort=lp.ort or "",
            lat=lp.lat, lon=lp.lon,
            gesperrt=zustand.quelle == "meldung"))
    return optionen


def planen(db, fahrt: models.Fahrt, ab_km: float, start_soc: float,
           parameter: dict, verbrauchsfaktor: float = 1.0,
           zeitfaktor: float = 1.0, tempo_faktor: float | None = None) -> dict:
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
    fahrzeug = fahrt.fahrzeug
    geometrie, km0 = rest_ab(fahrt.geometrie or [], ab_km)

    profil = None
    grundlage = "verbrauch gemessen" if verbrauchsfaktor != 1.0 else "planung"
    if tempo_faktor is not None:
        rest = [e for e in (fahrt.energieprofil or [])
                if (e.get("km") or 0.0) >= km0]
        try:
            profil = restprofil_physik(fahrt, rest, tempo_faktor,
                                       umgebung_unterwegs(fahrt, geometrie))
        except Exception as fehler:      # noqa: BLE001
            # Eine gescheiterte Neurechnung darf die Umplanung nicht kosten -
            # das Skalieren darunter ist schlechter, aber es steht.
            log.warning("Neurechnung mit gemessenem Tempo fehlgeschlagen: %s",
                        fehler)
        if profil is not None:
            grundlage = "tempo gemessen"

    if profil is None:
        profil = restprofil(fahrt.energieprofil or [], km0, verbrauchsfaktor,
                            zeitfaktor)

    if not profil.km or len(profil.km) < 2:
        return {**parameter, "machbar": False, "stand_km": round(ab_km, 1),
                "grund": "Zur Reststrecke gibt es kein Profil mehr.",
                "anzahl_stopps": 0, "stopps": []}

    optionen = optionen_suchen(db, geometrie, fahrzeug, parameter)
    plan = optimierer.planen(
        profil, optionen, Fahrzeugwerte.aus_fahrt(fahrt),
        kurven.als_paare(fahrzeug.ladekurve), start_soc=start_soc,
        ziel_soc=fahrzeug.ziel_soc,
        max_fahrzeug_kw=fahrzeug.max_ladeleistung_kw,
        temperatur_faktor=kurven.temperatur_faktor(
            fahrt.aussentemp_c if fahrt.aussentemp_c is not None else 15.0),
        umweg_grenze_min=parameter["umweg_grenze_min"],
        stopp_fixkosten_min=parameter["stopp_fixkosten_min"],
        bevorzugte_betreiber=fahrzeug.bevorzugte_betreiber or None)

    ergebnis = plan.als_dict()
    for stopp in ergebnis["stopps"]:
        stopp["km_auf_route"] = round(stopp["km_auf_route"] + km0, 1)
        if stopp.get("ausweich"):
            stopp["ausweich"]["km_auf_route"] = round(
                stopp["ausweich"]["km_auf_route"] + km0, 1)
    return {**parameter, **ergebnis, "stand_km": round(km0, 1),
            # Worauf der Plan beruht - damit am Steuer und im Log
            # nachvollziehbar ist, ob hier eine Messung wirkt oder noch der
            # Regler von vor der Abfahrt.
            "grundlage": grundlage,
            "tempo_faktor": tempo_faktor}


def stopps_gleich(alt: dict | None, neu: dict | None) -> bool:
    """Beschreiben zwei Pläne dieselben Stopps?

    Verglichen werden Standort und Abfahrts-Ladestand, nicht die Ladezeit auf
    die Nachkommastelle: Dass sich eine Standzeit um vierzig Sekunden
    verschiebt, ist keine Änderung, über die jemand am Steuer unterrichtet
    werden will. Ein anderer Standort oder ein spürbar anderer Ladehub schon.
    """
    if alt is None or neu is None:
        return alt is neu
    if bool(alt.get("machbar")) != bool(neu.get("machbar")):
        return False

    a = alt.get("stopps") or []
    b = neu.get("stopps") or []
    if len(a) != len(b):
        return False
    for eins, zwei in zip(a, b):
        if eins.get("id") != zwei.get("id"):
            return False
        if abs((eins.get("abfahrt_soc") or 0) - (zwei.get("abfahrt_soc") or 0)) > 3.0:
            return False
    return True


def aenderung_beschreiben(alt: dict | None, neu: dict) -> str:
    """Was hat sich geändert - in einem Satz, der am Steuer trägt.

    Kein Diff und keine Liste: Wer fährt, kann einen Satz hören oder im
    Vorbeischauen lesen. Alles Weitere steht in der Ansicht.
    """
    if not neu.get("machbar"):
        return neu.get("grund") or "Kein Ladeplan mehr möglich."

    neue_stopps = neu.get("stopps") or []
    alte_stopps = (alt or {}).get("stopps") or []
    if alt is not None and not alt.get("machbar"):
        return f"Wieder ein Plan möglich: {len(neue_stopps)} Ladestopp(s)."

    if not neue_stopps:
        return "Kein Ladestopp mehr nötig."

    erster = neue_stopps[0]
    name = erster.get("name") or erster.get("betreiber") or "Ladepunkt"
    if len(neue_stopps) != len(alte_stopps):
        richtung = "mehr" if len(neue_stopps) > len(alte_stopps) else "weniger"
        return (f"{len(neue_stopps)} Ladestopps statt {len(alte_stopps)} "
                f"({richtung}) - nächster: {name} bei km "
                f"{erster.get('km_auf_route')}.")
    if alte_stopps and erster.get("id") != alte_stopps[0].get("id"):
        return f"Nächster Ladestopp jetzt {name} bei km {erster.get('km_auf_route')}."
    return (f"{name}: laden bis {erster.get('abfahrt_soc')} % "
            f"({erster.get('ladezeit_minuten')} min).")
