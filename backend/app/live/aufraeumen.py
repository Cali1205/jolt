"""Vergessene Fahrten selbst beenden.

Eine Live-Sitzung endet, wenn jemand auf "Fahrt beenden" tippt. Das ist der
Handgriff, den man am Ziel am ehesten vergisst - man kommt an, steigt aus,
und das Telefon ist das Letzte, woran man denkt.

Fuer eine geplante Fahrt ist das halb so schlimm: Die Messpunkte liegen in
der Datenbank, der Plan war ohnehin gerechnet. Fuer eine **Aufzeichnung**
ist es der Totalverlust. Strecke, Hoehenprofil und Energieprofil entstehen
erst beim Beenden aus den Messpunkten; bis dahin ist die Fahrt eine Huelle
mit leerer Geometrie. Wer das Beenden vergisst, hat umsonst aufgezeichnet -
und merkt es erst, wenn er nachsehen will.

Deshalb beendet jolt von selbst, was seit einer Weile schweigt. Die Fristen
sind bewusst grosszuegig, weil die beiden Fehler ungleich teuer sind:

* **Zu frueh beendet** heisst, dass die Fahrt mitten entzwei geht. Der Rest
  der Strecke faellt weg, und wiederholen laesst er sich nicht.
* **Zu spaet beendet** heisst, dass die Fahrt eine Stunde zu lang gebucht
  ist. Die Strecke stimmt, das Energieprofil stimmt, nur der Zeitstempel am
  Ende ist grosszuegig - und das faellt beim Lernen kaum ins Gewicht, weil
  in dieser Stunde weder Strecke noch Verbrauch dazukommt.

Zu spaet ist also deutlich billiger als zu frueh, und die Fristen sind
entsprechend gesetzt.

Der gefaehrlichste Fall ist die **Ladepause**. Sie kann eine Stunde dauern,
das Telefon liegt derweil im Auto oder ist gesperrt, und danach geht die
Fahrt weiter. Wird waehrenddessen abgeraeumt, ist die zweite Haelfte der
Fahrt verloren. Deshalb sieht `_laedt_gerade` nach, ob der Ladestand am Ende
der Messpunkte *gestiegen* ist - dann war das Letzte, was jolt gesehen hat,
ein Ladevorgang, und die Frist wird noch einmal deutlich verlaengert.

Gerechnet wird gegen den **letzten Messpunkt**, nicht gegen den Beginn: Eine
lange Fahrt ist kein Grund, sie zu beenden, eine lange Stille schon.
"""
import asyncio
import logging
from datetime import datetime, timedelta

from .. import models
from ..energie import kalibrierung, ladephasen
from . import aufzeichnung

log = logging.getLogger("uvicorn.error")

# Wie lange eine Sitzung schweigen darf, bevor sie als beendet gilt.
# Drei Stunden decken Ladestopp, Mittagessen und Funkloch zusammen ab.
STILLE_MINUTEN = 180

# Und wenn zuletzt geladen wurde, noch einmal doppelt so lange. Eine
# Ladepause mit Essen kann gut zwei Stunden dauern, und danach geht es
# weiter - genau die Fahrt, die man nicht zerschneiden darf.
LADEPAUSE_MINUTEN = 360

# Wie weit zurueck nach steigendem Ladestand gesucht wird, und um wie viel er
# gestiegen sein muss. Hoeher als die Schwelle in `energie.ladephasen`, weil
# hier eine andere Frage gestellt wird: nicht "laedt dieser Abschnitt", sondern
# "war am Ende genug, um von einer Ladepause auszugehen". Ein Prozentpunkt ist
# mehr als das Rauschen der SoC-Messung und weniger als jeder Ladevorgang.
LADEFENSTER_MINUTEN = 25
LADEHUB_PROZENT = 1.0

# Eine Sitzung ohne jeden Messpunkt ist ein Fehlstart: Jemand hat auf
# "aufzeichnen" getippt und es sich anders ueberlegt, oder die Verbindung kam
# nie zustande. Die braucht keine 90 Minuten Nachsicht.
FEHLSTART_MINUTEN = 20

# Wie oft nachgesehen wird. Haeufiger brauchte niemand - es geht um Fristen
# von Stunden.
TAKT_SEKUNDEN = 5 * 60


