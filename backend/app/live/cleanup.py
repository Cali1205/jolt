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
from ..energy import calibration, charge_phases
from ..geo import haversine_m
from . import recording

log = logging.getLogger("uvicorn.error")

# Wie lange eine Sitzung schweigen darf, bevor sie als beendet gilt.
# Drei Stunden decken Ladestopp, Mittagessen und Funkloch zusammen ab.
QUIET_MINUTES = 180

# Und wenn zuletzt geladen wurde, noch einmal doppelt so lange. Eine
# Ladepause mit Essen kann gut zwei Stunden dauern, und danach geht es
# weiter - genau die Fahrt, die man nicht zerschneiden darf.
CHARGE_PAUSE_MINUTES = 360

# Wie weit zurueck nach steigendem Ladestand gesucht wird, und um wie viel er
# gestiegen sein muss. Hoeher als die Schwelle in `energie.ladephasen`, weil
# hier eine andere Frage gestellt wird: nicht "laedt dieser Abschnitt", sondern
# "war am Ende genug, um von einer Ladepause auszugehen". Ein Prozentpunkt ist
# mehr als das Rauschen der SoC-Messung und weniger als jeder Ladevorgang.
CHARGE_WINDOW_MINUTES = 25
CHARGE_SWING_PERCENT = 1.0

# Eine Sitzung ohne jeden Messpunkt ist ein Fehlstart: Jemand hat auf
# "aufzeichnen" getippt und es sich anders ueberlegt, oder die Verbindung kam
# nie zustande. Die braucht keine 90 Minuten Nachsicht.
FALSE_START_MINUTES = 20

# Wie oft nachgesehen wird. Haeufiger brauchte niemand - es geht um Fristen
# von Stunden.
TICK_SECONDS = 5 * 60


# Steht das Auto am Ende laenger als das, war es vergessen worden: Man steigt
# aus, geht weg und beendet die Fahrt erst spaeter. Alles ab dem letzten
# Fahren wird verworfen. Zehn Minuten sind mehr als jede Ampel, jeder
# Bahnuebergang und jeder Stau, der sich noch bewegt.
AS_OF_DISCARD_MINUTES = 10

# Ab dieser mittleren Geschwindigkeit zwischen zwei Messpunkten gilt das Auto
# als fahrend. Zu Fuss kommt man auf 5 km/h, und GPS-Rauschen im Stand liegt
# darunter; wer mit dem Telefon in der Hand weggeht, faehrt nicht.
DRIVE_KMH = 12.0


def as_of_at_end_cut_off(db, session) -> dict | None:
    """Die Zeit nach dem letzten Fahren verwerfen, wenn sie lang genug war.

    Der Anwendungsfall: Aussteigen und vergessen, die Fahrt zu beenden. Das
    Telefon liegt dann Stunden in der Tasche, die Messpunkte vom Parkplatz
    und vom Weg zur Wohnung hängen an der Strecke, und beim Lernen zaehlt
    jede dieser Minuten als Standverbrauch mit.

    Gefahren wird nach dem Weg zwischen zwei Messpunkten (Strecke durch Zeit),
    nicht nach `tempo_kmh`: Das fehlt auf iOS regelmaessig. Nur das **Ende**
    wird gekuerzt. Eine Ladepause mitten in der Fahrt bleibt, wie sie ist -
    danach geht es ja weiter.
    """
    points = list(session.points)
    if len(points) < 3:
        return None

    latest_trip = None
    for ahead_of, afterwards in zip(points, points[1:]):
        dt = (afterwards.timestamp - ahead_of.timestamp).total_seconds()
        if dt <= 0:
            continue
        kmh = haversine_m(ahead_of.lat, ahead_of.lon, afterwards.lat, afterwards.lon) / dt * 3.6
        if kmh >= DRIVE_KMH or (afterwards.speed_kmh or 0) >= DRIVE_KMH:
            latest_trip = afterwards
    # Nie gefahren: Das ist eine andere Geschichte (Fehlstart), kein Rest.
    if latest_trip is None:
        return None

    as_of = points[-1].timestamp - latest_trip.timestamp
    if as_of < timedelta(minutes=AS_OF_DISCARD_MINUTES):
        return None

    path = [p for p in points if p.timestamp > latest_trip.timestamp]
    for p in path:
        session.points.remove(p)
        db.delete(p)
    session.ended_at = latest_trip.timestamp
    db.flush()
    result = {"discarded_points": len(path),
                "discarded_minutes": round(as_of.total_seconds() / 60)}
    log.info("Sitzung %s: %s min Stand am Ende verworfen (%s Messpunkte).",
             session.id, result["discarded_minutes"], len(path))
    return result


