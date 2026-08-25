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

    ./tools/check_quellen.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pruefen import Pruefung, anwendung_bereitstellen  # noqa: E402

anwendung_bereitstellen("quellen", datenbank=False)

from app.live import quellen                          # noqa: E402
from app.live.quellen import QuellenFehler            # noqa: E402

pruefe = Pruefung()


def wirft(quelle, daten: dict, text: str, erwartet_im_grund: str = "") -> None:
    """Die Meldung muss abgelehnt werden - und der Grund muss etwas sagen."""
    try:
        ergebnis = quelle.normalisieren(daten)
    except QuellenFehler as fehler:
        grund = str(fehler)
        if erwartet_im_grund and erwartet_im_grund.lower() not in grund.lower():
            pruefe(False, text, f"Grund nennt {erwartet_im_grund!r} nicht: {grund}")
            return
        pruefe(True, text)
        return
    except Exception as fehler:      # noqa: BLE001
        # Eine andere Ausnahme ist kein Erfolg: Sie kommt als 500er heraus
        # statt als Satz, der sagt, was der Logger falsch schickt.
        pruefe(False, text, f"{type(fehler).__name__} statt QuellenFehler: {fehler}")
        return
    pruefe(False, text, f"wurde angenommen: {ergebnis}")


# ---------------------------------------------------------------------------
# Die Registry
# ---------------------------------------------------------------------------

def teil_registry():
    print("\nFormate nachschlagen")

    pruefe(set(quellen.formate()) >= {"jolt", "abrp"},
           "jolt und abrp sind bekannt", str(sorted(quellen.formate())))
    pruefe(quellen.finden("ABRP").name == "abrp",
           "der Name wird unabhängig von Gross- und Kleinschreibung gefunden")
    pruefe(quellen.finden(" jolt ").name == "jolt",
           "und mit Leerraum drumherum auch")

    try:
        quellen.finden("torque")
        pruefe(False, "ein unbekanntes Format wird abgelehnt")
    except QuellenFehler as fehler:
        pruefe(True, "ein unbekanntes Format wird abgelehnt")
        pruefe("abrp" in str(fehler) and "jolt" in str(fehler),
               "und der Grund zählt auf, was es stattdessen gibt", str(fehler))


# ---------------------------------------------------------------------------
# jolts eigenes Format
# ---------------------------------------------------------------------------

def teil_jolt():
    print("\njolts eigenes Format")
    q = quellen.finden("jolt")

    punkt = q.normalisieren({"lat": 48.13, "lon": 11.58, "soc": 62.5,
                             "tempo_kmh": 118.0, "aussentemp_c": -3.5})
    pruefe(punkt.soc == 62.5 and punkt.lat == 48.13,
           "eine vollständige Meldung kommt unverändert durch")
    pruefe(punkt.zeit is None and punkt.laedt is None,
           "was nicht mitgeschickt wurde, bleibt None - und wird nicht geraten")

    punkt = q.normalisieren({"lat": 48.13, "lon": 11.58, "soc": 62.5,
                             "zeit": "2026-08-25T09:30:00", "laedt": True})
    pruefe(punkt.zeit == datetime(2026, 8, 25, 9, 30),
           "ein ISO-Zeitstempel wird gelesen", str(punkt.zeit))
    pruefe(punkt.laedt is True, "und die Ladeanzeige auch")

    wirft(q, {"lon": 11.58, "soc": 62.5}, "ohne Breitengrad wird abgelehnt", "lat")
    wirft(q, {"lat": 48.13, "lon": 11.58}, "ohne Ladestand wird abgelehnt", "soc")

    # Aussentemperatur 0 °C und "keine Aussentemperatur" sind zwei
    # verschiedene Aussagen. Sie zu verwechseln heisst im Winter, die Heizung
    # nicht zu rechnen - und die ist der grösste Einzelposten der Kälte.
    punkt = q.normalisieren({"lat": 48.13, "lon": 11.58, "soc": 62.5,
                             "aussentemp_c": 0.0})
    pruefe(punkt.aussentemp_c == 0.0,
           "null Grad Aussentemperatur ist ein Messwert, kein fehlendes Feld")
    punkt = q.normalisieren({"lat": 48.13, "lon": 11.58, "soc": 62.5,
                             "aussentemp_c": None})
    pruefe(punkt.aussentemp_c is None,
           "ein ausdrückliches null dagegen ist ein fehlendes Feld")


# ---------------------------------------------------------------------------
# Das Format von Iternio/ABRP
# ---------------------------------------------------------------------------

