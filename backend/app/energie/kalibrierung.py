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
# Ab wie viel Anstieg ein Abschnitt als Ladevorgang gilt. Darunter ist es
# Rekuperation oder das Rauschen der SoC-Messung, und beides gehört zur Fahrt.
LADEN_PP = 0.5

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
    punkte = [p for p in sitzung.punkte if p.soll_soc is not None
              and p.km_auf_route is not None and p.soc is not None]
    if len(punkte) < 2:
        return None

    ist_pp, soll_pp, strecke_km = 0.0, 0.0, 0.0
    for vorher, nachher in zip(punkte, punkte[1:]):
        verbraucht = vorher.soc - nachher.soc
        if verbraucht < -LADEN_PP:
            # Geladen. Der Abschnitt sagt nichts ueber den Verbrauch - und
            # seine Strecke zaehlt auch nicht mit, sonst stuende dieselbe
            # Strecke im Nenner ohne den zugehoerigen Verbrauch.
            continue
        ist_pp += verbraucht
        soll_pp += (vorher.soll_soc or 0.0) - (nachher.soll_soc or 0.0)
        strecke_km += ((nachher.km_auf_route or 0.0)
                       - (vorher.km_auf_route or 0.0))

    return faktor_aus_fahrt(soll_pp / 100.0 * akku_netto_kwh,
                            ist_pp / 100.0 * akku_netto_kwh, strecke_km)
