"""Den Korrekturfaktor eines Fahrzeugs aus echten Fahrten lernen.

Die Physik in model.py braucht c_w-Wert, Stirnfläche, Rollwiderstand und
Wirkungsgrade. Die kennt niemand genau, und sie ändern sich mit Reifen,
Dachbox, Beladung und Batteriealter. Statt den Nutzer raten zu lassen, misst
jolt nach: prognostizierte kWh gegen tatsächlich verbrauchte kWh.

Das Ergebnis ist eine einzige Zahl je Fahrzeug. Bewusst keine Rückrechnung auf
die Einzelparameter - aus einer Abweichung von 8 % lässt sich nicht ableiten,
ob der c_w-Wert oder der Wirkungsgrad daneben lag, und ein überbestimmtes
Modell würde sich an Rauschen anpassen.
"""
import logging

from . import charge_phases

# Unterhalb dieser Strecke ist der Messfehler beim SoC (meist 1 % Auflösung)
# grösser als das, was gemessen werden soll.
MIN_DISTANCE_KM = 30.0
# Wie stark eine einzelne Fahrt den Faktor verschieben darf. Ohne die Dämpfung
# würde eine Fahrt mit unbemerkter Dachbox den Faktor dauerhaft verbiegen.
SMOOTHING = 0.25
# Ein Faktor ausserhalb dieser Grenzen bedeutet Messfehler, nicht Fahrzeug.
LOWER_LIMIT, UPPER_LIMIT = 0.6, 1.8

log = logging.getLogger("uvicorn.error")


def factor_from_trip(forecast_kwh: float, actual_kwh: float,
                     distance_km: float) -> float | None:
    """Der Rohfaktor einer einzelnen Fahrt, oder None wenn nicht verwertbar."""
    if distance_km < MIN_DISTANCE_KM or forecast_kwh <= 0:
        return None
    factor = actual_kwh / forecast_kwh
    if not LOWER_LIMIT <= factor <= UPPER_LIMIT:
        log.info("Kalibrierung verworfen: Faktor %.2f ausserhalb der Grenzen.",
                 factor)
        return None
    return factor


def carry_on(so_far: float, new_raw_factor: float) -> float:
    """Den gespeicherten Faktor um eine neue Messung fortschreiben."""
    return round(so_far * (1 - SMOOTHING) + new_raw_factor * SMOOTHING, 4)


def from_live_session(session, battery_net_kwh: float) -> float | None:
    """Rohfaktor aus einer abgeschlossenen Live-Sitzung.

    Verglichen wird der tatsächliche SoC-Verlust mit dem, den das Profil an
    derselben Stelle vorhergesagt hatte. Beides steht bereits an den
    Messpunkten - `soll_soc` wird beim Eintreffen mitgeschrieben, genau
    damit hier nichts nachgerechnet werden muss.

    Ausschliesslich **gemeldete** Ladestände zählen. Punkte ohne Ladestand
    tragen zwar Position und Zeit, ihr Ladestand wird aber aus demselben
    Modell hochgerechnet, das hier geprüft werden soll - sie mitzunehmen
    hiesse, das Modell gegen sich selbst zu messen und dabei zuverlässig
    einen Faktor von 1,0 zu erhalten.

    **Abschnittsweise gerechnet, und Ladeabschnitte fallen heraus.** Hier
    stand `erster.soc - letzter.soc`, also der Ladestand am Anfang minus dem
    am Ende - und damit fiel jede Ladung dazwischen unter den Tisch. Wer
    unterwegs 40 Prozentpunkte nachlädt, sieht am Ende einen Verlust, der um
    diese 40 Punkte zu klein ist; der gelernte Faktor faellt entsprechend zu
    niedrig aus, und weil er innerhalb der Plausibilitätsgrenzen bleibt,
    faellt es nicht auf. Das Fahrzeug lernt bei jeder Fahrt mit Ladestopp,
    es sei sparsamer als es ist - und das ist die Betriebsart, um die es
    hier ueberhaupt geht.

    Gemessen an einem Probelauf: 103 km mit einer Ladepause ergaben einen
    Rohfaktor von 0,695 statt der gefahrenen ~1,8.
    """
    points = [p for p in session.points if p.plan_soc is not None
              and p.km_on_route is not None and p.soc is not None]
    if len(points) < 2:
        return None

    actual_pp, distance_km = charge_phases.consumption(points)
    plan_pp = sum(a.begin.plan_soc - a.past.plan_soc
                  for a in charge_phases.sections(points) if not a.charges)

    return factor_from_trip(plan_pp / 100.0 * battery_net_kwh,
                            actual_pp / 100.0 * battery_net_kwh, distance_km)
