"""Route rechnen: Strecke, Höhenprofil, Wetter, Energiebedarf.

Der Endpunkt, der alles zusammenführt. Was er noch nicht tut, ist die
Ladestopps zu setzen - das ist Stufe 2. Was er bereits liefert, ist die
Antwort auf die Frage davor: Wie weit komme ich, bevor die Reserve greift?
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import deps, models, routing
from ..database import get_db
from ..energie import modell, wetter
from ..routing.provider import RoutingFehler

log = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api", tags=["route"],
                   dependencies=[Depends(deps.aktuelle_sitzung)])


class Punkt(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    text: str = ""


class Routenanfrage(BaseModel):
    fahrzeug_id: int
    start: Punkt
    ziel: Punkt
    start_soc: float = Field(default=80.0, ge=0, le=100)
    # 1.1 heisst "zehn Prozent schneller als das Routing annimmt". Der Regler
    # wirkt über v² überproportional - genau der Hebel, mit dem sich unterwegs
    # ein Ladestopp einsparen lässt.
    tempo_faktor: float = Field(default=1.0, ge=0.6, le=1.5)
    wetter_beruecksichtigen: bool = True


@router.get("/orte")
def orte_suchen(text: str = Query(min_length=2), land: str = "DE"):
    try:
        treffer = routing.provider().suchen(text, land)
    except RoutingFehler as fehler:
        raise HTTPException(502, str(fehler)) from fehler
    return {"demo": routing.ist_demo(),
            "treffer": [{"name": o.name, "lat": o.lat, "lon": o.lon}
                        for o in treffer]}


@router.post("/route")
def route_rechnen(anfrage: Routenanfrage, db: Session = Depends(get_db)):
    fahrzeug = db.get(models.Fahrzeug, anfrage.fahrzeug_id)
    if not fahrzeug:
        raise HTTPException(404, "Fahrzeug nicht gefunden.")

    anbieter = routing.provider()
    try:
        strecke = anbieter.route((anfrage.start.lat, anfrage.start.lon),
                                 (anfrage.ziel.lat, anfrage.ziel.lon))
    except RoutingFehler as fehler:
        raise HTTPException(502, str(fehler)) from fehler

    if len(strecke.punkte) < 2:
        raise HTTPException(502, "Route enthält zu wenige Punkte.")

    # Auf rund einen Punkt je 250 m ausdünnen. Auf einer Langstrecke liefert
    # das Routing fünfstellig viele Stützpunkte - für Karte und Prognose ist
    # das Rechenzeit ohne Erkenntnis. Höhensprünge bleiben dabei erhalten.
    punkte, tempo = modell.ausduennen(strecke.punkte, strecke.tempo_ms)

    if anfrage.wetter_beruecksichtigen:
        umgebung_fuer = wetter.entlang_route(punkte)
        mittel = wetter.mittelwert(punkte)
    else:
        umgebung_fuer = None
        mittel = modell.Umgebung()

    werte = modell.Fahrzeugwerte.aus_modell(fahrzeug)
    profil = modell.profil_rechnen(werte, punkte, tempo, anfrage.start_soc,
                                   umgebung_fuer, anfrage.tempo_faktor)

    fahrt = models.Fahrt(
        fahrzeug_id=fahrzeug.id,
        start_text=anfrage.start.text, start_lat=anfrage.start.lat,
        start_lon=anfrage.start.lon, ziel_text=anfrage.ziel.text,
        ziel_lat=anfrage.ziel.lat, ziel_lon=anfrage.ziel.lon,
        start_soc=anfrage.start_soc, tempo_faktor=anfrage.tempo_faktor,
        aussentemp_c=mittel.temp_c,
        strecke_m=strecke.strecke_m or profil.strecke_km * 1000,
        fahrzeit_s=strecke.fahrzeit_s or profil.minuten * 60,
        geometrie=punkte,
        energieprofil=[p.als_dict() for p in profil.punkte])
    db.add(fahrt)
    db.commit()

    return {"fahrt_id": fahrt.id, **_antwort(fahrt, profil, mittel, fahrzeug)}


@router.get("/fahrten/{fahrt_id}")
def fahrt_lesen(fahrt_id: int, db: Session = Depends(get_db)):
    fahrt = db.get(models.Fahrt, fahrt_id)
    if not fahrt:
        raise HTTPException(404, "Fahrt nicht gefunden.")
    profil = fahrt.energieprofil or []
    return {"fahrt_id": fahrt.id, "demo": routing.ist_demo(),
            "start": {"lat": fahrt.start_lat, "lon": fahrt.start_lon,
                      "text": fahrt.start_text},
            "ziel": {"lat": fahrt.ziel_lat, "lon": fahrt.ziel_lon,
                     "text": fahrt.ziel_text},
            "fahrzeug": {"id": fahrt.fahrzeug.id, "name": fahrt.fahrzeug.name,
                         "reserve_soc": fahrt.fahrzeug.reserve_soc},
            "start_soc": fahrt.start_soc, "tempo_faktor": fahrt.tempo_faktor,
            "aussentemp_c": fahrt.aussentemp_c,
            "strecke_km": round((fahrt.strecke_m or 0) / 1000.0, 1),
            "fahrzeit_minuten": round((fahrt.fahrzeit_s or 0) / 60.0),
            "geometrie": fahrt.geometrie or [],
            "profil": _profil_ausduennen(profil),
            "soc_am_ziel": profil[-1]["soc"] if profil else None}


@router.get("/fahrten")
def fahrten_liste(db: Session = Depends(get_db), grenze: int = 20):
    fahrten = (db.query(models.Fahrt).order_by(models.Fahrt.id.desc())
               .limit(grenze).all())
    return [{"id": f.id, "start": f.start_text, "ziel": f.ziel_text,
             "angelegt": f.angelegt.isoformat(),
             "strecke_km": round((f.strecke_m or 0) / 1000.0, 1),
             "fahrzeug": f.fahrzeug.name} for f in fahrten]


# ---------- intern ----------

def _profil_ausduennen(profil: list, hoechstens: int = 400) -> list:
    """Für die Anzeige reicht ein Bruchteil der Punkte.

    Die Diagramme in der Oberfläche sind ein paar hundert Pixel breit - mehr
    Punkte als Pixel zu übertragen bringt nichts ausser Ladezeit.
    """
    if len(profil) <= hoechstens:
        return profil
    schritt = len(profil) / hoechstens
    ausgewaehlt = [profil[int(i * schritt)] for i in range(hoechstens)]
    ausgewaehlt.append(profil[-1])
    return ausgewaehlt


def _antwort(fahrt, profil, mittel, fahrzeug) -> dict:
    reserve_punkt = None
    if profil.reserve_bei_km is not None:
        for eintrag in profil.punkte:
            if eintrag.km >= profil.reserve_bei_km:
                reserve_punkt = {"km": eintrag.km, "lat": eintrag.lat,
                                 "lon": eintrag.lon}
                break

    return {
        "demo": routing.ist_demo(),
        "strecke_km": profil.strecke_km,
        "fahrzeit_minuten": round((fahrt.fahrzeit_s or 0) / 60.0),
        "kwh_gesamt": profil.kwh_gesamt,
        "verbrauch_kwh_100km": profil.verbrauch_kwh_100km,
        "soc_am_ziel": profil.soc_am_ziel,
        "reserve_bei_km": profil.reserve_bei_km,
        "reserve_punkt": reserve_punkt,
        "reicht": profil.reserve_bei_km is None,
        "wetter": {"temp_c": mittel.temp_c,
                   "wind_ms": mittel.windgeschwindigkeit_ms},
        "fahrzeug": {"id": fahrzeug.id, "name": fahrzeug.name,
                     "reserve_soc": fahrzeug.reserve_soc,
                     "akku_netto_kwh": fahrzeug.akku_netto_kwh},
        "geometrie": fahrt.geometrie or [],
        "profil": _profil_ausduennen(fahrt.energieprofil or []),
    }
