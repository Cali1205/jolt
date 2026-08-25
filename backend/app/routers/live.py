"""Live-Endpunkte: Messpunkte herein, Zustand hinaus.

Der POST-Endpunkt ist bewusst so schlicht gehalten, dass ein OBD2-Logger oder
ein Apple-Kurzbefehl ihn ohne Bibliothek bedienen kann - ein JSON-Objekt mit
Position und Ladestand, mehr braucht es nicht. Das ist die Schnittstelle, an
der später die echten Fahrzeugdaten andocken.
"""
import asyncio
import logging
from datetime import datetime

from fastapi import (APIRouter, Depends, HTTPException, Query, WebSocket,
                     WebSocketDisconnect)
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import deps, models, push
from ..database import SessionLocal, get_db
from ..energie import kalibrierung
from ..live import kanal, simulator, umplanung
from ..live import sitzung as live_sitzung

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api/live", tags=["live"])


class Messpunkt(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    soc: float = Field(ge=0, le=100)
    tempo_kmh: float | None = None
    aussentemp_c: float | None = None


def _sitzung_holen(db: Session, sitzung_id: int) -> models.LiveSitzung:
    sitzung = db.get(models.LiveSitzung, sitzung_id)
    if not sitzung:
        raise HTTPException(404, "Live-Sitzung nicht gefunden.")
    return sitzung


@router.post("/start/{fahrt_id}", dependencies=[Depends(deps.aktuelle_sitzung)])
def starten(fahrt_id: int, radius_km: float = Query(10.0, gt=0, le=50),
            min_kw: float = Query(50.0, ge=0), steckertyp: str = "",
            umweg_grenze_min: float = Query(umplanung.VORGABEN["umweg_grenze_min"],
                                            gt=0, le=60),
            db: Session = Depends(get_db)):
    fahrt = db.get(models.Fahrt, fahrt_id)
    if not fahrt:
        raise HTTPException(404, "Fahrt nicht gefunden.")

    # Eine zweite laufende Sitzung zur selben Fahrt wäre nur verwirrend:
    # Zwei Verbrauchsfaktoren zu derselben Strecke, und niemand weiss, welcher
    # gilt. Die alte wird deshalb beendet.
    for alt in db.query(models.LiveSitzung).filter_by(fahrt_id=fahrt_id,
                                                      laeuft=True).all():
        alt.laeuft = False

    sitzung = models.LiveSitzung(fahrt_id=fahrt_id)

    # Der Plan beim Losfahren. Er ist der Bezugspunkt für alles Weitere: Ohne
    # ihn liesse sich unterwegs nicht sagen, *dass* sich etwas geändert hat -
    # nur, dass etwas anders ist als das Energieprofil erwartet hat. Und die
    # Suchparameter bleiben für die ganze Fahrt dieselben.
    parameter = {"radius_km": radius_km, "min_kw": min_kw,
                 "steckertyp": steckertyp, "umweg_grenze_min": umweg_grenze_min}
    if fahrt.energieprofil:
        try:
            sitzung.plan = umplanung.planen(db, fahrt, 0.0, fahrt.start_soc,
                                            parameter)
        except Exception as fehler:      # noqa: BLE001
            # Ohne Startplan läuft die Fahrt trotzdem - die Nachführung
            # arbeitet dann gegen das Energieprofil, wie in Stufe 1.
            log.warning("Startplan fehlgeschlagen: %s", fehler)

    db.add(sitzung)
    db.commit()
    return {"sitzung_id": sitzung.id, "fahrt_id": fahrt_id,
            "plan": sitzung.plan}


@router.post("/{sitzung_id}/punkt")
async def punkt_melden(sitzung_id: int, messpunkt: Messpunkt,
                       db: Session = Depends(get_db)):
    """Einen Messpunkt einsortieren.

    Ohne Anmeldung erreichbar, damit ein Logger im Auto nichts über Tokens
    wissen muss - die Sitzungs-ID ist der Schlüssel. Geschrieben wird nur in
    eine bereits laufende Sitzung, und die legt ausschliesslich an, wer
    angemeldet ist.
    """
    sitzung = _sitzung_holen(db, sitzung_id)
    if not sitzung.laeuft:
        raise HTTPException(409, "Diese Live-Sitzung ist beendet.")

    zustand = live_sitzung.messpunkt_aufnehmen(
        db, sitzung, messpunkt.lat, messpunkt.lon, messpunkt.soc,
        messpunkt.tempo_kmh, messpunkt.aussentemp_c)

    nachricht = {"typ": "zustand", "simuliert": False,
                 **live_sitzung.zustand_als_dict(zustand)}
    await kanal.senden(sitzung_id, nachricht)
    # Eine geänderte Planung ist der einzige Anlass, jemanden am Steuer zu
    # stören - und der einzige, der auch ein dunkles Telefon erreichen muss.
    # Im Hintergrund, weil die Antwort an ein fahrendes Auto nicht auf einen
    # Push-Dienst warten darf.
    if zustand.plan_geaendert:
        push.senden_hintergrund(SessionLocal, "jolt – Ladeplan geändert",
                                zustand.aenderung)
    return nachricht


@router.get("/{sitzung_id}")
def zustand_lesen(sitzung_id: int, db: Session = Depends(get_db)):
    sitzung = _sitzung_holen(db, sitzung_id)
    letzter = sitzung.punkte[-1] if sitzung.punkte else None
    return {"sitzung_id": sitzung.id, "fahrt_id": sitzung.fahrt_id,
            "laeuft": sitzung.laeuft, "hinweis": sitzung.hinweis,
            "verbrauchsfaktor": round(sitzung.verbrauchsfaktor, 3),
            "zeitfaktor": round(sitzung.zeitfaktor, 3),
            # Der aktuell gültige Plan, damit ein Gerät, das sich neu
            # verbindet, nicht auf den nächsten Messpunkt warten muss.
            "plan": sitzung.plan,
            "zuschauer": kanal.zuschauer(sitzung_id),
            "punkte": len(sitzung.punkte),
            "letzter": None if not letzter else {
                "lat": letzter.lat, "lon": letzter.lon, "soc": letzter.soc,
                "km_auf_route": letzter.km_auf_route,
                "soll_soc": letzter.soll_soc,
                "zeit": letzter.zeit.isoformat()}}


@router.post("/{sitzung_id}/ende", dependencies=[Depends(deps.aktuelle_sitzung)])
def beenden(sitzung_id: int, db: Session = Depends(get_db)):
    """Fahrt abschliessen - und aus ihr lernen.

    Der Verbrauchsfaktor der Sitzung gilt nur für diese eine Fahrt; er stirbt
    mit ihr. Was bleiben soll, ist die Erkenntnis dahinter: Wenn das Modell
    systematisch zu optimistisch rechnet, soll die *nächste* Planung das schon
    wissen, statt es nach achtzig Kilometern erneut zu lernen. Genau dafür
    trägt das Fahrzeug einen Korrekturfaktor, und genau hier wird er
    fortgeschrieben - gedämpft, damit eine einzelne Fahrt mit Dachbox ihn
    nicht dauerhaft verbiegt.
    """
    sitzung = _sitzung_holen(db, sitzung_id)
    sitzung.laeuft = False
    sitzung.beendet = datetime.utcnow()

    gelernt = None
    fahrzeug = sitzung.fahrt.fahrzeug if sitzung.fahrt else None
    if fahrzeug:
        roh = kalibrierung.aus_live_sitzung(sitzung, fahrzeug.akku_netto_kwh)
        if roh is not None:
            vorher = fahrzeug.korrekturfaktor
            fahrzeug.korrekturfaktor = kalibrierung.nachfuehren(vorher, roh)
            gelernt = {"rohfaktor": round(roh, 3), "vorher": round(vorher, 3),
                       "nachher": fahrzeug.korrekturfaktor}
            log.info("Kalibrierung %s: %.3f -> %.3f (roh %.3f)",
                     fahrzeug.name, vorher, fahrzeug.korrekturfaktor, roh)

    db.commit()
    return {"ok": True, "verbrauchsfaktor": round(sitzung.verbrauchsfaktor, 3),
            # None heisst "diese Fahrt war nicht verwertbar" - zu kurz, oder
            # der Faktor lag ausserhalb der Plausibilitätsgrenzen.
            "gelernt": gelernt}


@router.post("/{sitzung_id}/simulieren",
             dependencies=[Depends(deps.aktuelle_sitzung)])
async def simulieren(sitzung_id: int,
                     mehrverbrauch: float = Query(1.0, ge=0.5, le=2.0),
                     takt_s: float = Query(0.5, ge=0.05, le=10.0),
                     zeitfaktor: float = Query(1.0, ge=0.5, le=3.0),
                     db: Session = Depends(get_db)):
    """Die geplante Fahrt abspielen, mit einstellbarem Mehrverbrauch.

    Mit 1.0 folgt die Simulation dem Plan exakt, mit 1.2 verbraucht sie
    zwanzig Prozent mehr - dann muss die Nachführung anschlagen und die
    Reserve vorziehen. Das ist der Prüfstein der Live-Funktion.

    `zeitfaktor` simuliert Stau: 1.4 heisst "vierzig Prozent länger unterwegs".
    Der Verbrauch merkt das kaum, die Ankunftszeit sehr wohl - und damit
    lässt sich der Auslöser prüfen, den der Verbrauch allein nie auslöst.
    """
    sitzung = _sitzung_holen(db, sitzung_id)
    if not sitzung.laeuft:
        raise HTTPException(409, "Diese Live-Sitzung ist beendet.")
    if not (sitzung.fahrt.energieprofil or []):
        raise HTTPException(409, "Zur Fahrt gibt es kein Energieprofil.")

    asyncio.create_task(simulator.abspielen(
        SessionLocal, sitzung_id, mehrverbrauch, takt_s,
        zeitfaktor=zeitfaktor))
    return {"gestartet": True, "mehrverbrauch": mehrverbrauch,
            "takt_s": takt_s, "zeitfaktor": zeitfaktor}


@router.websocket("/{sitzung_id}/ws")
async def live_kanal(websocket: WebSocket, sitzung_id: int):
    await websocket.accept()
    await kanal.anmelden(sitzung_id, websocket)
    try:
        while True:
            # Es wird nichts erwartet; der Empfang hält nur die Verbindung
            # offen und meldet ihren Abbruch.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as fehler:      # noqa: BLE001
        log.debug("Live-WebSocket beendet: %s", fehler)
    finally:
        await kanal.abmelden(sitzung_id, websocket)
