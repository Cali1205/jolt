#!/usr/bin/env python3
"""Checks the translation of foreign measurement formats - `live/sources/`.

The point of this layer is that it knows no network: a translator gets a
parsed object and returns a `RawPoint`. That is why everything can be
checked completely here that would otherwise only have shown up in the
moving car - and a new format can be added from a recorded response
without anyone having to go for a drive.

Mainly what goes wrong is checked. A message that is correct is the boring
case; what is interesting is the missing field, the number in the wrong
unit and the timestamp from the year 1970. A translator that lets those
through merely shifts the error - it then ends up as a 500 in the log or,
worse, as silent nonsense in the energy profile.

Without network, without Postgres, without API key:

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
    """The message must be rejected - and the reason must say something."""
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
        # Another exception is not a success: it comes out as a 500 instead of as a
        # sentence that says what the logger is sending wrong.
        verify(False, text, f"{type(failure).__name__} statt SourcesError: {failure}")
        return
    verify(False, text, f"wurde angenommen: {result}")


# ---------------------------------------------------------------------------
# The registry
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
# jolt's own format
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

    # An outside temperature of 0 °C and "no outside temperature" are two
    # different statements. Confusing them means, in winter, not counting the
    # heating - and that is the largest single item of the cold.
    point = q.normalize({"lat": 48.13, "lon": 11.58, "soc": 62.5,
                             "outside_temp_c": 0.0})
    verify(point.outside_temp_c == 0.0,
           "null Grad Aussentemperatur ist ein Messwert, kein fehlendes Feld")
    point = q.normalize({"lat": 48.13, "lon": 11.58, "soc": 62.5,
                             "outside_temp_c": None})
    verify(point.outside_temp_c is None,
           "ein ausdrückliches null dagegen ist ein fehlendes Feld")


# ---------------------------------------------------------------------------
# The Iternio/ABRP format
# ---------------------------------------------------------------------------

def part_abrp():
    print("\nTelemetrieformat von Iternio (ABRP)")
    q = sources.find("abrp")

    # This is what a message looks like as it goes to /1/tlm/send.
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

    # Fields that jolt does not need must not interfere - the format has two
    # dozen of them, and more are added.
    verify(q.normalize({**tlm, "completely_fresh_field": 42}).soc == 57.0,
           "unbekannte Felder werden übergangen statt abgelehnt")

    # The same payload, three wrappings: sent (`tlm`), fetched (`result`),
    # passed on by hand (bare).
    verify(q.normalize({"tlm": tlm}).soc == 57.0,
           "die Sende-Hülle tlm wird ausgepackt")
    response = {"status": "ok", "result": tlm}
    verify(q.normalize(response).soc == 57.0,
           "und die Antwort-Hülle result ebenso")

    # Milliseconds instead of seconds is the most common error at this point
    # and would otherwise only be noticed when the time factor yields nonsense.
    in_ms = q.normalize({**tlm, "utc": 1787654321000})
    verify(in_ms.timestamp == point.timestamp,
           "ein Zeitstempel in Millisekunden ergibt dieselbe Zeit wie in "
           "Sekunden", f"{in_ms.timestamp} gegen {point.timestamp}")

    # An unset clock - the classic with a tiny logger without network.
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

    # Without a timestamp the message is still usable: the time of arrival
    # then applies, and for a logger that sends continuously this is nearly
    # the same.
    without_time = dict(tlm)
    del without_time["utc"]
    verify(q.normalize(without_time).timestamp is None,
           "ohne Zeitstempel bleibt die Zeit offen, statt die Meldung zu "
           "verwerfen")

    # A fraction instead of percentage points is deliberately NOT converted:
    # 0.4 is meant either as "40 %" or as "0.4 %", and guessing at an almost
    # empty battery is wrong exactly where it counts.
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
