#!/usr/bin/env python3
"""Prüft die Übersetzung fremder Messformate - `live/quellen/`.

Der Punkt dieser Schicht ist, dass sie kein Netz kennt: Ein Übersetzer
bekommt ein geparstes Objekt und gibt einen `Rohpunkt` zurück. Deshalb lässt
sich hier vollständig prüfen, was sonst nur im fahrenden Auto aufgefallen
wäre - und ein neues Format lässt sich anhand einer aufgezeichneten Antwort
einbauen, ohne dass jemand losfahren muss.

Geprüft wird vor allem das, was schiefgeht. Eine Meldung, die stimmt, ist
der langweilige Fall; interessant sind das fehlende Feld, die Zahl in der
falschen Einheit und der Zeitstempel aus dem Jahr 1970. Ein Übersetzer, der
die durchlässt, verlagert den Fehler nur - er landet dann als 500er im Log
oder, schlimmer, als stiller Unsinn im Energieprofil.

Ohne Netz, ohne Postgres, ohne API-Schlüssel:

    ./tools/check_sources.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from examine import Check, application_provide  # noqa: E402

application_provide("quellen", db_name=False)

from app.live import sources                          # noqa: E402
from app.live.sources import SourcesError            # noqa: E402

verify = Check()


def raises(source, records: dict, text: str, expected_in_reason: str = "") -> None:
    """Die Meldung muss abgelehnt werden - und der Grund muss etwas sagen."""
    try:
        result = source.normalize(records)
    except SourcesError as failure:
        reason = str(failure)
        if expected_in_reason and expected_in_reason.lower() not in reason.lower():
            verify(False, text, f"Grund nennt {expected_in_reason!r} nicht: {reason}")
            return
        verify(True, text)
        return
    except Exception as failure:      # noqa: BLE001
        # Eine andere Ausnahme ist kein Erfolg: Sie kommt als 500er heraus
        # statt als Satz, der sagt, was der Logger falsch schickt.
        verify(False, text, f"{type(failure).__name__} statt SourcesError: {failure}")
        return
    verify(False, text, f"wurde angenommen: {result}")


# ---------------------------------------------------------------------------
# Die Registry
# ---------------------------------------------------------------------------

def part_registry():
    print("\nFormate nachschlagen")

    verify(set(sources.formate()) >= {"jolt", "abrp"},
           "jolt und abrp sind bekannt", str(sorted(sources.formate())))
    verify(sources.find("ABRP").name == "abrp",
           "der Name wird unabhängig von Gross- und Kleinschreibung gefunden")
    verify(sources.find(" jolt ").name == "jolt",
           "und mit Leerraum drumherum auch")

    try:
        sources.find("torque")
        verify(False, "ein unbekanntes Format wird abgelehnt")
    except SourcesError as failure:
        verify(True, "ein unbekanntes Format wird abgelehnt")
        verify("abrp" in str(failure) and "jolt" in str(failure),
               "und der Grund zählt auf, was es stattdessen gibt", str(failure))


# ---------------------------------------------------------------------------
# jolts eigenes Format
# ---------------------------------------------------------------------------

def part_jolt():
    print("\njolts eigenes Format")
    q = sources.find("jolt")

    point = q.normalize({"lat": 48.13, "lon": 11.58, "soc": 62.5,
                             "speed_kmh": 118.0, "outside_temp_c": -3.5})
    verify(point.soc == 62.5 and point.lat == 48.13,
           "eine vollständige Meldung kommt unverändert durch")
    verify(point.timestamp is None and point.charges is None,
           "was nicht mitgeschickt wurde, bleibt None - und wird nicht geraten")

    point = q.normalize({"lat": 48.13, "lon": 11.58, "soc": 62.5,
                             "timestamp": "2026-08-25T09:30:00", "charges": True})
    verify(point.timestamp == datetime(2026, 8, 25, 9, 30),
           "ein ISO-Zeitstempel wird gelesen", str(point.timestamp))
    verify(point.charges is True, "und die Ladeanzeige auch")

    raises(q, {"lon": 11.58, "soc": 62.5}, "ohne Breitengrad wird abgelehnt", "lat")
    raises(q, {"lat": 48.13, "lon": 11.58}, "ohne Ladestand wird abgelehnt", "soc")

    # Aussentemperatur 0 °C und "keine Aussentemperatur" sind zwei
    # verschiedene Aussagen. Sie zu verwechseln heisst im Winter, die Heizung
    # nicht zu rechnen - und die ist der grösste Einzelposten der Kälte.
    point = q.normalize({"lat": 48.13, "lon": 11.58, "soc": 62.5,
                             "outside_temp_c": 0.0})
    verify(point.outside_temp_c == 0.0,
           "null Grad Aussentemperatur ist ein Messwert, kein fehlendes Feld")
    point = q.normalize({"lat": 48.13, "lon": 11.58, "soc": 62.5,
                             "outside_temp_c": None})
    verify(point.outside_temp_c is None,
           "ein ausdrückliches null dagegen ist ein fehlendes Feld")


# ---------------------------------------------------------------------------
# Das Format von Iternio/ABRP
# ---------------------------------------------------------------------------

def part_abrp():
    print("\nTelemetrieformat von Iternio (ABRP)")
    q = sources.find("abrp")

    # So sieht eine Meldung aus, wie sie an /1/tlm/send geht.
    tlm = {"utc": 1787654321, "soc": 57.0, "lat": 45.19, "lon": 0.72,
           "speed": 104.5, "ext_temp": 21.0, "is_charging": 0,
           "soh": 98.0, "power": -34.2, "car_model": "volkswagen:id_buzz:22:77"}
    point = q.normalize(tlm)
    verify(point.soc == 57.0 and point.lat == 45.19 and point.lon == 0.72,
           "eine Meldung im Sendeformat wird übersetzt")
    verify(point.speed_kmh == 104.5 and point.outside_temp_c == 21.0,
           "Tempo und Aussentemperatur kommen mit")
    verify(point.charges is False,
           "is_charging=0 heisst nicht 'keine Aussage', sondern 'lädt nicht'",
           str(point.charges))
    verify(point.timestamp is not None and point.timestamp.year == 2026,
           "der Zeitstempel wird gelesen", str(point.timestamp))

    # Felder, die jolt nicht braucht, dürfen nicht stören - das Format hat
    # zwei Dutzend davon, und es kommen welche dazu.
    verify(q.normalize({**tlm, "completely_fresh_field": 42}).soc == 57.0,
           "unbekannte Felder werden übergangen statt abgelehnt")

    # Dieselbe Nutzlast, drei Verpackungen: gesendet (`tlm`), abgeholt
    # (`result`), von Hand weitergereicht (nackt).
    verify(q.normalize({"tlm": tlm}).soc == 57.0,
           "die Sende-Hülle tlm wird ausgepackt")
    response = {"status": "ok", "result": tlm}
    verify(q.normalize(response).soc == 57.0,
           "und die Antwort-Hülle result ebenso")

    # Millisekunden statt Sekunden ist der häufigste Fehler an dieser Stelle
    # und fällt sonst erst auf, wenn der Zeitfaktor Unsinn ergibt.
    in_ms = q.normalize({**tlm, "utc": 1787654321000})
    verify(in_ms.timestamp == point.timestamp,
           "ein Zeitstempel in Millisekunden ergibt dieselbe Zeit wie in "
           "Sekunden", f"{in_ms.timestamp} gegen {point.timestamp}")

    # Eine ungestellte Uhr - der Klassiker beim Kleinstrechner ohne Netz.
    raises(q, {**tlm, "utc": 0}, "ein Zeitstempel aus 1970 wird abgelehnt", "Uhr")

    raises(q, {**tlm, "soc": 137.0}, "ein Ladestand über 100 % wird abgelehnt",
          "Ladestand")
    raises(q, {**tlm, "lat": 91.0}, "ein Breitengrad über 90° wird abgelehnt",
          "Breitengrad")
    raises(q, {**tlm, "soc": "ziemlich voll"},
          "ein Ladestand, der keine Zahl ist, wird abgelehnt", "soc")
    raises(q, {**tlm, "soc": float("nan")},
          "und NaN erst recht - es macht jede Schranke dahinter wirkungslos",
          "soc")

    missing = dict(tlm)
    del missing["soc"]
    raises(q, missing, "ohne Ladestand wird abgelehnt", "soc")

    # Ohne Zeitstempel ist die Meldung trotzdem brauchbar: Dann gilt der
    # Zeitpunkt des Eintreffens, und für einen Logger, der laufend sendet,
    # ist das nahezu dasselbe.
    without_time = dict(tlm)
    del without_time["utc"]
    verify(q.normalize(without_time).timestamp is None,
           "ohne Zeitstempel bleibt die Zeit offen, statt die Meldung zu "
           "verwerfen")

    # Ein Anteil statt Prozentpunkten wird bewusst NICHT umgerechnet: 0,4 ist
    # als "40 %" gemeint oder als "0,4 %", und bei fast leerem Akku zu raten
    # ist genau da falsch, wo es zählt.
    tight = q.normalize({**tlm, "soc": 0.4})
    verify(tight.soc == 0.4,
           "ein Ladestand unter 1 wird als Prozentpunkt genommen und nicht "
           "als Anteil hochgerechnet", f"{tight.soc}")


def main() -> int:
    part_registry()
    part_jolt()
    part_abrp()

    return verify.balance()


if __name__ == "__main__":
    sys.exit(main())
