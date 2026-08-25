"""Route rechnen: Strecke, Höhenprofil, Wetter, Energiebedarf, Ladestopps.

Der Endpunkt, der alles zusammenführt: die Strecke aus dem Routing, den
Energiebedarf aus dem Verbrauchsmodell und - über `/ladeplan` - die zeitoptimale
Folge von Ladestopps aus dem Optimierer.

`/route` rechnet dabei nicht eine, sondern bis zu drei Varianten - schnellste,
kürzeste, und die, die openrouteservice ohne weitere Vorgabe empfiehlt. Für
"verbrauchsoptimal" gibt es keine vierte Anfrage: openrouteservice kennt
Energie nicht als Kantengewicht, das könnte nur ein eigener Routing-Layer
(siehe konzept-routenplaner.md). Stattdessen rechnet jolts eigenes
Verbrauchsmodell - mit dem echten Höhenprofil - für jede der drei Varianten
den tatsächlichen Energiebedarf, und die günstigste bekommt zusätzlich das
Etikett "sparsamste". Das kann mit einer der anderen beiden zusammenfallen -
und genau das ist dann die ehrliche Antwort auf "welche Route spart am
meisten", nicht eine erfundene vierte Strecke.

Der Ladeplan hängt bewusst an einer bereits gerechneten Fahrt und nicht an der
Routenanfrage: Radius, Mindestleistung und Steckertyp will man durchprobieren,
ohne jedes Mal das Routing-Kontingent zu belasten.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import deps, models, routing
from ..database import get_db
from ..energie import modell, wetter
from ..laden import kurven, optimierer, verfuegbarkeit
from ..routing import korridor
from ..routing.provider import PRAEFERENZEN, RoutingFehler

# Von der ORS-"preference" auf die Bezeichnung, die der Mensch am Steuer
# liest. "empfohlen" statt "recommended", weil das Wort sonst niemand
# verwendet, der nicht selbst openrouteservice-Kunde ist.
ETIKETT = {"fastest": "schnellste", "shortest": "kürzeste",
          "recommended": "empfohlene"}
# Wie nah zwei Varianten in Strecke und Fahrzeit beieinanderliegen müssen, um
# als "dieselbe Route" zu gelten. Ein Kilometer und eine Minute Toleranz
# fangen Rundungsunterschiede zwischen den ORS-Antworten ab, ohne zwei
# tatsächlich verschiedene Strecken fälschlich zusammenzulegen.
GLEICH_KM = 1.0
GLEICH_MIN = 1.0

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
    # Zuladung dieser einen Fahrt. None heisst "wie im Fahrzeugprofil" - der
    # Normalfall. Gesetzt wird sie, wenn dieselbe Fahrt einmal zu zweit und
    # einmal voll beladen geplant wird: Masse geht linear in Roll- und
    # Steigungswiderstand ein, auf einer Bergstrecke sind 600 kg Unterschied
    # deutlich mehr als Kosmetik.
    zuladung_kg: float | None = Field(default=None, ge=0, le=2000)


@router.get("/orte")
def orte_suchen(text: str = Query(min_length=2), land: str = ""):
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
    werte = modell.Fahrzeugwerte.aus_modell(fahrzeug)
    if anfrage.zuladung_kg is not None:
        # Nur für diese Rechnung, nicht am Fahrzeug gespeichert: Die Zuladung
        # ist eine Eigenschaft der Fahrt, nicht des Autos.
        werte.masse_kg = fahrzeug.leermasse_kg + anfrage.zuladung_kg

    # Schritt 1: alle drei Vorgaben abfragen und anhand der reinen Routing-
    # Antwort (Strecke, Fahrzeit) zusammenlegen, was dieselbe Strasse ist.
    # Bewusst vor Wetter und Verbrauchsmodell - die sind der teure Teil, und
    # im Demo-Modus wie oft auch in echt (kürzere Strecken haben meist nur
    # einen sinnvollen Weg) landen zwei oder drei Vorgaben ohnehin auf
    # derselben Route.
    gruppen: list[dict] = []
    letzter_fehler: RoutingFehler | None = None
    for praeferenz in PRAEFERENZEN:
        try:
            strecke = anbieter.route((anfrage.start.lat, anfrage.start.lon),
                                     (anfrage.ziel.lat, anfrage.ziel.lon),
                                     praeferenz=praeferenz)
        except RoutingFehler as fehler:
            # Eine Vorgabe, die scheitert, darf die anderen nicht mitreissen -
            # nur wenn am Ende keine einzige übrig ist, ist die Anfrage
            # gescheitert.
            letzter_fehler = fehler
            continue
        if len(strecke.punkte) < 2:
            continue

        passend = next((g for g in gruppen
                        if abs(g["strecke"].strecke_m - strecke.strecke_m)
                        <= GLEICH_KM * 1000
                        and abs(g["strecke"].fahrzeit_s - strecke.fahrzeit_s)
                        <= GLEICH_MIN * 60), None)
        if passend:
            passend["etiketten"].append(ETIKETT[praeferenz])
            continue
        gruppen.append({"etiketten": [ETIKETT[praeferenz]], "strecke": strecke})

    if not gruppen:
        if letzter_fehler:
            raise HTTPException(502, str(letzter_fehler)) from letzter_fehler
        raise HTTPException(502, "Route enthält zu wenige Punkte.")

    # Schritt 2: für jede tatsächlich unterschiedliche Route - und nur für
    # die - Wetter und Verbrauchsmodell rechnen.
    kandidaten: list[dict] = []
    for gruppe in gruppen:
        strecke = gruppe["strecke"]
        # Auf rund einen Punkt je 250 m ausdünnen. Auf einer Langstrecke
        # liefert das Routing fünfstellig viele Stützpunkte - für Karte und
        # Prognose ist das Rechenzeit ohne Erkenntnis. Höhensprünge bleiben
        # dabei erhalten.
        punkte, tempo = modell.ausduennen(strecke.punkte, strecke.tempo_ms)

        if anfrage.wetter_beruecksichtigen:
            umgebung_fuer = wetter.entlang_route(punkte)
            mittel = wetter.mittelwert(punkte)
        else:
            umgebung_fuer = None
            mittel = modell.Umgebung()

        profil = modell.profil_rechnen(werte, punkte, tempo, anfrage.start_soc,
                                       umgebung_fuer, anfrage.tempo_faktor)
        kandidaten.append({
            "etiketten": gruppe["etiketten"],
            "strecke_km": strecke.strecke_m / 1000.0 if strecke.strecke_m
                else profil.strecke_km,
            "fahrzeit_min": strecke.fahrzeit_s / 60.0 if strecke.fahrzeit_s
                else profil.minuten,
            "punkte": punkte, "profil": profil, "mittel": mittel})

    # Die günstigste Variante bekommt zusätzlich "sparsamste" - das ist keine
    # vierte Anfrage, sondern jolts eigenes Verbrauchsmodell, angewandt auf
    # die schon vorliegenden Kandidaten. Bei nur einem Kandidaten (Demo-Modus,
    # oder wenn zwei Vorgaben dieselbe Strecke ergeben) landet das Etikett
    # zwangsläufig dort, wo die anderen auch schon stehen.
    guenstigste = min(kandidaten, key=lambda k: k["profil"].kwh_gesamt)
    guenstigste["etiketten"].append("sparsamste")

    varianten = []
    for kandidat in kandidaten:
        fahrt = models.Fahrt(
            fahrzeug_id=fahrzeug.id,
            start_text=anfrage.start.text, start_lat=anfrage.start.lat,
            start_lon=anfrage.start.lon, ziel_text=anfrage.ziel.text,
            ziel_lat=anfrage.ziel.lat, ziel_lon=anfrage.ziel.lon,
            start_soc=anfrage.start_soc, tempo_faktor=anfrage.tempo_faktor,
            aussentemp_c=kandidat["mittel"].temp_c,
            zuladung_kg=anfrage.zuladung_kg,
            strecke_m=kandidat["strecke_km"] * 1000,
            fahrzeit_s=kandidat["fahrzeit_min"] * 60,
            geometrie=kandidat["punkte"],
            energieprofil=[p.als_dict() for p in kandidat["profil"].punkte])
        db.add(fahrt)
        db.flush()      # braucht fahrt.id, ohne schon endgültig zu committen
        varianten.append({"fahrt_id": fahrt.id, "etiketten": kandidat["etiketten"],
                          **_antwort(fahrt, kandidat["profil"], kandidat["mittel"],
                                    fahrzeug)})
    db.commit()

    # Reihenfolge fürs Auge: die günstigste zuerst, sie ist meist die Antwort
    # auf die Frage, die jolt beantworten soll.
    varianten.sort(key=lambda v: "sparsamste" not in v["etiketten"])
    return {"varianten": varianten}


@router.get("/fahrten/{fahrt_id}")
def fahrt_lesen(fahrt_id: int, db: Session = Depends(get_db)):
    """Eine gespeicherte Fahrt - in derselben Form wie eine frische Variante.

    Die abgeleiteten Werte (Verbrauch, Reserve-Punkt, "reicht es?") werden aus
    dem gespeicherten Energieprofil neu bestimmt statt mitgespeichert: Sie
    sind Funktionen des Profils, und zwei Quellen für dieselbe Zahl laufen
    auseinander. Die Form entspricht bewusst der von `/api/route`, damit die
    Oberfläche eine alte Fahrt mit demselben Code zeichnet wie eine neue.
    """
    fahrt = db.get(models.Fahrt, fahrt_id)
    if not fahrt:
        raise HTTPException(404, "Fahrt nicht gefunden.")

    profil = fahrt.energieprofil or []
    reserve_soc = fahrt.fahrzeug.reserve_soc
    strecke_km = round((fahrt.strecke_m or 0) / 1000.0, 1)
    kwh_gesamt = profil[-1].get("kwh") if profil else None

    reserve_bei_km, reserve_punkt = None, None
    for eintrag in profil:
        if eintrag.get("soc") is not None and eintrag["soc"] <= reserve_soc:
            reserve_bei_km = eintrag.get("km")
            reserve_punkt = {"km": eintrag.get("km"), "lat": eintrag.get("lat"),
                             "lon": eintrag.get("lon")}
            break

    return {"fahrt_id": fahrt.id, "demo": routing.ist_demo(),
            "start": {"lat": fahrt.start_lat, "lon": fahrt.start_lon,
                      "text": fahrt.start_text},
            "ziel": {"lat": fahrt.ziel_lat, "lon": fahrt.ziel_lon,
                     "text": fahrt.ziel_text},
            "fahrzeug": {"id": fahrt.fahrzeug.id, "name": fahrt.fahrzeug.name,
                         "reserve_soc": reserve_soc},
            "start_soc": fahrt.start_soc, "tempo_faktor": fahrt.tempo_faktor,
            "aussentemp_c": fahrt.aussentemp_c, "zuladung_kg": fahrt.zuladung_kg,
            "strecke_km": strecke_km,
            "fahrzeit_minuten": round((fahrt.fahrzeit_s or 0) / 60.0),
            "kwh_gesamt": round(kwh_gesamt, 3) if kwh_gesamt is not None else None,
            "verbrauch_kwh_100km": (round(kwh_gesamt / strecke_km * 100.0, 2)
                                    if kwh_gesamt and strecke_km else None),
            "reserve_bei_km": reserve_bei_km,
            "reserve_punkt": reserve_punkt,
            "reicht": reserve_bei_km is None,
            # Der Wind der damaligen Fahrt ist nicht gespeichert - nur die
            # Temperatur, mit der gerechnet wurde. Sie ist die Zahl, die in
            # der Oberfläche steht ("gerechnet bei 4 °C").
            "wetter": {"temp_c": fahrt.aussentemp_c},
            "geometrie": fahrt.geometrie or [],
            "profil": _profil_ausduennen(profil),
            "soc_am_ziel": profil[-1]["soc"] if profil else None}


@router.post("/fahrten/{fahrt_id}/ladeplan")
def ladeplan_rechnen(fahrt_id: int, radius_km: float = Query(8.0, gt=0, le=50),
                     min_kw: float = Query(50.0, ge=0),
                     steckertyp: str = "",
                     umweg_grenze_min: float = Query(
                         optimierer.UMWEG_GRENZE_MIN, gt=0, le=60),
                     # Null ist erlaubt, aber die Folge steht im Text der
                     # Oberfläche: Ohne Fixkosten je Halt zersplittert der
                     # Plan in viele Kurzstopps. Wer das sehen will, soll es
                     # sehen können.
                     stopp_fixkosten_min: float = Query(
                         optimierer.STOPP_FIXKOSTEN_MIN, ge=0, le=30),
                     db: Session = Depends(get_db)):
    """Die zeitoptimale Folge von Ladestopps für eine gerechnete Fahrt.

    Gerechnet wird auf dem gespeicherten Energieprofil - der Bedarf einer
    Etappe hängt nicht vom Ladestand ab, deshalb genügt der eine Durchlauf des
    Verbrauchsmodells aus `/route`. Ein zweiter Aufruf mit anderem Radius
    kostet damit weder Routing- noch Wetterabfragen.
    """
    fahrt = db.get(models.Fahrt, fahrt_id)
    if not fahrt:
        raise HTTPException(404, "Fahrt nicht gefunden.")
    if not fahrt.energieprofil or len(fahrt.energieprofil) < 2:
        raise HTTPException(409, "Zu dieser Fahrt liegt kein Energieprofil vor.")

    fahrzeug = fahrt.fahrzeug
    typ = steckertyp or fahrzeug.steckertyp
    kandidaten = korridor.suchen(db, fahrt.geometrie or [], radius_km=radius_km,
                                 min_kw=min_kw, steckertyp=typ)

    optionen = []
    for kandidat in kandidaten:
        lp = kandidat.ladepunkt
        zustand = verfuegbarkeit.MELDUNGEN.zustand(lp)
        optionen.append(optimierer.Ladeoption(
            id=lp.id, km_auf_route=kandidat.km_auf_route,
            umweg_minuten=kandidat.umweg_minuten, max_kw=lp.max_kw or 0.0,
            anzahl_punkte=lp.anzahl_punkte or 1, name=lp.name or "",
            betreiber=lp.betreiber or "", ort=lp.ort or "",
            lat=lp.lat, lon=lp.lon,
            gesperrt=zustand.quelle == "meldung"))

    werte = modell.Fahrzeugwerte.aus_modell(fahrzeug)
    if fahrt.zuladung_kg is not None:
        werte.masse_kg = fahrzeug.leermasse_kg + fahrt.zuladung_kg

    plan = optimierer.planen(
        optimierer.Streckenprofil.aus_dicts(fahrt.energieprofil),
        optionen, werte,
        kurven.als_paare(fahrzeug.ladekurve), start_soc=fahrt.start_soc,
        ziel_soc=fahrzeug.ziel_soc,
        max_fahrzeug_kw=fahrzeug.max_ladeleistung_kw,
        # Die Batterietemperatur wird nicht gemessen; die Aussentemperatur der
        # Fahrt ist die beste verfügbare Näherung. Sie unterschätzt die Kälte
        # der Batterie nach einer Nacht im Freien - die Ladezeit im Winter ist
        # also eher zu kurz gerechnet als zu lang.
        temperatur_faktor=kurven.temperatur_faktor(
            fahrt.aussentemp_c if fahrt.aussentemp_c is not None else 15.0),
        umweg_grenze_min=umweg_grenze_min,
        stopp_fixkosten_min=stopp_fixkosten_min,
        bevorzugte_betreiber=fahrzeug.bevorzugte_betreiber or None)

    return {"fahrt_id": fahrt.id, "demo": routing.ist_demo(),
            "steckertyp": typ, "min_kw": min_kw, "radius_km": radius_km,
            "stopp_fixkosten_min": stopp_fixkosten_min,
            **plan.als_dict()}


@router.get("/fahrten")
def fahrten_liste(db: Session = Depends(get_db), grenze: int = Query(30, le=200)):
    """Die zuletzt geplanten Fahrten.

    Bewusst mehr als Start und Ziel: Ohne Verbrauch, Aussentemperatur und
    Zuladung ist eine Liste vergangener Fahrten eine Liste von Namen. Erst
    mit diesen Zahlen wird sie zu dem, wofür man sie aufschlägt - dem
    Vergleich, warum dieselbe Strecke im Januar zwei Ladestopps brauchte und
    im Juni einen.
    """
    fahrten = (db.query(models.Fahrt).order_by(models.Fahrt.id.desc())
               .limit(grenze).all())

    ergebnis = []
    for f in fahrten:
        profil = f.energieprofil or []
        kwh = profil[-1].get("kwh") if profil else None
        strecke_km = round((f.strecke_m or 0) / 1000.0, 1)
        ergebnis.append({
            "id": f.id, "start": f.start_text, "ziel": f.ziel_text,
            "angelegt": f.angelegt.isoformat(),
            "strecke_km": strecke_km,
            "fahrzeit_minuten": round((f.fahrzeit_s or 0) / 60.0),
            "fahrzeug": f.fahrzeug.name,
            "start_soc": f.start_soc,
            "soc_am_ziel": profil[-1].get("soc") if profil else None,
            "kwh_gesamt": round(kwh, 1) if kwh is not None else None,
            "verbrauch_kwh_100km": (round(kwh / strecke_km * 100.0, 1)
                                    if kwh and strecke_km else None),
            "aussentemp_c": f.aussentemp_c,
            "tempo_faktor": f.tempo_faktor,
            "zuladung_kg": f.zuladung_kg,
            # Ob zu dieser Fahrt tatsächlich gefahren wurde - eine geplante
            # Fahrt ohne Live-Sitzung ist ein Entwurf, keine Erinnerung.
            "gefahren": bool(f.live_sitzungen)})
    return ergebnis


@router.delete("/fahrten/{fahrt_id}")
def fahrt_loeschen(fahrt_id: int, db: Session = Depends(get_db)):
    """Eine Fahrt aus der Historie entfernen.

    Jede Routenberechnung legt bis zu drei Fahrten an (eine je Variante) -
    ohne diesen Endpunkt wächst die Liste mit jedem Versuch, und die eine
    Fahrt, die man wiederfinden will, verschwindet zwischen Entwürfen.
    """
    fahrt = db.get(models.Fahrt, fahrt_id)
    if not fahrt:
        raise HTTPException(404, "Fahrt nicht gefunden.")
    db.delete(fahrt)
    db.commit()
    return {"ok": True}


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
