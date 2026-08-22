"""Eine Fahrt abspielen, ohne zu fahren.

Ohne den Simulator liesse sich die Live-Kette erst prüfen, wenn ein Auto,
ein Datenlieferant und eine echte Langstrecke zusammenkommen. Damit wäre
genau der Teil ungetestet, um den es in diesem Projekt geht.

Der Simulator läuft die geplante Route ab und meldet Ladestände, die um
`mehrverbrauch` vom Plan abweichen. Mit 1.0 folgt er dem Plan exakt, mit 1.2
verbraucht er zwanzig Prozent mehr - und genau dann muss die Nachführung
anschlagen und die Reserve vorziehen. Das ist der Prüfstein.
"""
import asyncio
import logging

from .. import models
from . import kanal, sitzung as live_sitzung

log = logging.getLogger("uvicorn.error")

SCHRITT_KM = 5.0


def schritte(fahrt: models.Fahrt, mehrverbrauch: float = 1.0,
             schritt_km: float = SCHRITT_KM) -> list[dict]:
    """Die Messpunkte einer simulierten Fahrt.

    Reine Funktion ohne Datenbank und ohne Warten - damit sie sich in einem
    Prüfskript direkt durchrechnen lässt.
    """
    profil = fahrt.energieprofil or []
    if not profil:
        return []

    gesamt_km = profil[-1].get("km") or 0.0
    punkte = []
    km = 0.0
    while km <= gesamt_km:
        eintrag = _profil_bei(profil, km)
        verbraucht = fahrt.start_soc - (eintrag.get("soc") or fahrt.start_soc)
        soc = fahrt.start_soc - verbraucht * mehrverbrauch
        punkte.append({
            "lat": eintrag.get("lat"), "lon": eintrag.get("lon"),
            "soc": round(max(0.0, soc), 2),
            "tempo_kmh": eintrag.get("tempo_kmh"),
            "aussentemp_c": fahrt.aussentemp_c,
            "km": round(km, 1)})
        # Bei null ist Schluss. Ein simuliertes Auto, das mit leerem Akku
        # weiterfährt und dabei brav 0 % meldet, würde genau den Fall
        # verschleiern, den die Simulation sichtbar machen soll: dass es
        # vorher hätte laden müssen. Wer mehr verbraucht, kommt kürzer -
        # und das muss man an der Zahl der Messpunkte sehen.
        if soc <= 0:
            break
        km += schritt_km
    return punkte


def _profil_bei(profil: list, km: float) -> dict:
    if km <= (profil[0].get("km") or 0.0):
        return profil[0]
    for vorher, nachher in zip(profil, profil[1:]):
        if (vorher.get("km") or 0.0) <= km <= (nachher.get("km") or 0.0):
            return nachher
    return profil[-1]


async def abspielen(db_factory, sitzung_id: int, mehrverbrauch: float = 1.0,
                    takt_s: float = 1.0, schritt_km: float = SCHRITT_KM) -> None:
    """Die Simulation als Hintergrundaufgabe.

    Jeder Schritt bekommt eine eigene Datenbanksitzung: Die Aufgabe läuft
    minutenlang, und eine über die ganze Zeit offen gehaltene Verbindung
    wäre genau die, die beim ersten Netzhänger stirbt.
    """
    db = db_factory()
    try:
        sitzung = db.get(models.LiveSitzung, sitzung_id)
        if not sitzung:
            return
        punkte = schritte(sitzung.fahrt, mehrverbrauch, schritt_km)
    finally:
        db.close()

    for messpunkt in punkte:
        db = db_factory()
        try:
            sitzung = db.get(models.LiveSitzung, sitzung_id)
            if not sitzung or not sitzung.laeuft:
                return
            zustand = live_sitzung.messpunkt_aufnehmen(
                db, sitzung, messpunkt["lat"], messpunkt["lon"], messpunkt["soc"],
                messpunkt.get("tempo_kmh"), messpunkt.get("aussentemp_c"))
        except Exception as fehler:      # noqa: BLE001
            log.warning("Simulation abgebrochen: %s", fehler)
            return
        finally:
            db.close()

        await kanal.senden(sitzung_id, {"typ": "zustand", "simuliert": True,
                                        **live_sitzung.zustand_als_dict(zustand)})
        await asyncio.sleep(takt_s)

    db = db_factory()
    try:
        sitzung = db.get(models.LiveSitzung, sitzung_id)
        if sitzung:
            sitzung.laeuft = False
            db.commit()
    finally:
        db.close()
    await kanal.senden(sitzung_id, {"typ": "ende", "simuliert": True})
