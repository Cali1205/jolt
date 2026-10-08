"""Eine Fahrt abspielen, ohne zu fahren.

Ohne den Simulator liesse sich die Live-Kette erst prüfen, wenn ein Auto,
ein Datenlieferant und eine echte Langstrecke zusammenkommen. Damit wäre
genau der Teil ungetestet, um den es in diesem Projekt geht.

Der Simulator läuft die geplante Route ab und meldet Ladestände, die um
`mehrverbrauch` vom Plan abweichen. Mit 1.0 folgt er dem Plan exakt, mit 1.2
verbraucht er zwanzig Prozent mehr - und genau dann muss die Nachführung
anschlagen und die Reserve vorziehen. Das ist der Prüfstein.

`zeitfaktor` macht dasselbe mit der Uhr: 1.4 heisst "vierzig Prozent länger
unterwegs als geplant", also Stau. Dafür trägt jeder Messpunkt eine
**simulierte** Zeit. Ohne die wäre der Zeitfaktor hier nicht zu prüfen - der
Simulator spielt Stunden in Sekunden ab, und gegen die echte Uhr gemessen
wäre jede Fahrt absurd schnell.
"""
import asyncio
import logging
from datetime import datetime, timedelta

from .. import models, push
from ..energy.profile import entry_at
from . import channel, session as live_session

log = logging.getLogger("uvicorn.error")

STEP_KM = 5.0


def steps(trip: models.Trip, extra_consumption: float = 1.0,
             step_km: float = STEP_KM,
             time_factor: float = 1.0) -> list[dict]:
    """Die Messpunkte einer simulierten Fahrt.

    Reine Funktion ohne Datenbank und ohne Warten - damit sie sich in einem
    Prüfskript direkt durchrechnen lässt.
    """
    profile = trip.energy_profile or []
    if not profile:
        return []

    total_km = profile[-1].get("km") or 0.0
    points = []
    km = 0.0
    while km <= total_km:
        entry = entry_at(profile, km)
        consumed = trip.start_soc - (entry.get("soc") or trip.start_soc)
        soc = trip.start_soc - consumed * extra_consumption
        points.append({
            "lat": entry.get("lat"), "lon": entry.get("lon"),
            "soc": round(max(0.0, soc), 2),
            "speed_kmh": entry.get("speed_kmh"),
            "outside_temp_c": trip.outside_temp_c,
            "km": round(km, 1),
            # Minuten seit Abfahrt, wie sie *im Auto* vergangen wären.
            "mins": round((entry.get("mins") or 0.0) * time_factor, 3)})
        # Bei null ist Schluss. Ein simuliertes Auto, das mit leerem Akku
        # weiterfährt und dabei brav 0 % meldet, würde genau den Fall
        # verschleiern, den die Simulation sichtbar machen soll: dass es
        # vorher hätte laden müssen. Wer mehr verbraucht, kommt kürzer -
        # und das muss man an der Zahl der Messpunkte sehen.
        if soc <= 0:
            break
        km += step_km
    return points



async def replay(db_factory, session_id: int, extra_consumption: float = 1.0,
                    tick_s: float = 1.0, step_km: float = STEP_KM,
                    time_factor: float = 1.0) -> None:
    """Die Simulation als Hintergrundaufgabe.

    Jeder Schritt bekommt eine eigene Datenbanksitzung: Die Aufgabe läuft
    minutenlang, und eine über die ganze Zeit offen gehaltene Verbindung
    wäre genau die, die beim ersten Netzhänger stirbt.
    """
    db = db_factory()
    try:
        session = db.get(models.LiveSession, session_id)
        if not session:
            return
        points = steps(session.trip, extra_consumption, step_km, time_factor)
    finally:
        db.close()

    # Der Nullpunkt der simulierten Uhr. Die Messpunkte tragen ihre Zeit
    # relativ dazu, damit die Nachführung eine plausible Fahrt sieht und
    # nicht sechshundert Kilometer in vier Sekunden.
    onset = datetime.utcnow()

    for sample in points:
        db = db_factory()
        try:
            session = db.get(models.LiveSession, session_id)
            if not session or not session.running:
                return
            state = live_session.record_sample(
                db, session, sample["lat"], sample["lon"], sample["soc"],
                sample.get("speed_kmh"), sample.get("outside_temp_c"),
                timestamp=onset + timedelta(minutes=sample.get("mins") or 0.0))
        except Exception as failure:      # noqa: BLE001
            log.warning("Simulation abgebrochen: %s", failure)
            return
        finally:
            db.close()

        await channel.send(session_id, {"kind": "zustand", "simulated": True,
                                        **live_session.state_as_dict(state)})
        # Auch die Simulation benachrichtigt - sonst liesse sich die Kette bis
        # aufs Telefon nie durchspielen, ohne wirklich zu fahren.
        if state.plan_changed:
            push.send_background(db_factory, "jolt – Ladeplan geändert",
                                    state.change)
        await asyncio.sleep(tick_s)

    db = db_factory()
    try:
        session = db.get(models.LiveSession, session_id)
        if session:
            session.running = False
            db.commit()
    finally:
        db.close()
    await channel.send(session_id, {"kind": "ende", "simulated": True})
