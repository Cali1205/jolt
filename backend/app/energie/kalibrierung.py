"""Den Korrekturfaktor eines Fahrzeugs aus echten Fahrten lernen.

Die Physik in modell.py braucht c_w-Wert, Stirnfläche, Rollwiderstand und
Wirkungsgrade. Die kennt niemand genau, und sie ändern sich mit Reifen,
Dachbox, Beladung und Batteriealter. Statt den Nutzer raten zu lassen, misst
jolt nach: prognostizierte kWh gegen tatsächlich verbrauchte kWh.

Das Ergebnis ist eine einzige Zahl je Fahrzeug. Bewusst keine Rückrechnung auf
die Einzelparameter - aus einer Abweichung von 8 % lässt sich nicht ableiten,
ob der c_w-Wert oder der Wirkungsgrad daneben lag, und ein überbestimmtes
Modell würde sich an Rauschen anpassen.
"""
import logging

# Unterhalb dieser Strecke ist der Messfehler beim SoC (meist 1 % Auflösung)
# grösser als das, was gemessen werden soll.
MINDESTSTRECKE_KM = 30.0
# Wie stark eine einzelne Fahrt den Faktor verschieben darf. Ohne die Dämpfung
# würde eine Fahrt mit unbemerkter Dachbox den Faktor dauerhaft verbiegen.
GLAETTUNG = 0.25
# Ein Faktor ausserhalb dieser Grenzen bedeutet Messfehler, nicht Fahrzeug.
UNTERGRENZE, OBERGRENZE = 0.6, 1.8

log = logging.getLogger("uvicorn.error")


def faktor_aus_fahrt(prognose_kwh: float, tatsaechlich_kwh: float,
                     strecke_km: float) -> float | None:
    """Der Rohfaktor einer einzelnen Fahrt, oder None wenn nicht verwertbar."""
    if strecke_km < MINDESTSTRECKE_KM or prognose_kwh <= 0:
        return None
    faktor = tatsaechlich_kwh / prognose_kwh
    if not UNTERGRENZE <= faktor <= OBERGRENZE:
        log.info("Kalibrierung verworfen: Faktor %.2f ausserhalb der Grenzen.",
                 faktor)
        return None
    return faktor


def nachfuehren(bisher: float, neuer_rohfaktor: float) -> float:
    """Den gespeicherten Faktor um eine neue Messung fortschreiben."""
    return round(bisher * (1 - GLAETTUNG) + neuer_rohfaktor * GLAETTUNG, 4)


def aus_live_sitzung(sitzung, akku_netto_kwh: float) -> float | None:
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
    """
    punkte = [p for p in sitzung.punkte if p.soll_soc is not None
              and p.km_auf_route is not None and p.soc is not None]
    if len(punkte) < 2:
        return None

    erster, letzter = punkte[0], punkte[-1]
    strecke_km = (letzter.km_auf_route or 0.0) - (erster.km_auf_route or 0.0)
    ist_kwh = (erster.soc - letzter.soc) / 100.0 * akku_netto_kwh
    soll_kwh = ((erster.soll_soc or 0.0) - (letzter.soll_soc or 0.0)) / 100.0 * akku_netto_kwh
    return faktor_aus_fahrt(soll_kwh, ist_kwh, strecke_km)