def beenden_und_lernen(db, sitzung) -> dict:
    """Eine Sitzung ordentlich zu Ende bringen: Strecke bauen, dann lernen.

    Die Reihenfolge ist keine Geschmacksfrage. Die Kalibrierung vergleicht
    `soll_soc` mit `soc` an den Messpunkten, und der Sollwert entsteht erst
    beim Bauen der Strecke - andersherum lernt sie gegen lauter Nullen.

    Diese Funktion steht hier und nicht im Router, weil es **drei** Wege
    gibt, auf denen eine Sitzung endet: der Knopf am Telefon, das Aufraeumen
    weiter unten, und das Starten einer neuen Aufzeichnung, die die alte
     abloest. Drei Abschriften desselben Ablaufs laufen unweigerlich
    auseinander, und auf dem dritten Weg fehlte er zuletzt ganz.
    """
    ergebnis: dict = {"aufzeichnung": None, "gelernt": None,
                      "nicht_gelernt": None}
    fahrt = sitzung.fahrt

    if fahrt is not None and fahrt.aufzeichnung and not fahrt.geometrie:
        try:
            ergebnis["aufzeichnung"] = aufzeichnung.abschliessen(
                db, fahrt, sitzung)
        except Exception as fehler:      # noqa: BLE001
            # Die Messpunkte bleiben; eine gescheiterte Rekonstruktion darf
            # sie nicht mitnehmen.
            log.warning("Aufzeichnung %s nicht abzuschliessen: %s",
                        fahrt.id, fehler)
            ergebnis["aufzeichnung"] = {"ok": False, "grund": str(fehler)}

    fahrzeug = fahrt.fahrzeug if fahrt else None

    # Eine Fahrt mit Fahrradtraeger oder Dachbox lehrt nichts ueber das
    # *Fahrzeug*: Der gemessene Mehrverbrauch enthaelt dann zwei Unbekannte,
    # und aus einer Messung lassen sich nicht zwei Zahlen bestimmen.
    zuschlag = (fahrt.luftwiderstand_faktor or 1.0) if fahrt else 1.0
    if fahrzeug and abs(zuschlag - 1.0) > 0.001:
        ergebnis["nicht_gelernt"] = (
            f"Fahrt mit Luftwiderstands-Zuschlag ×{zuschlag:g} - daraus "
            f"lässt sich der Faktor des Fahrzeugs nicht bestimmen.")
        fahrzeug = None

    if fahrzeug:
        # Auch hier die gemessene Kapazitaet: Der gelernte Faktor ist der
        # Quotient aus gemessener und vorhergesagter Energie, und die
        # gemessene entsteht aus Prozent mal Kapazitaet.
        roh = kalibrierung.aus_live_sitzung(sitzung, fahrzeug.kapazitaet_kwh)
        if roh is not None:
            vorher = fahrzeug.korrekturfaktor
            fahrzeug.korrekturfaktor = kalibrierung.nachfuehren(vorher, roh)
            ergebnis["gelernt"] = {"rohfaktor": round(roh, 3),
                                   "vorher": round(vorher, 3),
                                   "nachher": fahrzeug.korrekturfaktor}
            log.info("Kalibrierung %s: %.3f -> %.3f (roh %.3f)",
                     fahrzeug.name, vorher, fahrzeug.korrekturfaktor, roh)
    return ergebnis


def verwaiste_beenden(db) -> list[dict]:
    """Alle Sitzungen beenden, die zu lange schweigen.

    Gibt zurueck, was beendet wurde - fuers Log und damit ein Prueflauf
    etwas nachsehen kann.
    """
    jetzt = datetime.utcnow()
    beendet = []
    for sitzung in db.query(models.LiveSitzung).filter_by(laeuft=True).all():
        punkte = sitzung.punkte
        letzte = punkte[-1].zeit if punkte else sitzung.gestartet
        if not punkte:
            minuten = FEHLSTART_MINUTEN
        elif ladephasen.laedt_am_ende(punkte, LADEFENSTER_MINUTEN,
                                      LADEHUB_PROZENT):
            minuten = LADEPAUSE_MINUTEN
        else:
            minuten = STILLE_MINUTEN
        if letzte is None or jetzt - letzte < timedelta(minutes=minuten):
            continue

        sitzung.laeuft = False
        sitzung.beendet = jetzt
        ergebnis = {"sitzung_id": sitzung.id, "punkte": len(punkte),
                    "frist_minuten": minuten,
                    "stille_minuten": round((jetzt - letzte).total_seconds() / 60)}

        # Gelernt wird auch hier - eine vergessene Fahrt ist keine schlechtere
        # Messung als eine ordentlich beendete.
        ergebnis.update(beenden_und_lernen(db, sitzung))

        beendet.append(ergebnis)
        log.info("Verwaiste Sitzung %s nach %s min Stille beendet: %s",
                 sitzung.id, ergebnis["stille_minuten"], ergebnis)

    if beendet:
        db.commit()
    return beendet


async def schleife(db_factory) -> None:
    """Die Hintergrundaufgabe. Wird beim Start der Anwendung angeworfen.

    Jeder Durchlauf bekommt eine eigene Datenbanksitzung: Die Aufgabe laeuft
    ueber die ganze Laufzeit des Prozesses, und eine dauerhaft offen
    gehaltene Verbindung ist genau die, die beim ersten Netzhaenger stirbt.
    """
    while True:
        await asyncio.sleep(TAKT_SEKUNDEN)
        db = db_factory()
        try:
            verwaiste_beenden(db)
        except Exception as fehler:      # noqa: BLE001
            # Eine gescheiterte Runde darf die Aufgabe nicht beenden - sonst
            # faellt das Aufraeumen beim ersten Fehler dauerhaft aus, und
            # niemand merkt es.
            log.warning("Aufräumen fehlgeschlagen: %s", fehler)
        finally:
            db.close()