def teil_abrp():
    print("\nTelemetrieformat von Iternio (ABRP)")
    q = quellen.finden("abrp")

    # So sieht eine Meldung aus, wie sie an /1/tlm/send geht.
    tlm = {"utc": 1787654321, "soc": 57.0, "lat": 45.19, "lon": 0.72,
           "speed": 104.5, "ext_temp": 21.0, "is_charging": 0,
           "soh": 98.0, "power": -34.2, "car_model": "volkswagen:id_buzz:22:77"}
    punkt = q.normalisieren(tlm)
    pruefe(punkt.soc == 57.0 and punkt.lat == 45.19 and punkt.lon == 0.72,
           "eine Meldung im Sendeformat wird übersetzt")
    pruefe(punkt.tempo_kmh == 104.5 and punkt.aussentemp_c == 21.0,
           "Tempo und Aussentemperatur kommen mit")
    pruefe(punkt.laedt is False,
           "is_charging=0 heisst nicht 'keine Aussage', sondern 'lädt nicht'",
           str(punkt.laedt))
    pruefe(punkt.zeit is not None and punkt.zeit.year == 2026,
           "der Zeitstempel wird gelesen", str(punkt.zeit))

    # Felder, die jolt nicht braucht, dürfen nicht stören - das Format hat
    # zwei Dutzend davon, und es kommen welche dazu.
    pruefe(q.normalisieren({**tlm, "voellig_neues_feld": 42}).soc == 57.0,
           "unbekannte Felder werden übergangen statt abgelehnt")

    # Dieselbe Nutzlast, drei Verpackungen: gesendet (`tlm`), abgeholt
    # (`result`), von Hand weitergereicht (nackt).
    pruefe(q.normalisieren({"tlm": tlm}).soc == 57.0,
           "die Sende-Hülle tlm wird ausgepackt")
    antwort = {"status": "ok", "result": tlm}
    pruefe(q.normalisieren(antwort).soc == 57.0,
           "und die Antwort-Hülle result ebenso")

    # Millisekunden statt Sekunden ist der häufigste Fehler an dieser Stelle
    # und fällt sonst erst auf, wenn der Zeitfaktor Unsinn ergibt.
    in_ms = q.normalisieren({**tlm, "utc": 1787654321000})
    pruefe(in_ms.zeit == punkt.zeit,
           "ein Zeitstempel in Millisekunden ergibt dieselbe Zeit wie in "
           "Sekunden", f"{in_ms.zeit} gegen {punkt.zeit}")

    # Eine ungestellte Uhr - der Klassiker beim Kleinstrechner ohne Netz.
    wirft(q, {**tlm, "utc": 0}, "ein Zeitstempel aus 1970 wird abgelehnt", "Uhr")

    wirft(q, {**tlm, "soc": 137.0}, "ein Ladestand über 100 % wird abgelehnt",
          "Ladestand")
    wirft(q, {**tlm, "lat": 91.0}, "ein Breitengrad über 90° wird abgelehnt",
          "Breitengrad")
    wirft(q, {**tlm, "soc": "ziemlich voll"},
          "ein Ladestand, der keine Zahl ist, wird abgelehnt", "soc")
    wirft(q, {**tlm, "soc": float("nan")},
          "und NaN erst recht - es macht jede Schranke dahinter wirkungslos",
          "soc")

    fehlt = dict(tlm)
    del fehlt["soc"]
    wirft(q, fehlt, "ohne Ladestand wird abgelehnt", "soc")

    # Ohne Zeitstempel ist die Meldung trotzdem brauchbar: Dann gilt der
    # Zeitpunkt des Eintreffens, und für einen Logger, der laufend sendet,
    # ist das nahezu dasselbe.
    ohne_zeit = dict(tlm)
    del ohne_zeit["utc"]
    pruefe(q.normalisieren(ohne_zeit).zeit is None,
           "ohne Zeitstempel bleibt die Zeit offen, statt die Meldung zu "
           "verwerfen")

    # Ein Anteil statt Prozentpunkten wird bewusst NICHT umgerechnet: 0,4 ist
    # als "40 %" gemeint oder als "0,4 %", und bei fast leerem Akku zu raten
    # ist genau da falsch, wo es zählt.
    knapp = q.normalisieren({**tlm, "soc": 0.4})
    pruefe(knapp.soc == 0.4,
           "ein Ladestand unter 1 wird als Prozentpunkt genommen und nicht "
           "als Anteil hochgerechnet", f"{knapp.soc}")


def main() -> int:
    teil_registry()
    teil_jolt()
    teil_abrp()

    return pruefe.bilanz()


if __name__ == "__main__":
    sys.exit(main())
