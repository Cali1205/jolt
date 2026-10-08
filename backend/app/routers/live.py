"""Live-Endpunkte: Messpunkte herein, Zustand hinaus.

Der POST-Endpunkt ist bewusst so schlicht gehalten, dass ein OBD2-Logger oder
ein Apple-Kurzbefehl ihn ohne Bibliothek bedienen kann - ein JSON-Objekt mit
Position und Ladestand, mehr braucht es nicht. Das ist die Schnittstelle, an
der später die echten Fahrzeugdaten andocken.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import (APIRouter, Depends, HTTPException, Query, Request,
                     WebSocket, WebSocketDisconnect)
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from .. import deps, models, push, security
from ..timestamp import utc_iso
from ..database import SessionLocal, get_db
from ..live import cleanup, channel, sources, simulator, replanning
from ..live import session as live_session

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api/live", tags=["live"])


# Die Messung des Dongles hat knapp zwanzig Werte; das Vierfache ist Luft.
RAW_VALUES_MAX_KEY = 80
RAW_VALUES_MAX_BYTES = 8000


class Sample(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    # Ohne Ladestand ist es eine reine Positionsmeldung - der Normalfall,
    # solange das Auto seinen Ladestand nicht selbst liefert. Das Telefon
    # liefert die Position im Sekundentakt, der Ladestand kommt gelegentlich
    # von Hand dazu. Ohne diese Punkte gäbe es unterwegs weder Zeitfaktor
    # noch Ankunftsprognose.
    soc: float | None = Field(default=None, ge=0, le=100)
    # Grenzen, die kein Auto verlässt: Ein Wert aus einem kaputten Logger
    # (Einheit vertauscht, Überlauf) würde sonst ungeprüft in die Kalibrierung
    # laufen und den Lernfaktor verziehen.
    speed_kmh: float | None = Field(default=None, ge=0, le=500,
                                    allow_inf_nan=False)
    outside_temp_c: float | None = Field(default=None, ge=-80, le=70,
                                       allow_inf_nan=False)
    # Alles Weitere, was die Quelle liefert - wird nur aufbewahrt, nicht
    # verrechnet. Siehe models.LivePunkt.rohwerte.
    raw_values: dict | None = None

    @field_validator("raw_values")
    @classmethod
    def _limit_raw_values(cls, val):
        """Aufbewahrt wird es je Punkt als JSON - also muss es klein bleiben.

        Ein Stapel hat bis zu 500 Punkte; ohne Grenze liesse sich mit einer
        einzigen Anfrage Datenbank und Speicher füllen.
        """
        if val is None:
            return val
        if len(val) > RAW_VALUES_MAX_KEY:
            raise ValueError(f"höchstens {RAW_VALUES_MAX_KEY} Rohwerte je Punkt")
        if len(json.dumps(val, default=str)) > RAW_VALUES_MAX_BYTES:
            raise ValueError(f"Rohwerte höchstens {RAW_VALUES_MAX_BYTES} Zeichen")
        return val

    # Zeitpunkt der **Messung**. Fehlt er, gilt der Eingang. Gesetzt wird er
    # von einem Gerät, das einen Funkloch-Puffer nachreicht - sonst lägen alle
    # nachgereichten Punkte auf derselben Sekunde, und der Zeitfaktor wäre
    # Unsinn. Mit Zeitzone (`Z`) oder ohne; ohne gilt UTC.
    timestamp: datetime | None = None


class SampleBatch(BaseModel):
    points: list[Sample] = Field(min_length=1, max_length=500)


# Wie weit ein Zeitstempel von der Gegenwart abweichen darf. Nach vorn nur
# ein Uhrenfehler des Telefons, nach hinten eine lange Fahrt ohne Netz.
TIME_AHEAD = timedelta(minutes=5)
TIME_BACK = timedelta(hours=48)


def _examine_time(timestamp: datetime | None) -> datetime | None:
    """Zeitstempel eines Geräts auf naives UTC bringen - oder ablehnen.

    Die Datenbank führt naive UTC-Zeiten (`datetime.utcnow`). Ein Stempel mit
    Zeitzone wird umgerechnet statt abgeschnitten, sonst wäre `+02:00` zwei
    Stunden daneben. Ein Stempel aus der Zukunft oder aus dem Jahr 1970 ist
    ein Uhrenfehler und würde die Reihenfolge der Punkte zerlegen.
    """
    if timestamp is None:
        return None
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
    now_ts = datetime.utcnow()
    if timestamp > now_ts + TIME_AHEAD or timestamp < now_ts - TIME_BACK:
        raise HTTPException(422, "Der Zeitstempel der Messung liegt zu weit "
                                 "von der Gegenwart entfernt.")
    return timestamp


class LoggerReport(BaseModel):
    """Eine Meldung von einem Gerät im Auto.

    Ausser `token` und `format` ist hier bewusst nichts festgeschrieben: Die
    übrigen Felder gehören dem jeweiligen Format und werden von dessen
    Übersetzer in `live/quellen/` geprüft. Sie hier ein zweites Mal zu
    beschreiben hiesse, jede Formatänderung an zwei Stellen nachzuziehen -
    und die Prüfung läge dann bei Pydantic statt bei dem Modul, das das
    Format wirklich kennt.
    """
    model_config = ConfigDict(extra="allow")

    token: str = Field(min_length=1, max_length=64)
    format: str = "jolt"


def _fetch_session(db: Session, session_id: int) -> models.LiveSession:
    session = db.get(models.LiveSession, session_id)
    if not session:
        raise HTTPException(404, "Live-Sitzung nicht gefunden.")
    return session


def _fetch_active_session(db: Session, session_id: int) -> models.LiveSession:
    session = _fetch_session(db, session_id)
    if not session.running:
        raise HTTPException(409, "Diese Live-Sitzung ist beendet.")
    return session


def _vehicle_to_token(db: Session, token: str):
    return (db.query(models.Vehicle)
            .filter(models.Vehicle.logger_token == token).one_or_none())


def _active_session_to_vehicle(db: Session, vehicle_id: int):
    return (db.query(models.LiveSession)
            .join(models.Trip, models.LiveSession.trip_id == models.Trip.id)
            .filter(models.Trip.vehicle_id == vehicle_id,
                    models.LiveSession.running.is_(True))
            .order_by(models.LiveSession.id.desc())
            .first())


async def _process_point(db: Session, session: models.LiveSession,
                             point: sources.RawPoint,
                             new_plan: bool = True) -> dict:
    """Einen Messpunkt einsortieren und alle unterrichten, die es angeht.

    `neu_planen=False` heisst: nachgereichter Punkt, nicht die Gegenwart.
    Dann wird weder umgeplant noch an die Zuschauer gesendet - ein Zustand
    von vor zehn Minuten liesse die Anzeige zurückspringen.
    """
    # Im Threadpool: `messpunkt_aufnehmen` rechnet und schreibt synchron, und
    # in einem `async def` hielte das den Event-Loop an - ein Stapel von 500
    # Punkten liess jede andere Anfrage und jeden WebSocket warten.
    state = await run_in_threadpool(
        live_session.record_sample,
        db, session, point.lat, point.lon, point.soc,
        point.speed_kmh, point.outside_temp_c, timestamp=point.timestamp,
        raw_values=point.raw_values, new_plan=new_plan)

    msg = {"kind": "zustand", "simulated": False,
                 **live_session.state_as_dict(state)}
    if not new_plan:
        return msg
    await channel.send(session.id, msg)
    # Eine geänderte Planung ist der einzige Anlass, jemanden am Steuer zu
    # stören - und der einzige, der auch ein dunkles Telefon erreichen muss.
    # Im Hintergrund, weil die Antwort an ein fahrendes Auto nicht auf einen
    # Push-Dienst warten darf.
    if state.plan_changed:
        push.send_background(SessionLocal, "jolt – Ladeplan geändert",
                                state.change)
    return msg


@router.post("/start/{trip_id}", dependencies=[Depends(deps.current_session)])
def launch(trip_id: int, radius_km: float = Query(10.0, gt=0, le=50),
            min_kw: float = Query(50.0, ge=0), connector_type: str = Query("", max_length=40),
            detour_limit_min: float = Query(replanning.DEFAULTS["detour_limit_min"],
                                            gt=0, le=60),
            stop_fixed_cost_min: float = Query(
                replanning.DEFAULTS["stop_fixed_cost_min"], ge=0, le=30),
            charge_park_bonus_min: float = Query(
                replanning.DEFAULTS["charge_park_bonus_min"], ge=0, le=15),
            time_value_eur_h: float = Query(
                replanning.DEFAULTS["time_value_eur_h"], ge=0, le=200),
            db: Session = Depends(get_db)):
    trip = db.get(models.Trip, trip_id)
    if not trip:
        raise HTTPException(404, "Fahrt nicht gefunden.")

    # Eine zweite laufende Sitzung zur selben Fahrt wäre nur verwirrend:
    # Zwei Verbrauchsfaktoren zu derselben Strecke, und niemand weiss, welcher
    # gilt. Die alte wird deshalb beendet.
    for old in db.query(models.LiveSession).filter_by(trip_id=trip_id,
                                                      running=True).all():
        old.running = False
        old.ended_at = datetime.utcnow()

    session = models.LiveSession(trip_id=trip_id)

    # Der Plan beim Losfahren. Er ist der Bezugspunkt für alles Weitere: Ohne
    # ihn liesse sich unterwegs nicht sagen, *dass* sich etwas geändert hat -
    # nur, dass etwas anders ist als das Energieprofil erwartet hat. Und die
    # Suchparameter bleiben für die ganze Fahrt dieselben.
    parameter = {"radius_km": radius_km, "min_kw": min_kw,
                 "connector_type": connector_type, "detour_limit_min": detour_limit_min,
                 "stop_fixed_cost_min": stop_fixed_cost_min,
                 "charge_park_bonus_min": charge_park_bonus_min,
                 "time_value_eur_h": time_value_eur_h}
    if trip.energy_profile:
        try:
            session.plan = replanning.schedule(db, trip, 0.0, trip.start_soc,
                                            parameter)
        except Exception as failure:      # noqa: BLE001
            # Ohne Startplan läuft die Fahrt trotzdem - die Nachführung
            # arbeitet dann gegen das Energieprofil, wie in Stufe 1.
            log.warning("Startplan fehlgeschlagen: %s", failure)

    db.add(session)
    db.commit()
    return {"session_id": session.id, "trip_id": trip_id,
            "plan": session.plan}


@router.post("/{session_id}/punkt", dependencies=[Depends(deps.current_session)])
async def report_point(session_id: int, sample: Sample,
                       db: Session = Depends(get_db)):
    """Einen Messpunkt einsortieren.

    Nur mit Anmeldung (`X-Token`). Vorher stand hier, die Sitzungs-ID sei
    der Schlüssel - eine fortlaufende Zahl, die jeder raten konnte, mit der
    sich Position und Ladestand lesen und falsche Messpunkte einspielen
    liessen. Ein Gerät im Auto ohne Anmeldung nimmt `/melden` mit dem
    Logger-Token seines Fahrzeugs.
    """
    session = await run_in_threadpool(_fetch_active_session, db, session_id)
    return await _process_point(db, session, sources.RawPoint(
        lat=sample.lat, lon=sample.lon, soc=sample.soc,
        speed_kmh=sample.speed_kmh, outside_temp_c=sample.outside_temp_c,
        timestamp=_examine_time(sample.timestamp), raw_values=sample.raw_values))


@router.post("/{session_id}/punkte", dependencies=[Depends(deps.current_session)])
async def report_points(session_id: int, batch: SampleBatch,
                        db: Session = Depends(get_db)):
    """Mehrere Messpunkte auf einmal - der Weg für einen Funkloch-Puffer.

    Die Punkte werden nach Messzeit geordnet und der Reihe nach aufgenommen;
    die Reihenfolge der Anfrage ist gleichgültig. Neu geplant wird nur beim
    **letzten** (siehe `messpunkt_aufnehmen`), und nur ihn gibt es als
    Zustand an die Zuschauer. Antwort ist der Zustand nach dem letzten Punkt
    - dieselbe Form wie bei `/punkt`, damit die Oberfläche beides gleich
    behandelt.

    Alles oder nichts: Ein ungültiger Zeitstempel lehnt den ganzen Stapel ab,
    bevor irgendetwas geschrieben wurde.
    """
    session = await run_in_threadpool(_fetch_active_session, db, session_id)
    now_ts = datetime.utcnow()
    checked = [(_examine_time(p.timestamp) or now_ts, i, p)
                for i, p in enumerate(batch.points)]
    checked.sort(key=lambda t: (t[0], t[1]))
    msg = None
    for nr, (timestamp, _, p) in enumerate(checked):
        msg = await _process_point(db, session, sources.RawPoint(
            lat=p.lat, lon=p.lon, soc=p.soc, speed_kmh=p.speed_kmh,
            outside_temp_c=p.outside_temp_c, timestamp=timestamp, raw_values=p.raw_values),
            new_plan=nr == len(checked) - 1)
    return msg


class RecordingStart(BaseModel):
    vehicle_id: int
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    soc: float | None = Field(default=None, ge=0, le=100)
    name: str = Field(default="", max_length=120)


@router.post("/aufzeichnung", dependencies=[Depends(deps.current_session)])
def start_recording(start: RecordingStart,
                         db: Session = Depends(get_db)):
    """Eine Fahrt aufzeichnen, ohne sie vorher zu planen.

    Der Weg für den Fall, für den sich Planen nicht lohnt: eine bekannte
    kurze Strecke, ein paarmal gefahren, um den Verbrauch des Fahrzeugs zu
    lernen. Strecke, Höhenprofil und Prognose entstehen erst beim Beenden
    aus den Messpunkten (`live/recording.py`).

    Start ist, wo das Gerät gerade steht - das Ziel ist zu diesem Zeitpunkt
    noch unbekannt und wird zunächst gleichgesetzt. Beides wird beim
    Abschliessen aus dem ersten und letzten Messpunkt berichtigt.
    """
    vehicle = db.get(models.Vehicle, start.vehicle_id)
    if not vehicle:
        raise HTTPException(404, "Fahrzeug nicht gefunden.")

    # Eine noch laufende Sitzung desselben Fahrzeugs weicht - aber sie wird
    # **abgeschlossen** und nicht stumm fallengelassen.
    #
    # Hier stand nur `alt.laeuft = False`. Fuer eine Aufzeichnung war das der
    # Totalverlust: Strecke und Energieprofil entstehen erst beim Abschliessen
    # aus den Messpunkten, und mit `laeuft = False` sieht auch das Aufraeumen
    # sie nie wieder. Wer eine vergessene Fahrt dadurch bemerkte, dass er eine
    # neue startete, loeschte damit genau die, die er retten wollte.
    for old in (db.query(models.LiveSession)
                .join(models.Trip, models.LiveSession.trip_id == models.Trip.id)
                .filter(models.Trip.vehicle_id == vehicle.id,
                        models.LiveSession.running.is_(True)).all()):
        old.running = False
        old.ended_at = datetime.utcnow()
        try:
            cleanup.end_and_learn(db, old)
        except Exception as failure:      # noqa: BLE001
            # Die neue Fahrt darf daran nicht scheitern - jemand sitzt im
            # Auto und will losfahren.
            log.warning("Vorige Sitzung %s nicht abzuschliessen: %s",
                        old.id, failure)

    label = (start.name or "").strip() or "Aufzeichnung"
    trip = models.Trip(
        vehicle_id=vehicle.id, recording=True,
        start_text=label, target_text="unterwegs",
        start_lat=start.lat, start_lon=start.lon,
        target_lat=start.lat, target_lon=start.lon,
        start_soc=start.soc if start.soc is not None else 100.0,
        geometry=[], energy_profile=[])
    db.add(trip)
    db.flush()

    session = models.LiveSession(trip_id=trip.id)
    db.add(session)
    db.commit()

    # Der Startladestand ist eine Messung - die erste der Fahrt. Als Messpunkt
    # aufgenommen, hat die Aufzeichnung von der ersten Sekunde an einen
    # Ladestand: Die Live-Anzeige zeigt ihn, und die Rekonstruktion beim
    # Abschliessen beginnt bei dem, was das Auto gemeldet hat, nicht bei 100 %.
    #
    # Bisher stand er nur an der Fahrt. Eine Aufzeichnung hat kein Profil, aus
    # dem sich ein Ladestand schätzen liesse - ohne Messung blieb die Anzeige
    # leer, bis das Auto zum ersten Mal antwortete.
    if start.soc is not None:
        try:
            live_session.record_sample(db, session, start.lat, start.lon,
                                             soc=start.soc)
        except Exception as failure:      # noqa: BLE001
            # Die Fahrt darf daran nicht scheitern - jemand sitzt im Auto.
            log.warning("Startpunkt der Aufzeichnung %s nicht aufgenommen: %s",
                        session.id, failure)
            db.rollback()
    return {"session_id": session.id, "trip_id": trip.id,
            "recording": True}


@router.post("/melden")
async def report_logger(report: LoggerReport, request: Request,
                        db: Session = Depends(get_db)):
    """Einen Messpunkt melden, ohne die Sitzungs-ID zu kennen.

    Der Weg für ein Gerät, das fest im Auto sitzt: ein OBD2-Dongle, ein
    Kurzbefehl, ein Skript auf einem Kleinstrechner. Es weist sich mit dem
    Logger-Token des **Fahrzeugs** aus - einem Geheimnis, das bleibt - und das
    Backend sucht sich die laufende Live-Sitzung dieses Fahrzeugs selbst. Die
    wechselt mit jeder Fahrt, und ein verbautes Gerät hat keine Möglichkeit,
    davon zu erfahren.

    Läuft gerade keine Fahrt, ist das **kein Fehler**: Das Auto steht dann
    einfach vor der Tür, und der Logger sendet trotzdem. Er bekommt deshalb
    200 mit `aufgenommen: false` und nicht 404 - ein unbeaufsichtigtes Gerät,
    das auf Fehlerantworten stösst, fängt an, Fehler zu protokollieren oder
    sich abzuschalten, und beides hilft niemandem.

    In welchem Format die Messwerte stehen, sagt `format`; übersetzt wird in
    `live/quellen/`. Ohne Angabe gilt jolts eigenes.
    """
    # Das Token zuerst: Wer keines hat, soll nicht erst die Übersetzung und
    # damit Rechenzeit bekommen. Und wer zu oft ein falsches schickt, wird
    # gebremst - der Pfad ist vom allgemeinen Limit ausgenommen.
    if security.report_locked(request):
        raise HTTPException(429, "Zu viele ungültige Logger-Token. "
                                 "Später erneut versuchen.")
    vehicle = await run_in_threadpool(_vehicle_to_token, db, report.token)
    if not vehicle:
        # Ein falsches Token ist ein Fehler - sonst liesse sich nicht
        # unterscheiden, ob der Logger falsch eingerichtet ist oder ob nur
        # gerade keine Fahrt läuft.
        security.count_report_error(request)
        raise HTTPException(401, "Logger-Token unbekannt.")

    try:
        translator = sources.find(report.format)
        point = translator.normalize(
            report.model_dump(exclude={"token", "format"}))
    except sources.SourcesError as failure:
        # 400 und nicht 422: Der Satz aus dem Übersetzer sagt, was der Logger
        # falsch schickt, und der soll ungefiltert beim Einrichtenden ankommen.
        raise HTTPException(400, str(failure))

    session = await run_in_threadpool(_active_session_to_vehicle, db,
                                      vehicle.id)
    if not session:
        return {"recorded": False, "vehicle": vehicle.name,
                "reason": "Zu diesem Fahrzeug läuft gerade keine Fahrt."}

    msg = await _process_point(db, session, point)
    return {"recorded": True, "session_id": session.id, **msg}


@router.get("/{session_id}", dependencies=[Depends(deps.current_session)])
def read_state(session_id: int, db: Session = Depends(get_db)):
    session = _fetch_session(db, session_id)
    last = session.points[-1] if session.points else None
    return {"session_id": session.id, "trip_id": session.trip_id,
            "running": session.running, "hint": session.hint,
            "consumption_factor": round(session.consumption_factor, 3),
            "time_factor": round(session.time_factor, 3),
            # Der aktuell gültige Plan, damit ein Gerät, das sich neu
            # verbindet, nicht auf den nächsten Messpunkt warten muss.
            "plan": session.plan,
            "viewer": channel.viewer(session_id),
            "points": len(session.points),
            "last": None if not last else {
                "lat": last.lat, "lon": last.lon, "soc": last.soc,
                "km_on_route": last.km_on_route,
                "plan_soc": last.plan_soc,
                "timestamp": utc_iso(last.timestamp)}}


@router.get("/{session_id}/punkte", dependencies=[Depends(deps.current_session)])
def read_points(session_id: int, db: Session = Depends(get_db)):
    """Die Messpunkte einer Sitzung - fuer ein Geraet, das neu dazukommt.

    Der Zustand allein reicht dafuer nicht: Er kennt nur den *letzten*
    Punkt. Wer die Seite neu laedt, bekam bisher eine leere Spur, ein leeres
    Balkendiagramm und eine Ladestandskurve, die bei null anfing - die Fahrt
    lief weiter, sah aber aus wie neu. Genau das hat am 2. September dazu
    gefuehrt, dass eine laufende Fahrt fuer verloren gehalten und eine neue
    geplant wurde.

    Aus `rohwerte` kommen nur die vier Zahlen mit, aus denen die Oberflaeche
    den Verbrauch zurueckrechnet. Der ganze Satz waere je Punkt siebzehnmal
    so gross, und auf einer Langstrecke mit ein paar tausend Punkten laedt
    das niemand ueber Mobilfunk.
    """
    session = _fetch_session(db, session_id)
    origin_of = []
    for point in session.points:
        raw = point.raw_values if isinstance(point.raw_values, dict) else {}
        origin_of.append({
            "timestamp": utc_iso(point.timestamp),
            "lat": point.lat, "lon": point.lon,
            "soc": point.soc, "km_on_route": point.km_on_route,
            "odometer_km": raw.get("odometer_km"),
            "discharge_kwh": raw.get("discharge_kwh"),
            "charged_kwh": raw.get("charged_kwh"),
            "soc_raw": raw.get("soc_raw"),
        })
    return {"session_id": session.id, "points": origin_of}


@router.post("/{session_id}/ende", dependencies=[Depends(deps.current_session)])
def finish(session_id: int, db: Session = Depends(get_db)):
    """Fahrt abschliessen - und aus ihr lernen.

    Der Verbrauchsfaktor der Sitzung gilt nur für diese eine Fahrt; er stirbt
    mit ihr. Was bleiben soll, ist die Erkenntnis dahinter: Wenn das Modell
    systematisch zu optimistisch rechnet, soll die *nächste* Planung das schon
    wissen, statt es nach achtzig Kilometern erneut zu lernen. Genau dafür
    trägt das Fahrzeug einen Korrekturfaktor, und genau hier wird er
    fortgeschrieben - gedämpft, damit eine einzelne Fahrt mit Dachbox ihn
    nicht dauerhaft verbiegt.
    """
    session = _fetch_session(db, session_id)
    # Zweimal beenden (Wiederholung nach Funkloch, Doppeltippen, oder das
    # Aufraeumen war schneller) darf nicht zweimal lernen: Der Korrekturfaktor
    # des Fahrzeugs wuerde aus derselben Fahrt doppelt fortgeschrieben.
    if not session.running:
        return {"ok": True, "already_ended_at": True, "recording": None,
                "as_of_discarded": None, "not_learned": None,
                "consumption_factor": round(session.consumption_factor, 3),
                "learned": None}
    session.running = False
    session.ended_at = datetime.utcnow()

    # Eine Aufzeichnung wird hier erst zur Fahrt: Strecke, Höhenprofil und
    # Prognose entstehen aus den Messpunkten. Das muss **vor** der
    # Kalibrierung geschehen - die vergleicht Soll und Ist an den Punkten,
    # und der Sollwert steht erst danach dort.
    # Strecke bauen, dann lernen - der Ablauf steht in
    # `live/aufraeumen.beenden_und_lernen`, weil ihn drei Wege brauchen.
    result = cleanup.end_and_learn(db, session)
    built = result["recording"]
    learned = result["learned"]
    not_learned = result["not_learned"]

    db.commit()
    return {"ok": True, "recording": built,
            "as_of_discarded": result.get("as_of_discarded"),
            "not_learned": not_learned,
            "consumption_factor": round(session.consumption_factor, 3),
            # None heisst "diese Fahrt war nicht verwertbar" - zu kurz, oder
            # der Faktor lag ausserhalb der Plausibilitätsgrenzen.
            "learned": learned}


def _session_exists(session_id: int) -> bool:
    db = SessionLocal()
    try:
        return db.get(models.LiveSession, session_id) is not None
    finally:
        db.close()


# Hintergrundaufgaben: Die Ereignisschleife hält nur eine schwache Referenz.
# Ohne eigene kann der Müllsammler eine laufende Simulation abräumen.
_tasks: set = set()
_simulations: dict = {}


def _task_hold(task):
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


@router.post("/{session_id}/simulieren",
             dependencies=[Depends(deps.current_session)])
async def simulate(session_id: int,
                     extra_consumption: float = Query(1.0, ge=0.5, le=2.0),
                     tick_s: float = Query(0.5, ge=0.05, le=10.0),
                     time_factor: float = Query(1.0, ge=0.5, le=3.0),
                     db: Session = Depends(get_db)):
    """Die geplante Fahrt abspielen, mit einstellbarem Mehrverbrauch.

    Mit 1.0 folgt die Simulation dem Plan exakt, mit 1.2 verbraucht sie
    zwanzig Prozent mehr - dann muss die Nachführung anschlagen und die
    Reserve vorziehen. Das ist der Prüfstein der Live-Funktion.

    `zeitfaktor` simuliert Stau: 1.4 heisst "vierzig Prozent länger unterwegs".
    Der Verbrauch merkt das kaum, die Ankunftszeit sehr wohl - und damit
    lässt sich der Auslöser prüfen, den der Verbrauch allein nie auslöst.
    """
    def examine():
        session = _fetch_active_session(db, session_id)
        if not (session.trip.energy_profile or []):
            raise HTTPException(409, "Zur Fahrt gibt es kein Energieprofil.")

    await run_in_threadpool(examine)

    if session_id in _simulations:
        raise HTTPException(409, "Für diese Fahrt läuft schon eine Simulation.")
    _simulations[session_id] = _task_hold(asyncio.create_task(
        simulator.replay(SessionLocal, session_id, extra_consumption, tick_s,
                            time_factor=time_factor)))
    _simulations[session_id].add_done_callback(
        lambda _t, s=session_id: _simulations.pop(s, None))
    return {"started_at": True, "extra_consumption": extra_consumption,
            "tick_s": tick_s, "time_factor": time_factor}


@router.websocket("/{session_id}/ws")
async def live_channel(websocket: WebSocket, session_id: int):
    """Der Zustand einer Fahrt, live.

    Ein Browser kann beim WebSocket keine Header setzen, und ein Token in der
    Adresse stünde in jedem Proxy-Protokoll. Deshalb schickt der Client den
    Token als **erste Nachricht** (`{"token": "..."}`); erst danach wird er
    in den Verteiler aufgenommen und bekommt etwas zu sehen. Ohne Passwort
    entfällt das, die Antwort `{"typ": "bereit"}` kommt trotzdem.
    """
    await websocket.accept()
    if deps.password_set():
        try:
            first_item = await asyncio.wait_for(websocket.receive_text(), timeout=10)
            token = json.loads(first_item).get("token", "")
            valid = isinstance(token, str) and await run_in_threadpool(
                deps.token_valid, token)
        except (asyncio.TimeoutError, ValueError, AttributeError,
                WebSocketDisconnect):
            valid = False
        if not valid:
            try:
                await websocket.close(code=4401)
            except Exception:      # noqa: BLE001
                pass
            return
    present = await run_in_threadpool(_session_exists, session_id)
    if not present or not await channel.sign_in(session_id, websocket):
        try:
            await websocket.close(code=4404 if not present else 4429)
        except Exception:      # noqa: BLE001
            pass
        return
    try:
        await websocket.send_json({"kind": "bereit"})
        while True:
            # Es wird nichts erwartet; der Empfang hält nur die Verbindung
            # offen und meldet ihren Abbruch.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as failure:      # noqa: BLE001
        log.debug("Live-WebSocket beendet: %s", failure)
    finally:
        await channel.sign_out(session_id, websocket)