def end_and_learn(db, session) -> dict:
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
    result: dict = {"recording": None, "learned": None,
                      "not_learned": None}
    trip = session.trip

    # Zuerst kuerzen: Strecke und Lernen sollen das Ende der Fahrt nie sehen,
    # nur die Fahrt selbst.
    result["as_of_discarded"] = as_of_at_end_cut_off(db, session)

    if trip is not None and trip.recording and not trip.geometry:
        try:
            result["recording"] = recording.complete(
                db, trip, session)
        except Exception as failure:      # noqa: BLE001
            # Die Messpunkte bleiben; eine gescheiterte Rekonstruktion darf
            # sie nicht mitnehmen.
            log.warning("Aufzeichnung %s nicht abzuschliessen: %s",
                        trip.id, failure)
            result["recording"] = {"ok": False, "reason": str(failure)}

    vehicle = trip.vehicle if trip else None

    # Eine Fahrt mit Fahrradtraeger oder Dachbox lehrt nichts ueber das
    # *Fahrzeug*: Der gemessene Mehrverbrauch enthaelt dann zwei Unbekannte,
    # und aus einer Messung lassen sich nicht zwei Zahlen bestimmen.
    surcharge = (trip.air_drag_factor or 1.0) if trip else 1.0
    # Mit Anhänger gilt dasselbe: Masse und Luftwiderstand des Gespanns
    # stecken im gemessenen Verbrauch und gehören nicht ins Fahrzeug.
    if vehicle and trip.trailer_kg:
        result["not_learned"] = (
            f"Fahrt mit Anhänger ({trip.trailer_kg:g} kg) - daraus "
            f"lässt sich der Faktor des Fahrzeugs nicht bestimmen.")
        vehicle = None
    elif vehicle and abs(surcharge - 1.0) > 0.001:
        result["not_learned"] = (
            f"Fahrt mit Luftwiderstands-Zuschlag ×{surcharge:g} - daraus "
            f"lässt sich der Faktor des Fahrzeugs nicht bestimmen.")
        vehicle = None

    if vehicle:
        # Auch hier die gemessene Kapazitaet: Der gelernte Faktor ist der
        # Quotient aus gemessener und vorhergesagter Energie, und die
        # gemessene entsteht aus Prozent mal Kapazitaet.
        raw = calibration.from_live_session(session, vehicle.capacity_kwh)
        if raw is not None:
            earlier = vehicle.correction_factor
            vehicle.correction_factor = calibration.carry_on(earlier, raw)
            result["learned"] = {"raw_factor": round(raw, 3),
                                   "earlier": round(earlier, 3),
                                   "after": vehicle.correction_factor}
            log.info("Kalibrierung %s: %.3f -> %.3f (roh %.3f)",
                     vehicle.name, earlier, vehicle.correction_factor, raw)
    return result


def end_orphaned(db) -> list[dict]:
    """Alle Sitzungen beenden, die zu lange schweigen.

    Gibt zurueck, was beendet wurde - fuers Log und damit ein Prueflauf
    etwas nachsehen kann.
    """
    now_ts = datetime.utcnow()
    ended_at = []
    for session in db.query(models.LiveSession).filter_by(running=True).all():
        points = session.points
        tail = points[-1].timestamp if points else session.started_at
        if not points:
            mins = FALSE_START_MINUTES
        elif charge_phases.charges_at_end(points, CHARGE_WINDOW_MINUTES,
                                      CHARGE_SWING_PERCENT):
            mins = CHARGE_PAUSE_MINUTES
        else:
            mins = QUIET_MINUTES
        if tail is None or now_ts - tail < timedelta(minutes=mins):
            continue

        session.running = False
        session.ended_at = now_ts
        result = {"session_id": session.id, "points": len(points),
                    "deadline_minutes": mins,
                    "quiet_minutes": round((now_ts - tail).total_seconds() / 60)}

        # Gelernt wird auch hier - eine vergessene Fahrt ist keine schlechtere
        # Messung als eine ordentlich beendete.
        # Eine Sitzung, an der das Lernen scheitert, darf die uebrigen nicht
        # aufhalten - sonst probiert jede Runde dieselbe zuerst und kommt nie
        # weiter. Sie ist beendet; was fehlschlug, steht im Log.
        try:
            with db.begin_nested():
                result.update(end_and_learn(db, session))
        except Exception as failure:      # noqa: BLE001
            log.warning("Verwaiste Sitzung %s nicht abzuschliessen: %s",
                        session.id, failure)
            result["failure"] = str(failure)

        ended_at.append(result)
        log.info("Verwaiste Sitzung %s nach %s min Stille beendet: %s",
                 session.id, result["quiet_minutes"], result)

    if ended_at:
        db.commit()
    return ended_at


async def loop(db_factory) -> None:
    """Die Hintergrundaufgabe. Wird beim Start der Anwendung angeworfen.

    Jeder Durchlauf bekommt eine eigene Datenbanksitzung: Die Aufgabe laeuft
    ueber die ganze Laufzeit des Prozesses, und eine dauerhaft offen
    gehaltene Verbindung ist genau die, die beim ersten Netzhaenger stirbt.
    """
    while True:
        await asyncio.sleep(TICK_SECONDS)
        db = db_factory()
        try:
            end_orphaned(db)
        except Exception as failure:      # noqa: BLE001
            # Eine gescheiterte Runde darf die Aufgabe nicht beenden - sonst
            # faellt das Aufraeumen beim ersten Fehler dauerhaft aus, und
            # niemand merkt es.
            log.warning("Aufräumen fehlgeschlagen: %s", failure)
        finally:
            db.close()
